import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS authors (
    ol_key TEXT PRIMARY KEY,          -- e.g. OL26320A
    name TEXT NOT NULL,
    alt_names TEXT,                   -- ' | ' separated
    birth_date TEXT,
    death_date TEXT,
    work_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS works (
    ol_key TEXT PRIMARY KEY,          -- e.g. OL27448W
    title TEXT NOT NULL,
    subtitle TEXT,
    first_publish_year INTEGER,
    subjects TEXT,                    -- ' | ' separated, first few only
    cover_id INTEGER
);

CREATE TABLE IF NOT EXISTS work_authors (
    work_key TEXT NOT NULL,
    author_key TEXT NOT NULL,
    PRIMARY KEY (work_key, author_key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',   -- queued running done failed cancelled skipped interrupted
    depends_on INTEGER,
    progress TEXT NOT NULL DEFAULT '{}',
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-200000")  # ~200 MB page cache
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
