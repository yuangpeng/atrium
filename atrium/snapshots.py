"""Git snapshots — the primary baseline for per-session diffs.

When a turn starts and when it ends, Atrium records the session's whole
checkout as a git *tree object* without touching the user's index, branches,
stash or refs: the real index is copied to a temp file, ``git add -A`` runs
against the copy (``.gitignore`` applies; unchanged files are not re-hashed
thanks to the copied stat cache) and ``git write-tree`` returns the tree id.

No refs are created, so snapshots never appear in the commit graph. The objects
are unreachable and git may prune them after its prune window (~2 weeks);
diffs then fall back to what the transcript recorded.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from typing import Any

from .config import RUNTIME_DIR

log = logging.getLogger("atrium.snapshots")

SNAP_TIMEOUT = 30
_SNAP_TMP = RUNTIME_DIR / "snapshots"
_blob_cache: OrderedDict[tuple[str, str, str], str | None] = OrderedDict()
_BLOB_CACHE_MAX = 512


def _git(cwd: str, *args: str, env: dict[str, str] | None = None, timeout: float = 10, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=text, timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", **(env or {})},
    )


def take(cwd: str, phase: str) -> dict[str, Any] | None:
    """Snapshot the checkout containing ``cwd``. Returns ``{tree, root, ts, phase}`` or None."""
    try:
        r = _git(cwd, "rev-parse", "--show-toplevel", "--path-format=absolute", "--git-path", "index")
        if r.returncode != 0:
            return None
        root, index = r.stdout.strip().splitlines()[:2]
        _SNAP_TMP.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_SNAP_TMP, suffix=".index")
        os.close(fd)
        try:
            if os.path.exists(index):
                shutil.copyfile(index, tmp)
            else:
                os.unlink(tmp)  # git creates a fresh index
            env = {"GIT_INDEX_FILE": tmp}
            add = _git(root, "add", "-A", env=env, timeout=SNAP_TIMEOUT)
            if add.returncode != 0:
                log.debug("snapshot add failed in %s: %s", root, add.stderr.strip()[:300])
                return None
            tree = _git(root, "write-tree", env=env, timeout=SNAP_TIMEOUT)
            if tree.returncode != 0:
                return None
            return {"tree": tree.stdout.strip(), "root": root, "ts": time.time(), "phase": phase}
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        log.debug("snapshot failed in %s: %s", cwd, e)
        return None


def read_blob(root: str, tree: str, rel: str) -> tuple[bool, str | None]:
    """(present, content) of ``rel`` in ``tree``. present=False → not in the snapshot (or pruned)."""
    key = (root, tree, rel)
    if key in _blob_cache:
        _blob_cache.move_to_end(key)
        v = _blob_cache[key]
        return v is not None, v
    try:
        r = _git(root, "cat-file", "blob", f"{tree}:{rel}", text=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    content = r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None
    _blob_cache[key] = content
    if len(_blob_cache) > _BLOB_CACHE_MAX:
        _blob_cache.popitem(last=False)
    return content is not None, content


def tree_exists(root: str, tree: str) -> bool:
    try:
        return _git(root, "cat-file", "-e", f"{tree}^{{tree}}").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def restore(root: str, tree: str) -> None:
    """Make the files of the checkout at ``root`` match a snapshot (a forked worktree starts from
    its source's files). Index and HEAD are left alone: the differences show up as changes."""
    r = _git(root, "restore", f"--source={tree}", "--worktree", "--", ".", timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"couldn't restore the files: {r.stderr.strip()[:300]}")


def changed_paths(root: str, a: str, b: str) -> list[str]:
    """Paths (absolute) that differ between two snapshot trees."""
    try:
        r = _git(root, "diff-tree", "-r", "--name-only", "-z", "--no-renames", a, b, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return [os.path.join(root, p) for p in r.stdout.split("\0") if p]


def is_ignored(root: str, rel: str) -> bool:
    try:
        return _git(root, "check-ignore", "-q", "--", rel).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def head_blob(root: str, rel: str) -> tuple[bool, str | None]:
    try:
        r = _git(root, "cat-file", "blob", f"HEAD:{rel}", text=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    return (True, r.stdout.decode("utf-8", errors="replace")) if r.returncode == 0 else (False, None)
