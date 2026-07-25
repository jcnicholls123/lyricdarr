import logging
import os
from datetime import datetime, timezone

from app.db import get_conn, get_setting
from app import lrclib
from app import netease

logger = logging.getLogger("lyricdarr.fetcher")

FUZZY_SOURCES = ("lrclib_fuzzy", "netease_fuzzy")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _result(status, source=None, pending_synced=None, pending_plain=None):
    return {
        "status": status,
        "match_source": source,
        "pending_synced": pending_synced,
        "pending_plain": pending_plain,
    }


def fetch_one(track_row) -> dict:
    """Attempt to fetch lyrics for a single track row.

    A confident exact LRCLIB match (/get) is written to disk immediately. A
    fuzzy-search match (LRCLIB /search or NetEase - lower confidence even
    after the duration/version/artist/language checks in lrclib.py and
    netease.py) is held as a pending candidate instead of being written, so
    it can be previewed and approved rather than silently overwriting the
    track with lyrics that might belong to the wrong recording.

    Returns a dict: {status, match_source, pending_synced, pending_plain}.
    status is one of 'found', 'found_unsynced', 'pending_review', 'not_found',
    'instrumental', 'error'.
    """
    artist = track_row["artist"]
    title = track_row["title"]
    album = track_row["album"]
    duration = track_row["duration"]
    lrc_path = track_row["lrc_path"]

    try:
        result = lrclib.get_lyrics(artist, title, album, duration)
        source = "lrclib_exact" if result is not None else None

        if result is None:
            result = lrclib.search_lyrics(artist, title, duration)
            source = "lrclib_fuzzy" if result is not None else None

        if result is None and get_setting("netease_fallback_enabled", "true") == "true":
            result = netease.search_lyrics(artist, title, duration)
            source = "netease_fuzzy" if result is not None else None

        if result is None:
            return _result("not_found")

        if result["instrumental"] and not result["synced"] and not result["plain"]:
            return _result("instrumental", source)

        is_fuzzy = source in FUZZY_SOURCES

        if result["synced"]:
            if is_fuzzy:
                return _result("pending_review", source, pending_synced=result["synced"])
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(result["synced"])
            return _result("found", source)

        if result["plain"]:
            if is_fuzzy:
                return _result("pending_review", source, pending_plain=result["plain"])
            # No synced timing available, but plain lyrics exist. Still write them
            # as .lrc (unsynced) so at least something shows up, flagged distinctly.
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(result["plain"])
            return _result("found_unsynced", source)

        return _result("not_found")

    except Exception as e:
        logger.exception(f"Error fetching lyrics for {artist} - {title}: {e}")
        _record_error(track_row["id"], str(e))
        return _result("error")


def _record_error(track_id: int, error: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracks SET last_error = ? WHERE id = ?",
            (error, track_id),
        )
        conn.commit()


def _apply_fetch_result(conn, track_id: int, r: dict):
    conn.execute(
        "UPDATE tracks SET status = ?, match_source = ?, pending_synced = ?, "
        "pending_plain = ?, last_checked = ? WHERE id = ?",
        (r["status"], r["match_source"], r["pending_synced"], r["pending_plain"], _now(), track_id),
    )


def process_missing(limit: int = None, progress_cb=None) -> dict:
    """Process all tracks not currently marked 'found'/'found_unsynced'/'instrumental',
    or already sitting in 'pending_review' awaiting a decision.

    Skips tracks whose .lrc already exists on disk (status will be corrected by scan).
    If progress_cb is given, it's called as progress_cb(current=label, processed=n, total=n)
    before each track is fetched, so callers can surface real-time progress.
    """
    summary = {
        "found": 0, "found_unsynced": 0, "not_found": 0,
        "instrumental": 0, "pending_review": 0, "error": 0,
    }

    with get_conn() as conn:
        query = (
            "SELECT * FROM tracks WHERE status NOT IN "
            "('found', 'found_unsynced', 'instrumental', 'pending_review')"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()

    total = len(rows)
    for i, row in enumerate(rows, start=1):
        if progress_cb:
            label = f'{row["artist"] or "Unknown Artist"} - {row["title"]}'
            progress_cb(current=label, processed=i - 1, total=total)

        r = fetch_one(row)
        summary[r["status"]] = summary.get(r["status"], 0) + 1
        with get_conn() as conn:
            _apply_fetch_result(conn, row["id"], r)
            conn.commit()

    if progress_cb:
        progress_cb(current=None, processed=total, total=total)

    return summary


def verify_tracks(progress_cb=None) -> dict:
    """Re-check on-disk presence of each tracked .lrc file without walking the
    filesystem for new tracks (that's what scan_library is for).

    Corrects status when a file was added or removed outside the app (e.g. a
    lyric was manually placed, or deleted). If progress_cb is given, it's
    called as progress_cb(current=label, processed=n, total=n) per track.
    """
    summary = {"now_found": 0, "now_missing": 0, "unchanged": 0}

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM tracks").fetchall()

    total = len(rows)
    for i, row in enumerate(rows, start=1):
        if progress_cb:
            label = f'{row["artist"] or "Unknown Artist"} - {row["title"]}'
            progress_cb(current=label, processed=i - 1, total=total)

        has_lrc = os.path.exists(row["lrc_path"])
        old_status = row["status"]
        # A file appearing/disappearing outside the app (manual drop, manual
        # delete) invalidates whatever match_source/pending candidate was recorded before.
        new_source = row["match_source"]
        clear_pending = False

        if has_lrc and old_status not in ("found", "found_unsynced"):
            new_status = "found"
            new_source = None
            clear_pending = True
            summary["now_found"] += 1
        elif not has_lrc and old_status in ("found", "found_unsynced"):
            new_status = "pending"
            new_source = None
            clear_pending = True
            summary["now_missing"] += 1
        else:
            new_status = old_status
            summary["unchanged"] += 1

        if new_status != old_status:
            with get_conn() as conn:
                if clear_pending:
                    conn.execute(
                        "UPDATE tracks SET status = ?, match_source = ?, pending_synced = NULL, "
                        "pending_plain = NULL, last_checked = ? WHERE id = ?",
                        (new_status, new_source, _now(), row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE tracks SET status = ?, match_source = ?, last_checked = ? WHERE id = ?",
                        (new_status, new_source, _now(), row["id"]),
                    )
                conn.commit()

    if progress_cb:
        progress_cb(current=None, processed=total, total=total)

    return summary


def retry_track(track_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if not row:
            return "not_found_in_db"

    r = fetch_one(row)
    with get_conn() as conn:
        _apply_fetch_result(conn, track_id, r)
        conn.commit()
    return r["status"]


def approve_track(track_id: int) -> str:
    """Writes a pending fuzzy-matched candidate to disk, promoting it to a
    normal found/found_unsynced track. Clears match_source since a human has
    now confirmed it - it no longer needs to show up in the review queue."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if not row:
            return "not_found_in_db"
        if row["status"] != "pending_review":
            return "no_pending_review"

        if row["pending_synced"]:
            with open(row["lrc_path"], "w", encoding="utf-8") as f:
                f.write(row["pending_synced"])
            new_status = "found"
        elif row["pending_plain"]:
            with open(row["lrc_path"], "w", encoding="utf-8") as f:
                f.write(row["pending_plain"])
            new_status = "found_unsynced"
        else:
            return "no_pending_review"

        conn.execute(
            "UPDATE tracks SET status = ?, match_source = NULL, pending_synced = NULL, "
            "pending_plain = NULL, last_checked = ? WHERE id = ?",
            (new_status, _now(), track_id),
        )
        conn.commit()
    return new_status


def reject_track(track_id: int) -> str:
    """Discards a pending fuzzy-matched candidate without writing it. The
    track goes back to 'not_found' so it's picked up again by the next fetch
    pass (which may turn up nothing better, or the same candidate for review
    again) rather than being stuck."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if not row:
            return "not_found_in_db"

        conn.execute(
            "UPDATE tracks SET status = 'not_found', match_source = NULL, pending_synced = NULL, "
            "pending_plain = NULL, last_checked = ? WHERE id = ?",
            (_now(), track_id),
        )
        conn.commit()
    return "not_found"
