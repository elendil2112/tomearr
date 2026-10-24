"""Guess title and author from a file's name and folders.

Download names are inconsistent ("Author - Title", "Title - Author", "Title (Author)", "Author/Title/file.epub",
dots for spaces, release tags), so this returns several hypotheses and lets the matcher decide which one fits
the catalog best.
"""
import re
from pathlib import PurePosixPath

_TAGS = re.compile(
    r"[\(\[\{][^\)\]\}]*\b(retail|epub|mobi|azw3?|pdf|ebook|e-book|ocr|scan|scanned|v\d+(\.\d+)?|z-lib(\.org)?|"
    r"libgen|anna'?s archive|true pdf|converted|calibre|unabridged|kindle|nook|kobo|hq|lq|repack)\b[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE)
_SITE = re.compile(r"\b(z-?lib(rary)?(\.org)?|libgen(\.\w+)?|b-ok(\.\w+)?|annas?-?archive|ebook3000|"
                   r"www\.\S+|\S+\.(com|net|org|ru|io))\b", re.IGNORECASE)
_COPY = re.compile(r"\s*\((\d{1,3}|copy)\)\s*$", re.IGNORECASE)        # "Book (1)", Calibre's "Title (123)"
_YEAR = re.compile(r"[\(\[]\s*(1[5-9]\d\d|20\d\d)\s*[\)\]]")
_EXT2 = re.compile(r"\.(fb2|kepub)$", re.IGNORECASE)
_SEP = re.compile(r"\s+[-–—]+\s+|\s*--\s*")
_SERIES_NUM = re.compile(r"^(?:.*?\b)?(?:book|vol(?:ume)?|#|no\.?)?\s*\d{1,3}(?:\.\d)?\s*$", re.IGNORECASE)
_SKIP_DIRS = {"ebooks", "ebook", "books", "book", "completed", "complete", "downloads", "download", "epub", "mobi",
              "pdf", "azw3", "new", "incoming", "ingest", "unsorted", "misc", "other", "calibre library", "library"}


def clean(s: str) -> str:
    s = _TAGS.sub(" ", s)
    s = _SITE.sub(" ", s)
    if " " not in s.strip() and ("." in s or "_" in s):   # "The.Hobbit.J.R.R.Tolkien" / "the_hobbit"
        s = re.sub(r"(?<=[^\s.])[._](?=[^\s.])", " ", s)
    s = s.replace("_", " ")
    s = _YEAR.sub(" ", s)
    s = _COPY.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—.,;:_")
    return s


def _looks_like_name(s: str) -> bool:
    """Two to four capitalized words or initials, e.g. 'J.R.R. Tolkien', 'Ursula K. Le Guin', 'Tolkien, J.R.R.'."""
    s = s.strip()
    if "," in s and len(s.split(",")) == 2:
        return True
    words = s.replace(".", ". ").split()
    if not 1 < len(words) <= 5:
        return False
    return all(w[:1].isupper() or w.lower() in ("de", "le", "la", "van", "von", "der", "da", "di", "du", "del")
               for w in words)


def hypotheses(relpath: str) -> list[dict]:
    """[{'title': ..., 'authors': [...], 'how': ...}], most likely first. Never empty for a non-empty name."""
    p = PurePosixPath(relpath)
    stem = _EXT2.sub("", p.stem) if p.suffix.lower() == ".zip" else p.stem
    stem = _EXT2.sub("", stem)
    name = clean(stem)
    parents = [clean(x) for x in p.parts[:-1] if clean(x) and clean(x).lower() not in _SKIP_DIRS]
    out: list[dict] = []

    def add(title, authors, how):
        title = (title or "").strip(" -–—.,;:")
        authors = [a.strip(" -–—.,;:") for a in authors if a and a.strip(" -–—.,;:")]
        if title and len(title) > 1 and not any(h["title"] == title and h["authors"] == authors for h in out):
            out.append({"title": title, "authors": authors, "how": how})

    m = re.match(r"^(.+?)\s+by\s+(.+)$", name, re.IGNORECASE)
    if m:
        add(m.group(1), [m.group(2)], "title by author")
    m = re.match(r"^(.+?)\s*[\(\[]([^\)\]]+)[\)\]]$", name)
    if m and _looks_like_name(m.group(2)):
        add(m.group(1), [m.group(2)], "title (author)")

    parts = [x for x in _SEP.split(name) if x.strip()]
    if len(parts) == 1 and name.count("-") == 1 and not re.search(r"\w-\w+\s*$", name.split()[-1] if name.split() else ""):
        parts = [x.strip() for x in name.split("-")]    # "Tolkien-The Silmarillion"
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        # "Author - Series 03 - Title": drop series-number middles
        middle = [x for x in parts[1:-1] if not _SERIES_NUM.match(x)]
        if _looks_like_name(first) or not _looks_like_name(last):
            add(last if len(parts) > 2 else parts[1], [first], "author - title")
            add(" - ".join(parts[1:]), [first], "author - title")
            add(first, [last], "title - author")
        else:
            add(first, [last], "title - author")
            add(last, [first], "author - title")
        if middle:
            add(middle[-1], [first], "author - series - title")
        add(name, [], "whole name")
    else:
        # Folder hints: "Author/Title/file.epub" (Calibre layout) or "Author/file.epub".
        if len(parents) >= 2 and _looks_like_name(parents[-2]):
            add(name, [parents[-2]], "author/title folders")
            add(parents[-1], [parents[-2]], "author/title folders")
        if parents and _looks_like_name(parents[-1]):
            add(name, [parents[-1]], "author folder")
        add(name, [], "whole name")
    if parents and not _looks_like_name(parents[-1]) and len(parents[-1]) > 3:
        add(parents[-1], [], "folder name")   # release folders often carry the real title
    return out[:6]
