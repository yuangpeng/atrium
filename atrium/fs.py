"""File-system access for the editor (scoped to a project and its worktrees)."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

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
