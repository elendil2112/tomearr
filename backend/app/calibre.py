"""Read-only access to a Calibre library.

Tomearr never opens Calibre's metadata.db directly. It copies the file (plus its -wal, if any) into /config,
checks the copy is consistent, and reads the copy. That way it takes no lock on the live database, can't
interfere with a running Calibre / Calibre-Web, and works even when the library is mounted read-only.
"""
import shutil
import sqlite3
import time
from pathlib import Path

from .isbn import to_isbn13


class CalibreError(Exception):
    pass


def _stat(p: Path) -> tuple:
    try:
        st = p.stat()
        return st.st_size, st.st_mtime_ns
    except FileNotFoundError:
        return None


def snapshot(library: Path, cache_dir: Path, attempts: int = 4, wait: float = 1.5) -> Path:
    """Copy library/metadata.db into cache_dir and return the path of a consistent standalone copy."""
    src = Path(library) / "metadata.db"
    if not src.is_file():
        raise CalibreError(f"No metadata.db found in {library}. Is this the Calibre library folder?")
    wal = Path(str(src) + "-wal")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / "calibre-copy.db"
    out = cache_dir / "calibre-metadata.db"

    last_err = "unknown error"
    for attempt in range(attempts):
        if attempt:
            time.sleep(wait)
        for f in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
            f.unlink(missing_ok=True)
        before = (_stat(src), _stat(wal))
        shutil.copyfile(src, tmp)
        if wal.exists():
            shutil.copyfile(wal, Path(str(tmp) + "-wal"))
        if (_stat(src), _stat(wal)) != before:
            last_err = "Calibre was writing to the library during the copy"
            continue
        try:
            c = sqlite3.connect(tmp)
            try:
                if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    last_err = "the copied database failed an integrity check"
                    continue
                c.execute("SELECT COUNT(*) FROM books").fetchone()
                dst = sqlite3.connect(out.with_suffix(".new"))
                c.backup(dst)  # folds any WAL content into one clean file
                dst.close()
            finally:
                c.close()
        except sqlite3.DatabaseError as exc:
            last_err = f"the copied database could not be read ({exc})"
            continue
        out.with_suffix(".new").replace(out)
        for f in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
            f.unlink(missing_ok=True)
        return out
    raise CalibreError(f"Could not get a consistent copy of metadata.db: {last_err}")


def _open(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def summary(snapshot_path: Path) -> dict:
    c = _open(snapshot_path)
    try:
        return {
            "books": c.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "authors": c.execute("SELECT COUNT(*) FROM authors").fetchone()[0],
            "formats": {r[0]: r[1] for r in c.execute("SELECT format, COUNT(*) FROM data GROUP BY format ORDER BY 2 DESC")},
        }
    finally:
        c.close()


def _date(v) -> str | None:
    """Calibre stores 0101-01-01 for 'unknown'."""
    v = (v or "")[:10]
    return v if v and not v.startswith("0101") else None


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


OPTIONAL_BOOK_COLS = ("title", "author_sort", "isbn", "pubdate", "series_index", "has_cover", "last_modified")


def read_books(snapshot_path: Path):
    """Yield one dict per Calibre book. Streams, so a big library never sits in memory at once."""
    c = _open(snapshot_path)
    try:
        tables = _tables(c)
        # Calibre's books table has changed over the years (newer versions dropped books.isbn, for example), so
        # only select the optional columns that exist and read the rest as NULL.
        have = {r[1] for r in c.execute("PRAGMA table_info(books)")}
        book_cols = ", ".join(f"b.{col}" if col in have else f"NULL AS {col}" for col in OPTIONAL_BOOK_COLS)
        has_ids = "identifiers" in tables
        has_lang = {"books_languages_link", "languages"} <= tables
        has_series = {"books_series_link", "series"} <= tables
        lang_sql = ("(SELECT l.lang_code FROM books_languages_link bl JOIN languages l ON l.id = bl.lang_code "
                    " WHERE bl.book = b.id ORDER BY bl.item_order LIMIT 1)") if has_lang else "NULL"
        series_sql = ("(SELECT s.name FROM books_series_link bs JOIN series s ON s.id = bs.series "
                      " WHERE bs.book = b.id LIMIT 1)") if has_series else "NULL"
        rows = c.execute(
            f"SELECT b.id, {book_cols}, "
            f"  (SELECT group_concat(name, ' & ') FROM (SELECT a.name FROM books_authors_link ba "
            f"     JOIN authors a ON a.id = ba.author WHERE ba.book = b.id ORDER BY ba.id)) AS authors, "
            f"  (SELECT group_concat(format, ',') FROM data d WHERE d.book = b.id) AS formats, "
            f"  {lang_sql} AS language, {series_sql} AS series "
            f"FROM books b ORDER BY b.id"
        )
        ident = c.cursor()
        for r in rows:
            ids = ({t.lower(): v for t, v in ident.execute("SELECT type, val FROM identifiers WHERE book=?", (r["id"],))}
                   if has_ids else {})
            isbn = to_isbn13(ids.get("isbn")) or to_isbn13(r["isbn"])
            formats = sorted({f.upper() for f in (r["formats"] or "").split(",") if f})
            yield {
                "calibre_id": r["id"],
                "title": r["title"] or "Unknown",
                "authors": r["authors"] or "",
                "author_sort": r["author_sort"],
                "isbn": isbn,
                "ol_id": (ids.get("openlibrary") or "").strip().rstrip("/").split("/")[-1] or None,
                "formats": f",{','.join(formats)}," if formats else None,
                "language": r["language"],
                "series": r["series"],
                "series_index": r["series_index"] if r["series"] else None,
                "pubdate": _date(r["pubdate"]),
                "has_cover": int(bool(r["has_cover"])),
                "last_modified": r["last_modified"],
            }
    finally:
        c.close()


def read_files(snapshot_path: Path):
    """Yield (book_id, FORMAT, size, path relative to the library) for every file Calibre tracks."""
    c = _open(snapshot_path)
    try:
        if "path" not in {r[1] for r in c.execute("PRAGMA table_info(books)")}:
            return
        for bid, fmt, size, name, bpath in c.execute(
                "SELECT d.book, d.format, d.uncompressed_size, d.name, b.path FROM data d JOIN books b ON b.id = d.book"):
            if bpath and name and fmt:
                yield bid, fmt.upper(), size, f"{bpath}/{name}.{fmt.lower()}"
    finally:
        c.close()
