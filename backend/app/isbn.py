"""ISBN normalization. Everything is stored and looked up as ISBN-13."""
import re

_STRIP = re.compile(r"[^0-9Xx]")


def _isbn10_ok(s: str) -> bool:
    total = 0
    for i, ch in enumerate(s):
        if ch in "Xx":
            if i != 9:
                return False
            v = 10
        else:
            v = ord(ch) - 48
        total += (10 - i) * v
    return total % 11 == 0


def _isbn13_check(first12: str) -> str:
    total = sum((1 if i % 2 == 0 else 3) * (ord(c) - 48) for i, c in enumerate(first12))
    return str((10 - total % 10) % 10)


def to_isbn13(raw: str | None) -> str | None:
    """Return a checksum-valid ISBN-13 for an ISBN-10 or ISBN-13 in any punctuation, or None.

    Values with a bad checksum are rejected rather than guessed at: converting a mistyped ISBN-10 would produce
    an ISBN-13 that belongs to some other book.
    """
    if not isinstance(raw, str):
        return None
    s = _STRIP.sub("", raw)
    if len(s) == 10:
        if not _isbn10_ok(s):
            return None
        core = "978" + s[:9]
        return core + _isbn13_check(core)
    if len(s) == 13 and s.isdigit() and s[:3] in ("978", "979"):
        return s if _isbn13_check(s[:12]) == s[12] else None
    return None


def looks_like_isbn(text: str) -> bool:
    """True when the whole input is plausibly an ISBN (used to switch the search box into ISBN lookup)."""
    text = _PREFIX.sub("", text or "").strip()
    if not text or re.search(r"[^0-9Xx\-\s]", text):
        return False
    return to_isbn13(text) is not None


_PREFIX = re.compile(r"^\s*isbn(?:-?1[03])?\s*:?\s*", re.IGNORECASE)


def from_query(text: str) -> str | None:
    """ISBN-13 if the search text is an ISBN (optionally prefixed with 'ISBN'), else None."""
    return to_isbn13(_PREFIX.sub("", text or "")) if looks_like_isbn(text) else None
