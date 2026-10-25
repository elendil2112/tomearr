import sqlite3
from pathlib import Path

# Version 1: the Phase 1 schema. Each later entry in MIGRATIONS moves the database up one version.
SCHEMA_V1 = """
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

# Editions and ISBNs are bulk loaded with no indexes; build_indexes() adds them afterwards, which is much
# faster than maintaining B-trees during a 40M+ row insert.
EDITIONS_DDL = """
CREATE TABLE IF NOT EXISTS editions (
    ol_key TEXT NOT NULL,             -- e.g. OL7353617M
    work_key TEXT NOT NULL,
    title TEXT,
    subtitle TEXT,
    language TEXT,                    -- MARC code of the first listed language, e.g. eng
    publisher TEXT,
    publish_year INTEGER,
    pages INTEGER,
    format TEXT                       -- ebook | print | audio | NULL (unknown)
);

CREATE TABLE IF NOT EXISTS isbns (
    isbn TEXT NOT NULL,               -- normalized ISBN-13
    edition_key TEXT NOT NULL
);
"""

SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS work_signals (
    work_key TEXT PRIMARY KEY,
    rating_sum INTEGER NOT NULL DEFAULT 0,
    rating_count INTEGER NOT NULL DEFAULT 0,
    want_count INTEGER NOT NULL DEFAULT 0,
    reading_count INTEGER NOT NULL DEFAULT 0,
    read_count INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
"""


def _migrate_2(conn: sqlite3.Connection) -> None:
    """Phase 2: editions, ISBNs, ratings/reading-log signals, popularity columns on works."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(works)")}
    for name, ddl in (
        ("edition_count", "INTEGER NOT NULL DEFAULT 0"),
        ("ebook_count", "INTEGER NOT NULL DEFAULT 0"),
        ("languages", "TEXT"),            # ',eng,fre,' (wrapped in commas so instr() can match whole codes)
        ("popularity", "REAL NOT NULL DEFAULT 0"),
        ("pop_rank", "INTEGER"),          # 1 = most popular; also the works_fts rowid once rebuilt
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE works ADD COLUMN {name} {ddl}")
    conn.executescript(EDITIONS_DDL + SIGNALS_DDL)


def _migrate_3(conn: sqlite3.Connection) -> None:
    """Phase 3: what the user owns (mirrored from Calibre) and which authors they monitor."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS library_books (
        calibre_id INTEGER PRIMARY KEY,   -- books.id in Calibre's metadata.db
        title TEXT NOT NULL,
        authors TEXT,                     -- ' & ' separated, Calibre's order
        author_sort TEXT,
        isbn TEXT,                        -- normalized ISBN-13 if Calibre has a valid one
        ol_id TEXT,                       -- Calibre 'openlibrary' identifier, if any
        formats TEXT,                     -- ',EPUB,MOBI,' (comma wrapped for instr())
        language TEXT,
        series TEXT,
        series_index REAL,
        pubdate TEXT,
        has_cover INTEGER NOT NULL DEFAULT 0,
        last_modified TEXT,
        work_key TEXT,                    -- matched catalog work, NULL if unmatched
        match_method TEXT,                -- openlibrary | isbn | title_author | manual | NULL
        match_confidence INTEGER NOT NULL DEFAULT 0,
        match_locked INTEGER NOT NULL DEFAULT 0,  -- 1 = set by the user; sync never changes it
        synced_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_library_work ON library_books(work_key);
    CREATE INDEX IF NOT EXISTS idx_library_sort ON library_books(author_sort, title);

    CREATE TABLE IF NOT EXISTS monitored_authors (
        author_key TEXT PRIMARY KEY,
        monitor TEXT NOT NULL DEFAULT 'none',     -- all | future | none | selected (used from Phase 7)
        added_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)


def _migrate_4(conn: sqlite3.Connection) -> None:
    """Problems Tomearr found in a Calibre record (comma separated codes, e.g. 'isbn_mismatch')."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(library_books)")}
    if "issues" not in cols:
        conn.execute("ALTER TABLE library_books ADD COLUMN issues TEXT")


def _migrate_5(conn: sqlite3.Connection) -> None:
    """Phase 4: files found in the ingest folder, and Calibre's files (for duplicate detection)."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ingest_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,         -- relative to the ingest folder
        size INTEGER, mtime_ns INTEGER, sha256 TEXT,
        kind TEXT,                         -- ebook | archive | comic
        format TEXT,                       -- from the file's contents: EPUB, MOBI, AZW3, PDF, ...
        drm INTEGER, drm_detail TEXT,
        meta_json TEXT, meta_source TEXT,  -- what the file says about itself
        sidecar_json TEXT,                 -- metadata.opf / <name>.opf next to it
        entries_json TEXT,                 -- for archives: ebook files inside
        error TEXT,
        title TEXT, authors TEXT, isbn TEXT,           -- the reading that matched best
        match_source TEXT,                 -- embedded | sidecar | filename:<how>
        work_key TEXT, match_method TEXT, match_confidence INTEGER NOT NULL DEFAULT 0,
        candidates_json TEXT, issues TEXT,
        bucket TEXT,                       -- matched | review | unmatched | duplicate | drm | archive | comic | error
        duplicate_of TEXT,                 -- 'ingest:<path>' or 'calibre:<book id>'
        owned_formats TEXT,                -- formats already in Calibre for the matched work
        first_seen TEXT NOT NULL DEFAULT (datetime('now')),
        scanned_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ingest_bucket ON ingest_files(bucket);
    CREATE INDEX IF NOT EXISTS idx_ingest_sha ON ingest_files(sha256);
    CREATE INDEX IF NOT EXISTS idx_ingest_work ON ingest_files(work_key);

    CREATE TABLE IF NOT EXISTS calibre_files (
        calibre_id INTEGER NOT NULL,
        format TEXT NOT NULL,
        size INTEGER,
        relpath TEXT,                      -- relative to the Calibre library folder
        sha256 TEXT,                       -- filled in lazily, only when an ingest file has the same size
        PRIMARY KEY (calibre_id, format)
    );
    CREATE INDEX IF NOT EXISTS idx_calibre_files_size ON calibre_files(size);
    """)


def _migrate_6(conn: sqlite3.Connection) -> None:
    """Phase 5: human review decisions on ingest files, kept apart from the automatic match, plus an undo log."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ingest_files)")}
    for name, ddl in (
        ("decision", "TEXT"),             # NULL (pending) | accepted | not_book | skipped
        ("decided_work_key", "TEXT"),     # the work you accepted (may differ from the automatic match)
        ("override_json", "TEXT"),        # fields you edited: title, authors, series, series_index, isbn, language
        ("decided_at", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE ingest_files ADD COLUMN {name} {ddl}")
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_ingest_decision ON ingest_files(decision);
    CREATE TABLE IF NOT EXISTS review_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER NOT NULL,
        before_json TEXT NOT NULL,        -- decision fields before the change, for undo
        action TEXT NOT NULL,
        at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)


def _migrate_7(conn: sqlite3.Connection) -> None:
    """Phase 6: imports into Calibre (with everything needed to undo them) and the PDF opt-in state."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ingest_files)")}
    if "pdf_state" not in cols:
        # NULL (held) | convert (you opted in) | converted (EPUB staged, waiting for you) | approved
        conn.execute("ALTER TABLE ingest_files ADD COLUMN pdf_state TEXT")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch INTEGER,                    -- the job that did it
        action TEXT NOT NULL,             -- add | add_format | convert_existing | delete_duplicate | unpack
        path TEXT,                        -- ingest path (relative), if it came from the ingest folder
        sha256 TEXT,
        source_format TEXT,
        work_key TEXT,
        calibre_id INTEGER,
        added_format TEXT,                -- what Tomearr added to Calibre (EPUB)
        removed_json TEXT,                -- [{format, backup}] formats removed from Calibre, with backup copies
        trash_path TEXT,                  -- where the ingest file went (inside /config/trash)
        meta_json TEXT,                   -- metadata written
        status TEXT NOT NULL,             -- done | failed | undone
        error TEXT,
        at TEXT NOT NULL DEFAULT (datetime('now')),
        undone_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_imports_batch ON imports(batch);
    """)


def _migrate_8(conn: sqlite3.Connection) -> None:
    """Your choice after comparing an ingest file with the EPUB Calibre already has: calibre | ingest | skip."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ingest_files)")}
    if "compare_choice" not in cols:
        conn.execute("ALTER TABLE ingest_files ADD COLUMN compare_choice TEXT")


def _migrate_9(conn: sqlite3.Connection) -> None:
    """Phase 7: monitoring. Which authors you follow, per-book overrides, and the metadata profile."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(monitored_authors)")}
    if "monitor_from" not in cols:
        # for 'future' monitoring: works already in the catalog when you started don't count
        conn.execute("ALTER TABLE monitored_authors ADD COLUMN monitor_from TEXT")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS author_baseline (       -- works an author had when 'future' monitoring started
        author_key TEXT NOT NULL,
        work_key TEXT NOT NULL,
        PRIMARY KEY (author_key, work_key)
    ) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS work_status (           -- your per-book choices; absent = follow the author
        work_key TEXT PRIMARY KEY,
        state TEXT NOT NULL,                           -- wanted | ignored
        at TEXT NOT NULL DEFAULT (datetime('now'))
    ) WITHOUT ROWID;
    """)


def _migrate_10(conn: sqlite3.Connection) -> None:
    """Phases 8-9: releases sent to a download client, followed until they're imported."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS grabs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_key TEXT NOT NULL,
        title TEXT NOT NULL,               -- the release name
        indexer TEXT, protocol TEXT, format TEXT, size INTEGER,
        client TEXT NOT NULL,              -- deluge | sabnzbd
        client_id TEXT,                    -- torrent hash or SABnzbd nzo id
        state TEXT NOT NULL DEFAULT 'queued',   -- queued downloading completed failed missing imported removed
        progress REAL NOT NULL DEFAULT 0,
        message TEXT,
        release_json TEXT,
        added_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_grabs_work ON grabs(work_key);
    CREATE INDEX IF NOT EXISTS idx_grabs_state ON grabs(state);
    """)


MIGRATIONS = [_migrate_2, _migrate_3, _migrate_4, _migrate_5, _migrate_6, _migrate_7, _migrate_8, _migrate_9,
              _migrate_10]  # MIGRATIONS[i] upgrades version i+1 to i+2
SCHEMA_VERSION = 1 + len(MIGRATIONS)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-200000")  # ~200 MB page cache
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "schema_version", 1))


def init(conn: sqlite3.Connection) -> None:
    """Create the base schema if needed, then apply any pending migrations in order."""
    conn.executescript(SCHEMA_V1)
    conn.commit()
    version = schema_version(conn)
    for i in range(version - 1, len(MIGRATIONS)):
        with conn:
            MIGRATIONS[i](conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(i + 2),),
            )


def reset_editions(conn: sqlite3.Connection) -> None:
    """Editions and ISBNs come wholly from the editions dump, so a reimport starts from empty tables."""
    conn.executescript("DROP TABLE IF EXISTS editions; DROP TABLE IF EXISTS isbns;" + EDITIONS_DDL)
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
