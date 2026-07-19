import os
import logging
from mutagen import File as MutagenFile

from app.db import get_conn

logger = logging.getLogger("lyricdarr.scanner")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac"}


def _read_tags(path: str):
    """Best-effort tag extraction. Returns (artist, title, album, duration_seconds)."""
    artist = title = album = None
    duration = None
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None:
            if audio.tags:
                artist = (audio.tags.get("artist") or [None])[0]
                title = (audio.tags.get("title") or [None])[0]
                album = (audio.tags.get("album") or [None])[0]
            if audio.info and hasattr(audio.info, "length"):
                duration = int(round(audio.info.length))
    except Exception as e:
        logger.warning(f"Failed to read tags for {path}: {e}")

    # Fallback: guess title from filename if tag missing
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]

    return artist, title, album, duration


def lrc_path_for(audio_path: str) -> str:
    base, _ext = os.path.splitext(audio_path)
    return base + ".lrc"


def _count_audio_files(music_dir: str) -> int:
    count = 0
    for _root, _dirs, files in os.walk(music_dir):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in AUDIO_EXTENSIONS:
                count += 1
    return count


def scan_library(music_dir: str, progress_cb=None) -> dict:
    """Walk the music directory, upsert tracks into the DB, and prune tracks
    whose audio file has disappeared from disk (e.g. deleted/removed from Lidarr).

    Returns summary counts. If progress_cb is given, it's called as
    progress_cb(current=path, processed=n, total=n) after each file, so
    callers can surface real-time scan progress.
    """
    found = 0
    added = 0
    already_has_lrc = 0
    seen_paths = set()

    total = _count_audio_files(music_dir) if progress_cb else 0

    with get_conn() as conn:
        for root, _dirs, files in os.walk(music_dir):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue

                full_path = os.path.join(root, fname)
                lrc_path = lrc_path_for(full_path)
                found += 1
                seen_paths.add(full_path)

                if progress_cb:
                    progress_cb(current=full_path, processed=found, total=total)

                existing = conn.execute(
                    "SELECT id, status FROM tracks WHERE path = ?", (full_path,)
                ).fetchone()

                has_lrc_on_disk = os.path.exists(lrc_path)
                if has_lrc_on_disk:
                    already_has_lrc += 1

                if existing:
                    # If an lrc has appeared on disk since last scan (e.g. manually added),
                    # reflect that, but don't clobber other statuses.
                    if has_lrc_on_disk and existing["status"] != "found":
                        conn.execute(
                            "UPDATE tracks SET status = 'found' WHERE id = ?",
                            (existing["id"],),
                        )
                    continue

                artist, title, album, duration = _read_tags(full_path)
                status = "found" if has_lrc_on_disk else "pending"

                conn.execute(
                    """
                    INSERT INTO tracks (path, lrc_path, artist, title, album, duration, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (full_path, lrc_path, artist, title, album, duration, status),
                )
                added += 1

        # Prune tracks whose file vanished from disk since the last scan. Checked
        # against seen_paths first (cheap) and confirmed with os.path.exists (safe
        # against e.g. a temporarily unmounted music_dir cutting the walk short).
        stale_ids = [
            row["id"]
            for row in conn.execute("SELECT id, path FROM tracks").fetchall()
            if row["path"] not in seen_paths and not os.path.exists(row["path"])
        ]
        if stale_ids:
            conn.executemany(
                "DELETE FROM tracks WHERE id = ?", [(i,) for i in stale_ids]
            )

        conn.commit()

    return {
        "scanned_files": found,
        "new_tracks": added,
        "already_had_lrc": already_has_lrc,
        "removed_tracks": len(stale_ids),
    }
