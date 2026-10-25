"""Phase 8: decide which indexer releases are the book you want, and rank them.

Release names are free text ("J.R.R. Tolkien - The Children of Hurin (2007) [Retail EPUB]"). Each release is checked
against the work: does it carry the title's words and the author's surname, what format is it, is it an audiobook,
a comic, another language or a collection, and is anyone seeding it. Releases that can't be the book are kept, with
the reason, so you can see why something was passed over.
"""
import hashlib
import math
import re
import unicodedata

from . import matcher

FORMATS = {"epub": "EPUB", "kepub": "EPUB", "azw3": "AZW3", "azw": "AZW3", "mobi": "MOBI", "pdf": "PDF",
           "lit": "LIT", "fb2": "FB2", "djvu": "DJVU", "rtf": "RTF", "docx": "DOCX", "txt": "TXT"}
FORMAT_POINTS = {"EPUB": 40, "AZW3": 28, "MOBI": 26, "FB2": 18, "LIT": 12, "DOCX": 10, "RTF": 8, "PDF": 4,
                 "DJVU": 0, "TXT": 2, None: 15}
AUDIO = re.compile(r"\b(audio ?book|audiobooks?|unabridged|abridged|narrated|read by|m4b|mp3|aac|flac|ogg|"
                   r"\d+\s?kbps|audible)\b")
COMIC = re.compile(r"\b(cbz|cbr|cb7|comics?|graphic novel|manga|tpb)\b")
COLLECTION = re.compile(r"\b(collection|box(ed)? ?set|boxset|omnibus|complete works|anthology|"
                        r"books? \d+ ?(-|to) ?\d+|\d+ books|series pack|bundle|ebook pack|megapack)\b")
# language words anywhere, short codes only in brackets: "[GER]", "(de)" (a bare "de" is usually a name, "Antoine de")
LANGUAGES = {
    "ger": ("german", "deutsch", "ger", "de", "deu"), "fre": ("french", "francais", "fre", "fra", "fr"),
    "spa": ("spanish", "espanol", "spa", "es", "esp"), "ita": ("italian", "italiano", "ita", "it"),
    "dut": ("dutch", "nederlands", "dut", "nl", "nld"), "por": ("portuguese", "portugues", "por", "pt", "ptbr"),
    "rus": ("russian", "rus", "ru"), "pol": ("polish", "polski", "pol", "pl"), "swe": ("swedish", "svenska", "swe"),
    "jpn": ("japanese", "jpn", "ja"), "chi": ("chinese", "chi", "zh"), "hun": ("hungarian", "magyar", "hun"),
}


def _language(low: str) -> str | None:
    words = set(re.findall(r"[a-z]+", low))
    bracketed = set(re.findall(r"[\[\(\{]\s*([a-z]{2,4})\s*[\]\)\}]", low))
    for code, names in LANGUAGES.items():
        if words & {n for n in names if len(n) > 3} or bracketed & set(names):
            return code
    return None
NOISE = {"epub", "kepub", "azw3", "azw", "mobi", "pdf", "fb2", "lit", "djvu", "rtf", "docx", "txt", "ebook", "ebooks",
         "retail", "true", "converted", "illustrated", "edition", "ed", "unabridged", "repack", "proper", "by", "and",
         "the", "a", "an", "of", "x", "eng", "english", "en", "kindle", "nook", "scan", "ocr", "html", "zip", "rar",
         "v5", "first", "1st", "anniversary", "deluxe", "revised", "new", "uk", "us", "audiobook", "book", "books",
         "novel", "fiction", "fantasy", "scifi", "nonfiction", "hc", "hardcover", "paperback", "special", "vintage",
         "classics", "publishing", "press", "publisher", "with", "complete", "annotated", "re", "pack", "co", "ltd"}
LANG_NAMES = {"ger": "German", "fre": "French", "spa": "Spanish", "ita": "Italian", "dut": "Dutch",
              "por": "Portuguese", "rus": "Russian", "pol": "Polish", "swe": "Swedish", "jpn": "Japanese",
              "chi": "Chinese", "hun": "Hungarian"}
MIN_TITLE = 75      # how much of the title must be in the release name


def _words(s: str) -> list[str]:
    s = matcher.fold(s).replace("&", " and ").replace("_", " ").replace(".", " ")
    return re.findall(r"[a-z0-9']+", s.replace("'", ""))


def release_id(r: dict) -> str:
    return hashlib.sha1(f"{r.get('indexerId')}|{r.get('guid')}".encode()).hexdigest()[:16]


def detect_format(name: str) -> str | None:
    found = [FORMATS[w] for w in _words(name) if w in FORMATS]
    if not found:
        return None
    return min(found, key=lambda f: -FORMAT_POINTS[f])      # best format named in the release


def surnames(authors: list[str]) -> list[str]:
    out = []
    for a in authors:
        toks = [t for t in matcher.name_tokens(a) if len(t) > 1]
        if toks:
            out.append(toks[-1])
    return out


def query_for(work: dict) -> list[str]:
    """Searches to run: author surname + title (most precise), then the bare title."""
    plain = lambda t: "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))  # noqa
    title = re.sub(r"[^\w\s'&-]", " ", plain(matcher.main_title(work["title"]))).strip()
    title = re.sub(r"\s+", " ", title)
    sn = [s.capitalize() for s in surnames([a["name"] for a in work.get("authors") or []])]
    qs = [f"{sn[0]} {title}" if sn else title, title]
    return list(dict.fromkeys(q for q in qs if q))


def evaluate(r: dict, work: dict, prefs: dict) -> dict:
    """Score one Prowlarr result against the work. Returns a release dict for the UI."""
    name = r.get("title") or ""
    low = matcher.fold(name)
    words = _words(name)
    wset = set(words)
    title_words = [w for w in _words(matcher.main_title(work["title"])) if w not in NOISE] or _words(work["title"])
    sub_words = [w for w in _words(work.get("subtitle") or "") if w not in NOISE]
    author_names = [a["name"] for a in work.get("authors") or []]
    sns = surnames(author_names)
    author_words = {t for a in author_names for t in matcher.name_tokens(a)}

    have = sum(1 for w in title_words if w in wset)
    title_pct = round(100 * have / len(title_words)) if title_words else 0
    # an exact phrase counts even if word splitting differs ("Hurin" vs "Húrin" is already folded)
    if matcher.norm_title(work["title"]) and matcher.norm_title(work["title"]) in " ".join(words):
        title_pct = 100
    author_ok = any(s in wset for s in sns) if sns else True
    extra = [w for w in words if w not in NOISE and w not in title_words and w not in sub_words
             and w not in author_words and not re.fullmatch(r"\d{1,4}", w) and len(w) > 2]

    fmt = detect_format(name)
    cats = {c.get("id") for c in r.get("categories") or [] if isinstance(c, dict)}
    size = r.get("size") or 0
    protocol = r.get("protocol") or "unknown"
    seeders = r.get("seeders")
    reasons, rejected = [], []

    if AUDIO.search(low) or cats & {3030}:
        rejected.append("audiobook")
    if COMIC.search(low) or cats & {7030}:
        rejected.append("comic")
    if cats & {7010}:
        rejected.append("magazine")
    if title_pct < MIN_TITLE:
        rejected.append(f"title doesn't match ({title_pct}% of its words)")
    if sns and not author_ok:
        reasons.append(f"author's name ({sns[0]}) isn't in the release name")
    want_langs = prefs.get("languages") or []
    lang = _language(low)
    if lang and want_langs and lang not in want_langs:
        rejected.append(f"looks {LANG_NAMES.get(lang, lang)}")
    max_mb = prefs.get("max_size_mb") or 0
    if max_mb and size > max_mb * 1024 * 1024:
        rejected.append(f"too big ({size / 1048576:.0f} MB)")
    if protocol == "torrent" and seeders is not None and seeders < (prefs.get("min_seeders") or 0):
        rejected.append("no seeders" if seeders == 0 else f"only {seeders} seeders")
    if fmt in ("DJVU",):
        rejected.append(f"{fmt} can't be converted to EPUB")
    collection = bool(COLLECTION.search(low)) and not COLLECTION.search(matcher.fold(work["title"]))
    if collection:
        reasons.append("looks like a collection; Tomearr will import the books it can match")

    score = FORMAT_POINTS[fmt] + title_pct * 0.3 + (15 if author_ok else -10)
    score -= min(30, 8 * len(extra))
    if extra:
        reasons.append("extra words: " + " ".join(extra[:5]))
    if "retail" in wset:
        score += 5
    if collection:
        score -= 20
    if protocol == "torrent" and seeders:
        score += min(12, 4 * math.log10(1 + seeders) * 2)
    if protocol == "usenet":
        score += 6           # complete and fast when it's there
    if fmt is None:
        reasons.append("format not named")
    elif fmt != "EPUB":
        reasons.append(f"{fmt}, will be converted to EPUB" if fmt not in ("PDF",) else "PDF (conversion is rough)")
    if size and size < 20 * 1024:
        rejected.append("too small to be a book")

    return {
        "id": release_id(r), "title": name, "indexer": r.get("indexer"), "indexer_id": r.get("indexerId"),
        "protocol": protocol, "size": size, "seeders": seeders, "leechers": r.get("leechers"),
        "grabs": r.get("grabs"), "age_days": r.get("age"), "publish_date": r.get("publishDate"),
        "info_url": r.get("infoUrl"), "format": fmt, "title_match": title_pct, "author_match": author_ok,
        "score": round(score, 1) if not rejected else 0, "reasons": reasons, "rejected": rejected,
        "_download_url": r.get("downloadUrl"), "_magnet_url": r.get("magnetUrl"), "_guid": r.get("guid"),
    }


def rank(results: list[dict], work: dict, prefs: dict) -> list[dict]:
    seen, out = set(), []
    for r in results:
        rel = evaluate(r, work, prefs)
        if rel["id"] in seen:
            continue
        seen.add(rel["id"])
        out.append(rel)
    out.sort(key=lambda x: (bool(x["rejected"]), -x["score"], -(x["seeders"] or 0)))
    return out


def public(rel: dict) -> dict:
    return {k: v for k, v in rel.items() if not k.startswith("_")}
