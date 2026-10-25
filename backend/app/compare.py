"""Compare a file in the ingest folder with the EPUB Calibre already has for the same book.

Both sides are read as EPUB (a MOBI/AZW3 in the ingest folder is converted to a staged EPUB first, exactly as an
import would), then measured: length, chapters, images, cover, table of contents, fonts, EPUB version, signs
of automated conversion, metadata, and a passage of text from the same place in both. Tomearr suggests which
copy looks better, but the choice is yours.
"""
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

import orjson

from . import calibre_cli, extract, ingest, library

MAX_TEXT = 40 * 1024 * 1024        # don't read more than this much (X)HTML from one book
_TAG = re.compile(r"<[^>]+>")
_HEADSTYLE = re.compile(r"(?is)<(head|style|script)\b.*?</\1>")
_WS = re.compile(r"\s+")
# Files ebook-convert (calibre) and KindleUnpack produce; publishers name things after their content.
_CONVERTED_NAMES = re.compile(r"(^|/)(index_split_\d+|part\d{4}|split_\d+|text\d{5})\.x?html?$", re.IGNORECASE)
_CALIBRE_CLASS = re.compile(rb'class="[^"]*\bcalibre\d*\b')
_OCR_HINT = re.compile(r"\b(\w+)- (\w+)\b")     # words broken across a line and never rejoined


class CompareUnavailable(Exception):
    """There's nothing to compare against (no Calibre EPUB, file gone, ...)."""


def _xml(b: bytes):
    return extract._xml(b)


def _local(t: str) -> str:
    return extract._local(t)


def _resolve(base: str, href: str) -> str:
    parts = []
    for p in (PurePosixPath(base).parent / href.split("#")[0]).parts:
        if p == "..":
            if parts:
                parts.pop()
        elif p != ".":
            parts.append(p)
    return "/".join(parts)


def _text(html: bytes) -> str:
    s = html.decode("utf-8", "replace")
    s = _HEADSTYLE.sub(" ", s)
    s = _TAG.sub(" ", s)
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    s = re.sub(r"&[a-z]+;|&#\d+;", "", s)
    return _WS.sub(" ", s).strip()


def inspect_epub(path: Path) -> dict:
    """Measure one EPUB. Never raises for a damaged book; problems are reported in 'error'."""
    out = {"size": path.stat().st_size, "words": 0, "chapters": 0, "images": 0, "fonts": 0, "toc_entries": 0,
           "has_cover": False, "epub_version": None, "origin": "unknown", "origin_why": [], "meta": {},
           "error": None, "_text": ""}
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        out["error"] = f"not a readable EPUB ({exc})"
        return out
    with z:
        names = z.namelist()
        container = _xml(extract._zip_read(z, "META-INF/container.xml") or b"")
        opf_path = None
        if container is not None:
            opf_path = next((e.attrib.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile"), None)
        opf_path = opf_path or next((n for n in names if n.lower().endswith(".opf")), None)
        opf_bytes = extract._zip_read(z, opf_path) if opf_path else None
        opf = _xml(opf_bytes or b"")
        if opf is None:
            out["error"] = "no readable package (OPF) file"
            return out
        out["epub_version"] = opf.attrib.get("version")
        out["meta"] = extract.parse_opf(opf_bytes) or {}
        manifest, spine, cover_ids = {}, [], set()
        nav_href = ncx_href = None
        for e in opf.iter():
            t = _local(e.tag)
            if t == "item":
                href = _resolve(opf_path, e.attrib.get("href", ""))
                manifest[e.attrib.get("id")] = (href, e.attrib.get("media-type", ""), e.attrib.get("properties", ""))
                if "nav" in e.attrib.get("properties", "").split():
                    nav_href = href
                if e.attrib.get("media-type") == "application/x-dtbncx+xml":
                    ncx_href = href
                if "cover-image" in e.attrib.get("properties", ""):
                    cover_ids.add(e.attrib.get("id"))
            elif t == "itemref":
                spine.append(e.attrib.get("idref"))
            elif t == "meta" and e.attrib.get("name") == "cover":
                cover_ids.add(e.attrib.get("content"))
            elif t == "reference" and e.attrib.get("type") == "cover":
                cover_ids.add("guide-cover")
        media = [m for _h, m, _p in manifest.values()]
        out["images"] = sum(1 for m in media if m.startswith("image/"))
        out["fonts"] = sum(1 for (h, m, _p) in manifest.values()
                           if "font" in m or re.search(r"\.(ttf|otf|woff2?)$", h, re.IGNORECASE))
        out["has_cover"] = any(manifest.get(i, ("", ""))[1].startswith("image/") for i in cover_ids) or \
            "guide-cover" in cover_ids or any(re.search(r"cover\.(jpe?g|png|gif)$", h, re.IGNORECASE)
                                              for h, m, _p in manifest.values() if m.startswith("image/"))
        if nav_href and nav_href in names:
            nav = extract._zip_read(z, nav_href) or b""
            out["toc_entries"] = len(re.findall(rb"<a\b", nav))
        elif ncx_href and ncx_href in names:
            out["toc_entries"] = len(re.findall(rb"<navPoint\b", extract._zip_read(z, ncx_href) or b""))

        docs = [manifest[i][0] for i in spine if i in manifest]
        out["chapters"] = len(docs)
        texts, read, calibre_classes = [], 0, 0
        for d in docs:
            if d not in names or read > MAX_TEXT:
                continue
            raw = z.read(d)
            read += len(raw)
            calibre_classes += len(_CALIBRE_CLASS.findall(raw))
            texts.append(_text(raw))
        full = "\n".join(texts)
        out["words"] = len(full.split())
        out["_text"] = full

        why = []
        if sum(1 for d in docs if _CONVERTED_NAMES.search(d)) >= max(1, len(docs) // 2):
            why.append("file names typical of an automatic conversion")
        if calibre_classes > 20:
            why.append("styled by a calibre conversion")
        broken = len(_OCR_HINT.findall(full[:200_000]))
        if broken > 40:
            why.append(f"{broken} words broken across lines (looks OCR'd from a scan)")
        if why:
            out["origin"], out["origin_why"] = "converted", why
        else:
            out["origin"] = "publisher"
            out["origin_why"] = ["no signs of automatic conversion (likely publisher-made)"]
    return out


def _passage(a: str, b: str, at: float = 0.3, words: int = 90) -> tuple[str, str]:
    """The same passage from both texts: take a run of words ~30% into A and find it in B."""
    aw, bw = a.split(), b.split()
    if not aw:
        return "", " ".join(bw[int(len(bw) * at):][:words])
    start = int(len(aw) * at)
    snippet_a = " ".join(aw[start:start + words])
    norm = lambda ws: [re.sub(r"\W", "", w.lower()) for w in ws]  # noqa: E731
    na, nb = norm(aw[start:start + 8]), norm(bw)
    for i in range(0, max(0, len(nb) - 8)):
        if nb[i:i + 8] == na:
            return snippet_a, " ".join(bw[i:i + words])
    return snippet_a, " ".join(bw[int(len(bw) * at):][:words])


def suggest(cal: dict, new: dict) -> dict:
    """Which copy to keep, and why. 'calibre', 'ingest' or 'either'."""
    score, why = 0.0, []
    if cal["words"] and new["words"]:
        r = new["words"] / cal["words"]
        if r < 0.9:
            score -= 3
            why.append(f"this file is {round((1 - r) * 100)}% shorter than Calibre's (abridged, a sample, or incomplete?)")
        elif r > 1.1:
            score += 3
            why.append(f"this file is {round((r - 1) * 100)}% longer than Calibre's (Calibre's may be incomplete)")
    if cal["origin"] != new["origin"] and "unknown" not in (cal["origin"], new["origin"]):
        score += 2 if new["origin"] == "publisher" else -2
        why.append("this file looks publisher-made, Calibre's looks converted" if new["origin"] == "publisher"
                   else "Calibre's looks publisher-made, this file looks converted")
    for key, label, w in (("has_cover", "a cover", 1.0),):
        if cal[key] != new[key]:
            score += w if new[key] else -w
            why.append(f"only {'this file' if new[key] else 'Calibre'} has {label}")
    if (cal["toc_entries"] > 2) != (new["toc_entries"] > 2):
        score += 1 if new["toc_entries"] > 2 else -1
        why.append(f"only {'this file' if new['toc_entries'] > 2 else 'Calibre'} has a proper table of contents")
    if cal.get("error") or new.get("error"):
        bad = "ingest" if new.get("error") else "calibre"
        score += -10 if bad == "ingest" else 10
        why.append(f"{'this file' if bad == 'ingest' else 'Calibre'} couldn't be read properly")
    choice = "ingest" if score >= 2 else "calibre" if score <= -1 else "either"
    if choice == "either" and not why:
        why.append("they look equivalent; keeping Calibre's avoids churn")
    return {"choice": choice, "score": score, "why": why}


def compare(conn, settings, file_id: int) -> dict:
    """Build (or reuse) the comparison for an ingest file and the EPUB Calibre has for its work."""
    r = conn.execute("SELECT * FROM ingest_files WHERE id=?", (file_id,)).fetchone()
    if not r:
        raise KeyError(file_id)
    wk = r["decided_work_key"] or r["work_key"]
    lb = conn.execute("SELECT b.calibre_id, b.title, b.authors, f.relpath FROM library_books b "
                      "JOIN calibre_files f ON f.calibre_id = b.calibre_id AND f.format = 'EPUB' "
                      "WHERE b.work_key=? ORDER BY b.calibre_id LIMIT 1", (wk,)).fetchone()
    if not lb:
        raise CompareUnavailable("Calibre doesn't have an EPUB for this book (sync the library?)")
    cal_path = Path(library.calibre_path(conn)) / lb["relpath"]
    if not cal_path.is_file():
        raise CompareUnavailable(f"can't read Calibre's EPUB at {cal_path}")
    src = Path(ingest.setting(conn, "ingest_path")) / r["path"]
    if not src.is_file():
        raise CompareUnavailable("the file is no longer in the ingest folder")

    cache_dir = Path(settings.config_dir) / "cache" / "compare"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"{file_id}-{r['sha256'][:16] if r['sha256'] else 'x'}-{cal_path.stat().st_size}-{int(cal_path.stat().st_mtime)}"
    cached = cache_dir / f"{key}.json"
    if cached.is_file():
        return {**orjson.loads(cached.read_bytes()), "choice": r["compare_choice"]}

    work = cache_dir / str(file_id)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    new_epub, converted = src, False
    if r["format"] not in ("EPUB",):
        new_epub = work / "ingest.epub"
        calibre_cli.convert(src, new_epub)
        converted = True
    cal_i, new_i = inspect_epub(cal_path), inspect_epub(new_epub)
    if converted:
        # the conversion was ours, so its markers say nothing about where the original came from
        new_i["origin"], new_i["origin_why"] = "unknown", [f"{r['format']} file; converted to EPUB to compare"]
    pa, pb = _passage(cal_i.pop("_text"), new_i.pop("_text"))
    for side, p, d in (("calibre", cal_path, cal_i), ("ingest", new_epub, new_i)):
        cover = work / f"cover-{side}.jpg"
        d["cover_url"] = None
        if d["has_cover"] and calibre_cli.tool("ebook-meta"):
            try:
                calibre_cli._run([calibre_cli.tool("ebook-meta"), str(p), "--get-cover", str(cover)], 60)
                if cover.is_file() and cover.stat().st_size:
                    d["cover_url"] = f"/api/import/compare/{file_id}/cover/{side}"
            except calibre_cli.CalibreCLIError:
                pass
    res = {
        "file_id": file_id, "path": r["path"], "format": r["format"], "converted_for_compare": converted,
        "calibre_id": lb["calibre_id"], "calibre_title": lb["title"], "calibre_authors": lb["authors"],
        "calibre": {**cal_i, "path": lb["relpath"], "sample": pa},
        "ingest": {**new_i, "sample": pb},
        "suggestion": suggest(cal_i, new_i),
        "choice": r["compare_choice"],
    }
    for old in cache_dir.glob(f"{file_id}-*.json"):
        old.unlink(missing_ok=True)
    cached.write_bytes(orjson.dumps(res))
    return res


def cover_path(settings, file_id: int, side: str) -> Path:
    if side not in ("calibre", "ingest"):
        raise KeyError(side)
    p = Path(settings.config_dir) / "cache" / "compare" / str(file_id) / f"cover-{side}.jpg"
    if not p.is_file():
        raise KeyError(side)
    return p
