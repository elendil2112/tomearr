"""Calibre's command-line tools: ebook-convert, ebook-meta and calibredb.

Writes to the Calibre library always go through calibredb, so Calibre names the folders and files and keeps
metadata.db consistent. Two modes:
  direct  calibredb --library-path /calibre (default): Calibre's own tool updates metadata.db and the folders,
          the same way Calibre-Web writes to the library. Needs /calibre mounted read-write. No login.
  server  calibredb --with-library http://host:port[/#library]: goes through a running Calibre content server.
          Calibre only accepts changes over the network from a logged-in user (or, with "allow un-authenticated
          local connections to make changes", from the same machine), so this mode needs a Calibre user.
The server password is passed on stdin, never on the command line, so it doesn't show up in process lists.
"""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CONVERT_TIMEOUT = 15 * 60
CALIBREDB_TIMEOUT = 5 * 60
_NOISE = re.compile(r"^(Using proxies|Removing invalid|.*GetPassWarning|\s*passwd = |Warning: Password input|"
                    r"Enter the password|QStandardPaths)")


class CalibreCLIError(RuntimeError):
    pass


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")   # no display in the container
    env.setdefault("CALIBRE_CONFIG_DIRECTORY", os.environ.get("TOMEARR_CALIBRE_CONFIG", "/config/calibre-cli"))
    return env


def _clean(text: str) -> str:
    return "\n".join(l for l in (text or "").splitlines() if l.strip() and not _NOISE.match(l)).strip()


def tool(name: str) -> str | None:
    for cand in (shutil.which(name), f"/opt/calibre/{name}"):
        if cand and Path(cand).exists():
            return cand
    return None


def version() -> str | None:
    exe = tool("calibredb")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60, env=_env()).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"calibre ([\d.]+)", out)
    return m.group(1) if m else None


@dataclass
class Target:
    mode: str = "direct"              # direct | server
    url: str = ""                     # http://192.168.0.229:8221
    library_id: str = ""              # only needed with more than one library
    username: str = ""
    password: str = ""
    library_path: str = "/calibre"    # direct mode

    def args(self) -> list[str]:
        if self.mode == "direct":
            return ["--library-path", self.library_path]
        if not self.url:
            raise CalibreCLIError("Set the Calibre content server address in Settings")
        url = self.url.rstrip("/") + (f"/#{self.library_id}" if self.library_id else "")
        a = ["--with-library", url]
        if self.username:
            a += ["--username", self.username, "--password", "<stdin>"]
        return a

    def describe(self) -> str:
        return self.url if self.mode == "server" else f"{self.library_path} (direct)"


def _run(cmd: list[str], timeout: int, stdin: str | None = None, merge: bool = False) -> str:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=_env())
    except subprocess.TimeoutExpired:
        raise CalibreCLIError(f"{Path(cmd[0]).name} timed out after {timeout}s")
    except FileNotFoundError:
        raise CalibreCLIError(f"{cmd[0]} is not installed in this container")
    if p.returncode != 0:
        msg = _clean(p.stderr) or _clean(p.stdout) or f"exit code {p.returncode}"
        raise CalibreCLIError(msg.splitlines()[-1][:500] if "Traceback" in msg else msg[:500])
    return _clean(p.stdout + ("\n" + p.stderr if merge else ""))


class Calibre:
    def __init__(self, target: Target):
        self.t = target
        self.db = tool("calibredb")

    def _db(self, *args: str, timeout: int = CALIBREDB_TIMEOUT, merge: bool = False) -> str:
        if not self.db:
            raise CalibreCLIError("calibredb is not installed in this container")
        stdin = self.t.password + "\n" if self.t.mode == "server" and self.t.username else None
        return _run([self.db, *args, *self.t.args()], timeout, stdin, merge)

    # --- reads -----------------------------------------------------------------------------------
    def books(self, search: str | None = None, fields: str = "title,authors,formats") -> list[dict]:
        args = ["list", "--for-machine", "--fields", fields]
        if search:
            args += ["--search", search]
        out = self._db(*args)
        start = out.find("[")
        return json.loads(out[start:]) if start >= 0 else []

    def book(self, book_id: int) -> dict | None:
        r = self.books(f"id:{int(book_id)}", "title,authors,formats,identifiers,series,series_index,languages")
        return r[0] if r else None

    def formats(self, book_id: int) -> list[str]:
        b = self.book(book_id)
        if not b:
            return []
        return sorted({Path(f).suffix.lstrip(".").upper() if "/" in f or "." in f else f.upper()
                       for f in b.get("formats") or []})

    def test(self) -> dict:
        if self.t.mode == "direct":
            lib = Path(self.t.library_path)
            if not (lib / "metadata.db").is_file():
                raise CalibreCLIError(f"No metadata.db in {lib}. Is the Calibre library mounted there?")
            if not os.access(lib, os.W_OK) or not os.access(lib / "metadata.db", os.W_OK):
                raise CalibreCLIError(f"{lib} is read-only. Remove ':ro' from the /calibre mount in docker-compose.yml "
                                      "so Tomearr can add books.")
        ids = self.books(fields="id")
        return {"ok": True, "books": len(ids), "target": self.t.describe(), "version": version()}

    # --- writes ----------------------------------------------------------------------------------
    def add(self, path: Path, meta: dict) -> int:
        args = ["add"]
        if meta.get("title"):
            args += ["--title", meta["title"]]
        if meta.get("authors"):
            args += ["--authors", " & ".join(meta["authors"])]
        if meta.get("isbn"):
            args += ["--isbn", meta["isbn"]]
        for k, v in (meta.get("identifiers") or {}).items():
            args += ["--identifier", f"{k}:{v}"]
        if meta.get("series"):
            args += ["--series", meta["series"]]
            if meta.get("series_index") is not None:
                args += ["--series-index", str(meta["series_index"])]
        if meta.get("languages"):
            args += ["--languages", ",".join(meta["languages"])]
        out = self._db(*args, str(path), merge=True)
        m = re.search(r"Added book ids:\s*([\d,\s]+)", out)
        if not m:
            if "already exist" in out:
                raise CalibreCLIError("Calibre already has a book with this title and author; sync the library and "
                                      "the import will add the EPUB to that book instead")
            raise CalibreCLIError(f"Calibre did not report an added book: {out[:300]}")
        return int(m.group(1).split(",")[0])

    def add_format(self, book_id: int, path: Path) -> None:
        self._db("add_format", str(int(book_id)), str(path))

    def remove_format(self, book_id: int, fmt: str) -> None:
        self._db("remove_format", str(int(book_id)), fmt.upper())

    def set_fields(self, book_id: int, fields: dict[str, str]) -> None:
        args = ["set_metadata", str(int(book_id))]
        for k, v in fields.items():
            args += ["--field", f"{k}:{v}"]
        self._db(*args)

    def remove(self, book_id: int, permanent: bool = True) -> None:
        """Remove a book. permanent=False sends it to Calibre's own trash bin, where Calibre can restore it."""
        self._db("remove", str(int(book_id)), *(["--permanent"] if permanent else []))


# --- conversion and file metadata (local, no library involved) ------------------------------------------

def convert(src: Path, dst: Path, timeout: int = CONVERT_TIMEOUT) -> None:
    exe = tool("ebook-convert")
    if not exe:
        raise CalibreCLIError("ebook-convert is not installed in this container")
    _run([exe, str(src), str(dst)], timeout)
    if not dst.exists() or dst.stat().st_size == 0:
        raise CalibreCLIError("ebook-convert finished but produced no file")


def write_meta(path: Path, meta: dict) -> None:
    """Write title, authors, ISBN, identifiers, series and language into an ebook file (our staged copy)."""
    exe = tool("ebook-meta")
    if not exe:
        raise CalibreCLIError("ebook-meta is not installed in this container")
    args = [exe, str(path)]
    if meta.get("title"):
        args += ["--title", meta["title"]]
    if meta.get("authors"):
        args += ["--authors", " & ".join(meta["authors"])]
    if meta.get("isbn"):
        args += ["--isbn", meta["isbn"]]
    for k, v in (meta.get("identifiers") or {}).items():
        args += ["--identifier", f"{k}:{v}"]
    if meta.get("series"):
        args += ["--series", meta["series"]]
        if meta.get("series_index") is not None:
            args += ["--index", str(meta["series_index"])]
    if meta.get("languages"):
        args += ["--language", ",".join(meta["languages"])]
    if meta.get("pubdate"):
        args += ["--date", meta["pubdate"]]
    if meta.get("cover"):
        args += ["--cover", str(meta["cover"])]
    _run(args, 120)


def has_cover(path: Path) -> bool:
    """Whether an ebook file has a cover image (asks ebook-meta to extract it)."""
    exe = tool("ebook-meta")
    if not exe:
        return True   # can't tell; don't go fetching
    out = path.with_suffix(".cover-check.jpg")
    try:
        msg = _run([exe, str(path), "--get-cover", str(out)], 120, merge=True)
        return "No cover found" not in msg and out.is_file() and out.stat().st_size > 0
    except CalibreCLIError:
        return True
    finally:
        out.unlink(missing_ok=True)


def unpack(archive: Path, dest: Path, timeout: int = 15 * 60) -> list[Path]:
    """Extract an archive (zip, rar, 7z, tar) with bsdtar. Refuses absolute paths and '..' entries."""
    exe = shutil.which("bsdtar")
    if not exe:
        raise CalibreCLIError("bsdtar is not installed in this container")
    listing = _run([exe, "-tf", str(archive)], 120).splitlines()
    for name in listing:
        if name.startswith("/") or ".." in Path(name).parts:
            raise CalibreCLIError(f"archive has an unsafe path: {name}")
    dest.mkdir(parents=True, exist_ok=True)
    _run([exe, "-xf", str(archive), "-C", str(dest), "--no-same-owner", "--no-same-permissions"], timeout)
    return [p for p in dest.rglob("*") if p.is_file()]
