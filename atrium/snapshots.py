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

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

from .config import RUNTIME_DIR

log = logging.getLogger("atrium.snapshots")

SNAP_TIMEOUT = 30
_SNAP_TMP = RUNTIME_DIR / "snapshots"
_blob_cache: OrderedDict[tuple[str, str, str], str | None] = OrderedDict()
_BLOB_CACHE_MAX = 512
# (checkout read from, root, tree, tree) -> the paths that differ: trees don't change, and every look at a session's
# changes asks again for each of its turns
_changed_cache: dict[tuple[str, str, str, str], list[str]] = {}
_CHANGED_CACHE_MAX = 4096


def _git(cwd: str, *args: str, env: dict[str, str] | None = None, timeout: float = 10, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=text, timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", **(env or {})},
    )


class _Batch:
    """One ``git cat-file --batch`` in a checkout, read object after object: a git process each takes milliseconds,
    and the files of hundreds of agents make that many seconds. A read that takes longer than a git call may (``_git``)
    kills it: the read fails, and the caller runs git itself (a git that hangs, e.g. fetching a missing object from a
    promisor remote, would otherwise hold the Changes inbox and the Tracker for good)."""

    TIMEOUT = 10

    def __init__(self, root: str):
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
        )
        self.deadline: float | None = None  # when the read under way is given up
        threading.Thread(target=self._watch, name="git-batch-watch", daemon=True).start()

    def _watch(self) -> None:
        while self.proc.poll() is None:
            time.sleep(0.5)
            if self.deadline is not None and time.monotonic() > self.deadline:
                self.proc.kill()

    def read(self, spec: str) -> tuple[str, bytes] | None:
        """(type, content) of the object ``spec`` names (``<tree>:<path>``…); None: there is none."""
        stdin, stdout = self.proc.stdin, self.proc.stdout
        assert stdin and stdout
        self.deadline = time.monotonic() + self.TIMEOUT
        try:
            stdin.write(spec.encode() + b"\n")
            stdin.flush()
            head = stdout.readline()
            if not head:
                raise OSError("git cat-file --batch stopped")
            if not head.endswith(b"\n") or len(fields := head.split()) != 3 or not fields[2].isdigit():
                return None  # "<spec> missing", "ambiguous"…
            size = int(fields[2])
            data = stdout.read(size + 1)  # and the newline after it
            if len(data) != size + 1:
                raise OSError("git cat-file --batch stopped")
            return fields[1].decode(), data[:-1]
        finally:
            self.deadline = None

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        if self.proc.returncode is None:
            self.proc.kill()


_local = threading.local()


@contextlib.contextmanager
def batch() -> Iterator[None]:
    """Within it, this thread's reads of git objects (read_blob, head_blob, tree_exists) go through one
    ``git cat-file --batch`` per checkout, started when first needed."""
    if getattr(_local, "batches", None) is not None:  # inside another: its processes
        yield
        return
    _local.batches = {}
    try:
        yield
    finally:
        batches, _local.batches = _local.batches, None
        for b in batches.values():
            if b is not None:
                b.close()


_NOT_BATCHED = object()


def _batched(root: str, spec: str) -> Any:
    """The object ``spec`` names, read through the batch round this call (see ``read``); _NOT_BATCHED: there is no
    batch to read it with (or it failed), so the caller runs git itself."""
    batches: dict[str, _Batch | None] | None = getattr(_local, "batches", None)
    if batches is None or "\n" in spec or "\r" in spec:  # (one per line, a line's \r dropped)
        return _NOT_BATCHED
    if root in batches and batches[root] is None:  # it failed there: git itself, from then on
        return _NOT_BATCHED
    b = batches.get(root)
    try:
        if b is None:
            b = batches[root] = _Batch(root)
        return b.read(spec)
    except (OSError, ValueError):
        batches[root] = None
        if b is not None:
            b.close()
        return _NOT_BATCHED


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
    got = _batched(root, f"{tree}:{rel}")
    if got is not _NOT_BATCHED:
        content = got[1].decode("utf-8", errors="replace") if got and got[0] == "blob" else None
    else:
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
    got = _batched(root, f"{tree}^{{tree}}")
    if got is not _NOT_BATCHED:
        return bool(got and got[0] == "tree")
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


def changed_paths(root: str, a: str, b: str, git: str | None = None) -> list[str]:
    """Paths (absolute, under ``root``) that differ between two snapshot trees; ``git``: the checkout
    to read them from, if not ``root`` (removed since)."""
    key = (git or root, root, a, b)
    hit = _changed_cache.get(key)
    if hit is not None:
        return hit
    try:
        r = _git(git or root, "diff-tree", "-r", "--name-only", "-z", "--no-renames", a, b, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    out = _changed_cache[key] = [os.path.join(root, p) for p in r.stdout.split("\0") if p]
    while len(_changed_cache) > _CHANGED_CACHE_MAX:  # the oldest go
        _changed_cache.pop(next(iter(_changed_cache)), None)
    return out


def is_ignored(root: str, rel: str) -> bool:
    try:
        return _git(root, "check-ignore", "-q", "--", rel).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def head_blob(root: str, rel: str) -> tuple[bool, str | None]:
    got = _batched(root, f"HEAD:{rel}")
    if got is not _NOT_BATCHED:
        return (True, got[1].decode("utf-8", errors="replace")) if got and got[0] == "blob" else (False, None)
    try:
        r = _git(root, "cat-file", "blob", f"HEAD:{rel}", text=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    return (True, r.stdout.decode("utf-8", errors="replace")) if r.returncode == 0 else (False, None)
