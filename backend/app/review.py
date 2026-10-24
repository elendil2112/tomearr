"""Phase 5: human decisions on ingest files the matcher wasn't sure about.

Decisions live in their own columns (decision, decided_work_key, override_json) so rescans and re-matching
never overwrite them. Every change is written to review_log first, so any decision (or a whole bulk action)
can be undone.
"""
import uuid

import orjson

from . import library, matcher, search
from .ingest import QUEUE_SQL
from .search import WORK_COLS

ACTIONS = ("accept", "not_book", "skip", "reset")
QUEUE_ORDER = "ORDER BY CASE bucket WHEN 'review' THEN 0 ELSE 1 END, match_confidence DESC, path COLLATE NOCASE"
OVERRIDE_FIELDS = ("title", "authors", "series", "series_index", "isbn", "language")


class ReviewError(ValueError):
    pass


# --- reading what we know about a file --------------------------------------------------------------------------

def _row(conn, file_id: int):
    r = conn.execute("SELECT * FROM ingest_files WHERE id=?", (file_id,)).fetchone()
    if r is None:
        raise KeyError(file_id)
    return r


def _j(v):
    return orjson.loads(v) if v else None


def reading(r) -> dict:
    """The best description of the book: your edits, then a metadata.opf, then the file's own metadata, then
    whatever reading the matcher used (often the file name)."""
    meta, side, over = _j(r["meta_json"]) or {}, _j(r["sidecar_json"]) or {}, _j(r["override_json"]) or {}
    out, source = {}, {}
    for layer_name, layer in (("matched reading", {"title": r["title"], "isbns": [r["isbn"]] if r["isbn"] else [],
                                                   "authors": matcher.split_authors(r["authors"] or "")}),
                              ("file", meta), ("metadata.opf", side), ("your edit", over)):
        for k in ("title", "authors", "isbns", "series", "series_index", "language", "year", "publisher"):
            v = layer.get(k)
            if k == "isbns" and layer is over and over.get("isbn"):
                v = [over["isbn"]]
            if v not in (None, "", []):
                out[k], source[k] = v, layer_name
    out.setdefault("authors", [])
    out.setdefault("isbns", [])
    out["source"] = source
    return out


def _edition_isbns(conn, work_keys: list[str]) -> dict[str, set]:
    if not work_keys:
        return {}
    marks = ",".join("?" * len(work_keys))
    out: dict[str, set] = {}
    for wk, isbn in conn.execute(f"SELECT e.work_key, i.isbn FROM editions e JOIN isbns i ON i.edition_key = e.ol_key "
                                 f"WHERE e.work_key IN ({marks})", work_keys):
        out.setdefault(wk, set()).add(isbn)
    return out


def enrich(conn, keys: list[str], rd: dict, scores: dict[str, float] | None = None) -> list[dict]:
    """Candidate works with everything the review card shows: cover, authors, year, editions, owned formats,
    and why each one is (or isn't) a good fit."""
    keys = list(dict.fromkeys(k for k in keys if k))
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    rows = {r["ol_key"]: dict(r) for r in conn.execute(f"SELECT {WORK_COLS} FROM works w WHERE w.ol_key IN ({marks})", keys)}
    works = [rows[k] for k in keys if k in rows]
    search._attach_authors(conn, works)
    library.attach_owned(conn, works)
    file_isbns = set(rd.get("isbns") or [])
    ed_isbns = _edition_isbns(conn, [w["ol_key"] for w in works]) if file_isbns else {}
    title, authors = rd.get("title") or "", rd.get("authors") or []
    for w in works:
        names = [a["name"] for a in w["authors"]]
        t = matcher.title_similarity(title, w, authors) if title else None
        a = max((matcher.author_similarity(x, y) for x in authors for y in names), default=None) if authors and names else None
        isbn_hit = sorted(file_isbns & ed_isbns.get(w["ol_key"], set()))
        why = []
        if isbn_hit:
            why.append(f"ISBN {isbn_hit[0]} is an edition of this book")
        if t is not None:
            why.append(f"title {round(t)}% similar")
        if a is not None:
            why.append(f"author {round(a)}% similar" if a < 100 else "same author")
        elif not authors:
            why.append("no author to compare")
        if w.get("edition_count"):
            why.append(f"{w['edition_count']} edition{'' if w['edition_count'] == 1 else 's'}")
        sc = (scores or {}).get(w["ol_key"])
        if sc is None:
            sc = 98.0 if isbn_hit else (matcher.score(title, authors, w) if title else 0.0)
        w.update(title_sim=t, author_sim=a, isbn_match=bool(isbn_hit), score=round(sc, 1), why=why)
    return works


def detail(conn, file_id: int) -> dict:
    r = _row(conn, file_id)
    rd = reading(r)
    cands = _j(r["candidates_json"]) or []
    scores = {c["ol_key"]: c.get("score") for c in cands if c.get("score") is not None}
    keys = [r["decided_work_key"], r["work_key"], *[c["ol_key"] for c in cands]]
    out = {k: r[k] for k in r.keys() if not k.endswith("_json")}
    out.update(meta=_j(r["meta_json"]), sidecar=_j(r["sidecar_json"]), overrides=_j(r["override_json"]),
               entries=_j(r["entries_json"]), drm=None if r["drm"] is None else bool(r["drm"]),
               owned_formats=[x for x in (r["owned_formats"] or "").split(",") if x],
               reading=rd, candidates=enrich(conn, keys, rd, scores),
               selected=r["decided_work_key"] or r["work_key"] or (cands[0]["ol_key"] if cands else None),
               can_accept=r["kind"] == "ebook" and not r["drm"] and r["bucket"] not in ("error",))
    return out


# --- queue ----------------------------------------------------------------------------------------------------

QUEUES = {
    "queue": QUEUE_SQL,
    "skipped": "decision = 'skipped'",
    "accepted": "decision = 'accepted'",
    "not_book": "decision = 'not_book'",
    "matched": "bucket = 'matched' AND decision IS NULL",
}


def queue(conn, which: str = "queue") -> dict:
    if which not in QUEUES:
        raise ReviewError(f"unknown queue {which}")
    ids = [r[0] for r in conn.execute(f"SELECT id FROM ingest_files WHERE {QUEUES[which]} {QUEUE_ORDER}")]
    counts = {k: conn.execute(f"SELECT COUNT(*) FROM ingest_files WHERE {v}").fetchone()[0] for k, v in QUEUES.items()}
    return {"queue": which, "ids": ids, "counts": counts}


# --- decisions --------------------------------------------------------------------------------------------------

def _clean_overrides(o: dict | None) -> dict | None:
    if not o:
        return None
    out = {}
    for k in OVERRIDE_FIELDS:
        v = o.get(k)
        if k == "authors" and isinstance(v, str):
            v = matcher.split_authors(v)
        if k == "series_index" and v not in (None, ""):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ReviewError("series number must be a number")
        if k == "isbn" and v:
            from .isbn import to_isbn13
            v = to_isbn13(str(v))
            if not v:
                raise ReviewError("that ISBN is not valid")
        if isinstance(v, str):
            v = v.strip()
        if v not in (None, "", []):
            out[k] = v
    return out or None


def _log(conn, r, action: str) -> None:
    before = {k: r[k] for k in ("decision", "decided_work_key", "override_json", "decided_at")}
    conn.execute("INSERT INTO review_log(file_id, before_json, action) VALUES(?,?,?)",
                 (r["id"], orjson.dumps(before).decode(), action))


def _apply(conn, r, action: str, work_key: str | None, overrides: dict | None, log_action: str) -> None:
    if action == "accept":
        if not (r["kind"] == "ebook" and not r["drm"] and r["bucket"] != "error"):
            raise ReviewError("this file can't be imported (DRM, archive, comic or unreadable); mark it 'not a book' or skip")
        wk = work_key or r["decided_work_key"] or r["work_key"]
        if not wk:
            raise ReviewError("pick a work to accept this file as")
        if not conn.execute("SELECT 1 FROM works WHERE ol_key=?", (wk,)).fetchone():
            raise ReviewError("unknown work")
        values = ("accepted", wk, orjson.dumps(overrides).decode() if overrides else r["override_json"])
    elif action == "not_book":
        values = ("not_book", None, None)
    elif action == "skip":
        values = ("skipped", r["decided_work_key"], r["override_json"])
    elif action == "reset":
        values = (None, None, None)
    else:
        raise ReviewError(f"unknown action {action}")
    _log(conn, r, log_action)
    conn.execute("UPDATE ingest_files SET decision=?, decided_work_key=?, override_json=?, "
                 "decided_at=datetime('now') WHERE id=?", (*values, r["id"]))


def decide(conn, file_id: int, action: str, work_key: str | None = None, overrides: dict | None = None) -> dict:
    r = _row(conn, file_id)
    _apply(conn, r, action, work_key, _clean_overrides(overrides), action)
    conn.commit()
    return next_after(conn, file_id)


def next_after(conn, file_id: int) -> dict:
    """The next file still in the review queue, so the UI can move straight on."""
    ids = queue(conn)["ids"]
    return {"id": file_id, "next": ids[0] if ids else None, "remaining": len(ids)}


def undo(conn) -> dict | None:
    """Undo the most recent decision, or the whole of the most recent bulk action."""
    last = conn.execute("SELECT * FROM review_log ORDER BY id DESC LIMIT 1").fetchone()
    if not last:
        return None
    rows = ([last] if not last["action"].startswith("bulk:") else
            conn.execute("SELECT * FROM review_log WHERE action=? ORDER BY id DESC", (last["action"],)).fetchall())
    for lg in rows:
        b = orjson.loads(lg["before_json"])
        conn.execute("UPDATE ingest_files SET decision=?, decided_work_key=?, override_json=?, decided_at=? "
                     "WHERE id=?", (b["decision"], b["decided_work_key"], b["override_json"], b["decided_at"],
                                    lg["file_id"]))
        conn.execute("DELETE FROM review_log WHERE id=?", (lg["id"],))
    conn.commit()
    return {"undone": len(rows), "file_ids": [lg["file_id"] for lg in rows], "action": last["action"]}


# --- finding other candidates -------------------------------------------------------------------------------------

def rematch(conn, file_id: int, title: str = "", authors: list[str] | str = (), isbn: str | None = None) -> list[dict]:
    """Candidates for edited details (not saved until you accept)."""
    r = _row(conn, file_id)
    if isinstance(authors, str):
        authors = matcher.split_authors(authors)
    rd = {**reading(r), "title": title or reading(r).get("title"), "authors": list(authors)}
    if isbn:
        rd["isbns"] = [isbn]
    m = matcher.match_book(conn, rd["title"] or "", rd["authors"], isbn=isbn or None)
    scores = {c["ol_key"]: c.get("score") for c in m.candidates if c.get("score") is not None}
    if m.work_key and m.method == "isbn":
        scores[m.work_key] = m.confidence
    return enrich(conn, [m.work_key, *[c["ol_key"] for c in m.candidates]], rd, scores)


def search_catalog(conn, file_id: int, q: str) -> list[dict]:
    r = _row(conn, file_id)
    hits = search.search_works(conn, q, limit=12, lang="all")
    return enrich(conn, [h["ol_key"] for h in hits], reading(r))


# --- bulk -----------------------------------------------------------------------------------------------------------

def bulk_targets(conn, min_confidence: int | None = None, author_key: str | None = None) -> list:
    where, args = [QUEUE_SQL, "work_key IS NOT NULL", "(drm IS NULL OR drm = 0)"], []
    if min_confidence is not None:
        where.append("match_confidence >= ?")
        args.append(max(int(min_confidence), matcher.MIN_CONFIDENCE))
    if author_key:
        where.append("work_key IN (SELECT work_key FROM work_authors WHERE author_key = ?)")
        args.append(author_key)
    if min_confidence is None and not author_key:
        raise ReviewError("give a minimum confidence or an author")
    return conn.execute(f"SELECT * FROM ingest_files WHERE {' AND '.join(where)} {QUEUE_ORDER}", args).fetchall()


def bulk_accept(conn, min_confidence: int | None = None, author_key: str | None = None, dry_run: bool = True) -> dict:
    rows = bulk_targets(conn, min_confidence, author_key)
    if not dry_run and rows:
        tag = f"bulk:{uuid.uuid4().hex[:12]}"
        for r in rows:
            _apply(conn, r, "accept", r["work_key"], None, tag)
        conn.commit()
    return {"count": len(rows), "applied": not dry_run,
            "sample": [{"id": r["id"], "path": r["path"], "work_key": r["work_key"],
                        "confidence": r["match_confidence"]} for r in rows[:10]]}
