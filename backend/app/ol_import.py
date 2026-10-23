"""Stream Open Library dump files into SQLite.

Dump format: one record per line, tab separated:
    type <TAB> key <TAB> revision <TAB> last_modified <TAB> json
"""
import gzip
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterator

import orjson

from . import db

BATCH = 50_000
PROGRESS_EVERY = 100_000
YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")

Progress = Callable[[dict], None]
Cancelled = Callable[[], bool]


class ImportCancelled(Exception):
    pass


def iter_dump(path: Path) -> Iterator[tuple[bytes, bytes, bytes]]:
    """Yield (type, key, json_bytes) for each well-formed line."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as fh:
        for line in fh:
            parts = line.rstrip(b"\r\n").split(b"\t", 4)
            if len(parts) < 5:
                continue
            yield parts[0], parts[1], parts[4]


def _strip(key: str, prefix: str) -> str:
    return key[len(prefix):] if key.startswith(prefix) else key


def _text(v) -> str | None:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return None


def parse_author(raw: bytes) -> tuple | None:
    rec = orjson.loads(raw)
    key = _text(rec.get("key"))
    name = _text(rec.get("name")) or _text(rec.get("personal_name"))
    if not key or not name:
        return None
    alts = rec.get("alternate_names")
    alt = " | ".join(a.strip() for a in alts if isinstance(a, str) and a.strip()) if isinstance(alts, list) else None
    return (
        _strip(key, "/authors/"),
        name,
        alt or None,
        _text(rec.get("birth_date")),
        _text(rec.get("death_date")),
    )


def parse_work(raw: bytes) -> tuple[tuple, list[str]] | None:
    rec = orjson.loads(raw)
    key = _text(rec.get("key"))
    title = _text(rec.get("title"))
    if not key or not title:
        return None
    wkey = _strip(key, "/works/")

    year = None
    fpd = rec.get("first_publish_date")
    if isinstance(fpd, str):
        m = YEAR_RE.search(fpd)
        if m:
            year = int(m.group(1))

    subjects = rec.get("subjects")
    subj = None
    if isinstance(subjects, list):
        subj = " | ".join(s for s in subjects[:10] if isinstance(s, str)) or None

    cover = None
    covers = rec.get("covers")
    if isinstance(covers, list):
        for c in covers:
            if isinstance(c, int) and c > 0:
                cover = c
                break

    author_keys: list[str] = []
    for item in rec.get("authors") or []:
        a = item.get("author") if isinstance(item, dict) else None
        if isinstance(a, dict):
            a = a.get("key")
        if isinstance(a, str):
            author_keys.append(_strip(a, "/authors/"))

    return (wkey, title, _text(rec.get("subtitle")), year, subj, cover), author_keys


def import_authors(conn: sqlite3.Connection, path: Path, progress: Progress, cancelled: Cancelled) -> int:
    batch: list[tuple] = []
    seen = kept = 0
    sql = ("INSERT INTO authors(ol_key, name, alt_names, birth_date, death_date) VALUES(?,?,?,?,?) "
           "ON CONFLICT(ol_key) DO UPDATE SET name=excluded.name, alt_names=excluded.alt_names, "
           "birth_date=excluded.birth_date, death_date=excluded.death_date")
    for rtype, _key, raw in iter_dump(path):
        seen += 1
        if rtype == b"/type/author":
            try:
                row = parse_author(raw)
            except (orjson.JSONDecodeError, AttributeError):
                row = None
            if row:
                batch.append(row)
                kept += 1
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            conn.commit()
            batch.clear()
        if seen % PROGRESS_EVERY == 0:
            if cancelled():
                raise ImportCancelled()
            progress({"phase": "authors", "lines": seen, "kept": kept})
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
    progress({"phase": "authors", "lines": seen, "kept": kept})
    return kept


def import_works(conn: sqlite3.Connection, path: Path, progress: Progress, cancelled: Cancelled,
                 require_author: bool = True) -> int:
    wbatch: list[tuple] = []
    abatch: list[tuple] = []
    seen = kept = 0
    wsql = ("INSERT INTO works(ol_key, title, subtitle, first_publish_year, subjects, cover_id) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(ol_key) DO UPDATE SET title=excluded.title, subtitle=excluded.subtitle, "
            "first_publish_year=excluded.first_publish_year, subjects=excluded.subjects, cover_id=excluded.cover_id")
    asql = "INSERT OR IGNORE INTO work_authors(work_key, author_key) VALUES(?,?)"

    def flush():
        conn.executemany(wsql, wbatch)
        conn.executemany(asql, abatch)
        conn.commit()
        wbatch.clear()
        abatch.clear()

    for rtype, _key, raw in iter_dump(path):
        seen += 1
        if rtype == b"/type/work":
            try:
                parsed = parse_work(raw)
            except (orjson.JSONDecodeError, AttributeError):
                parsed = None
            if parsed:
                row, akeys = parsed
                if akeys or not require_author:
                    wbatch.append(row)
                    abatch.extend((row[0], a) for a in akeys)
                    kept += 1
        if len(wbatch) >= BATCH:
            flush()
        if seen % PROGRESS_EVERY == 0:
            if cancelled():
                raise ImportCancelled()
            progress({"phase": "works", "lines": seen, "kept": kept})
    if wbatch or abatch:
        flush()
    progress({"phase": "works", "lines": seen, "kept": kept})
    return kept


def build_indexes(conn: sqlite3.Connection, progress: Progress) -> dict:
    """Create secondary indexes, refresh author work counts, and rebuild both FTS tables."""
    progress({"phase": "indexes", "step": "secondary indexes"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_author ON work_authors(author_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(name)")
    conn.commit()

    progress({"phase": "indexes", "step": "counting works per author"})
    conn.execute("UPDATE authors SET work_count = 0 WHERE work_count != 0")
    conn.execute(
        "UPDATE authors SET work_count = t.c FROM "
        "(SELECT author_key, COUNT(*) AS c FROM work_authors GROUP BY author_key) AS t "
        "WHERE authors.ol_key = t.author_key"
    )
    conn.commit()

    progress({"phase": "indexes", "step": "authors search index"})
    conn.execute("DROP TABLE IF EXISTS authors_fts")
    conn.execute("CREATE VIRTUAL TABLE authors_fts USING fts5("
                 "name, alt, content='', columnsize=0, tokenize='unicode61 remove_diacritics 2')")
    conn.execute("INSERT INTO authors_fts(rowid, name, alt) SELECT rowid, name, COALESCE(alt_names,'') FROM authors")
    conn.commit()

    progress({"phase": "indexes", "step": "works search index"})
    conn.execute("DROP TABLE IF EXISTS works_fts")
    conn.execute("CREATE VIRTUAL TABLE works_fts USING fts5("
                 "title, authors, content='', columnsize=0, tokenize='unicode61 remove_diacritics 2')")
    conn.execute(
        "INSERT INTO works_fts(rowid, title, authors) "
        "SELECT w.rowid, w.title || ' ' || COALESCE(w.subtitle, ''), "
        "COALESCE((SELECT group_concat(a.name, ' ') FROM work_authors wa "
        "          JOIN authors a ON a.ol_key = wa.author_key WHERE wa.work_key = w.ol_key), '') "
        "FROM works w"
    )
    conn.commit()

    counts = {
        "authors": conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0],
        "works": conn.execute("SELECT COUNT(*) FROM works").fetchone()[0],
    }
    for k, v in counts.items():
        db.set_meta(conn, f"count_{k}", v)
    db.set_meta(conn, "indexed_at", conn.execute("SELECT datetime('now')").fetchone()[0])
    progress({"phase": "indexes", "step": "done", **counts})
    return counts
