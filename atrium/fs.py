"""File-system access for the editor (scoped to a project and its worktrees)."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import gitops
from .project import Project

HIDDEN = {".git", ".DS_Store", ".atrium"}
MAX_TEXT = 4 * 1024 * 1024


class Conflict(Exception):
    def __init__(self, current_sha: str):
        super().__init__("file changed on disk")
        self.current_sha = current_sha


def sha_of(data: bytes | str | None) -> str | None:
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha1(data).hexdigest()


def _rel(root: str, p: Path) -> str:
    return os.path.relpath(str(p), root)


def list_dir(project: Project, root: str | None, rel: str, show_hidden: bool = False) -> dict[str, Any]:
    root = os.path.realpath(root or project.path)
    target = project.resolve(rel or ".", root=root)
    status = gitops.status_map_sync(root)
    dirty_dirs: set[str] = set()
    for p in status:
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirty_dirs.add("/".join(parts[:i]))
    entries = []
    try:
        it = list(os.scandir(target))
    except OSError as e:
        raise FileNotFoundError(str(e))
    for e in it:
        if not show_hidden and e.name in HIDDEN:
            continue
        rp = _rel(root, Path(e.path))
        is_dir = e.is_dir(follow_symlinks=False)
        entry = {"name": e.name, "path": rp, "type": "dir" if is_dir else "file"}
        if is_dir:
            if rp in dirty_dirs:
                entry["git"] = "•"
        else:
            try:
                entry["size"] = e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            if rp in status:
                entry["git"] = status[rp]
        entries.append(entry)
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return {"root": root, "path": _rel(root, target), "entries": entries}


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except (OSError, IsADirectoryError):
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def read_file(project: Project, root: str | None, rel: str) -> dict[str, Any]:
    root = os.path.realpath(root or project.path)
    p = project.resolve(rel, root=root)
    st = p.stat()
    info: dict[str, Any] = {"root": root, "path": _rel(root, p), "abs": str(p), "size": st.st_size, "mtime": st.st_mtime}
    if st.st_size > MAX_TEXT:
        return {**info, "binary": True, "too_large": True, "content": None, "sha": None}
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        return {**info, "binary": True, "content": None, "sha": sha_of(data)}
    return {**info, "binary": False, "content": data.decode("utf-8", errors="replace"), "sha": sha_of(data)}


# ---- a file as itself: what the Files panel's preview shows (an image, a PDF, a page and what it loads) -----------
# what a browser only displays; anything else it is handed (HTML, SVG, XML) comes sandboxed, so a page from the
# project never runs as Atrium, with its login and its API
_PASSIVE = ("image/", "audio/", "video/", "application/pdf", "text/plain")
_TYPES = {".mjs": "text/javascript", ".md": "text/markdown", ".webp": "image/webp", ".avif": "image/avif", ".wasm": "application/wasm"}


def mime_of(p: Path) -> str:
    return _TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def raw_headers(mime: str, scripts: bool = False) -> dict[str, str]:
    """``scripts``: what a previewed page loads (Sites): its scripts run, in an opaque origin."""
    h = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
    if mime == "image/svg+xml" or not mime.startswith(_PASSIVE):
        h["Content-Security-Policy"] = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads" if scripts else "sandbox"
    return h


def raw_file(project: Project, root: str | None, rel: str) -> tuple[Path, str]:
    root = os.path.realpath(root or project.path)
    p = project.resolve(rel, root=root)
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p, mime_of(p)


class Sites:
    """Keys to the folder of an HTML page the Files panel previews. The page runs sandboxed, in an opaque origin (out of
    reach of Atrium's API), so the browser sends no login cookie with what it loads: the key in those URLs stands in for
    one. It opens that folder only, without its hidden files (.git, .env: the page's scripts could read them), and
    lapses; a restart forgets every key."""

    TTL = 6 * 3600

    def __init__(self) -> None:
        self._keys: dict[str, tuple[str, str, float]] = {}  # key -> project id, folder, expiry
        self._lock = threading.Lock()

    def key(self, pid: str, folder: str) -> str:
        now = time.time()
        with self._lock:
            self._keys = {k: v for k, v in self._keys.items() if v[2] > now}
            for k, (p, f, exp) in self._keys.items():
                if (p, f) == (pid, folder) and exp - now > self.TTL / 2:
                    return k
            k = secrets.token_urlsafe(18)
            self._keys[k] = (pid, folder, now + self.TTL)
            return k

    def get(self, key: str) -> tuple[str, str] | None:
        v = self._keys.get(key)
        return (v[0], v[1]) if v and v[2] > time.time() else None


sites = Sites()


def site_url(project: Project, root: str | None, rel: str) -> dict[str, Any]:
    """Where the page ``rel`` is served for its preview: what it loads by a relative URL resolves beside it."""
    root = os.path.realpath(root or project.path)
    p = project.resolve(rel, root=root)
    return {"url": f"/api/site/{sites.key(project.id, str(p.parent))}/{quote(p.name)}"}


def site_file(project: Project, folder: str, rel: str) -> tuple[Path, str]:
    p = project.resolve(os.path.join(folder, rel))
    inner = os.path.relpath(p, folder)
    if inner != "." and any(seg.startswith(".") for seg in inner.split(os.sep)):  # "..": outside it
        raise PermissionError(f"{rel} is hidden, or outside the previewed page's folder")
    if p.is_dir():
        p = p / "index.html"
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p, mime_of(p)


# ---- paths an agent writes in its replies: which are the project's files, for the chat to link them ---------------
RESOLVE_MAX = 400  # paths per request
LIST_SECONDS = 10  # a checkout's file list, for paths given by their end; `fresh` asks past it
LIST_MAX = 200_000
_lists: dict[str, tuple[float, dict[str, list[str]]]] = {}  # checkout -> (when, file name -> paths ending in it)
_lists_lock = threading.Lock()


def _files_by_name(checkout: str, fresh: bool) -> dict[str, list[str]]:
    """The checkout's files as git lists them (tracked and new, not ignored), by name; nothing outside a repository."""
    now = time.monotonic()
    with _lists_lock:
        for k in [k for k, (t, _) in _lists.items() if now - t >= LIST_SECONDS]:  # none is kept past its use
            del _lists[k]
        hit = _lists.get(checkout)
    if hit and not (fresh and now - hit[0] >= 1):
        return hit[1]
    listed = gitops._run_sync(checkout, "ls-files", "-z", "--cached", "--others", "--exclude-standard") or ""
    by_name: dict[str, list[str]] = {}
    for rel in listed.split("\0")[:LIST_MAX]:
        if rel:
            by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    with _lists_lock:
        _lists[checkout] = (time.monotonic(), by_name)
    return by_name


def resolve_paths(project: Project, cwd: str | None, paths: list[str], fresh: bool = False) -> dict[str, dict[str, str] | None]:
    """Which of ``paths``, as an agent working in ``cwd`` wrote them, are files of the project:
    ``{path: {root, path} | None}``, the root one of ``project.roots()``. A path is taken relative to ``cwd``, or
    absolute (``~/`` too) inside the project, or without a diff's ``a/`` / ``b/``; else as the end of a path
    (``api.ts``, ``chat/ChatView.tsx``) when one file of the checkout, and only one, has it. Directories are not
    files. Only whether they exist is told, nothing of what is in them."""
    roots = sorted(((os.path.realpath(r), r) for r in project.roots()), key=lambda x: len(x[0]), reverse=True)

    def owner(real: str) -> tuple[str, str] | None:  # the innermost root holding it: a worktree inside the project
        return next(((rr, r) for rr, r in roots if real == rr or real.startswith(rr + os.sep)), None)

    def found(p: str) -> dict[str, str] | None:
        real = os.path.realpath(p)
        o = owner(real)
        return {"root": o[1], "path": os.path.relpath(real, o[0])} if o and os.path.isfile(real) else None

    base = os.path.realpath(cwd) if cwd else ""
    home = owner(base) if base else None
    if not home:  # a directory that is gone (a removed worktree), or not the project's
        base = os.path.realpath(project.path)
        home = owner(base)
    checkout = home[0] if home else base
    out: dict[str, dict[str, str] | None] = {}
    for raw in paths[:RESOLVE_MAX]:
        s = raw.strip()
        if not s or len(s) > 1024 or "\0" in s:
            out[raw] = None
            continue
        if s.startswith("~/"):
            s = os.path.expanduser(s)
        if os.path.isabs(s):
            out[raw] = found(s)
            continue
        hit = found(os.path.join(base, s))
        if not hit and s.startswith(("a/", "b/")):
            hit = found(os.path.join(base, s[2:]))
        if not hit and not s.startswith(("./", "../")):
            name = s.rstrip("/").rsplit("/", 1)[-1]
            ends = [rel for rel in _files_by_name(checkout, fresh).get(name, []) if rel == s or rel.endswith("/" + s)]
            ends = [rel for rel in ends if os.path.isfile(os.path.join(checkout, rel))]  # tracked, but deleted since
            if len(ends) == 1:
                hit = found(os.path.join(checkout, ends[0]))
        out[raw] = hit
    return out


def write_file(project: Project, root: str | None, rel: str, content: str, base_sha: str | None = None, force: bool = False) -> dict[str, Any]:
    root = os.path.realpath(root or project.path)
    p = project.resolve(rel, root=root)
    if p.exists() and base_sha and not force:
        cur = sha_of(p.read_bytes())
        if cur != base_sha:
            raise Conflict(cur or "")
    p.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode()
    tmp = p.with_name(f".{p.name}.atrium-tmp")
    tmp.write_bytes(data)
    if p.exists():
        shutil.copymode(p, tmp)
    tmp.replace(p)
    return {"path": _rel(root, p), "sha": sha_of(data), "mtime": p.stat().st_mtime}


def create(project: Project, root: str | None, rel: str, kind: str) -> dict[str, Any]:
    root = os.path.realpath(root or project.path)
    p = project.resolve(rel, root=root)
    if p.exists():
        raise FileExistsError(rel)
    if kind == "dir":
        p.mkdir(parents=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    return {"path": _rel(root, p)}


def rename(project: Project, root: str | None, src: str, dst: str) -> dict[str, Any]:
    root = os.path.realpath(root or project.path)
    a = project.resolve(src, root=root)
    b = project.resolve(dst, root=root)
    if b.exists():
        raise FileExistsError(dst)
    b.parent.mkdir(parents=True, exist_ok=True)
    a.rename(b)
    return {"path": _rel(root, b)}


def trash(project: Project, root: str | None, rel: str) -> dict[str, Any]:
    """Delete by moving into ``.atrium/trash/`` so mistakes are recoverable."""
    root = os.path.realpath(root or project.path)
    p = project.resolve(rel, root=root)
    if str(p) in project.roots():
        raise PermissionError("refusing to delete a project root")
    dest = project.state_dir / "trash" / time.strftime("%Y%m%d-%H%M%S") / _rel(root, p)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(dest))
    return {"trashed_to": str(dest)}


def browse(path: str | None) -> dict[str, Any]:
    """Directory picker for choosing a project folder (any local directory)."""
    p = Path(os.path.expanduser(path or "~")).resolve()
    if not p.is_dir():
        p = p.parent
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.is_dir() and not e.name.startswith("."):
                dirs.append({"name": e.name, "path": str(Path(e.path)), "git": os.path.isdir(os.path.join(e.path, ".git"))})
    except PermissionError:
        pass
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs, "git": (p / ".git").exists()}
