import os
import json
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel

from app.db import init_db, get_conn, get_setting, set_setting
from app.scanner import scan_library
from app.fetcher import process_missing, retry_track, verify_tracks
from app import state as job_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("lyricdarr.main")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
ENV_SCAN_INTERVAL_HOURS = float(os.environ.get("SCAN_INTERVAL_HOURS", "6"))
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN")
SATISFIED_STATUSES = ("found", "found_unsynced", "instrumental")

scheduler = BackgroundScheduler()
_job_lock = threading.Lock()


def _effective_scan_interval() -> float:
    val = get_setting("scan_interval_hours")
    return float(val) if val else ENV_SCAN_INTERVAL_HOURS


def _auto_scan_enabled() -> bool:
    return get_setting("auto_scan_enabled", "true") == "true"


def _netease_fallback_enabled() -> bool:
    return get_setting("netease_fallback_enabled", "true") == "true"


def _reschedule_auto_scan():
    if scheduler.get_job("auto_scan"):
        scheduler.remove_job("auto_scan")
    if _auto_scan_enabled():
        scheduler.add_job(
            scheduled_job, "interval", hours=_effective_scan_interval(), id="auto_scan"
        )


def scheduled_job():
    logger.info("Running scheduled scan + fetch")
    try:
        scan_result = scan_library(MUSIC_DIR)
        logger.info(f"Scan result: {scan_result}")
        fetch_result = process_missing()
        logger.info(f"Fetch result: {fetch_result}")
    except Exception as e:
        logger.exception(f"Scheduled job failed: {e}")


def _run_job(phase: str, target) -> bool:
    """Runs `target(progress_cb)` in a background thread, guarded so only one
    job runs at a time. Returns False if a job is already running."""
    if not _job_lock.acquire(blocking=False):
        return False

    job_state.start(phase)

    def cb(current=None, processed=None, total=None):
        job_state.update(current=current, processed=processed, total=total)

    def worker():
        try:
            result = target(cb)
            job_state.finish(extra=result)
        except Exception as e:
            logger.exception(f"{phase} job failed: {e}")
            job_state.fail(str(e))
        finally:
            _job_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    set_setting("music_dir", MUSIC_DIR)
    _reschedule_auto_scan()
    scheduler.start()
    logger.info(f"Lyricdarr started. Watching {MUSIC_DIR}")
    yield
    scheduler.shutdown()


app = FastAPI(title="Lyricdarr", lifespan=lifespan)


@app.get("/api/status")
def api_status():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]
        by_status = conn.execute(
            "SELECT status, COUNT(*) c FROM tracks GROUP BY status"
        ).fetchall()
    return {
        "music_dir": MUSIC_DIR,
        "total_tracks": total,
        "by_status": {r["status"]: r["c"] for r in by_status},
    }


# --- Synchronous endpoints: kept for scripting/webhook use (e.g. a Lidarr
# post-import hook), where a blocking call that returns a final summary is
# what you want. The dashboard UI uses the /start variants below instead. ---


@app.post("/api/scan")
def api_scan():
    return scan_library(MUSIC_DIR)


@app.post("/api/fetch")
def api_fetch(limit: int = None):
    return process_missing(limit=limit)


@app.post("/api/scan-and-fetch")
def api_scan_and_fetch():
    scan_result = scan_library(MUSIC_DIR)
    fetch_result = process_missing()
    return {"scan": scan_result, "fetch": fetch_result}


# --- Background job endpoints: used by the dashboard for real-time progress ---


@app.post("/api/scan/start")
def api_scan_start():
    ok = _run_job("scan", lambda cb: scan_library(MUSIC_DIR, progress_cb=cb))
    if not ok:
        raise HTTPException(status_code=409, detail="A job is already running")
    return {"started": True}


@app.post("/api/fetch/start")
def api_fetch_start():
    ok = _run_job("fetch", lambda cb: process_missing(progress_cb=cb))
    if not ok:
        raise HTTPException(status_code=409, detail="A job is already running")
    return {"started": True}


def _scan_and_fetch_job(cb):
    scan_result = scan_library(MUSIC_DIR, progress_cb=cb)
    fetch_result = process_missing(progress_cb=cb)
    return {"scan": scan_result, "fetch": fetch_result}


@app.post("/api/scan-and-fetch/start")
def api_scan_and_fetch_start():
    ok = _run_job("scan_and_fetch", _scan_and_fetch_job)
    if not ok:
        raise HTTPException(status_code=409, detail="A job is already running")
    return {"started": True}


@app.post("/api/webhook/lidarr")
async def webhook_lidarr(request: Request, token: str = None):
    """Point a Lidarr Connect webhook (On Import / On Upgrade) at this URL to
    scan and fetch lyrics right after new tracks land, instead of waiting for
    the schedule. If WEBHOOK_TOKEN is set, requests must include a matching
    ?token= query param or X-Webhook-Token header.
    """
    if WEBHOOK_TOKEN:
        provided = token or request.headers.get("X-Webhook-Token")
        if provided != WEBHOOK_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid or missing webhook token")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event_type = payload.get("eventType", "unknown")
    logger.info(f"Lidarr webhook received (eventType={event_type})")

    started = _run_job("scan_and_fetch", _scan_and_fetch_job)
    return {"triggered": started, "event_type": event_type}


@app.post("/api/verify/start")
def api_verify_start():
    """Re-checks existing tracks for their .lrc file on disk without a full
    filesystem rescan — a quick way to confirm what's actually found."""
    ok = _run_job("verify", lambda cb: verify_tracks(progress_cb=cb))
    if not ok:
        raise HTTPException(status_code=409, detail="A job is already running")
    return {"started": True}


@app.get("/api/progress")
def api_progress():
    return job_state.get_state()


@app.get("/api/progress/stream")
async def api_progress_stream(request: Request):
    async def event_gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            current = job_state.get_state()
            if current != last:
                yield f"data: {json.dumps(current)}\n\n"
                last = current
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# --- Library browsing ---


@app.get("/api/tracks")
def api_tracks(
    status: str = None, search: str = None, page: int = 1, page_size: int = 100
):
    page = max(page, 1)
    page_size = max(1, min(page_size, 500))
    offset = (page - 1) * page_size

    query = "SELECT * FROM tracks WHERE 1=1"
    count_query = "SELECT COUNT(*) c FROM tracks WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        count_query += " AND status = ?"
        params.append(status)
    if search:
        query += " AND (artist LIKE ? OR title LIKE ? OR album LIKE ?)"
        count_query += " AND (artist LIKE ? OR title LIKE ? OR album LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like]

    with get_conn() as conn:
        total = conn.execute(count_query, params).fetchone()["c"]
        query += " ORDER BY artist, album, title LIMIT ? OFFSET ?"
        rows = conn.execute(query, params + [page_size, offset]).fetchall()

    return {
        "tracks": [dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@app.get("/api/library/tree")
def api_library_tree(search: str = None, group_by: str = "artist"):
    """Groups the library for browsing, with a completion tick at each level.

    group_by: "artist" (nested artist -> album -> tracks), "album" (flat list
    of albums with their artist), or "song" (flat track list).
    """
    query = "SELECT * FROM tracks WHERE 1=1"
    params = []
    if search:
        query += " AND (artist LIKE ? OR title LIKE ? OR album LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like]
    query += " ORDER BY artist, album, title"

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    if group_by == "song":
        return {"group_by": "song", "tracks": rows}

    artists = {}
    for r in rows:
        artist_name = r["artist"] or "Unknown Artist"
        album_name = r["album"] or "Unknown Album"
        satisfied = r["status"] in SATISFIED_STATUSES

        artist = artists.setdefault(
            artist_name, {"name": artist_name, "albums": {}, "track_count": 0, "satisfied_count": 0}
        )
        artist["track_count"] += 1
        artist["satisfied_count"] += int(satisfied)

        album = artist["albums"].setdefault(
            album_name, {"name": album_name, "tracks": [], "track_count": 0, "satisfied_count": 0}
        )
        album["tracks"].append(r)
        album["track_count"] += 1
        album["satisfied_count"] += int(satisfied)

    artist_list = []
    for artist_name, artist in sorted(artists.items(), key=lambda kv: kv[0].lower()):
        albums = []
        for album_name, album in sorted(artist["albums"].items(), key=lambda kv: kv[0].lower()):
            albums.append(
                {
                    "name": album["name"],
                    "tracks": album["tracks"],
                    "track_count": album["track_count"],
                    "satisfied_count": album["satisfied_count"],
                    "complete": album["satisfied_count"] == album["track_count"],
                }
            )
        artist_list.append(
            {
                "name": artist["name"],
                "albums": albums,
                "track_count": artist["track_count"],
                "satisfied_count": artist["satisfied_count"],
                "complete": artist["satisfied_count"] == artist["track_count"],
            }
        )

    if group_by == "album":
        flat_albums = []
        for artist in artist_list:
            for album in artist["albums"]:
                flat_albums.append({**album, "artist": artist["name"]})
        flat_albums.sort(key=lambda a: a["name"].lower())
        return {"group_by": "album", "albums": flat_albums}

    return {"group_by": "artist", "artists": artist_list}


@app.post("/api/tracks/{track_id}/retry")
def api_retry_track(track_id: int):
    status = retry_track(track_id)
    if status == "not_found_in_db":
        raise HTTPException(status_code=404, detail="Track not found")
    return {"track_id": track_id, "status": status}


@app.delete("/api/tracks/{track_id}")
def api_delete_track(track_id: int):
    """Removes a single track from Lyricdarr's tracking. Does not touch any
    files on disk — use this to untrack an entry manually."""
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Track not found")
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        conn.commit()
    logger.info(f"Deleted track {track_id}")
    return {"deleted": 1}


@app.delete("/api/library/album")
def api_delete_album(artist: str, album: str):
    """Removes every track for the given artist+album from Lyricdarr's
    tracking. Does not touch any files on disk."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tracks WHERE COALESCE(artist, 'Unknown Artist') = ? "
            "AND COALESCE(album, 'Unknown Album') = ?",
            (artist, album),
        )
        conn.commit()
    logger.info(f"Deleted album '{album}' by '{artist}' ({cur.rowcount} tracks)")
    return {"deleted": cur.rowcount}


@app.delete("/api/library/artist")
def api_delete_artist(artist: str):
    """Removes every track for the given artist from Lyricdarr's tracking.
    Does not touch any files on disk."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tracks WHERE COALESCE(artist, 'Unknown Artist') = ?",
            (artist,),
        )
        conn.commit()
    logger.info(f"Deleted artist '{artist}' ({cur.rowcount} tracks)")
    return {"deleted": cur.rowcount}


# --- Settings ---


class SettingsUpdate(BaseModel):
    scan_interval_hours: Optional[float] = None
    auto_scan_enabled: Optional[bool] = None
    netease_fallback_enabled: Optional[bool] = None


@app.get("/api/settings")
def api_get_settings():
    return {
        "music_dir": MUSIC_DIR,
        "scan_interval_hours": _effective_scan_interval(),
        "auto_scan_enabled": _auto_scan_enabled(),
        "netease_fallback_enabled": _netease_fallback_enabled(),
        "webhook_configured": bool(WEBHOOK_TOKEN),
    }


@app.post("/api/settings")
def api_update_settings(payload: SettingsUpdate):
    if payload.scan_interval_hours is not None:
        if payload.scan_interval_hours <= 0:
            raise HTTPException(status_code=400, detail="scan_interval_hours must be positive")
        set_setting("scan_interval_hours", str(payload.scan_interval_hours))

    if payload.auto_scan_enabled is not None:
        set_setting("auto_scan_enabled", "true" if payload.auto_scan_enabled else "false")

    if payload.netease_fallback_enabled is not None:
        set_setting(
            "netease_fallback_enabled", "true" if payload.netease_fallback_enabled else "false"
        )

    _reschedule_auto_scan()
    return api_get_settings()


# Serve the dashboard UI
app.mount("/", StaticFiles(directory="static", html=True), name="static")
