"""File watchers: project trees (editor/git refresh) and agent transcripts (session list refresh)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from watchfiles import Change, awatch

from .events import hub
from .harness import all_harnesses
from .project import Project

log = logging.getLogger("atrium.watch")

_IGNORED_PARTS = ("/node_modules/", "/.venv/", "/venv/", "/__pycache__/", "/.mypy_cache/", "/.pytest_cache/", "/dist/", "/build/", "/.next/", "/target/")


def _project_filter(change: Change, path: str) -> bool:
    if any(p in path for p in _IGNORED_PARTS):
        return False
    if "/.atrium/" in path and "/.atrium/worktrees/" not in path:
        return False
    if "/.git/" in path:
        tail = path.split("/.git/", 1)[1]
        return tail in ("HEAD", "index") or tail.startswith("refs/") or tail.startswith("worktrees/")
    return not path.endswith((".atrium-tmp", ".swp", "~"))


class ProjectWatcher:
    def __init__(self, project: Project):
        self.project = project
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"watch-{self.project.id}")

    def stop(self) -> None:
        self._stop.set()

    async def _run(self) -> None:
        roots = [self.project.path] + [r for r in self.project.roots() if not r.startswith(self.project.path + os.sep) and r != self.project.path]
        topic = f"project:{self.project.id}"
        try:
            async for changes in awatch(*roots, watch_filter=_project_filter, stop_event=self._stop, debounce=400, step=100, recursive=True):
                files: list[str] = []
                git_changed = worktrees_changed = False
                for change, path in changes:
                    if "/.git/" in path:
                        git_changed = True
                        tail = path.split("/.git/", 1)[1]
                        # a worktree added or removed: its admin dir worktrees/<name> (with its gitdir file) comes or goes
                        if change != Change.modified and tail.startswith("worktrees/") and (tail.count("/") == 1 or tail.endswith("/gitdir")):
                            worktrees_changed = True
                        continue
                    files.append(path)
                if files:
                    hub.publish(topic, {"type": "fs", "paths": files[:200]})
                if files or git_changed:
                    hub.publish(topic, {"type": "git_changed"})
                if worktrees_changed:  # the worktree pickers, and agents whose worktree went away
                    hub.publish(topic, {"type": "worktrees_changed"})
        except Exception:
            log.exception("project watcher stopped for %s", self.project.path)


class TranscriptWatcher:
    """One watcher over every harness's transcripts; maps changes to projects.

    Per-project transcript directories (Claude Code: ``~/.claude/projects/<key>``)
    map directly. Shared trees (Codex: ``~/.codex/sessions/YYYY/MM/DD``) are mapped
    through the session's working directory, read from the transcript itself.
    """

    def __init__(self) -> None:
        self._dirs: dict[str, set[str]] = {}  # per-project transcript dir -> project ids
        self._projects: dict[str, Project] = {}
        self._roots: dict[str, tuple[float, list[str]]] = {}  # project id -> (fetched_at, roots)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._restart = asyncio.Event()

    def add_project(self, project: Project) -> None:
        self._projects[project.id] = project
        for h in all_harnesses():
            for d in h.transcript_dirs(project.path):
                self._dirs.setdefault(str(d), set()).add(project.id)
        self._restart.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="watch-transcripts")

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()

    def _bases(self) -> list[str]:
        bases = {str(Path(d).parent) for d in self._dirs}
        for h in all_harnesses():
            bases |= {str(r) for r in h.watch_roots()}
        return [b for b in bases if os.path.isdir(b)]

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

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._restart.clear()
            bases = self._bases()
            if not bases:
                await self._restart.wait()
                continue
            stop = asyncio.Event()

            async def relay() -> None:
                await self._restart.wait()
                stop.set()

            relay_task = asyncio.create_task(relay())
            try:
                async for changes in awatch(*bases, stop_event=stop, debounce=1500, step=200, recursive=True):
                    paths = {path for _c, path in changes}
                    hit: set[str] = set()
                    for owners in await asyncio.to_thread(lambda: [self._owners(p) for p in paths]):
                        hit |= owners
                    for pid in hit:
                        hub.publish(f"project:{pid}", {"type": "sessions_changed", "source": "transcripts"})
            except Exception:
                log.exception("transcript watcher error")
                await asyncio.sleep(5)
            finally:
                relay_task.cancel()


transcript_watcher = TranscriptWatcher()
