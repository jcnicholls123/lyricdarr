import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler

from app.db import init_db, get_conn, get_setting, set_setting
from app.scanner import scan_library
from app.fetcher import process_missing, retry_track

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("lyricdarr.main")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
SCAN_INTERVAL_HOURS = float(os.environ.get("SCAN_INTERVAL_HOURS", "6"))

scheduler = BackgroundScheduler()


def scheduled_job():
    logger.info("Running scheduled scan + fetch")
    try:
        scan_result = scan_library(MUSIC_DIR)
        logger.info(f"Scan result: {scan_result}")
        fetch_result = process_missing()
        logger.info(f"Fetch result: {fetch_result}")
    except Exception as e:
        logger.exception(f"Scheduled job failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    set_setting("music_dir", MUSIC_DIR)
    scheduler.add_job(scheduled_job, "interval", hours=SCAN_INTERVAL_HOURS, id="auto_scan")
    scheduler.start()
    logger.info(f"Lyricdarr started. Watching {MUSIC_DIR}, auto-scan every {SCAN_INTERVAL_HOURS}h")
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


@app.post("/api/scan")
def api_scan():
    result = scan_library(MUSIC_DIR)
    return result


@app.post("/api/fetch")
def api_fetch(limit: int = None):
    result = process_missing(limit=limit)
    return result


@app.post("/api/scan-and-fetch")
def api_scan_and_fetch():
    scan_result = scan_library(MUSIC_DIR)
    fetch_result = process_missing()
    return {"scan": scan_result, "fetch": fetch_result}


@app.get("/api/tracks")
def api_tracks(status: str = None, search: str = None, page: int = 1, page_size: int = 100):
    offset = (page - 1) * page_size
    query = "SELECT * FROM tracks WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if search:
        query += " AND (artist LIKE ? OR title LIKE ? OR album LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like]
    query += " ORDER BY artist, album, title LIMIT ? OFFSET ?"
    params += [page_size, offset]

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]


@app.post("/api/tracks/{track_id}/retry")
def api_retry_track(track_id: int):
    status = retry_track(track_id)
    if status == "not_found_in_db":
        raise HTTPException(status_code=404, detail="Track not found")
    return {"track_id": track_id, "status": status}


@app.get("/api/settings")
def api_get_settings():
    return {
        "music_dir": MUSIC_DIR,
        "scan_interval_hours": SCAN_INTERVAL_HOURS,
    }


# Serve the dashboard UI
app.mount("/", StaticFiles(directory="static", html=True), name="static")
