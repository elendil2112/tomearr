"""Identify an ebook file and read what it says about itself.

Everything here is pure and runs in worker processes (see ingest.py), so a hostile or broken file can only
cost one worker a timeout. Files are opened read-only and never modified.

analyze(path) returns a dict:
    kind        ebook | archive | comic | ignored
    format      EPUB, MOBI, AZW3, PDF, FB2, ... (from the file's contents, not just its extension)
    sha256, size
    meta        {title, authors[], isbns[], asin, language, publisher, year, series, series_index}
    meta_source where meta came from: epub-opf, mobi-exth, pdf-info, pdf-text, fb2, docx, opf-sidecar, txt-header,
                ebook-meta, or None
    drm         True / False / None (unknown)
    drm_detail  short reason
    entries     for archives: ebook files inside
    error       set if the file couldn't be read
"""
import hashlib
import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .isbn import to_isbn13

MAX_XML = 4 * 1024 * 1024          # never parse an XML member bigger than this (zip bombs, XML bombs)
PDF_TEXT_PAGES = 8                 # copyright pages are near the front
ISBN_TEXT_RE = re.compile(r"(?:ISBN|isbn)(?:-?1[03])?[\s:.]*((?:97[89][\s\-]?)?[0-9][0-9\s\-]{7,15}[0-9Xx])")
BARE_ISBN13_RE = re.compile(r"\b(97[89][\-\s]?(?:[0-9][\-\s]?){9}[0-9])\b")

EBOOK_EXTS = {"epub", "kepub", "mobi", "azw", "azw3", "azw4", "prc", "pdb", "kfx", "pdf", "fb2", "fbz", "lit",
              "lrf", "djvu", "djv", "chm", "rtf", "txt", "doc", "docx", "odt", "htmlz", "txtz", "snb", "tcr", "rb"}
COMIC_EXTS = {"cbz", "cbr", "cb7", "cbt"}
ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "tgz"}
PARTIAL_EXTS = {"part", "!qb", "crdownload", "tmp", "download", "partial", "aria2"}
SIDE_EXTS = {"opf"}
MIN_TXT_SIZE = 20_000
EBOOK_META_FORMATS = {"LIT", "LRF", "DJVU", "CHM", "RTF", "DOC", "PDB", "SNB", "TCR", "RB", "AZW4", "KFX"}              # smaller .txt files are almost always readmes and release notes

JUNK_TITLE = re.compile(
    r"^(untitled|unknown|document\d*|microsoft word.*|title|book|ebook|cover|\d+|.*\.(docx?|pdf|indd|tex|qxd|"
    r"rtf|html?|txt)|calibre.*|adobe .*)$", re.IGNORECASE)
JUNK_AUTHOR = re.compile(r"^(unknown|admin(istrator)?|user|owner|author|calibre.*|adobe .*|\d+)$", re.IGNORECASE)


# --- helpers ----------------------------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean(s) -> str | None:
    if not isinstance(s, str):
        return None
    s = re.sub(r"\s+", " ", s.replace("\x00", "")).strip()
    return s or None


def _good_title(t) -> str | None:
    t = _clean(t)
    return t if t and not JUNK_TITLE.match(t) else None


def _good_authors(names) -> list[str]:
    out = []
    for n in names or []:
        n = _clean(n)
        if n and not JUNK_AUTHOR.match(n) and n not in out:
            out.append(n)
    return out


def _year(s) -> int | None:
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", s or "")
    return int(m.group(1)) if m else None


def isbns_in_text(text: str, limit: int = 5) -> list[str]:
    found: list[str] = []
    for rx in (ISBN_TEXT_RE, BARE_ISBN13_RE):
        for m in rx.finditer(text or ""):
            n = to_isbn13(m.group(1))
            if n and n not in found:
                found.append(n)
            if len(found) >= limit:
                return found
    return found


def _meta(title=None, authors=None, isbns=None, **kw) -> dict:
    m = {"title": _good_title(title), "authors": _good_authors(authors),
         "isbns": [i for i in dict.fromkeys(to_isbn13(x) for x in isbns or []) if i]}
    for k in ("asin", "language", "publisher", "series"):
        m[k] = _clean(kw.get(k))
    m["year"] = kw.get("year") if isinstance(kw.get("year"), int) else _year(kw.get("year"))
    si = kw.get("series_index")
    try:
        m["series_index"] = float(si) if si not in (None, "") else None
    except (TypeError, ValueError):
        m["series_index"] = None
    return m


def _empty(m: dict | None) -> bool:
    return not m or not (m.get("title") or m.get("isbns"))


def _xml(data: bytes) -> ET.Element | None:
    if len(data) > MAX_XML:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        # Many real-world OPFs have stray entities or bad bytes; drop the DOCTYPE and undefined entities, retry once.
        cleaned = re.sub(rb"<!DOCTYPE[^>]*>", b"", data)
        cleaned = re.sub(rb"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", b"&amp;", cleaned)
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError:
            return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _zip_read(z: zipfile.ZipFile, name: str, limit: int = MAX_XML) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    with z.open(info) as fh:
        return fh.read(limit + 1)[:limit]


# --- OPF (EPUB, HTMLZ, sidecar metadata.opf) ----------------------------------------------------------------

def parse_opf(data: bytes) -> dict | None:
    root = _xml(data)
    if root is None:
        return None
    title = None
    authors, contributors, isbns = [], [], []
    lang = publisher = date = series = series_index = None
    for el in root.iter():
        tag = _local(el.tag)
        text = (el.text or "").strip()
        attrs = {_local(k): v for k, v in el.attrib.items()}
        if tag == "title" and text and title is None:
            title = text
        elif tag == "creator" and text:
            role = (attrs.get("role") or "").lower()
            (authors if role in ("", "aut") else contributors).append(text)
        elif tag == "identifier" and text:
            scheme = (attrs.get("scheme") or "").lower()
            val = re.sub(r"^urn:isbn:", "", text, flags=re.IGNORECASE)
            if scheme == "isbn" or text.lower().startswith("urn:isbn:") or to_isbn13(val):
                isbns.append(val)
        elif tag == "language" and text and not lang:
            lang = text
        elif tag == "publisher" and text and not publisher:
            publisher = text
        elif tag == "date" and text and not date:
            date = text
        elif tag == "meta":
            name = attrs.get("name", "")
            if name == "calibre:series":
                series = attrs.get("content")
            elif name == "calibre:series_index":
                series_index = attrs.get("content")
            elif attrs.get("property") == "belongs-to-collection" and text:
                series = series or text
    return _meta(title, authors or contributors, isbns, language=lang, publisher=publisher, year=date,
                 series=series, series_index=series_index)


def _epub(z: zipfile.ZipFile) -> tuple[dict | None, bool | None, str | None]:
    names = set(z.namelist())
    # DRM: Adobe ADEPT leaves rights.xml, Apple FairPlay sinf.xml, Barnes & Noble / Adobe encrypt content files
    # listed in encryption.xml. Font obfuscation also uses encryption.xml, so only non-font entries count.
    drm, detail = False, None
    if "META-INF/rights.xml" in names:
        drm, detail = True, "Adobe ADEPT (rights.xml)"
    elif "META-INF/sinf.xml" in names:
        drm, detail = True, "Apple FairPlay (sinf.xml)"
    else:
        enc = _zip_read(z, "META-INF/encryption.xml")
        if enc:
            root = _xml(enc)
            if root is not None:
                for ed in root.iter():
                    if _local(ed.tag) != "encrypteddata":
                        continue
                    alg = next((m.attrib.get("Algorithm", "") for m in ed.iter() if _local(m.tag) == "encryptionmethod"), "")
                    uri = next((c.attrib.get("URI", "") for c in ed.iter() if _local(c.tag) == "cipherreference"), "")
                    font = ("embedding" in alg or "obfuscation" in alg or "pdf/enc#RC" in alg
                            or re.search(r"\.(ttf|otf|woff2?)$", uri, re.IGNORECASE))
                    if not font:
                        drm, detail = True, "encrypted content (encryption.xml)"
                        break
    opf_path = None
    container = _zip_read(z, "META-INF/container.xml")
    if container:
        root = _xml(container)
        if root is not None:
            opf_path = next((el.attrib.get("full-path") for el in root.iter() if _local(el.tag) == "rootfile"), None)
    if not opf_path:
        opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
    meta = parse_opf(_zip_read(z, opf_path) or b"") if opf_path else None
    return meta, drm, detail


# --- MOBI / AZW / AZW3 / PRC ----------------------------------------------------------------------------------

EXTH = {100: "author", 101: "publisher", 104: "isbn", 106: "date", 113: "asin", 503: "title", 504: "asin",
        524: "language"}


def parse_mobi(data: bytes) -> tuple[dict | None, bool | None, str | None, int | None]:
    """Return (meta, drm, detail, mobi_version) from the first record of a PDB/MOBI file."""
    if len(data) < 78 + 8:
        return None, None, None, None
    name = data[:32].split(b"\x00", 1)[0].decode("latin-1", "replace")
    nrec = struct.unpack(">H", data[76:78])[0]
    if nrec < 1:
        return None, None, None, None
    rec0 = struct.unpack(">L", data[78:82])[0]
    if rec0 + 16 > len(data):
        return None, None, None, None
    enc_type = struct.unpack(">H", data[rec0 + 12:rec0 + 14])[0]
    drm = enc_type in (1, 2)
    detail = {1: "old Mobipocket encryption", 2: "Mobipocket / Kindle DRM"}.get(enc_type)
    if data[rec0 + 16:rec0 + 20] != b"MOBI":
        return _meta(name), drm, detail, None
    hlen, = struct.unpack(">L", data[rec0 + 20:rec0 + 24])
    codepage, = struct.unpack(">L", data[rec0 + 28:rec0 + 32])
    version, = struct.unpack(">L", data[rec0 + 36:rec0 + 40])
    enc = "utf-8" if codepage == 65001 else "cp1252"
    title = name
    try:
        off, ln = struct.unpack(">LL", data[rec0 + 84:rec0 + 92])
        if 0 < ln < 2000:
            title = data[rec0 + off:rec0 + off + ln].decode(enc, "replace")
    except struct.error:
        pass
    fields: dict[str, list[str]] = {}
    exth_flags = struct.unpack(">L", data[rec0 + 128:rec0 + 132])[0] if len(data) >= rec0 + 132 else 0
    pos = rec0 + 16 + hlen
    if exth_flags & 0x40 and data[pos:pos + 4] == b"EXTH":
        count, = struct.unpack(">L", data[pos + 8:pos + 12])
        p = pos + 12
        for _ in range(min(count, 500)):
            if p + 8 > len(data):
                break
            typ, ln = struct.unpack(">LL", data[p:p + 8])
            if ln < 8:
                break
            if typ in EXTH:
                fields.setdefault(EXTH[typ], []).append(data[p + 8:p + ln].decode(enc, "replace"))
            elif typ == 121:
                version = max(version, 8)  # has a KF8 section
            p += ln
    meta = _meta(
        (fields.get("title") or [title])[0], fields.get("author", []), fields.get("isbn", []),
        asin=(fields.get("asin") or [None])[0], language=(fields.get("language") or [None])[0],
        publisher=(fields.get("publisher") or [None])[0], year=(fields.get("date") or [None])[0],
    )
    return meta, drm, detail, version


# --- PDF ----------------------------------------------------------------------------------------------------

def _pdf(path: Path) -> tuple[dict | None, bool | None, str | None, str | None]:
    import logging

    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    reader = PdfReader(str(path), strict=False)
    drm, detail = False, None
    if reader.is_encrypted:
        filt = ""
        try:
            filt = str(reader.trailer["/Encrypt"].get_object().get("/Filter", ""))
        except Exception:  # noqa: BLE001
            pass
        if "EBX" in filt or "Adobe.APS" in filt:
            return None, True, "Adobe ADEPT (PDF)", None
        try:
            ok = reader.decrypt("")
        except Exception:  # noqa: BLE001
            ok = 0
        if not ok:
            return None, True, "password protected PDF", None
        detail = "permissions-only encryption (readable)"
    info = reader.metadata or {}
    title, authors = info.get("/Title"), []
    if isinstance(info.get("/Author"), str):
        authors = re.split(r"\s*(?:;|&|\band\b)\s*", info["/Author"])
    source = "pdf-info"
    try:
        x = reader.xmp_metadata
        if x is not None:
            if not _good_title(title) and x.dc_title:
                title = next(iter(x.dc_title.values()), None)
            if not _good_authors(authors) and x.dc_creator:
                authors = list(x.dc_creator)
    except Exception:  # noqa: BLE001
        pass
    text = []
    for page in reader.pages[:PDF_TEXT_PAGES]:
        try:
            text.append((page.extract_text() or "")[:20_000])
        except Exception:  # noqa: BLE001
            continue
    isbns = isbns_in_text("\n".join(text))
    meta = _meta(title, authors, isbns, year=info.get("/CreationDate"))
    if isbns and _empty(_meta(title, authors)):
        source = "pdf-text"
    return meta, drm, detail, source


# --- FB2, DOCX/ODT, TXT --------------------------------------------------------------------------------------

def parse_fb2(data: bytes) -> dict | None:
    root = _xml(data)
    if root is None:
        return None
    ti = next((e for e in root.iter() if _local(e.tag) == "title-info"), None)
    if ti is None:
        return None
    title = next((e.text for e in ti.iter() if _local(e.tag) == "book-title"), None)
    authors = []
    for a in (e for e in ti if _local(e.tag) == "author"):
        parts = {_local(p.tag): (p.text or "").strip() for p in a}
        name = " ".join(x for x in (parts.get("first-name"), parts.get("middle-name"), parts.get("last-name")) if x)
        authors.append(name or parts.get("nickname", ""))
    lang = next((e.text for e in ti.iter() if _local(e.tag) == "lang"), None)
    seq = next((e for e in ti.iter() if _local(e.tag) == "sequence"), None)
    isbns = [e.text for e in root.iter() if _local(e.tag) == "isbn" and e.text]
    year = next((e.text for e in root.iter() if _local(e.tag) == "year"), None)
    return _meta(title, authors, isbns, language=lang, year=year,
                 series=seq.attrib.get("name") if seq is not None else None,
                 series_index=seq.attrib.get("number") if seq is not None else None)


def _office(z: zipfile.ZipFile, member: str) -> dict | None:
    root = _xml(_zip_read(z, member) or b"")
    if root is None:
        return None
    vals = {}
    for e in root.iter():
        t = _local(e.tag)
        if t in ("title", "creator", "initial-creator", "language") and e.text and t not in vals:
            vals[t] = e.text
    return _meta(vals.get("title"), [vals.get("creator") or vals.get("initial-creator")], language=vals.get("language"))


def parse_txt_header(data: bytes) -> dict | None:
    """Project Gutenberg style 'Title: ... / Author: ...' headers, plus any ISBN near the top."""
    text = data.decode("utf-8", "replace")
    title = re.search(r"^\s*Title:\s*(.+)$", text, re.MULTILINE)
    author = re.search(r"^\s*Author:\s*(.+)$", text, re.MULTILINE)
    lang = re.search(r"^\s*Language:\s*(.+)$", text, re.MULTILINE)
    m = _meta(title.group(1) if title else None, [author.group(1)] if author else [], isbns_in_text(text),
              language=lang.group(1) if lang else None)
    return None if _empty(m) else m


def ebook_meta(path: Path, timeout: int = 60) -> dict | None:
    """Calibre's ebook-meta, if it's installed (it will be from Phase 6). Reads every format Calibre knows."""
    exe = shutil.which("ebook-meta")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, str(path)], capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    f = {}
    for line in out.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            f[k.strip().lower()] = v.strip()
    authors = re.sub(r"\s*\[[^\]]*\]", "", f.get("author(s)", ""))
    # Calibre falls back to the file name for a missing title; that's a guess, not the book's own metadata.
    if f.get("title", "").strip().lower() == path.stem.strip().lower():
        f["title"] = ""
    isbns = re.findall(r"isbn:([0-9Xx\-]+)", f.get("identifiers", ""))
    series = re.match(r"(.+?)\s*#([\d.]+)", f.get("series", ""))
    m = _meta(f.get("title"), [a for a in authors.split("&")], isbns, language=f.get("languages"),
              publisher=f.get("publisher"), year=f.get("published"),
              series=series.group(1) if series else None, series_index=series.group(2) if series else None)
    return None if _empty(m) else m


# --- dispatch -----------------------------------------------------------------------------------------------

def sniff(head: bytes, ext: str) -> str:
    """Format from magic bytes, falling back to the extension. Download sites mislabel files often."""
    if head.startswith(b"%PDF-"):
        return "PDF"
    if head[60:68] == b"BOOKMOBI":
        return {"azw3": "AZW3", "azw": "AZW", "prc": "PRC"}.get(ext, "MOBI")
    if head[60:68] in (b"TEXtREAd", b"PNRdPPrs", b"DataPlkr"):
        return "PDB"
    if head.startswith(b"\xeaDRMION\xee") or head.startswith(b"CONT\x02\x00"):
        return "KFX"
    if head.startswith(b"ITOLITLS"):
        return "LIT"
    if head.startswith(b"AT&TFORM"):
        return "DJVU"
    if head.startswith(b"ITSF"):
        return "CHM"
    if head.startswith(b"{\\rtf"):
        return "RTF"
    if head.startswith(b"L\x00R\x00F\x00"):
        return "LRF"
    if head.startswith(b"Rar!\x1a\x07"):
        return "RAR"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7Z"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "DOC"
    if head.startswith(b"PK\x03\x04"):
        return "ZIP"            # refined by _zip_kind
    if b"<FictionBook" in head:
        return "FB2"
    return ext.upper() or "UNKNOWN"


def _zip_kind(z: zipfile.ZipFile, ext: str) -> str:
    names = z.namelist()
    nset = set(names)
    mt = _zip_read(z, "mimetype", 200) or b""
    if b"application/epub+zip" in mt or "META-INF/container.xml" in nset:
        return "KEPUB" if ext == "kepub" else "EPUB"
    if "word/document.xml" in nset:
        return "DOCX"
    if b"opendocument.text" in mt:
        return "ODT"
    if ext in ("htmlz", "txtz") or ("metadata.opf" in nset and any(n.endswith((".html", ".txt")) for n in names)):
        return ext.upper() if ext in ("htmlz", "txtz") else "HTMLZ"
    if any(n.lower().endswith(".fb2") for n in names) and len(names) <= 3:
        return "FB2"
    images = sum(1 for n in names if re.search(r"\.(jpe?g|png|gif|webp)$", n, re.IGNORECASE))
    if names and images / len(names) > 0.8:
        return "CBZ"
    return "ZIP"


def classify_ext(name: str) -> str:
    """Cheap first pass on the filename: ebook | archive | comic | partial | sidecar | ignored."""
    low = name.lower()
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    if low.endswith(".fb2.zip"):
        return "ebook"
    if ext in PARTIAL_EXTS:
        return "partial"
    if ext in EBOOK_EXTS:
        return "ebook"
    if ext in COMIC_EXTS:
        return "comic"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in SIDE_EXTS:
        return "sidecar"
    return "ignored"


def analyze(path: str | Path) -> dict:
    p = Path(path)
    ext = p.name.lower().rsplit(".", 1)[-1] if "." in p.name else ""
    out = {"kind": "ebook", "format": None, "sha256": None, "size": None, "meta": None, "meta_source": None,
           "drm": None, "drm_detail": None, "entries": None, "error": None, "mobi_version": None}
    try:
        st = p.stat()
        out["size"] = st.st_size
        with open(p, "rb") as fh:
            head = fh.read(4096)
        fmt = sniff(head, ext)
        meta = None
        if fmt == "ZIP":
            with zipfile.ZipFile(p) as z:
                fmt = _zip_kind(z, ext)
                if fmt in ("EPUB", "KEPUB"):
                    meta, out["drm"], out["drm_detail"] = _epub(z)
                    out["meta_source"] = "epub-opf"
                elif fmt in ("DOCX", "ODT"):
                    meta = _office(z, "docProps/core.xml" if fmt == "DOCX" else "meta.xml")
                    out["meta_source"], out["drm"] = fmt.lower(), False
                elif fmt in ("HTMLZ", "TXTZ"):
                    meta = parse_opf(_zip_read(z, "metadata.opf") or b"")
                    out["meta_source"], out["drm"] = "opf-embedded", False
                elif fmt == "FB2":
                    inner = next(n for n in z.namelist() if n.lower().endswith(".fb2"))
                    meta = parse_fb2(_zip_read(z, inner, 50 * 1024 * 1024) or b"")
                    out["meta_source"], out["drm"] = "fb2", False
                elif fmt == "CBZ":
                    out["kind"] = "comic"
                else:
                    out["kind"] = "archive"
                    out["entries"] = [n for n in z.namelist() if classify_ext(n) == "ebook"][:200]
        elif fmt in ("MOBI", "AZW", "AZW3", "PRC", "PDB"):
            with open(p, "rb") as fh:
                data = fh.read(256 * 1024)
            meta, out["drm"], out["drm_detail"], out["mobi_version"] = parse_mobi(data)
            if fmt == "MOBI" and (out["mobi_version"] or 0) >= 8:
                fmt = "AZW3"
            out["meta_source"] = "mobi-exth"
        elif fmt == "KFX":
            out["drm"] = head.startswith(b"\xeaDRMION\xee")
            out["drm_detail"] = "Kindle KFX DRM" if out["drm"] else None
        elif fmt == "PDF":
            meta, out["drm"], out["drm_detail"], out["meta_source"] = _pdf(p)
        elif fmt == "FB2":
            meta = parse_fb2(p.read_bytes()[:50 * 1024 * 1024])
            out["meta_source"], out["drm"] = "fb2", False
        elif fmt == "TXT":
            with open(p, "rb") as fh:
                meta = parse_txt_header(fh.read(64 * 1024))
            out["meta_source"], out["drm"] = "txt-header", False
        elif fmt in ("RAR", "7Z"):
            out["kind"] = "archive"
        elif fmt == "LIT":
            out["drm"] = None  # DRM'd LIT files exist; Calibre's tools tell later
        elif fmt in ("DJVU", "CHM", "RTF", "LRF", "DOC"):
            out["drm"] = False
        if ext in COMIC_EXTS:
            out["kind"] = "comic"
        # Calibre's ebook-meta only for formats Tomearr can't read itself (it's slow to start).
        if out["kind"] == "ebook" and _empty(meta) and fmt in EBOOK_META_FORMATS:
            em = ebook_meta(p)
            if em:
                meta, out["meta_source"] = em, "ebook-meta"
        if _empty(meta):
            meta = meta if meta and (meta.get("authors") or meta.get("title")) else None
        out["format"] = fmt
        out["meta"] = meta
        if not meta:
            out["meta_source"] = None
        out["sha256"] = sha256_file(p)
    except Exception as exc:  # noqa: BLE001  -- a broken file must never stop a scan
        out["error"] = f"{type(exc).__name__}: {exc}"[:500]
        out["format"] = out["format"] or (ext.upper() or None)
        if out["sha256"] is None:
            try:
                out["sha256"] = sha256_file(p)
            except OSError:
                pass
    return out


def read_sidecar(folder: Path, stem: str) -> dict | None:
    """metadata.opf (Calibre, Readarr) or <name>.opf next to the book."""
    for cand in (folder / f"{stem}.opf", folder / "metadata.opf"):
        try:
            if cand.is_file() and cand.stat().st_size < MAX_XML:
                m = parse_opf(cand.read_bytes())
                if not _empty(m):
                    return m
        except OSError:
            continue
    return None
