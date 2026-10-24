"""Match a book (title, authors, ISBN, Open Library id) to a catalog work.

Order of evidence: Open Library id > ISBN > title plus author. Confidence is 0-100. Phase 4's ingest matcher
reuses this for files, so keep it independent of where the book came from.
"""
import functools
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
    issues: list[str] = field(default_factory=list)        # problems found in the source record, e.g. isbn_mismatch


def fold(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


_VOLUME = re.compile(r"\b(vol|volume|book|bk|part|no)\.?\s*-?\s*\d+\b")


@functools.lru_cache(maxsize=200_000)
def norm_title(s: str | None) -> str:
    s = _VOLUME.sub(" ", _NOISE.sub(" ", _PARENS.sub(" ", fold(s))))
    s = re.sub(r"[^\w\s]", " ", s.replace("&", " and "))
    s = re.sub(r"\s+", " ", s).strip()
    return _ARTICLE.sub("", s)


def main_title(s: str | None) -> str:
    """Title without its subtitle: 'The Hobbit: or There and Back Again' -> 'The Hobbit'."""
    return re.split(r"\s*(?::|;|\s-\s|\s—\s)\s*", s or "", maxsplit=1)[0]


_ROLE = re.compile(r"\s*[\(\[]\s*(ed|eds|editor|editors|trans|translator|translated by|illustrator|"
                   r"illus|introduction|intro|foreword|narrator|author|adapter|compiler)\.?\s*[\)\]]",
                   re.IGNORECASE)
_HONORIFIC = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "dr", "mr", "mrs", "ms", "prof", "sir", "dame", "rev",
              "ed", "eds", "editor", "trans", "translator"}
_JUNK_NAMES = {"unknown", "anonymous", "anon", "various", "various authors", "n a", "none", "author"}


@functools.lru_cache(maxsize=200_000)
def clean_author(name: str | None) -> str:
    """Make one author string readable: Calibre's '|' (an escaped comma), roles, quotes, stray punctuation.

    'Tolkien| J.R.R.' -> 'J.R.R. Tolkien', 'Neil Gaiman (Editor)' -> 'Neil Gaiman', '"Stephen  King"' -> 'Stephen King'.
    """
    n = unicodedata.normalize("NFKC", name or "").replace("|", ",")
    n = _ROLE.sub(" ", n)
    n = re.sub(r"[\"“”«»_*~^<>{}\[\]\\]", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" ,;:/-'")
    if n.count(",") == 1:                                    # "Last, First" -> "First Last"
        last, first = (x.strip() for x in n.split(","))
        if first and last and not re.fullmatch(r"(jr|sr|ii|iii|iv|phd|md)\.?", first, re.IGNORECASE):
            n = f"{first} {last}"
    return "" if fold(n) in _JUNK_NAMES else n


def split_authors(raw: str | None) -> list[str]:
    """Split a multi-author string ('A & B', 'A; B', 'A / B') into clean names, dropping placeholders."""
    parts = re.split(r"\s*(?:&|;|/|\s+and\s+(?=[A-Z][^,]*\s[A-Z]))\s*", raw or "")
    return list(dict.fromkeys(c for c in (clean_author(p) for p in parts) if c))


@functools.lru_cache(maxsize=200_000)
def name_tokens(name: str | None) -> tuple[str, ...]:
    n = fold(clean_author(name))
    toks = re.findall(r"[a-z0-9]+", n.replace(".", " "))
    return tuple(t for t in toks if t not in _HONORIFIC) or tuple(toks)


def author_similarity(a: str, b: str) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb or "".join(ta) == "".join(tb):
        return 100.0
    if sorted(ta) == sorted(tb):                   # leftover "Last First" order
        return 98.0
    if ta[-1] == tb[-1]:                           # same surname
        return 92.0 if ta[0][0] == tb[0][0] else 80.0
    return float(fuzz.token_sort_ratio(" ".join(ta), " ".join(tb)))


_SEGMENTS = re.compile(r"\s*(?::|;|\s[-–—]+\s|-\s|\s-)\s*")


def title_variants(book_title: str, authors: list[str] | tuple = ()) -> list[str]:
    return list(_title_variants(book_title, tuple(authors)))


@functools.lru_cache(maxsize=20_000)
def _title_variants(book_title: str, authors: tuple) -> tuple:
    """Normalized ways to read a messy file/Calibre title.

    'The History of Middle-earth Vol-11- The War of the Jewels J.R.R Tolkien' yields the whole thing, its
    segments ('history of middle earth', 'war of jewels j r r tolkien', ...) and each of those with the author's
    name stripped off the end or start ('war of jewels').
    """
    raw = [book_title, main_title(book_title), *_SEGMENTS.split(book_title or "")]
    names = [set(name_tokens(a)) for a in authors if name_tokens(a)]
    out: list[str] = []
    for r in raw:
        n = norm_title(r)
        if not n:
            continue
        out.append(n)
        words = n.split()
        for toks in names:
            w = list(words)
            while len(w) > 1 and w[-1] in toks:
                w.pop()
            while len(w) > 1 and w[0] in toks:
                w.pop(0)
            if len(w) < len(words):
                out.append(_ARTICLE.sub("", " ".join(w)))
    return tuple(dict.fromkeys(out))


def title_similarity(book_title: str, work: dict, authors: list[str] = ()) -> float:
    variants = title_variants(book_title, authors)
    full_b = norm_title(book_title)
    wt = norm_title(work["title"])
    wfull = norm_title(f"{work['title']} {work.get('subtitle') or ''}")
    best = max((max(fuzz.ratio(v, wt), fuzz.ratio(v, wfull)) for v in variants), default=0)
    # "Hamlet, Prince of Denmark" vs work "Hamlet": the book title is the work title plus a subtitle set off by
    # punctuation. Without the punctuation ("Dune Messiah" vs "Dune") it is more likely a different book.
    if wt and full_b.startswith(wt + " "):
        raw = re.sub(r"^(the|a|an)\s+", "", fold(book_title).strip())
        words = len(wt.split())
        m = re.match(r"^((?:[\w']+\W+){%d})" % (words - 1) + r"[\w']+\s*([,:;(\[\-–—])", raw)
        if m:
            best = max(best, 80 + 20 * len(wt) / len(full_b))
    return float(best)


AUTHOR_MISMATCH = 60      # best author similarity below this means "different author"
AUTHOR_MISMATCH_CAP = 55  # ...and caps the score below MIN_CONFIDENCE, however good the title is


def score(title: str, authors: list[str], work: dict) -> float:
    t = title_similarity(title, work, authors)
    wa = [a["name"] for a in work.get("authors", [])]
    if authors and wa:
        a = max(author_similarity(x, y) for x in authors for y in wa)
    else:
        a = 60.0                                   # no evidence either way
    pop = min(3.0, math.log10(1 + (work.get("popularity") or 0)))   # tie-breaker between duplicate works
    s = min(97.0, 0.65 * t + 0.35 * a + pop)
    # Same title, different author is a different book ("Quick Start Guide" by Calibre's author is not the
    # Blazor one). Keep it as a candidate but never as a match.
    return min(s, AUTHOR_MISMATCH_CAP) if authors and wa and a < AUTHOR_MISMATCH else s


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


def _edition_title_match(conn, title: str, authors: list[str]) -> dict | None:
    """Last resort: compare the title with every edition title of the named author's works.

    Catches translations and regional titles that the work title doesn't carry, e.g. the Swedish "Lida" is an
    edition of Stephen King's "Misery". Only runs for books the normal match couldn't place confidently, and is
    bounded by one author's bibliography.
    """
    if not authors:
        return None
    variants = title_variants(title, authors)
    best = None
    for name in authors[:2]:
        akeys = [a["ol_key"] for a in search.search_authors(conn, " ".join(name_tokens(name)), limit=10)
                 if author_similarity(name, a["name"]) >= 90][:2]
        for akey in akeys:
            rows = conn.execute(
                "SELECT e.work_key, e.title, e.subtitle FROM work_authors wa "
                "JOIN editions e ON e.work_key = wa.work_key WHERE wa.author_key = ? AND e.title IS NOT NULL "
                "LIMIT 20000", (akey,))
            for wk, et, est in rows:
                ne = norm_title(et)
                t = max(max(fuzz.ratio(v, ne), fuzz.ratio(v, norm_title(f"{et} {est or ''}"))) for v in variants)
                if t >= 90 and (best is None or t > best[0]):
                    best = (t, wk)
    if not best:
        return None
    w = conn.execute(f"SELECT {WORK_COLS} FROM works w WHERE w.ol_key=?", (best[1],)).fetchone()
    if not w:
        return None
    w = search._attach_authors(conn, [dict(w)])[0]
    # Title evidence comes from an edition, not the work, so stay below the auto-accept line.
    w["score"] = round(min(89.0, 0.65 * best[0] + 0.35 * 100), 1)
    return w


def _text_match(conn, title: str, authors: list[str], deep: bool = True) -> list[dict]:
    """Candidates by title and author, best first, each with a 'score'."""
    surname = name_tokens(authors[0])[-1] if authors and name_tokens(authors[0]) else ""
    words = norm_title(main_title(title)).split()
    # Every FTS word is required, so also try shorter prefixes of the title: an edition title often carries
    # extra words ("Hamlet, Prince of Denmark") that the work title doesn't. Messy titles also get each segment
    # with the author's name removed.
    queries = [f"{_key_words(main_title(title))} {surname}", _key_words(main_title(title)), _key_words(title),
               f"{' '.join(words[:3])} {surname}", f"{' '.join(words[:1])} {surname}"]
    variants = sorted(title_variants(title, authors), key=lambda v: -len(v.split()))
    queries += [f"{v} {surname}" for v in variants if 2 <= len(v.split()) <= 8][:4]
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
    cands = sorted(seen.values(), key=lambda w: w["score"], reverse=True)
    if deep and (not cands or cands[0]["score"] < 90):
        ed = _edition_title_match(conn, title, authors)
        if ed and (not cands or ed["score"] > cands[0]["score"]):
            ed["via_edition"] = True
            cands = [ed] + [c for c in cands if c["ol_key"] != ed["ol_key"]]
    return cands


def match_book(conn, title: str, authors: list[str], isbn: str | None = None, ol_id: str | None = None,
               deep: bool = True) -> Match:
    """deep=False skips the slow edition-title fallback (for callers that try several readings of one book)."""
    authors = [c for c in (clean_author(a) for a in authors) if c]
    if ol_id:
        key = _by_ol_id(conn, ol_id)
        if key:
            return Match(key, "openlibrary", 100)

    isbn_cands: list[dict] = []
    if isbn:
        hit = search.lookup_isbn(conn, isbn)
        if hit and hit["works"]:
            works = hit["works"]  # most popular first
            best = works[0]
            # Compare against the work title and the titles of the editions carrying this ISBN: "The Golden
            # Compass" is an edition of the work Open Library calls "Northern Lights".
            ed_titles = [{"title": e["title"], "subtitle": e["subtitle"]} for e in hit["editions"]
                         if e["work_key"] == best["ol_key"] and e["title"]]
            tsim = max([title_similarity(title, best, authors)] +
                       [title_similarity(title, e, authors) for e in ed_titles]) if title else 100
            if tsim >= 50:
                return Match(best["ol_key"], "isbn", 98 if len(works) == 1 else 94, works[:5])
            # The ISBN points at a book with a different title: one of the two records has a wrong ISBN. Trust
            # the title and author instead, and keep the ISBN's work only as a candidate.
            isbn_cands = works[:2]

    issues = ["isbn_mismatch"] if isbn_cands else []
    if not title or not search.has_index(conn):
        return Match(None, None, 0, isbn_cands, issues)
    cands = _text_match(conn, title, authors, deep)
    if not cands:
        return Match(None, None, 0, isbn_cands, issues)
    best = cands[0]
    conf = int(round(best["score"]))
    method = "edition_title" if best.get("via_edition") else "title_author"
    if conf < MIN_CONFIDENCE:
        return Match(None, None, conf, (cands + isbn_cands)[:5], issues)
    return Match(best["ol_key"], method, conf, cands[:5], issues)
