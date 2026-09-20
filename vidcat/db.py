import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    dir          TEXT NOT NULL,
    name         TEXT NOT NULL,            -- file name including extension
    ext          TEXT NOT NULL,            -- lowercase, no dot
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    created_at   INTEGER NOT NULL,         -- epoch seconds (UTC): metadata date, else filename date, else mtime
    date_source  TEXT,                     -- 'metadata' | 'filename' | 'mtime'; NULL = not yet determined
    duration    REAL,
    width        INTEGER,
    height       INTEGER,
    codec        TEXT,
    partial_hash TEXT,
    sha256       TEXT,
    name_score   INTEGER NOT NULL DEFAULT 100,
    caption      TEXT,
    missing      INTEGER NOT NULL DEFAULT 0,
    added_at     INTEGER NOT NULL,
    scanned_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_size ON videos(size);
CREATE INDEX IF NOT EXISTS idx_videos_sha256 ON videos(sha256);
CREATE INDEX IF NOT EXISTS idx_videos_dir ON videos(dir);
CREATE INDEX IF NOT EXISTS idx_videos_created ON videos(created_at);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags(tag_id);

CREATE TABLE IF NOT EXISTS rename_history (
    id       INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL,
    old_path TEXT NOT NULL,
    new_path TEXT NOT NULL,
    at       INTEGER NOT NULL
);
"""


def connect(path: Path | str | None = None, init: bool = True, cross_thread: bool = False) -> sqlite3.Connection:
    """Open the catalog.

    `init=False` skips schema creation (for per-request web connections). `cross_thread=True` lets a
    connection be used from a different thread than the one that opened it; FastAPI opens a request's
    connection in one worker thread and may run the endpoint in another. Each such connection is only
    ever used by one request at a time, so this is safe.
    """
    from . import config

    path = Path(path) if path else config.db_path()
    if init:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=not cross_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring catalogs created by older versions up to date."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "date_source" not in cols:
        # Left NULL for existing rows; the next `vidcat scan` re-reads them to fill it in.
        conn.execute("ALTER TABLE videos ADD COLUMN date_source TEXT")
        conn.commit()
