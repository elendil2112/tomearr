"""Phase 7: what you want, not just what you have.

Monitor an author with one of:
  all       every work that passes the metadata profile and you don't own is wanted
  future    only works that show up in the catalog after you started monitoring
  selected  only the works you mark wanted yourself
  none      nothing (you can still mark single books wanted)
Per-book choices (wanted / ignored) always win over the author's setting. Books you own are never wanted.

The metadata profile decides which of an author's Open Library works count at all: Open Library lists study
guides, box sets, duplicate works and foreign-language editions under big authors.
"""
import re
from datetime import datetime, timezone

from . import db, ingest, library, search
from .search import WORK_COLS

MONITOR_MODES = ("all", "future", "selected", "none")
PROFILE_DEFAULTS = {"profile_languages": "eng", "profile_min_editions": "2", "profile_exclude_collections": "1"}
RECENT_YEARS = 2     # works this new count with a single edition (new releases haven't had time to get more)

COLLECTION_RE = re.compile(
    r"\b(box(ed)?[\s-]?set|boxset|collection|omnibus|anthology|complete works|collected works|selected works|"
    r"(the )?works of|books?\s+\d+\s*(-|–|to|&|and)\s*\d+|volumes?\s+\d+\s*(-|–|to)\s*\d+|study guide|"
    r"sparknotes|cliffs\s?notes|summary (of|and analysis)|analysis of|teacher'?s guide|reader'?s guide|"
    r"workbook|colou?ring book|calendar \d{4}|\d{4} calendar|quiz book|trivia)\b", re.IGNORECASE)


# --- profile ------------------------------------------------------------------------------------------------

def profile(conn) -> dict:
    g = lambda k: db.get_meta(conn, f"setting_{k}", PROFILE_DEFAULTS[k])  # noqa: E731
    return {"languages": [l for l in g("profile_languages").replace(" ", "").split(",") if l],
            "min_editions": int(g("profile_min_editions")),
            "exclude_collections": g("profile_exclude_collections") == "1"}


def passes(w: dict, prof: dict, this_year: int | None = None) -> tuple[bool, str | None]:
    """Does this work count under the profile? (False, reason) when it doesn't."""
    this_year = this_year or datetime.now(timezone.utc).year
    recent = (w.get("first_publish_year") or 0) >= this_year - RECENT_YEARS
    langs = [l for l in (w.get("languages") or "").split(",") if l]
    if prof["languages"] and langs and not set(langs) & set(prof["languages"]):
        return False, f"not in {', '.join(prof['languages'])}"
    need = 1 if recent else prof["min_editions"]
    if (w.get("edition_count") or 0) < need:
        return False, f"fewer than {need} edition{'s' if need != 1 else ''}"
    if prof["exclude_collections"] and COLLECTION_RE.search(f"{w.get('title') or ''} {w.get('subtitle') or ''}"):
        return False, "looks like a collection, box set or study guide"
    return True, None


# --- monitoring --------------------------------------------------------------------------------------------

def get_monitor(conn, author_key: str) -> dict | None:
    r = conn.execute("SELECT * FROM monitored_authors WHERE author_key=?", (author_key,)).fetchone()
    return dict(r) if r else None


def set_monitor(conn, author_key: str, mode: str) -> dict:
    if mode not in MONITOR_MODES:
        raise ValueError(f"monitor must be one of {', '.join(MONITOR_MODES)}")
    if not conn.execute("SELECT 1 FROM authors WHERE ol_key=?", (author_key,)).fetchone():
        raise KeyError(author_key)
    prev = get_monitor(conn, author_key)
    conn.execute("INSERT INTO monitored_authors(author_key, monitor) VALUES(?, ?) "
                 "ON CONFLICT(author_key) DO UPDATE SET monitor=excluded.monitor", (author_key, mode))
    if mode == "future" and (not prev or prev["monitor"] != "future"):
        # everything the author has right now is 'before'; only works that appear later are wanted
        conn.execute("DELETE FROM author_baseline WHERE author_key=?", (author_key,))
        conn.execute("INSERT INTO author_baseline(author_key, work_key) SELECT author_key, work_key FROM work_authors "
                     "WHERE author_key=?", (author_key,))
        conn.execute("UPDATE monitored_authors SET monitor_from=datetime('now') WHERE author_key=?", (author_key,))
    conn.commit()
    return get_monitor(conn, author_key)


def remove_monitor(conn, author_key: str) -> bool:
    cur = conn.execute("DELETE FROM monitored_authors WHERE author_key=?", (author_key,))
    conn.execute("DELETE FROM author_baseline WHERE author_key=?", (author_key,))
    conn.commit()
    return cur.rowcount > 0


def set_work(conn, work_key: str, state: str | None) -> dict:
    """Mark one book wanted or ignored (None goes back to following the author)."""
    if state not in ("wanted", "ignored", None):
        raise ValueError("state must be wanted, ignored or null")
    if not conn.execute("SELECT 1 FROM works WHERE ol_key=?", (work_key,)).fetchone():
        raise KeyError(work_key)
    if state is None:
        conn.execute("DELETE FROM work_status WHERE work_key=?", (work_key,))
    else:
        conn.execute("INSERT INTO work_status(work_key, state) VALUES(?, ?) ON CONFLICT(work_key) DO UPDATE "
                     "SET state=excluded.state, at=datetime('now')", (work_key, state))
    conn.commit()
    return {"work_key": work_key, "state": state}


# --- statuses -----------------------------------------------------------------------------------------------

def _owned(conn, keys: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for wk, f in conn.execute(f"SELECT work_key, formats FROM library_books WHERE work_key IN "
                                  f"({','.join('?' * len(chunk))})", chunk):
            out.setdefault(wk, []).extend(x for x in (f or "").split(",") if x)
    return out


def _in_ingest(conn, keys: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        out |= {r[0] for r in conn.execute(
            f"SELECT coalesce(decided_work_key, work_key) FROM ingest_files WHERE "
            f"(({ingest.READY_SQL}) OR (bucket = 'review' AND decision IS NULL AND kind = 'ebook')) AND coalesce(decided_work_key, work_key) IN ({','.join('?' * len(chunk))})", chunk)}
    return out


def classify(conn, works: list[dict], monitor: dict | None, baseline: set[str], prof: dict) -> list[dict]:
    """Give each work a status: owned | wanted | in_ingest | ignored | filtered | unmonitored, with a reason."""
    keys = [w["ol_key"] for w in works]
    owned = _owned(conn, keys)
    waiting = _in_ingest(conn, keys)
    overrides = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        overrides.update({r[0]: r[1] for r in conn.execute(
            f"SELECT work_key, state FROM work_status WHERE work_key IN ({','.join('?' * len(chunk))})", chunk)})
    mode = monitor["monitor"] if monitor else "none"
    year = datetime.now(timezone.utc).year
    for w in works:
        ok, why = passes(w, prof, year)
        o = overrides.get(w["ol_key"])
        if w["ol_key"] in owned:
            st, reason = "owned", ", ".join(sorted(set(owned[w["ol_key"]]))) or "no files"
        elif o == "ignored":
            st, reason = "ignored", "you ignored it"
        elif o == "wanted":
            st, reason = "wanted", "you want it"
        elif mode == "none":
            st, reason = "unmonitored", "author not monitored"
        elif not ok:
            st, reason = "filtered", why
        elif mode == "selected":
            st, reason = "unmonitored", "not one of your selected books"
        elif mode == "future" and w["ol_key"] in baseline:
            st, reason = "unmonitored", "published before you started monitoring"
        else:
            st, reason = "wanted", "monitored"
        if st == "wanted" and w["ol_key"] in waiting:
            st, reason = "in_ingest", "a file for it is waiting in the ingest folder"
        w["status"], w["status_why"], w["override"] = st, reason, o
    return works


def _works_of(conn, author_key: str) -> list[dict]:
    rows = conn.execute(
        f"SELECT {WORK_COLS}, w.languages FROM work_authors wa JOIN works w ON w.ol_key = wa.work_key "
        f"WHERE wa.author_key=? ORDER BY (w.first_publish_year IS NULL), w.first_publish_year, w.title",
        (author_key,)).fetchall()
    return [dict(r) for r in rows]


def _baseline(conn, author_key: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT work_key FROM author_baseline WHERE author_key=?", (author_key,))}


def author_view(conn, author_key: str) -> dict | None:
    a = search.get_author(conn, author_key)
    if not a:
        return None
    m = get_monitor(conn, author_key)
    works = classify(conn, _works_of(conn, author_key), m, _baseline(conn, author_key), profile(conn))
    library.attach_owned(conn, works)
    a["works"] = works
    a["monitor"] = m
    a["counts"] = _count(works)
    return a


def _count(works: list[dict]) -> dict:
    c = {"counted": 0, "owned": 0, "wanted": 0, "in_ingest": 0, "ignored": 0, "filtered": 0, "unmonitored": 0}
    for w in works:
        c[w["status"]] += 1
        if w["status"] not in ("filtered",):
            c["counted"] += 1
    return c


def monitored(conn) -> list[dict]:
    """Every monitored author with counts."""
    out = []
    prof = profile(conn)
    for m in conn.execute("SELECT m.*, a.name, a.work_count FROM monitored_authors m "
                          "JOIN authors a ON a.ol_key = m.author_key ORDER BY a.name").fetchall():
        m = dict(m)
        works = classify(conn, _works_of(conn, m["author_key"]), m, _baseline(conn, m["author_key"]), prof)
        m["counts"] = _count(works)
        out.append(m)
    return out


def wanted(conn, q: str = "", include_waiting: bool = True) -> list[dict]:
    """Every wanted book: from monitored authors, plus books you marked wanted yourself."""
    prof = profile(conn)
    seen: dict[str, dict] = {}
    for m in conn.execute("SELECT * FROM monitored_authors WHERE monitor != 'none'").fetchall():
        m = dict(m)
        for w in classify(conn, _works_of(conn, m["author_key"]), m, _baseline(conn, m["author_key"]), prof):
            if w["status"] in ("wanted", "in_ingest"):
                seen.setdefault(w["ol_key"], w)
    singles = [r[0] for r in conn.execute("SELECT work_key FROM work_status WHERE state='wanted'")]
    singles = [k for k in singles if k not in seen]
    if singles:
        marks = ",".join("?" * len(singles))
        rows = [dict(r) for r in conn.execute(f"SELECT {WORK_COLS}, w.languages FROM works w WHERE w.ol_key IN ({marks})",
                                              singles)]
        for w in classify(conn, rows, {"monitor": "selected"}, set(), prof):
            if w["status"] in ("wanted", "in_ingest"):
                seen.setdefault(w["ol_key"], w)
    works = list(seen.values())
    if not include_waiting:
        works = [w for w in works if w["status"] == "wanted"]
    search._attach_authors(conn, works)
    if q:
        words = q.lower().split()
        works = [w for w in works if all(x in f"{w['title']} {' '.join(a['name'] for a in w['authors'])}".lower()
                                         for x in words)]
    works.sort(key=lambda w: ((w["authors"][0]["name"] if w["authors"] else "").lower(),
                              w.get("first_publish_year") or 9999, w["title"].lower()))
    return works


def new_releases(conn, years: int = 2) -> list[dict]:
    """Works from monitored authors first published in the last `years` years (as fresh as the latest dump)."""
    since = datetime.now(timezone.utc).year - years
    prof = profile(conn)
    out: dict[str, dict] = {}
    for m in conn.execute("SELECT * FROM monitored_authors").fetchall():
        m = dict(m)
        recent = [w for w in _works_of(conn, m["author_key"]) if (w.get("first_publish_year") or 0) >= since]
        for w in classify(conn, recent, m, _baseline(conn, m["author_key"]), prof):
            if w["status"] != "filtered":
                out.setdefault(w["ol_key"], w)
    works = search._attach_authors(conn, list(out.values()))
    works.sort(key=lambda w: (-(w.get("first_publish_year") or 0), w["title"].lower()))
    return works


def work_status(conn, work_key: str) -> dict | None:
    """One work's status, following whichever of its authors you monitor (the most inclusive one wins)."""
    row = conn.execute(f"SELECT {WORK_COLS}, w.languages FROM works w WHERE w.ol_key=?", (work_key,)).fetchone()
    if not row:
        return None
    prof = profile(conn)
    rank = {"owned": 0, "in_ingest": 1, "wanted": 2, "ignored": 3, "filtered": 4, "unmonitored": 5}
    best = None
    mons = [dict(m) for m in conn.execute(
        "SELECT m.* FROM monitored_authors m JOIN work_authors wa ON wa.author_key = m.author_key "
        "WHERE wa.work_key=?", (work_key,))] or [None]
    for m in mons:
        w = classify(conn, [dict(row)], m, _baseline(conn, m["author_key"]) if m else set(), prof)[0]
        if best is None or rank[w["status"]] < rank[best["status"]]:
            best = w
    return {"status": best["status"], "why": best["status_why"], "override": best["override"]}
