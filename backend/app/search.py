import math
import re
import sqlite3
import unicodedata

from . import db
from .isbn import to_isbn13

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# How many FTS matches to pull before reranking. The works index is stored in popularity order (see
# ol_import.build_indexes), so these are the most popular matches, not an arbitrary slice.
CANDIDATES = 1500
ARTICLES = {"the", "a", "an"}

WORK_COLS = ("w.ol_key, w.title, w.subtitle, w.first_publish_year, w.cover_id, w.subjects, "
             "w.edition_count, w.ebook_count, w.popularity")


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word is a prefix term, all required."""
    tokens = TOKEN_RE.findall(text or "")
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens[:12])


def has_index(conn: sqlite3.Connection) -> bool:
    n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('authors_fts','works_fts')").fetchone()[0]
    return n == 2


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _words(s: str | None) -> list[str]:
    return [t for t in TOKEN_RE.findall(_fold(s or "")) if t not in ARTICLES]


def _lang_clause(lang: str | None) -> tuple[str, tuple]:
    """Keep works with an edition in `lang`, plus works whose edition languages are unknown."""
    if not lang or lang == "all":
        return "", ()
    return " AND (w.languages IS NULL OR instr(w.languages, ?) > 0)", (f",{lang},",)


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


def score_work(query_words: list[str], work: dict) -> float:
    """Combined relevance: how much of the title the query covers, plus a damped popularity term.

    Title coverage is the share of title words the query accounts for, so "hobbit" covers all of "The Hobbit"
    but only half of "The Hobbit Companion". Popularity is log-scaled so that a famous book with a partial
    title match does not bury an exact match, but among equal text matches the famous one wins.
    """
    title = _words(work["title"])
    matched = sum(1 for t in title if any(t.startswith(q) for q in query_words))
    coverage = matched / len(title) if title else 0.0
    exact = 1.0 if title and matched == len(title) and len(title) == len(query_words) else 0.0
    return 2.0 * coverage + 1.0 * exact + 0.6 * math.log10(1 + (work.get("popularity") or 0))


def search_works(conn: sqlite3.Connection, q: str, limit: int = 40, lang: str | None = None) -> list[dict]:
    fq = fts_query(q)
    if not fq or not has_index(conn):
        return []
    join_col = "pop_rank" if db.get_meta(conn, "works_fts_key") == "pop_rank" else "rowid"
    lang_sql, lang_args = _lang_clause(lang)
    rows = conn.execute(
        f"SELECT {WORK_COLS} FROM works_fts f JOIN works w ON w.{join_col} = f.rowid "
        f"WHERE works_fts MATCH ?{lang_sql} LIMIT ?",
        (fq, *lang_args, CANDIDATES),
    ).fetchall()
    qwords = _words(q) or [t.lower() for t in TOKEN_RE.findall(q)]
    works = [dict(r) for r in rows]
    works.sort(key=lambda w: score_work(qwords, w), reverse=True)
    return _attach_authors(conn, works[:limit])


def lookup_isbn(conn: sqlite3.Connection, raw: str) -> dict | None:
    """Find editions (and their works) for an ISBN-10 or ISBN-13. Most popular work first."""
    isbn = to_isbn13(raw)
    if not isbn:
        return None
    eds = conn.execute(
        "SELECT e.ol_key, e.work_key, e.title, e.subtitle, e.language, e.publisher, e.publish_year, e.pages, e.format "
        "FROM isbns i JOIN editions e ON e.ol_key = i.edition_key WHERE i.isbn = ?",
        (isbn,),
    ).fetchall()
    keys = list(dict.fromkeys(e["work_key"] for e in eds))
    works = []
    if keys:
        marks = ",".join("?" * len(keys))
        works = [dict(r) for r in conn.execute(
            f"SELECT {WORK_COLS} FROM works w WHERE w.ol_key IN ({marks}) ORDER BY w.popularity DESC", keys)]
        _attach_authors(conn, works)
    return {"isbn": isbn, "editions": [dict(e) for e in eds], "works": works}


def get_work(conn: sqlite3.Connection, key: str, edition_limit: int = 200) -> dict | None:
    w = conn.execute(f"SELECT {WORK_COLS}, w.languages FROM works w WHERE w.ol_key=?", (key,)).fetchone()
    if not w:
        return None
    out = dict(w)
    out["languages"] = [l for l in (out["languages"] or "").split(",") if l]
    _attach_authors(conn, [out])
    sig = conn.execute("SELECT * FROM work_signals WHERE work_key=?", (key,)).fetchone()
    out["ratings"] = {
        "count": sig["rating_count"] if sig else 0,
        "average": round(sig["rating_sum"] / sig["rating_count"], 2) if sig and sig["rating_count"] else None,
    }
    out["readers"] = {k: sig[f"{k}_count"] if sig else 0 for k in ("want", "reading", "read")}
    eds = conn.execute(
        "SELECT ol_key, title, subtitle, language, publisher, publish_year, pages, format FROM editions "
        "WHERE work_key=? ORDER BY (format = 'ebook') DESC, (publish_year IS NULL), publish_year DESC LIMIT ?",
        (key, edition_limit),
    ).fetchall()
    editions = [dict(e) for e in eds]
    if editions:
        marks = ",".join("?" * len(editions))
        by_ed: dict[str, list[str]] = {}
        for r in conn.execute(f"SELECT edition_key, isbn FROM isbns WHERE edition_key IN ({marks})",
                              [e["ol_key"] for e in editions]):
            by_ed.setdefault(r["edition_key"], []).append(r["isbn"])
        for e in editions:
            e["isbns"] = by_ed.get(e["ol_key"], [])
    out["editions"] = editions
    return out


def get_author(conn: sqlite3.Connection, key: str) -> dict | None:
    a = conn.execute(
        "SELECT ol_key, name, alt_names, birth_date, death_date, work_count FROM authors WHERE ol_key=?", (key,)
    ).fetchone()
    if not a:
        return None
    works = conn.execute(
        f"SELECT {WORK_COLS} "
        "FROM work_authors wa JOIN works w ON w.ol_key = wa.work_key WHERE wa.author_key=? "
        "ORDER BY (w.first_publish_year IS NULL), w.first_publish_year, w.title",
        (key,),
    ).fetchall()
    out = dict(a)
    out["works"] = [dict(w) for w in works]
    return out
