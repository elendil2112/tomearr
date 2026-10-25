"""Is this the same book? Independent of Open Library records.

Open Library often has several work records for one book (one per edition or ISBN that was never merged), and their
titles and author names differ in small ways: "The Adventures of Sherlock Holmes" / "Adventures of Sherlock Holmes
(Illustrated)", "Arthur Conan Doyle" / "Sir Arthur Conan Doyle". Matching by record alone lets every version into
Calibre as a separate book. Two books are the same here when an author surname is shared and the titles agree once
articles, punctuation, bracketed notes and (when only one side has one) the subtitle are set aside. Numbers are kept,
so the volumes of a series stay apart, and two different subtitles ("Star Wars: Thrawn" / "Star Wars: Thrawn
Alliances") keep two books apart.
"""
import re
from functools import lru_cache

from rapidfuzz import fuzz

from . import matcher

_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_SPLIT = re.compile(r"\s*(?::|;|\s[-–—]\s)\s*")
_ARTICLE = re.compile(r"^(the|a|an|le|la|les|el|der|die|das)\s+")
_TAIL = re.compile(r"\s+(a novel|a memoir|novel|unabridged|illustrated|annotated|complete and unabridged)$")
FUZZY = 94          # full-title similarity that still counts as the same (spelling, "colour"/"color")


def _clean(s: str) -> str:
    s = matcher.fold(s).replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = _ARTICLE.sub("", s)
    return _TAIL.sub("", s).strip()


@lru_cache(maxsize=100_000)
def title_parts(title: str | None, subtitle: str | None = None) -> tuple[str, str]:
    """(main title, subtitle), both normalized."""
    t = _BRACKETS.sub(" ", title or "")
    parts = _SPLIT.split(t, maxsplit=1)
    main = _clean(parts[0])
    sub = _clean(parts[1]) if len(parts) > 1 else ""
    if subtitle and not sub:
        sub = _clean(_BRACKETS.sub(" ", subtitle))
    return main, sub


def surnames(authors) -> frozenset[str]:
    if isinstance(authors, str):
        authors = [authors]
    authors = [n for a in authors or [] for n in matcher.split_authors(a)]
    out = set()
    for a in authors or []:
        toks = [t for t in matcher.name_tokens(a) if len(t) > 1]
        if toks:
            out.add(toks[-1])
    return frozenset(out)


def same_title(a: tuple[str, str], b: tuple[str, str]) -> bool:
    (am, asub), (bm, bsub) = a, b
    if not am or not bm:
        return False
    main_same = am == bm or (min(len(am), len(bm)) >= 10 and (
        fuzz.ratio(am, bm) >= FUZZY or fuzz.token_sort_ratio(am, bm) == 100))   # same words, another order
    if not main_same:
        return False
    if asub and bsub:
        return asub == bsub or fuzz.ratio(asub, bsub) >= FUZZY
    return True


def same_book(t1, a1, t2, a2) -> bool:
    s1, s2 = surnames(a1), surnames(a2)
    if not s1 or not s2 or not (s1 & s2):
        return False
    return same_title(title_parts(t1) if isinstance(t1, str) else t1, title_parts(t2) if isinstance(t2, str) else t2)


def key(title, authors) -> str | None:
    """A grouping key: main title + first surname (alphabetically). Same key = probably the same book; confirm
    with same_book() when subtitles matter."""
    main, _ = title_parts(title)
    s = sorted(surnames(authors))
    return f"{main}|{s[0]}" if main and s else None


class LibraryIndex:
    """Calibre's books by author surname, for 'do I already have this book under another record?'"""

    def __init__(self, conn):
        self.by_surname: dict[str, list[dict]] = {}
        for r in conn.execute("SELECT calibre_id, title, authors, formats, work_key, has_cover, match_confidence "
                              "FROM library_books"):
            b = dict(r)
            b["formats_list"] = [f for f in (b["formats"] or "").split(",") if f]
            b["_title"] = title_parts(b["title"])
            b["_surnames"] = surnames(b["authors"] or "")
            for s in b["_surnames"]:
                self.by_surname.setdefault(s, []).append(b)

    def find(self, title: str, authors, subtitle: str | None = None) -> list[dict]:
        tp = title_parts(title, subtitle)
        seen, out = set(), []
        for s in surnames(authors):
            for b in self.by_surname.get(s, []):
                if b["calibre_id"] not in seen and same_title(tp, b["_title"]):
                    seen.add(b["calibre_id"])
                    out.append(b)
        return out


def work_names(conn, work_key: str) -> tuple[str | None, str | None, list[str]]:
    w = conn.execute("SELECT title, subtitle FROM works WHERE ol_key=?", (work_key,)).fetchone()
    names = [r[0] for r in conn.execute("SELECT a.name FROM work_authors wa JOIN authors a ON a.ol_key = wa.author_key "
                                        "WHERE wa.work_key=?", (work_key,))]
    return (w["title"], w["subtitle"], names) if w else (None, None, names)
