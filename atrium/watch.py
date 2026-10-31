"""File watchers: project trees (editor/git refresh) and agent transcripts (session list refresh).

Both watch a list of directories, each on its own, not a tree. inotify watches one directory at a
time, so for a recursive watch watchfiles first walks the whole tree, stat'ing every entry, and
holds the GIL until it is done: the event loop stands still. On a network disk the ignored run logs
of one project (hundreds of thousands of files) made that a quarter of an hour, and the server
didn't listen until every project's watch was up. The list (the plan) is made in a thread instead,
reading each directory once and leaving out what git ignores, ``node_modules`` and the like.

So nothing git ignores is watched: a log open in the editor from an ignored ``runs/`` doesn't follow
what is written to it, nor does the tree under it, until something else changes. That is the price
of not watching hundreds of thousands of files.

A directory that appears under a watched one makes a new plan while the watch goes on; then the
watch moves over to it with no gap, and catches up with what it couldn't see (``_DirWatcher``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from anyio import to_thread
from watchfiles import Change
from watchfiles._rust_notify import RustNotify
from watchfiles.main import _default_force_polling  # what awatch makes of force_polling=None: the env var, or WSL

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
# Watched in a project at most: inotify has a limit, and the event loop is held while the watches are added (~15µs each).
# Only reached when git can't say what it ignores, or in a very big tree.
_MAX_DIRS = 10_000
_GIT_TIMEOUT = 120  # seconds for git to say what it ignores: it reads every directory it doesn't, on a cold network disk
_REPLAN_GAP = 2.0  # seconds from one move of the watch to the next: a burst of new directories is planned once
_TICK_MS = 1000  # a watch hands back at least this often, to move over to a new plan
_RETRY_FIRST, _RETRY_MAX = 60, 1800  # seconds before trying again after a failure, twice as long each time in a row


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


class _Plan(NamedTuple):
    dirs: list[str]  # to watch
    seen: frozenset[str]  # every directory the walk came across, watched or left out


_said: dict[str, str] = {}  # the last warning about something, not to repeat it at every plan
_known_ignored: dict[str, set[str]] = {}  # repository -> what git said it ignores, the last time it could


def _say(key: str, warning: str | None) -> None:
    """Log ``warning`` about ``key`` unless it is what was said about it last; None: all well again."""
    if warning is None:
        _said.pop(key, None)
    elif _said.get(key) != warning:
        _said[key] = warning
        log.warning("%s", warning)


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


def _git_ignored(d: str) -> set[str]:
    """The directories git ignores under ``d``, whole (git doesn't look inside them either); none outside
    a repository. When git can't tell, a warning says why, and what it said last time stands in."""
    try:
        r = subprocess.run(["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
                           cwd=d, capture_output=True, timeout=_GIT_TIMEOUT, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as e:  # no git, or it took too long
        error = str(e)
    else:
        if r.returncode == 0:
            found = _known_ignored[d] = {os.path.join(d, os.fsdecode(p[:-1])) for p in r.stdout.split(b"\0") if p.endswith(b"/")}
            _say(f"git {d}", None)
            return found
        error = r.stderr.decode(errors="replace").strip() or f"exit status {r.returncode}"
    if "not a git repository" in error:
        return set()
    known = _known_ignored.get(d)
    instead = "what it said before is left out" if known is not None else "all of it is watched (up to the limit)"
    _say(f"git {d}", f"file watch: git can't say what it ignores in {d}, so {instead}: {error}")
    return known or set()


def _project_dirs(project: Project) -> _Plan:
    """The directories to watch: the project's and each worktree's (a root of its own), less what git
    ignores there or in a submodule (it has ignores of its own), at most ``_MAX_DIRS``, the shallowest
    first; and in .git what ``_project_filter`` lets through (HEAD, index, refs/, worktrees/)."""
    roots = project.roots()
    if _NATIVE_TREES:
        top = [r for r in roots if r == project.path or not r.startswith(project.path + os.sep)]
        return _Plan(top, frozenset(top))
    out: list[str] = []
    for root in roots:
        git = os.path.join(root, ".git")
        if os.path.isdir(git):
            out += [git, *_walk(os.path.join(git, "refs")), *_walk(os.path.join(git, "worktrees"))]
    seen, ignored = set(out) | set(roots), set()
    queue = deque(roots)  # breadth first: past the limit, the deepest go unwatched
    while queue and len(out) < _MAX_DIRS:
        d = queue.popleft()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:  # gone, or not ours to read
            continue
        out.append(d)
        if d in roots or any(e.name == ".git" for e in entries):
            ignored |= _git_ignored(d)
        for e in entries:
            if e.path not in seen and e.is_dir(follow_symlinks=False):
                seen.add(e.path)
                if e.name not in _NOT_WALKED and e.path not in ignored:
                    queue.append(e.path)
    _say(f"limit {project.path}", f"file watch: more than {_MAX_DIRS} directories in {project.path}, the deepest aren't watched" if queue else None)
    return _Plan(list(dict.fromkeys(out)), frozenset(seen))


def _within(path: str, tops: set[str]) -> bool:
    """Whether ``path`` is one of ``tops`` or under one."""
    while tops:
        if path in tops:
            return True
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return False


class _DirWatcher:
    """Watches the directories a plan lists (``_make_plan``), each on its own, and hands their changes
    to ``_changed``.

    A directory that appears under a watched one, or ``replan()``, makes a new plan while the watch goes
    on. The new watch is up before the old one goes (``_move``), so nothing falls in between. Then the
    directories it adds are read again (``_check``): what came into them before their watch was up goes
    to ``_missed``, and one the plan doesn't know of (made after the walk went by) takes another plan.
    After a failure it tries again later, twice as late each time; the watch in place, if any, goes on.
    """

    def __init__(self, name: str, debounce: int, step: int, watch_filter: Callable[[Change, str], bool] | None = None):
        self._name, self._debounce, self._step, self._watch_filter = name, debounce, step, watch_filter
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()  # a plan is ready, or stop
        self._task: asyncio.Task[None] | None = None
        self._planning: asyncio.Task[None] | None = None
        self._walking = False  # the plan being made is reading the disk
        self._again = False  # asked for meanwhile: that plan may have missed what this one is for
        self._fresh: _Plan | None = None  # made, not watched yet
        self._plan: _Plan | None = None  # watched
        self._appeared: set[str] = set()  # directories that came (or came back: a new one, unwatched) since the last move
        self._checks: set[asyncio.Task[None]] = set()
        self._since = 0.0  # when the watch last moved
        self._retry_at = 0.0  # no plan, nor watch, before this: after a failure
        self._fails = 0  # in a row
        self._failure = ""  # the last one, logged with its traceback

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=self._name)
        self.replan()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in (self._planning, *self._checks):
            if t:
                t.cancel()

    def replan(self) -> None:
        """Make a new plan: soon, but not within ``_REPLAN_GAP`` of the last move nor before a retry is due."""
        if self._stop.is_set():
            return
        if self._planning and not self._planning.done():
            self._again |= self._walking  # not started yet: it will see what this is for
        else:
            self._planning = asyncio.create_task(self._replan())

    async def _replan(self) -> None:
        while True:
            await asyncio.sleep(max(self._since + _REPLAN_GAP, self._retry_at) - time.monotonic())
            self._walking = True
            try:
                self._fresh = await self._make_plan()
                break
            except Exception as e:
                self._failed("can't list what to watch", e)
            finally:
                self._walking = False
        self._wake.set()

    async def _make_plan(self) -> _Plan:
        raise NotImplementedError

    async def _changed(self, changes: set[tuple[Change, str]]) -> None:
        raise NotImplementedError

    async def _missed(self, paths: list[str]) -> None:
        """What is in directories that came before their watch was up: it may have changed unseen."""
        await self._changed({(Change.added, p) for p in paths})

    def _watchable(self, path: str) -> bool:
        """Whether a directory that appeared would be in the next plan (it is worth one)."""
        return True

    def _failed(self, what: str, e: BaseException) -> None:
        self._fails += 1
        delay = min(_RETRY_FIRST * 2 ** (self._fails - 1), _RETRY_MAX)
        self._retry_at = time.monotonic() + delay
        failure = f"{what}: {type(e).__name__}: {e}"
        if failure != self._failure:  # its traceback once, then a line each time
            self._failure = failure
            log.warning("%s: %s; trying again in %ds", self._name, what, delay, exc_info=e)
        else:
            log.warning("%s: %s; trying again in %ds", self._name, failure, delay)

    async def _run(self) -> None:
        watcher: RustNotify | None = None
        try:
            while not self._stop.is_set():
                if self._fresh is not None:
                    watcher = await self._move(watcher)
                if watcher is None:
                    self._wake.clear()
                    if self._fresh is None:
                        await self._wake.wait()
                    continue
                try:
                    # in anyio's worker threads, as awatch did: one per watch all the time would fill asyncio's default pool
                    raw = await to_thread.run_sync(watcher.watch, self._debounce, self._step, _TICK_MS, self._stop)
                except Exception as e:  # the watch broke: another, later
                    watcher.close()
                    watcher = None
                    self._failed("the watch stopped", e)
                    self.replan()
                    continue
                await self._take(raw)
        finally:
            if watcher is not None:
                watcher.close()

    async def _take(self, raw: set[tuple[int, str]] | str) -> None:
        """Hand on what a watch caught (nothing on 'timeout', 'stop')."""
        if not isinstance(raw, set):
            return
        changes = {(Change(c), p) for c, p in raw}
        if self._watch_filter:
            changes = {(c, p) for c, p in changes if self._watch_filter(c, p)}
        if not changes:
            return
        try:
            added = [p for c, p in changes if c == Change.added and self._watchable(p)]
            if added and not _NATIVE_TREES:
                made = await asyncio.to_thread(lambda: [p for p in added if os.path.isdir(p) and not os.path.islink(p)])
                if made:  # to watch too, or again: the watch on the directory it replaces went with it
                    self._appeared.update(made)
                    self.replan()
            await self._changed(changes)
        except Exception:
            log.exception("%s: can't hand on %d changes", self._name, len(changes))

    @staticmethod
    def _notify(dirs: list[str]) -> RustNotify:
        # on the event loop: it holds the GIL while it adds the watches anyway (not recursive: no walk, ~15µs each)
        return RustNotify(dirs, False, _default_force_polling(None), 300, _NATIVE_TREES, False)

    async def _open(self, plan: _Plan) -> tuple[RustNotify | None, _Plan]:
        """A watch of the plan's directories (None for none), less those gone since it was made."""
        if not plan.dirs:
            return None, plan
        try:
            return self._notify(plan.dirs), plan
        except FileNotFoundError:
            plan = plan._replace(dirs=await asyncio.to_thread(lambda: [d for d in plan.dirs if os.path.isdir(d)]))
            return (self._notify(plan.dirs) if plan.dirs else None), plan

    async def _drain(self, watcher: RustNotify) -> set[tuple[int, str]] | str:
        """What a watch caught and hasn't handed on (it waits a step for it, and out its debounce)."""
        try:
            return await to_thread.run_sync(watcher.watch, self._debounce, self._step, 1, self._stop)
        except Exception:  # it broke: the new one is up already
            return "error"

    async def _move(self, old: RustNotify | None) -> RustNotify | None:
        """Watch the fresh plan: the new watch is up before the old one goes, and what the old one caught
        until then is handed on. If there can't be a new one, the old one stays."""
        plan, self._fresh = self._fresh, None
        assert plan is not None
        try:
            try:
                new, plan = await self._open(plan)
            except OSError as e:
                if old is None or isinstance(e, FileNotFoundError):
                    raise
                # past inotify's limit with both watches up? The new one alone (a gap, then), or else the old one again
                await self._take(await self._drain(old))
                old.close()
                old = None
                try:
                    new, plan = await self._open(plan)
                except OSError:
                    old = (await self._open(self._plan))[0] if self._plan else None
                    raise
        except Exception as e:
            self._failed("can't watch", e)
            self.replan()
            return old
        self._since = time.monotonic()
        self._fails, self._failure = 0, ""
        if old is not None:
            await self._take(await self._drain(old))
            old.close()
        prev, self._plan = self._plan, plan
        appeared, self._appeared = self._appeared, set()
        check = asyncio.create_task(self._check(prev, plan, appeared))
        self._checks.add(check)
        check.add_done_callback(self._checks.discard)
        if self._again:
            self._again = False
            self.replan()
        return new

    async def _check(self, prev: _Plan | None, plan: _Plan, appeared: set[str]) -> None:
        """Read again, the new watch being up, what the old one didn't cover: the directories new to the
        plan, and those that came back (``appeared``, with what is under them). What is in them may have
        come before the watch, unseen (``_missed``; not after the first plan, before which nobody looked);
        a directory the plan doesn't know of was made after the walk went by, and takes another plan."""
        before = set(prev.dirs) if prev else set()
        fresh = [d for d in plan.dirs if d not in before or _within(d, appeared)]

        def scan() -> tuple[list[str], bool]:
            found, unknown = [], False
            for d in fresh:
                try:
                    with os.scandir(d) as it:
                        entries = list(it)
                except OSError:
                    continue
                found += [e.path for e in entries]
                unknown = unknown or any(e.path not in plan.seen and e.is_dir(follow_symlinks=False) and self._watchable(e.path) for e in entries)
            return found, unknown

        if not fresh:
            return
        try:
            found, unknown = await asyncio.to_thread(scan)
            if unknown and not _NATIVE_TREES:
                self.replan()
            if found and prev is not None:
                await self._missed(found)
        except Exception:
            log.exception("%s: can't check the new watch", self._name)


class ProjectWatcher(_DirWatcher):
    def __init__(self, project: Project):
        super().__init__(f"watch-{project.id}", debounce=400, step=100, watch_filter=_project_filter)
        self.project = project

    async def _make_plan(self) -> _Plan:
        return await asyncio.to_thread(_project_dirs, self.project)

    def _watchable(self, path: str) -> bool:
        if "/.git/" in path:  # only refs/ and worktrees/ (and worktrees itself) are watched there
            return _project_filter(Change.added, path)
        return os.path.basename(path) not in _NOT_WALKED

    async def _missed(self, paths: list[str]) -> None:
        topic = f"project:{self.project.id}"
        files = [p for p in paths if "/.git/" not in p and _project_filter(Change.added, p)]
        if files:
            hub.publish(topic, {"type": "fs", "paths": files[:200]})
        hub.publish(topic, {"type": "git_changed"})

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

    async def _make_plan(self) -> _Plan:
        bases = self._bases()
        dirs = bases if _NATIVE_TREES else await asyncio.to_thread(lambda: list(dict.fromkeys(d for b in bases for d in _walk(b))))
        return _Plan(dirs, frozenset(dirs))

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
