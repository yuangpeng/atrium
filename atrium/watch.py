"""File watchers: project trees (editor/git refresh) and agent transcripts (session list refresh).

Both watch a list of directories, each on its own, not a tree. inotify watches one directory at a
time, so for a recursive watch watchfiles first walks the whole tree, stat'ing every entry, and
holds the GIL until it is done: the event loop stands still. On a network disk the ignored run logs
of one project (hundreds of thousands of files) made that a quarter of an hour, and the server
didn't listen until every project's watch was up. The list is made in a thread instead, reading
each directory once and leaving out what git ignores, ``node_modules`` and the like; it is made
again, while the watch goes on, when a directory appears under a watched one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from watchfiles import Change, awatch

from . import gitops
from .events import hub
from .harness import all_harnesses
from .project import Project

log = logging.getLogger("atrium.watch")

_SKIP_DIRS = frozenset(("node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".next", "target"))
_IGNORED_PARTS = tuple(f"/{d}/" for d in _SKIP_DIRS)
# Not walked in a project: .git is watched in part (_project_dirs); .atrium only holds worktrees, roots of their own.
_NOT_WALKED = _SKIP_DIRS | {".git", ".atrium"}
# FSEvents and ReadDirectoryChangesW watch a tree as cheaply as a directory, with no walk: the roots are watched as they are
_NATIVE_TREES = sys.platform in ("darwin", "win32")
_REPLAN_GAP = 2.0  # seconds from one watch to the next: a burst of new directories is planned once


def _project_filter(change: Change, path: str) -> bool:
    if any(p in path for p in _IGNORED_PARTS):
        return False
    if "/.atrium/" in path and "/.atrium/worktrees/" not in path:
        return False
    if "/.git/" in path:
        tail = path.split("/.git/", 1)[1]
        # worktrees itself: made by the first `git worktree add`, the watch on .git only sees it come
        return tail in ("HEAD", "index", "worktrees") or tail.startswith("refs/") or tail.startswith("worktrees/")
    return not path.endswith((".atrium-tmp", ".swp", "~"))


def _walk(top: str) -> list[str]:
    """``top`` and every directory under it. Reads each directory once, stats nothing (the entry's
    type comes with it) and doesn't follow symlinks."""
    out, stack = [], [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                subs = [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:  # gone, or not ours to read
            continue
        out.append(d)
        stack.extend(subs)
    return out


def _git_ignored(root: str) -> set[str]:
    """The directories git ignores under ``root``, whole (git doesn't look inside them either); none outside a repository."""
    text = gitops._run_sync(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
    return {os.path.join(root, p[:-1]) for p in (text or "").split("\0") if p.endswith("/")}


def _project_dirs(project: Project) -> list[str]:
    """The directories to watch: the project's and each worktree's (a root of its own), less what git
    ignores there or in a submodule (it has ignores of its own); and in .git what ``_project_filter``
    lets through (HEAD, index, refs/, worktrees/)."""
    roots = project.roots()
    if _NATIVE_TREES:
        return [r for r in roots if r == project.path or not r.startswith(project.path + os.sep)]
    out: list[str] = []
    skip = set(roots)  # the other roots: walked on their own
    for root in roots:
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError:
                continue
            out.append(d)
            if d == root or any(e.name == ".git" for e in entries):
                skip |= _git_ignored(d)
            stack += [e.path for e in entries if e.is_dir(follow_symlinks=False) and e.name not in _NOT_WALKED and e.path not in skip]
        git = os.path.join(root, ".git")
        if os.path.isdir(git):
            out += [git] + _walk(os.path.join(git, "refs")) + _walk(os.path.join(git, "worktrees"))
    return list(dict.fromkeys(out))


class _DirWatcher:
    """Watches the directories ``_plan`` lists, each on its own, and hands their changes to ``_changed``.

    A new plan is made, while the watch goes on, when a directory appears under a watched one (to
    watch it too) or ``replan()`` is called; the watch then moves over to it.
    """

    def __init__(self, name: str, debounce: int, step: int, watch_filter: Callable[[Change, str], bool] | None = None):
        self._name, self._debounce, self._step, self._watch_filter = name, debounce, step, watch_filter
        self._stop = asyncio.Event()
        self._restart = asyncio.Event()  # a new plan is ready, or stop
        self._task: asyncio.Task | None = None
        self._planning: asyncio.Task | None = None
        self._again = False  # asked for during a plan, which may have missed what it was for
        self._fresh: list[str] | None = None
        self._since = 0.0  # when the current watch started

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=self._name)

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        if self._planning:
            self._planning.cancel()

    def replan(self) -> None:
        if self._planning and not self._planning.done():
            self._again = True
        else:
            self._planning = asyncio.create_task(self._replan())

    async def _replan(self) -> None:
        await asyncio.sleep(self._since + _REPLAN_GAP - time.monotonic())
        try:
            self._fresh = await self._plan()
        except Exception:
            log.exception("%s: can't list what to watch", self._name)
            return
        self._restart.set()

    async def _plan(self) -> list[str]:
        raise NotImplementedError

    async def _changed(self, changes: set[tuple[Change, str]]) -> None:
        raise NotImplementedError

    def _watchable(self, path: str) -> bool:
        """Whether a directory that appeared would be in the next plan (it is worth one)."""
        return True

    async def _run(self) -> None:
        dirs: list[str] | None = None
        while not self._stop.is_set():
            self._restart.clear()
            self._since = time.monotonic()
            try:
                if dirs is None:
                    dirs = await self._plan()
                if not dirs:
                    await self._restart.wait()
                else:
                    async for changes in awatch(*dirs, watch_filter=self._watch_filter, stop_event=self._restart,
                                                debounce=self._debounce, step=self._step, recursive=_NATIVE_TREES):
                        # a directory made (or made again: the watch on the one it replaces went with it)
                        added = [p for c, p in changes if c == Change.added and self._watchable(p)]
                        if added and not _NATIVE_TREES and await asyncio.to_thread(lambda: any(os.path.isdir(p) and not os.path.islink(p) for p in added)):
                            self.replan()
                        await self._changed(changes)
            except FileNotFoundError:  # a directory went away between the plan and the watch
                await asyncio.sleep(1)
            except Exception:  # e.g. inotify's limit on watches: not again and again on every directory
                log.exception("%s: watcher error", self._name)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._restart.wait(), 60)
            if self._stop.is_set():
                break
            dirs, self._fresh = self._fresh, None  # None: plan again first
            if self._again:
                self._again = False
                self.replan()


class ProjectWatcher(_DirWatcher):
    def __init__(self, project: Project):
        super().__init__(f"watch-{project.id}", debounce=400, step=100, watch_filter=_project_filter)
        self.project = project

    async def _plan(self) -> list[str]:
        return await asyncio.to_thread(_project_dirs, self.project)

    def _watchable(self, path: str) -> bool:
        return os.path.basename(path) not in _NOT_WALKED

    async def _changed(self, changes: set[tuple[Change, str]]) -> None:
        topic = f"project:{self.project.id}"
        files: list[str] = []
        git_changed = worktrees_changed = False
        for change, path in changes:
            if "/.git/" in path:
                git_changed = True
                tail = path.split("/.git/", 1)[1]
                # a worktree added or removed: its admin dir worktrees/<name> (with its gitdir file) comes or goes,
                # or worktrees itself, with the first one in it
                if change != Change.modified and (tail == "worktrees" or tail.startswith("worktrees/") and (tail.count("/") == 1 or tail.endswith("/gitdir"))):
                    worktrees_changed = True
                continue
            files.append(path)
        if files:
            hub.publish(topic, {"type": "fs", "paths": files[:200]})
        if files or git_changed:
            hub.publish(topic, {"type": "git_changed"})
        if worktrees_changed:  # the worktree pickers, and agents whose worktree went away; and a root to watch, or one less
            hub.publish(topic, {"type": "worktrees_changed"})
            self.replan()


class TranscriptWatcher(_DirWatcher):
    """One watcher over every harness's transcripts; maps changes to projects.

    Per-project transcript directories (Claude Code: ``~/.claude/projects/<key>``)
    map directly. Shared trees (Codex: ``~/.codex/sessions/YYYY/MM/DD``) are mapped
    through the session's working directory, read from the transcript itself.
    """

    def __init__(self) -> None:
        super().__init__("watch-transcripts", debounce=1500, step=200)
        self._dirs: dict[str, set[str]] = {}  # per-project transcript dir -> project ids
        self._projects: dict[str, Project] = {}
        self._roots: dict[str, tuple[float, list[str]]] = {}  # project id -> (fetched_at, roots)

    def add_project(self, project: Project) -> None:
        self._projects[project.id] = project
        for h in all_harnesses():
            for d in h.transcript_dirs(project.path):
                self._dirs.setdefault(str(d), set()).add(project.id)
        self.replan()

    def _bases(self) -> list[str]:
        bases = {str(Path(d).parent) for d in self._dirs}
        for h in all_harnesses():
            bases |= {str(r) for r in h.watch_roots()}
        return [b for b in bases if os.path.isdir(b)]

    async def _plan(self) -> list[str]:
        bases = self._bases()
        if _NATIVE_TREES:
            return bases
        return await asyncio.to_thread(lambda: list(dict.fromkeys(d for b in bases for d in _walk(b))))

    def _project_roots(self, pid: str) -> list[str]:
        hit = self._roots.get(pid)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        roots = self._projects[pid].roots()
        self._roots[pid] = (time.time(), roots)
        return roots

    def _owners(self, path: str) -> set[str]:
        parent = str(Path(path).parent)
        while parent and parent not in self._dirs and parent != "/":
            parent = str(Path(parent).parent)
        if parent in self._dirs:
            return self._dirs[parent]
        for h in all_harnesses():
            if not any(path.startswith(str(r) + os.sep) for r in h.watch_roots()):
                continue
            cwd = h.transcript_cwd(path)
            if not cwd:
                return set()
            cwd = os.path.realpath(cwd)
            return {pid for pid in self._projects if any(cwd == r or cwd.startswith(r + os.sep) for r in self._project_roots(pid))}
        return set()

    async def _changed(self, changes: set[tuple[Change, str]]) -> None:
        paths = {path for _c, path in changes}
        hit: set[str] = set()
        for owners in await asyncio.to_thread(lambda: [self._owners(p) for p in paths]):
            hit |= owners
        for pid in hit:
            hub.publish(f"project:{pid}", {"type": "sessions_changed", "source": "transcripts"})


transcript_watcher = TranscriptWatcher()
