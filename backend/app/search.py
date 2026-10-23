import re
import sqlite3

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word is a prefix term, all required."""
    tokens = TOKEN_RE.findall(text or "")
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens[:12])


def has_index(conn: sqlite3.Connection) -> bool:
    n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('authors_fts','works_fts')").fetchone()[0]
    return n == 2


def search_authors(conn: sqlite3.Connection, q: str, limit: int = 25) -> list[dict]:
    fq = fts_query(q)
    if not fq or not has_index(conn):
        return []
    rows = conn.execute(
        "SELECT a.ol_key, a.name, a.birth_date, a.death_date, a.work_count FROM "
        "(SELECT rowid AS rid, bm25(authors_fts, 10.0, 1.0) AS rank FROM authors_fts "
        " WHERE authors_fts MATCH ? ORDER BY rank LIMIT 300) f "
        "JOIN authors a ON a.rowid = f.rid "
        "ORDER BY a.work_count DESC, f.rank LIMIT ?",
        (fq, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _attach_authors(conn: sqlite3.Connection, works: list[dict]) -> list[dict]:
    if not works:
        return works
    keys = [w["ol_key"] for w in works]
    marks = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT wa.work_key, a.ol_key, a.name FROM work_authors wa "
        f"JOIN authors a ON a.ol_key = wa.author_key WHERE wa.work_key IN ({marks})",
        keys,
    ).fetchall()
    by_work: dict[str, list[dict]] = {}
    for r in rows:
        by_work.setdefault(r["work_key"], []).append({"ol_key": r["ol_key"], "name": r["name"]})
    for w in works:
        w["authors"] = by_work.get(w["ol_key"], [])
    return works


def search_works(conn: sqlite3.Connection, q: str, limit: int = 40) -> list[dict]:
    fq = fts_query(q)
    if not fq or not has_index(conn):
        return []
    rows = conn.execute(
        "SELECT w.ol_key, w.title, w.subtitle, w.first_publish_year, w.cover_id, w.subjects FROM "
        "(SELECT rowid AS rid, bm25(works_fts, 5.0, 1.0) AS rank FROM works_fts "
        " WHERE works_fts MATCH ? ORDER BY rank LIMIT ?) f "
        "JOIN works w ON w.rowid = f.rid ORDER BY f.rank",
        (fq, limit),
    ).fetchall()
    return _attach_authors(conn, [dict(r) for r in rows])


def get_author(conn: sqlite3.Connection, key: str) -> dict | None:
    a = conn.execute(
        "SELECT ol_key, name, alt_names, birth_date, death_date, work_count FROM authors WHERE ol_key=?", (key,)
    ).fetchone()
    if not a:
        return None
    works = conn.execute(
        "SELECT w.ol_key, w.title, w.subtitle, w.first_publish_year, w.cover_id, w.subjects "
        "FROM work_authors wa JOIN works w ON w.ol_key = wa.work_key WHERE wa.author_key=? "
        "ORDER BY (w.first_publish_year IS NULL), w.first_publish_year, w.title",
        (key,),
    ).fetchall()
    out = dict(a)
    out["works"] = [dict(w) for w in works]
    return out
