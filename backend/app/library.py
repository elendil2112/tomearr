"""The user's library: a read-only mirror of Calibre, matched to catalog works."""
import os
import sqlite3
from pathlib import Path

from . import calibre, db, matcher, search

LOW_CONFIDENCE = 90


class SyncCancelled(Exception):
    pass

DEFAULT_CALIBRE_PATH = os.environ.get("TOMEARR_CALIBRE", "/calibre")

FILTERS = {
    "all": "1=1",
    "matched": "work_key IS NOT NULL",
    "unmatched": "work_key IS NULL",
    "low": f"work_key IS NOT NULL AND match_locked = 0 AND match_confidence < {LOW_CONFIDENCE}",
    "not_epub": "(formats IS NULL OR instr(formats, ',EPUB,') = 0)",
    "manual": "match_locked = 1",
    "issues": "issues IS NOT NULL",
}


def calibre_path(conn: sqlite3.Connection) -> str:
    return db.get_meta(conn, "setting_calibre_path", DEFAULT_CALIBRE_PATH)


def test_connection(path: str, cache_dir: Path) -> dict:
    try:
        snap = calibre.snapshot(Path(path), cache_dir)
        return {"ok": True, "path": path, **calibre.summary(snap)}
    except (calibre.CalibreError, OSError) as exc:
        return {"ok": False, "path": path, "message": str(exc)}


UPSERT = """
INSERT INTO library_books(calibre_id, title, authors, author_sort, isbn, ol_id, formats, language, series,
    series_index, pubdate, has_cover, last_modified, work_key, match_method, match_confidence, issues, synced_at)
VALUES(:calibre_id, :title, :authors, :author_sort, :isbn, :ol_id, :formats, :language, :series,
    :series_index, :pubdate, :has_cover, :last_modified, :work_key, :match_method, :match_confidence, :issues, :now)
ON CONFLICT(calibre_id) DO UPDATE SET
    title=excluded.title, authors=excluded.authors, author_sort=excluded.author_sort, isbn=excluded.isbn,
    ol_id=excluded.ol_id, formats=excluded.formats, language=excluded.language, series=excluded.series,
    series_index=excluded.series_index, pubdate=excluded.pubdate, has_cover=excluded.has_cover,
    last_modified=excluded.last_modified, synced_at=excluded.synced_at,
    work_key=CASE WHEN library_books.match_locked = 1 OR :keep_match THEN library_books.work_key
                  ELSE excluded.work_key END,
    match_method=CASE WHEN library_books.match_locked = 1 OR :keep_match THEN library_books.match_method
                      ELSE excluded.match_method END,
    match_confidence=CASE WHEN library_books.match_locked = 1 OR :keep_match THEN library_books.match_confidence
                          ELSE excluded.match_confidence END,
    issues=CASE WHEN :keep_match THEN library_books.issues ELSE excluded.issues END
"""


def sync(conn: sqlite3.Connection, cache_dir: Path, progress, cancelled, rematch: bool = False) -> dict:
    """Mirror Calibre into library_books and match new or changed books.

    Books whose Calibre record hasn't changed keep their match (unless `rematch`), unmatched books are retried
    every time, and manual matches are never touched. Books deleted from Calibre are removed here too.
    """
    path = calibre_path(conn)
    progress({"step": "copying Calibre database"})
    snap = calibre.snapshot(Path(path), cache_dir)
    total = calibre.summary(snap)["books"]
    can_match = search.has_index(conn)
    prev = {r[0]: (r[1], r[2], r[3]) for r in
            conn.execute("SELECT calibre_id, last_modified, work_key, match_locked FROM library_books")}
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    seen: set[int] = set()
    done = matched_now = 0
    for book in calibre.read_books(snap):
        if done % 100 == 0:
            if cancelled():
                conn.commit()
                raise SyncCancelled()
            progress({"step": "matching", "done": done, "total": total, "matched": matched_now})
        cid = book["calibre_id"]
        seen.add(cid)
        # Clean Calibre's author strings ("Tolkien| J.R.R." -> "J.R.R. Tolkien") for display and matching.
        authors = matcher.split_authors(book["authors"].replace(" & ", " ; "))
        book = {**book, "authors": " & ".join(authors)}
        old = prev.get(cid)
        need = can_match and (rematch or old is None or old[0] != book["last_modified"] or old[1] is None)
        if old and old[2]:
            need = False
        m = matcher.Match(None, None, 0)
        if need:
            m = matcher.match_book(conn, book["title"], authors, book["isbn"], book["ol_id"])
            matched_now += m.work_key is not None
        conn.execute(UPSERT, {**book, "work_key": m.work_key, "match_method": m.method,
                              "match_confidence": m.confidence, "issues": ",".join(m.issues) or None,
                              "now": now, "keep_match": int(not need)})
        done += 1
        if done % 500 == 0:
            conn.commit()
    conn.commit()

    # Calibre's file list, for spotting ingest files that are byte-for-byte copies of books you already have.
    # Hashes are kept when a file's size hasn't changed (they're computed lazily by the ingest scanner).
    known = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute("SELECT calibre_id, format, size, sha256 FROM calibre_files")}
    conn.execute("DELETE FROM calibre_files")
    conn.executemany(
        "INSERT OR REPLACE INTO calibre_files(calibre_id, format, size, relpath, sha256) VALUES(?,?,?,?,?)",
        [(bid, fmt, size, rel, known.get((bid, fmt), (None, None))[1] if known.get((bid, fmt), (None,))[0] == size
          else None) for bid, fmt, size, rel in calibre.read_files(snap)])
    conn.commit()

    gone = [cid for cid in prev if cid not in seen]
    for i in range(0, len(gone), 500):
        chunk = gone[i:i + 500]
        conn.execute(f"DELETE FROM library_books WHERE calibre_id IN ({','.join('?' * len(chunk))})", chunk)
    conn.commit()
    db.set_meta(conn, "library_synced_at", now)
    db.set_meta(conn, "library_synced_path", path)
    result = {"step": "done", "done": done, "total": total, "matched": matched_now, "removed": len(gone)}
    progress(result)
    return result


def stats(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(f"SELECT COUNT(*) FROM library_books WHERE {sql}").fetchone()[0]  # noqa: E731
    by_method = {r[0]: r[1] for r in conn.execute(
        "SELECT match_method, COUNT(*) FROM library_books WHERE work_key IS NOT NULL GROUP BY match_method")}
    formats: dict[str, int] = {}
    for (f,) in conn.execute("SELECT formats FROM library_books WHERE formats IS NOT NULL"):
        for x in f.strip(",").split(","):
            formats[x] = formats.get(x, 0) + 1
    return {
        "books": one("1=1"),
        **{k: one(v) for k, v in FILTERS.items() if k != "all"},
        "by_method": by_method,
        "formats": dict(sorted(formats.items(), key=lambda kv: -kv[1])),
        "synced_at": db.get_meta(conn, "library_synced_at"),
        "path": calibre_path(conn),
    }


def list_books(conn: sqlite3.Connection, q: str = "", filt: str = "all", offset: int = 0, limit: int = 100) -> dict:
    where = [FILTERS.get(filt, "1=1")]
    args: list = []
    for word in matcher.fold(q).split()[:8]:
        where.append("(lower(title) LIKE ? OR lower(authors) LIKE ?)")
        args += [f"%{word}%", f"%{word}%"]
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM library_books WHERE {w}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT b.*, w.title AS work_title, w.first_publish_year AS work_year "
        f"FROM (SELECT * FROM library_books WHERE {w}) b LEFT JOIN works w ON w.ol_key = b.work_key "
        f"ORDER BY b.author_sort COLLATE NOCASE, b.series COLLATE NOCASE, b.series_index, b.title COLLATE NOCASE "
        f"LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["formats"] = [f for f in (d["formats"] or "").split(",") if f]
        d["issues"] = [i for i in (d.get("issues") or "").split(",") if i]
        items.append(d)
    return {"total": total, "offset": offset, "items": items}


def set_match(conn: sqlite3.Connection, calibre_id: int, work_key: str | None) -> dict | None:
    """Pin a book to a work (or to 'no match' with None). Sync will leave it alone from then on."""
    if not conn.execute("SELECT 1 FROM library_books WHERE calibre_id=?", (calibre_id,)).fetchone():
        return None
    if work_key and not conn.execute("SELECT 1 FROM works WHERE ol_key=?", (work_key,)).fetchone():
        raise ValueError("unknown work")
    conn.execute("UPDATE library_books SET work_key=?, match_method=?, match_confidence=?, match_locked=1 "
                 "WHERE calibre_id=?", (work_key, "manual" if work_key else None, 100 if work_key else 0, calibre_id))
    conn.commit()
    return dict(conn.execute("SELECT * FROM library_books WHERE calibre_id=?", (calibre_id,)).fetchone())


def clear_manual(conn: sqlite3.Connection, calibre_id: int) -> bool:
    """Drop a manual match; the next sync matches the book automatically again."""
    cur = conn.execute("UPDATE library_books SET match_locked=0, work_key=NULL, match_method=NULL, "
                       "match_confidence=0 WHERE calibre_id=?", (calibre_id,))
    conn.commit()
    return cur.rowcount > 0


def attach_owned(conn: sqlite3.Connection, works: list[dict]) -> list[dict]:
    """Add `owned`: the formats you have for each work (empty list if you don't own it)."""
    if not works:
        return works
    keys = [w["ol_key"] for w in works]
    owned: dict[str, set] = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for wk, f in conn.execute(f"SELECT work_key, formats FROM library_books WHERE work_key IN "
                                  f"({','.join('?' * len(chunk))})", chunk):
            owned.setdefault(wk, set()).update(x for x in (f or "").split(",") if x)
            owned[wk].add("")  # marks ownership even with no files
    for w in works:
        s = owned.get(w["ol_key"])
        w["owned"] = sorted(x for x in s if x) if s is not None else None
    return works
