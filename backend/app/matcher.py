"""Match a book (title, authors, ISBN, Open Library id) to a catalog work.

Order of evidence: Open Library id > ISBN > title plus author. Confidence is 0-100. Phase 4's ingest matcher
reuses this for files, so keep it independent of where the book came from.
"""
import math
import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import db, search
from .search import WORK_COLS

MIN_CONFIDENCE = 60          # below this a book is left unmatched
_PARENS = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")   # "(Discworld #1)", "[Illustrated]"
_NOISE = re.compile(r"\b(a novel|unabridged|illustrated edition|annotated edition|a memoir)\b")
_ARTICLE = re.compile(r"^(the|a|an)\s+")


@dataclass
class Match:
    work_key: str | None
    method: str | None
    confidence: int
    candidates: list[dict] = field(default_factory=list)   # top few, best first


def fold(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_title(s: str | None) -> str:
    s = _NOISE.sub(" ", _PARENS.sub(" ", fold(s)))
    s = re.sub(r"[^\w\s]", " ", s.replace("&", " and "))
    s = re.sub(r"\s+", " ", s).strip()
    return _ARTICLE.sub("", s)


def main_title(s: str | None) -> str:
    """Title without its subtitle: 'The Hobbit: or There and Back Again' -> 'The Hobbit'."""
    return re.split(r"\s*(?::|;|\s-\s|\s—\s)\s*", s or "", maxsplit=1)[0]


def name_tokens(name: str | None) -> list[str]:
    n = fold(name)
    if "," in n:                                   # "Tolkien, J. R. R." -> "J. R. R. Tolkien"
        last, _, first = n.partition(",")
        n = f"{first} {last}"
    return re.findall(r"[a-z0-9]+", n.replace(".", " "))


def author_similarity(a: str, b: str) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb or "".join(ta) == "".join(tb):
        return 100.0
    if ta[-1] == tb[-1]:                           # same surname
        return 92.0 if ta[0][0] == tb[0][0] else 80.0
    return float(fuzz.token_sort_ratio(" ".join(ta), " ".join(tb)))


def title_similarity(book_title: str, work: dict) -> float:
    full_b, main_b = norm_title(book_title), norm_title(main_title(book_title))
    wt = norm_title(work["title"])
    wfull = norm_title(f"{work['title']} {work.get('subtitle') or ''}")
    best = max(fuzz.ratio(full_b, wt), fuzz.ratio(main_b, wt), fuzz.ratio(full_b, wfull))
    # "Hamlet, Prince of Denmark" vs work "Hamlet": the book title is the work title plus a subtitle set off by
    # punctuation. Without the punctuation ("Dune Messiah" vs "Dune") it is more likely a different book.
    if wt and full_b.startswith(wt + " "):
        raw = re.sub(r"^(the|a|an)\s+", "", fold(book_title).strip())
        words = len(wt.split())
        m = re.match(r"^((?:[\w']+\W+){%d})" % (words - 1) + r"[\w']+\s*([,:;(\[\-–—])", raw)
        if m:
            best = max(best, 80 + 20 * len(wt) / len(full_b))
    return float(best)


def score(title: str, authors: list[str], work: dict) -> float:
    t = title_similarity(title, work)
    wa = [a["name"] for a in work.get("authors", [])]
    if authors and wa:
        a = max(author_similarity(x, y) for x in authors for y in wa)
    else:
        a = 60.0                                   # no evidence either way
    pop = min(3.0, math.log10(1 + (work.get("popularity") or 0)))   # tie-breaker between duplicate works
    return min(97.0, 0.65 * t + 0.35 * a + pop)


STOPWORDS = {"of", "and", "in", "to", "on", "for", "at", "by", "with", "from", "or", "de", "la", "le", "el",
             "der", "die", "das", "und", "et", "y", "du", "des", "a", "an", "the", "is", "its", "s"}


def _fts_exact(text: str) -> str | None:
    """Whole-word FTS query without stopwords. Faster than search.fts_query's prefix terms, and the matcher
    always has whole words to work with."""
    words = [w for w in search.TOKEN_RE.findall(fold(text)) if w not in STOPWORDS]
    return " ".join(f'"{w}"' for w in dict.fromkeys(words[:8])) or None


def _fts_candidates(conn, fq: str | None, limit: int = 40) -> list[dict]:
    """fq is a query built by _fts_exact."""
    if not fq:
        return []
    col = "pop_rank" if db.get_meta(conn, "works_fts_key") == "pop_rank" else "rowid"
    rows = conn.execute(
        f"SELECT {WORK_COLS} FROM works_fts f JOIN works w ON w.{col} = f.rowid WHERE works_fts MATCH ? LIMIT ?",
        (fq, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _key_words(s: str, n: int = 8) -> str:
    return " ".join(norm_title(s).split()[:n])


def _by_ol_id(conn, ol_id: str) -> str | None:
    ol_id = ol_id.strip().upper()
    if ol_id.endswith("W"):
        r = conn.execute("SELECT ol_key FROM works WHERE ol_key=?", (ol_id,)).fetchone()
        return r[0] if r else None
    if ol_id.endswith("M"):
        r = conn.execute("SELECT work_key FROM editions WHERE ol_key=? LIMIT 1", (ol_id,)).fetchone()
        return r[0] if r else None
    return None


def match_book(conn, title: str, authors: list[str], isbn: str | None = None, ol_id: str | None = None) -> Match:
    if ol_id:
        key = _by_ol_id(conn, ol_id)
        if key:
            return Match(key, "openlibrary", 100)

    if isbn:
        hit = search.lookup_isbn(conn, isbn)
        if hit and hit["works"]:
            works = hit["works"]  # most popular first
            best = works[0]
            conf = 98 if len(works) == 1 else 94
            # A wrong ISBN in either Calibre or Open Library shows up as a title that doesn't match at all.
            if title and title_similarity(title, best) < 50:
                conf = 70
            return Match(best["ol_key"], "isbn", conf, works[:5])

    if not title or not search.has_index(conn):
        return Match(None, None, 0)
    surname = name_tokens(authors[0])[-1] if authors and name_tokens(authors[0]) else ""
    words = norm_title(main_title(title)).split()
    # Every FTS word is required, so also try shorter prefixes of the title: an edition title often carries
    # extra words ("Hamlet, Prince of Denmark") that the work title doesn't.
    queries = [f"{_key_words(main_title(title))} {surname}", _key_words(main_title(title)), _key_words(title),
               f"{' '.join(words[:3])} {surname}", f"{' '.join(words[:1])} {surname}"]
    queries = [q.strip() for q in queries]
    seen: dict[str, dict] = {}
    for i, q in enumerate(dict.fromkeys(_fts_exact(q) for q in queries if q)):
        if not q:
            continue
        new = [w for w in _fts_candidates(conn, q, 100 if i == 0 else 40) if w["ol_key"] not in seen]
        for w in search._attach_authors(conn, new):
            w["score"] = round(score(title, authors, w), 1)
            seen[w["ol_key"]] = w
        # The queries run from most to least specific; stop once there's a near-certain hit.
        if seen and max(w["score"] for w in seen.values()) >= 95:
            break
    if not seen:
        return Match(None, None, 0)
    cands = sorted(seen.values(), key=lambda w: w["score"], reverse=True)
    best = cands[0]
    conf = int(round(best["score"]))
    if conf < MIN_CONFIDENCE:
        return Match(None, None, conf, cands[:5])
    return Match(best["ol_key"], "title_author", conf, cands[:5])
