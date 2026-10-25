"""Clean up what's left in the ingest folder after books are imported.

A folder is cleaned up only when *everything* left in it (including subfolders) is leftover material: metadata
(.opf, .nfo, .sfv, ...), images, short text files, NAS/OS clutter. Anything that could still be a book (an ebook,
archive, comic, partial download, or a long .txt) keeps the folder, and so does any file type Tomearr doesn't
recognise (an audiobook's .mp3s, say). Files modified in the last `settle` seconds are left alone too, since a
download may still be arriving. The ingest folder itself is never removed.
"""
import os
import shutil
import time
from pathlib import Path

from . import extract

JUNK_EXTS = {
    "opf", "nfo", "sfv", "url", "md5", "sha1", "sha256", "nzb", "log", "xml", "json", "par2", "srr", "diz",
    "html", "htm", "ini", "db", "txt", "md", "cue",
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "svg", "ico",
}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".directory"}


def is_junk(p: Path) -> bool:
    name = p.name.lower()
    if name in JUNK_NAMES or name.startswith("._"):
        return True
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in JUNK_EXTS:
        return False
    if ext == "txt":
        try:
            return p.stat().st_size < extract.MIN_TXT_SIZE      # longer .txt files may be books
        except OSError:
            return False
    return True


def junk_tree(folder: Path, settle: int) -> list[Path] | None:
    """Every file under `folder` if all of it is settled leftover material, else None."""
    cutoff = time.time() - settle
    files: list[Path] = []
    for dirpath, _dirs, names in os.walk(folder):
        for n in names:
            p = Path(dirpath) / n
            try:
                st = p.lstat()
            except OSError:
                return None
            if not is_junk(p) or st.st_mtime > cutoff:
                return None
            files.append(p)
    return files


def _remove_tree(root: Path, folder: Path, files: list[Path], trash: Path | None) -> list[dict]:
    out = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        if trash is None:
            f.unlink()
            out.append({"rel": rel, "trash": None})
        else:
            dst = trash / "ingest" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dst))
            out.append({"rel": rel, "trash": str(dst)})
    for dirpath, dirs, _n in sorted(os.walk(folder), key=lambda t: -len(t[0])):
        try:
            Path(dirpath).rmdir()
        except OSError:
            pass
    return out


def cleanup_after(root: Path, rel: str, trash: Path | None, settle: int) -> list[dict]:
    """After `rel` left the ingest folder: remove its folder (and emptied parents) if only leftovers remain."""
    root = root.resolve()
    folder = (root / rel).parent.resolve()
    removed: list[dict] = []
    while folder != root and root in folder.parents:
        if not folder.is_dir():
            folder = folder.parent
            continue
        files = junk_tree(folder, settle)
        if files is None:
            break
        removed += _remove_tree(root, folder, files, trash)
        if folder.exists():
            break                       # something couldn't be removed; stop here
        folder = folder.parent
    return removed


def sweep_candidates(root: Path, settle: int) -> list[tuple[Path, list[Path]]]:
    """Top-most folders under the ingest folder that contain nothing but settled leftovers.

    A leftover-only folder inside a release that still holds its book ("Release/Covers/" next to "Release/book.epub")
    is not a candidate: it belongs to that release and goes when the book is imported.
    """
    root = root.resolve()
    out = []
    for dirpath, dirs, names in os.walk(root):
        here = Path(dirpath)
        if here != root and any(not is_junk(here / n) for n in names):
            dirs[:] = []            # this folder holds something real; its subfolders belong to it
            continue
        keep = []
        for d in sorted(dirs):
            if d in ("@eaDir", "#recycle", "#snapshot"):
                continue
            sub = here / d
            files = junk_tree(sub, settle)
            if files is not None:
                out.append((sub, files))    # the whole folder is leftovers; don't descend
            else:
                keep.append(d)
        dirs[:] = keep
    return out


def restore(items: list[dict], root: Path) -> int:
    n = 0
    for it in items:
        if it.get("trash") and Path(it["trash"]).exists():
            dst = root / it["rel"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.move(it["trash"], str(dst))
                n += 1
    return n
