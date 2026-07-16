import logging
from datetime import datetime, timezone

from app.db import get_conn
from app import lrclib

logger = logging.getLogger("lyricdarr.fetcher")


def _now():
    return datetime.now(timezone.utc).isoformat()


def fetch_one(track_row) -> str:
    """Attempt to fetch and write lyrics for a single track row.

    Returns the resulting status string: 'found', 'not_found', 'instrumental', 'error'
    """
    artist = track_row["artist"]
    title = track_row["title"]
    album = track_row["album"]
    duration = track_row["duration"]
    lrc_path = track_row["lrc_path"]

    try:
        result = lrclib.get_lyrics(artist, title, album, duration)
        if result is None:
            result = lrclib.search_lyrics(artist, title)

        if result is None:
            return "not_found"

        if result["instrumental"] and not result["synced"] and not result["plain"]:
            return "instrumental"

        if result["synced"]:
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(result["synced"])
            return "found"

        if result["plain"]:
            # No synced timing available, but plain lyrics exist. Still write them
            # as .lrc (unsynced) so at least something shows up, flagged distinctly.
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(result["plain"])
            return "found_unsynced"

        return "not_found"

    except Exception as e:
        logger.exception(f"Error fetching lyrics for {artist} - {title}: {e}")
        track_row_error = str(e)
        _record_error(track_row["id"], track_row_error)
        return "error"


def _record_error(track_id: int, error: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracks SET last_error = ? WHERE id = ?",
            (error, track_id),
        )
        conn.commit()


def process_missing(limit: int = None) -> dict:
    """Process all tracks not currently marked 'found'/'found_unsynced'/'instrumental'.

    Skips tracks whose .lrc already exists on disk (status will be corrected by scan).
    """
    summary = {"found": 0, "found_unsynced": 0, "not_found": 0, "instrumental": 0, "error": 0}

    with get_conn() as conn:
        query = (
            "SELECT * FROM tracks WHERE status NOT IN ('found', 'found_unsynced', 'instrumental')"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()

    for row in rows:
        status = fetch_one(row)
        summary[status] = summary.get(status, 0) + 1
        with get_conn() as conn:
            conn.execute(
                "UPDATE tracks SET status = ?, last_checked = ? WHERE id = ?",
                (status, _now(), row["id"]),
            )
            conn.commit()

    return summary


def retry_track(track_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if not row:
            return "not_found_in_db"

    status = fetch_one(row)
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracks SET status = ?, last_checked = ? WHERE id = ?",
            (status, _now(), track_id),
        )
        conn.commit()
    return status
