"""The pages the Board panel offers besides the board: the HTML files in the project and its worktrees.

Git lists them where it can: what it tracks and what is new, never reading what it ignores. A report is often
written where git ignores it (``htmlcov/``, ``outputs/``), so those directories get a look of their own, a short
one: breadth first, a few levels down, and a budget of entries per checkout, since an ignored directory may hold
hundreds of thousands of run logs on a network disk (see ``watch.py``). In a worktree only what is new or changed
there is listed; the files it shares with the project are listed once, under the project.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..project import Project

SUFFIXES = (".html", ".htm")
SKIP = frozenset((".git", ".atrium", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", ".next", ".cache", "site-packages"))
MAX_PAGES = 300
SCAN_ENTRIES = 5000  # entries read per checkout in what git ignores (or in all of it, outside a repository)
SCAN_DEPTH = 4  # levels below an ignored directory
GIT_TIMEOUT = 20
CACHE_SECONDS = 10  # the picker is opened again and again; a page that has just appeared waits this long at most

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _git_ls(root: str, *args: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z", *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in r.stdout.split("\0") if p] if r.returncode == 0 else None


def _skipped(rel: str) -> bool:
    return any(part in SKIP for part in rel.split("/"))


def _scan(root: str, dirs: list[str], depth: int) -> list[str]:
    """HTML files under ``dirs`` (relative to ``root``), breadth first, ``depth`` levels down, ``SCAN_ENTRIES`` at most."""
    out: list[str] = []
    budget = SCAN_ENTRIES
    queue = deque((d, 0) for d in dirs)
    while queue and budget > 0:
        rel, level = queue.popleft()
        try:
            with os.scandir(os.path.join(root, rel)) as it:
                for e in it:
                    budget -= 1
                    if budget <= 0:
                        break
                    sub = f"{rel}/{e.name}" if rel else e.name
                    if e.name in SKIP:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if level < depth:
                            queue.append((sub, level + 1))
                    elif e.name.lower().endswith(SUFFIXES):
                        out.append(sub)
        except OSError:
            continue
    return out


def _checkout(root: str, main: bool) -> list[dict[str, Any]]:
    globs = [f"*{s}" for s in SUFFIXES]
    # the project: everything; a worktree: what is new or changed in it
    listed = _git_ls(root, *(("--cached", "--others") if main else ("--others", "--modified")), "--exclude-standard", "--", *globs)
    if listed is None:  # not a repository, or git couldn't say
        rels = _scan(root, [""], 8)
    else:
        ignored = _git_ls(root, "--others", "--ignored", "--exclude-standard", "--directory") or []
        rels = listed + [p for p in ignored if p.lower().endswith(SUFFIXES)]
        rels += _scan(root, [p.rstrip("/") for p in ignored if p.endswith("/") and not _skipped(p.rstrip("/"))], SCAN_DEPTH)
    pages = []
    for rel in dict.fromkeys(rels):
        if _skipped(rel) or os.path.basename(rel).startswith("."):  # a hidden file: /file/site won't serve it
            continue
        try:
            st = os.stat(os.path.join(root, rel))  # --cached and --modified list deleted files too
        except OSError:
            continue
        pages.append({"root": root, "path": rel, "mtime": st.st_mtime, "size": st.st_size})
    return pages


def list_pages(project: "Project") -> dict[str, Any]:
    """The project's HTML files, the latest first: ``{"pages": [{root, path, mtime, size}], "truncated": bool}``."""
    roots = project.roots()
    now = time.monotonic()

    def one(root: str) -> list[dict[str, Any]]:
        hit = _cache.get(root)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        pages = _checkout(root, root == project.path)
        _cache[root] = (time.monotonic(), pages)
        return pages

    with ThreadPoolExecutor(max_workers=min(8, len(roots))) as pool:
        pages = [p for found in pool.map(one, roots) for p in found]
    pages.sort(key=lambda p: -p["mtime"])
    return {"pages": pages[:MAX_PAGES], "truncated": len(pages) > MAX_PAGES}
