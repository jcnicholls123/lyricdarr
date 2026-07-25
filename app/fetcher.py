import logging
import os
from datetime import datetime, timezone

from app.db import get_conn, get_setting
from app import lrclib
from app import netease

logger = logging.getLogger("lyricdarr.fetcher")


def _now():
    return datetime.now(timezone.utc).isoformat()


def fetch_one(track_row) -> tuple:
    """Attempt to fetch and write lyrics for a single track row.

    Returns (status, match_source). status is one of 'found', 'found_unsynced',
    'not_found', 'instrumental', 'error'. match_source records which lookup
    produced the result - 'lrclib_exact' (a confident /get match), or
    'lrclib_fuzzy' / 'netease_fuzzy' (a text search fallback - matched on
    duration and version qualifiers, but still a lower-confidence match worth
    spot-checking) - or None when nothing was found/written.
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
            return "not_found", None

        if result["instrumental"] and not result["synced"] and not result["plain"]:
            return "instrumental", source

        if result["synced"]:
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(result["synced"])
            return "found", source

        if result["plain"]:
            # No synced timing available, but plain lyrics exist. Still write them
            # as .lrc (unsynced) so at least something shows up, flagged distinctly.
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(result["plain"])
            return "found_unsynced", source

        return "not_found", None

    except Exception as e:
        logger.exception(f"Error fetching lyrics for {artist} - {title}: {e}")
        track_row_error = str(e)
        _record_error(track_row["id"], track_row_error)
        return "error", None


def _record_error(track_id: int, error: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracks SET last_error = ? WHERE id = ?",
            (error, track_id),
        )
        conn.commit()


def process_missing(limit: int = None, progress_cb=None) -> dict:
    """Process all tracks not currently marked 'found'/'found_unsynced'/'instrumental'.

    Skips tracks whose .lrc already exists on disk (status will be corrected by scan).
    If progress_cb is given, it's called as progress_cb(current=label, processed=n, total=n)
    before each track is fetched, so callers can surface real-time progress.
    """
    summary = {"found": 0, "found_unsynced": 0, "not_found": 0, "instrumental": 0, "error": 0}

    with get_conn() as conn:
        query = (
            "SELECT * FROM tracks WHERE status NOT IN ('found', 'found_unsynced', 'instrumental')"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()

    total = len(rows)
    for i, row in enumerate(rows, start=1):
        if progress_cb:
            label = f'{row["artist"] or "Unknown Artist"} - {row["title"]}'
            progress_cb(current=label, processed=i - 1, total=total)

        status, source = fetch_one(row)
        summary[status] = summary.get(status, 0) + 1
        with get_conn() as conn:
            conn.execute(
                "UPDATE tracks SET status = ?, match_source = ?, last_checked = ? WHERE id = ?",
                (status, source, _now(), row["id"]),
            )
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
        # delete) invalidates whatever match_source was recorded before.
        new_source = row["match_source"]

        if has_lrc and old_status not in ("found", "found_unsynced"):
            new_status = "found"
            new_source = None
            summary["now_found"] += 1
        elif not has_lrc and old_status in ("found", "found_unsynced"):
            new_status = "pending"
            new_source = None
            summary["now_missing"] += 1
        else:
            new_status = old_status
            summary["unchanged"] += 1

        if new_status != old_status:
            with get_conn() as conn:
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

    status, source = fetch_one(row)
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracks SET status = ?, match_source = ?, last_checked = ? WHERE id = ?",
            (status, source, _now(), track_id),
        )
        conn.commit()
    return status
