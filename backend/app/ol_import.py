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
from .isbn import to_isbn13

BATCH = 50_000
PROGRESS_EVERY = 100_000
YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")

Progress = Callable[[dict], None]
Cancelled = Callable[[], bool]


class ImportCancelled(Exception):
    pass


class DumpReader:
    """Iterate a (possibly gzipped) dump line by line and report how far through the compressed file we are."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.total = self.path.stat().st_size
        self._raw = open(self.path, "rb")
        self._fh = gzip.GzipFile(fileobj=self._raw) if str(path).endswith(".gz") else self._raw

    @property
    def pos(self) -> int:
        return self._raw.tell()

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._fh)

    def close(self) -> None:
        self._fh.close()
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def iter_dump(path: Path, reader: DumpReader | None = None) -> Iterator[tuple[bytes, bytes, bytes]]:
    """Yield (type, key, json_bytes) for each well-formed line."""
    rd = reader or DumpReader(path)
    try:
        for line in rd:
            parts = line.rstrip(b"\r\n").split(b"\t", 4)
            if len(parts) < 5:
                continue
            yield parts[0], parts[1], parts[4]
    finally:
        if reader is None:
            rd.close()


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




_EBOOK_WORDS = ("ebook", "e-book", "electronic", "kindle", "epub", "online resource", "digital")
_AUDIO_WORDS = ("audio", "cassette", "mp3", "sound recording", "audible", "compact disc", "audio cd")


def norm_format(physical_format) -> str | None:
    """Collapse Open Library's free-text physical_format into ebook / audio / print."""
    if not isinstance(physical_format, str) or not physical_format.strip():
        return None
    t = physical_format.lower()
    if any(w in t for w in _EBOOK_WORDS):
        return "ebook"
    if any(w in t for w in _AUDIO_WORDS):
        return "audio"
    return "print"


def _first_key(items, prefix: str) -> str | None:
    if not isinstance(items, list):
        return None
    for it in items:
        k = it.get("key") if isinstance(it, dict) else it
        if isinstance(k, str) and k.startswith(prefix):
            return k[len(prefix):]
    return None


def parse_edition(raw: bytes) -> tuple[tuple, list[str]] | None:
    """Return ((ol_key, work_key, title, subtitle, language, publisher, year, pages, format), [isbn13...])."""
    rec = orjson.loads(raw)
    key = _text(rec.get("key"))
    work = _first_key(rec.get("works"), "/works/")
    if not key or not work:
        return None

    year = None
    pd = rec.get("publish_date")
    if isinstance(pd, str):
        m = YEAR_RE.search(pd)
        if m:
            year = int(m.group(1))

    pubs = rec.get("publishers")
    publisher = next((p.strip() for p in pubs if isinstance(p, str) and p.strip()), None) if isinstance(pubs, list) else None

    pages = rec.get("number_of_pages")
    pages = pages if isinstance(pages, int) and 0 < pages < 100_000 else None

    isbns: list[str] = []
    for field in ("isbn_13", "isbn_10"):
        vals = rec.get(field)
        if isinstance(vals, list):
            for v in vals:
                n = to_isbn13(v)
                if n and n not in isbns:
                    isbns.append(n)

    row = (
        _strip(key, "/books/"), work, _text(rec.get("title")), _text(rec.get("subtitle")),
        _first_key(rec.get("languages"), "/languages/"), publisher, year, pages,
        norm_format(rec.get("physical_format")),
    )
    return row, isbns


def _report(progress: Progress, reader: DumpReader, phase: str, seen: int, kept: int, **extra) -> None:
    progress({"phase": phase, "lines": seen, "kept": kept, "bytes": reader.pos, "total": reader.total, **extra})


def import_authors(conn: sqlite3.Connection, path: Path, progress: Progress, cancelled: Cancelled) -> int:
    batch: list[tuple] = []
    seen = kept = 0
    sql = ("INSERT INTO authors(ol_key, name, alt_names, birth_date, death_date) VALUES(?,?,?,?,?) "
           "ON CONFLICT(ol_key) DO UPDATE SET name=excluded.name, alt_names=excluded.alt_names, "
           "birth_date=excluded.birth_date, death_date=excluded.death_date")
    with DumpReader(path) as rd:
        for rtype, _key, raw in iter_dump(path, rd):
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
                _report(progress, rd, "authors", seen, kept)
        if batch:
            conn.executemany(sql, batch)
            conn.commit()
        _report(progress, rd, "authors", seen, kept)
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

    with DumpReader(path) as rd:
        for rtype, _key, raw in iter_dump(path, rd):
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
                _report(progress, rd, "works", seen, kept)
        if wbatch or abatch:
            flush()
        _report(progress, rd, "works", seen, kept)
    return kept


def import_editions(conn: sqlite3.Connection, path: Path, progress: Progress, cancelled: Cancelled,
                    languages: list[str] | None = None, isbn_only: bool = False) -> int:
    """Load the editions dump into empty editions/isbns tables.

    Lite mode: `languages` keeps only editions in those MARC codes (editions with no language are kept, because
    many ebooks leave it blank); `isbn_only` keeps only editions that carry at least one valid ISBN.
    """
    db.reset_editions(conn)
    langs = {l.strip().lower() for l in languages or [] if l.strip()}
    ebatch: list[tuple] = []
    ibatch: list[tuple] = []
    seen = kept = isbn_rows = 0
    esql = "INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)"
    isql = "INSERT INTO isbns(isbn, edition_key) VALUES(?,?)"

    def flush():
        conn.executemany(esql, ebatch)
        conn.executemany(isql, ibatch)
        conn.commit()
        ebatch.clear()
        ibatch.clear()

    with DumpReader(path) as rd:
        for rtype, _key, raw in iter_dump(path, rd):
            seen += 1
            if rtype == b"/type/edition":
                try:
                    parsed = parse_edition(raw)
                except (orjson.JSONDecodeError, AttributeError, TypeError):
                    parsed = None
                if parsed:
                    row, isbns = parsed
                    if (not isbn_only or isbns) and (not langs or row[4] is None or row[4] in langs):
                        ebatch.append(row)
                        ibatch.extend((i, row[0]) for i in isbns)
                        kept += 1
                        isbn_rows += len(isbns)
            if len(ebatch) >= BATCH:
                flush()
            if seen % PROGRESS_EVERY == 0:
                if cancelled():
                    raise ImportCancelled()
                _report(progress, rd, "editions", seen, kept, isbns=isbn_rows)
        if ebatch or ibatch:
            flush()
        _report(progress, rd, "editions", seen, kept, isbns=isbn_rows)
    db.set_meta(conn, "editions_lite", orjson.dumps({"languages": sorted(langs), "isbn_only": isbn_only}).decode())
    return kept


def _work_key(field: bytes) -> str | None:
    k = field.strip().decode("utf-8", "replace")
    k = _strip(k, "/works/")
    return k if k.startswith("OL") and k.endswith("W") else None


_SHELVES = {"wanttoread": "want_count", "currentlyreading": "reading_count", "alreadyread": "read_count"}
_SHELF_RE = re.compile(rb"[^a-z]")


def import_signals(conn: sqlite3.Connection, path: Path, kind: str, progress: Progress,
                   cancelled: Cancelled) -> int:
    """Aggregate the ratings or reading-log dump into work_signals.

    Both files are one row per user action: work key, optional edition key, rating (1-5) or shelf name, date.
    Each batch is summed in memory and added to the table, so memory stays bounded however big the file gets.
    The columns this dump owns are zeroed first, which makes a reimport replace rather than double count.
    """
    if kind == "ratings":
        cols = ("rating_sum", "rating_count")
    elif kind == "reading-log":
        cols = ("want_count", "reading_count", "read_count")
    else:
        raise ValueError(kind)
    conn.execute(f"UPDATE work_signals SET {', '.join(c + '=0' for c in cols)}")
    conn.commit()
    sql = (f"INSERT INTO work_signals(work_key, {', '.join(cols)}) VALUES(?{',?' * len(cols)}) "
           f"ON CONFLICT(work_key) DO UPDATE SET {', '.join(f'{c}={c}+excluded.{c}' for c in cols)}")
    agg: dict[str, list[int]] = {}
    seen = kept = 0

    def flush():
        conn.executemany(sql, [(k, *v) for k, v in agg.items()])
        conn.commit()
        agg.clear()

    with DumpReader(path) as rd:
        for line in rd:
            seen += 1
            parts = line.rstrip(b"\r\n").split(b"\t")
            if len(parts) < 3:
                parts = line.rstrip(b"\r\n").split(b",")
            if len(parts) >= 3:
                wk = _work_key(parts[0])
                if wk:
                    val = parts[2].strip()
                    if kind == "ratings":
                        if val.isdigit() and 1 <= int(val) <= 5:
                            a = agg.setdefault(wk, [0, 0])
                            a[0] += int(val)
                            a[1] += 1
                            kept += 1
                    else:
                        col = _SHELVES.get(_SHELF_RE.sub(b"", val.lower()).decode())
                        if col:
                            a = agg.setdefault(wk, [0, 0, 0])
                            a[cols.index(col)] += 1
                            kept += 1
            if len(agg) >= BATCH:
                flush()
            if seen % PROGRESS_EVERY == 0:
                if cancelled():
                    raise ImportCancelled()
                _report(progress, rd, kind, seen, kept)
        if agg:
            flush()
        _report(progress, rd, kind, seen, kept)
    return kept


# Popularity is a weighted count of how much people engage with a work. Editions say how often it has been
# published; reading-log shelves and ratings say how many Open Library users care about it now.
POPULARITY_SQL = ("works.edition_count + 3 * s.rating_count + 2 * s.read_count + s.reading_count + s.want_count")


def build_indexes(conn: sqlite3.Connection, progress: Progress) -> dict:
    """Create secondary indexes, refresh derived counts and popularity, and rebuild both FTS tables."""
    progress({"phase": "indexes", "step": "secondary indexes"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_author ON work_authors(author_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_editions_work ON editions(work_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_editions_key ON editions(ol_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_isbns_isbn ON isbns(isbn)")
    conn.commit()

    progress({"phase": "indexes", "step": "counting works per author"})
    conn.execute("UPDATE authors SET work_count = 0 WHERE work_count != 0")
    conn.execute(
        "UPDATE authors SET work_count = t.c FROM "
        "(SELECT author_key, COUNT(*) AS c FROM work_authors GROUP BY author_key) AS t "
        "WHERE authors.ol_key = t.author_key"
    )
    conn.commit()

    progress({"phase": "indexes", "step": "counting editions per work"})
    conn.execute("UPDATE works SET edition_count = 0, ebook_count = 0, languages = NULL "
                 "WHERE edition_count != 0 OR ebook_count != 0 OR languages IS NOT NULL")
    conn.execute(
        "UPDATE works SET edition_count = t.c, ebook_count = t.e, languages = t.l FROM "
        "(SELECT work_key, COUNT(*) AS c, COUNT(CASE WHEN format = 'ebook' THEN 1 END) AS e, "
        "        ',' || group_concat(DISTINCT language) || ',' AS l "
        " FROM editions GROUP BY work_key) AS t "
        "WHERE works.ol_key = t.work_key"
    )
    conn.commit()

    progress({"phase": "indexes", "step": "scoring popularity"})
    conn.execute("UPDATE works SET popularity = edition_count")
    conn.execute(f"UPDATE works SET popularity = {POPULARITY_SQL} FROM work_signals s WHERE s.work_key = works.ol_key")
    conn.execute("DROP INDEX IF EXISTS idx_works_pop_rank")
    conn.execute(
        "UPDATE works SET pop_rank = t.r FROM "
        "(SELECT rowid AS rid, ROW_NUMBER() OVER (ORDER BY popularity DESC, rowid) AS r FROM works) AS t "
        "WHERE works.rowid = t.rid"
    )
    conn.execute("CREATE UNIQUE INDEX idx_works_pop_rank ON works(pop_rank)")
    conn.commit()

    progress({"phase": "indexes", "step": "authors search index"})
    conn.execute("DROP TABLE IF EXISTS authors_fts")
    conn.execute("CREATE VIRTUAL TABLE authors_fts USING fts5("
                 "name, alt, content='', columnsize=0, tokenize='unicode61 remove_diacritics 2')")
    conn.execute("INSERT INTO authors_fts(rowid, name, alt) SELECT rowid, name, COALESCE(alt_names,'') FROM authors")
    conn.commit()

    # The works index uses pop_rank as its rowid. FTS5 returns matches in rowid order, so the first N matches
    # for any query are the N most popular ones, without scoring every match. search.py relies on this.
    progress({"phase": "indexes", "step": "works search index"})
    conn.execute("DROP TABLE IF EXISTS works_fts")
    conn.execute("CREATE VIRTUAL TABLE works_fts USING fts5("
                 "title, authors, content='', columnsize=0, tokenize='unicode61 remove_diacritics 2')")
    conn.execute(
        "INSERT INTO works_fts(rowid, title, authors) "
        "SELECT w.pop_rank, w.title || ' ' || COALESCE(w.subtitle, ''), "
        "COALESCE((SELECT group_concat(a.name, ' ') FROM work_authors wa "
        "          JOIN authors a ON a.ol_key = wa.author_key WHERE wa.work_key = w.ol_key), '') "
        "FROM works w ORDER BY w.pop_rank"
    )
    conn.commit()
    db.set_meta(conn, "works_fts_key", "pop_rank")

    counts = {
        "authors": conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0],
        "works": conn.execute("SELECT COUNT(*) FROM works").fetchone()[0],
        "editions": conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0],
        "isbns": conn.execute("SELECT COUNT(*) FROM isbns").fetchone()[0],
        "rated_works": conn.execute("SELECT COUNT(*) FROM work_signals WHERE rating_count > 0").fetchone()[0],
    }
    for k, v in counts.items():
        db.set_meta(conn, f"count_{k}", v)
    db.set_meta(conn, "indexed_at", conn.execute("SELECT datetime('now')").fetchone()[0])
    progress({"phase": "indexes", "step": "done", **counts})
    return counts
