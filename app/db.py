import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("LYRICDARR_DB", "/config/lyricdarr.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                lrc_path TEXT NOT NULL,
                artist TEXT,
                title TEXT,
                album TEXT,
                duration INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                last_checked TEXT,
                last_error TEXT,
                match_source TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        _add_column_if_missing(conn, "tracks", "match_source", "TEXT")
        conn.commit()


def _add_column_if_missing(conn, table: str, column: str, coltype: str):
    """CREATE TABLE IF NOT EXISTS is a no-op on a table that already exists
    from a previous version, so new columns need an explicit migration."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
