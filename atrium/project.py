"""Per-project state (``<project>/.atrium/``) and the project registry.

    .atrium/
      .gitignore        "*" — keeps Atrium state out of the user's repo
      board.html        the fixed, self-contained board file (generated)
      board.json        board source of truth
      agents.json       session → agent name / role / harness
      reviews.json      per-session file review marks
      tracker.json      tracker offsets + run log
      git_view.json     the graph view chosen by the user or the Git Agent
      worktrees.json    every linked worktree seen, removed ones included (their sessions stay listed)
      worktrees/        default location for worktrees created from Atrium
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import gitops
from .config import config, project_id_for


class ProjectNotFound(KeyError):
    pass


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


class Project:
    def __init__(self, pid: str, path: str):
        self.id = pid
        self.path = path
        self.name = os.path.basename(path) or path
        self.state_dir = Path(path) / ".atrium"
        self._lock = threading.RLock()
        self._ensure_state_dir()
        from .board.store import BoardStore  # local import: board depends on project

        self.board = BoardStore(self)
        self.tracker: Any = None  # set by ProjectManager
        self.deleted: set[str] = set()  # sessions deleted while running: late board writes are dropped

    def _ensure_state_dir(self) -> None:
        self.state_dir.mkdir(exist_ok=True)
        gi = self.state_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")

    # ---- session meta ------------------------------------------------------
    def _meta(self) -> dict[str, Any]:
        data = _read_json(self.state_dir / "agents.json", {})
        data.setdefault("sessions", {})
        return data

    def sessions_meta(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._meta()["sessions"]

    def session_meta(self, sid: str) -> dict[str, Any]:
        return self.sessions_meta().get(sid, {})

    def set_session_meta(self, sid: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            data = self._meta()
            entry = data["sessions"].setdefault(sid, {"created_at": time.time()})
            entry.update({k: v for k, v in fields.items() if v is not None})
            _write_json(self.state_dir / "agents.json", data)
            return entry

    def forget_session(self, sid: str) -> None:
        """Drop everything Atrium kept about a deleted session (name, snapshots, review marks)."""
        self.deleted.add(sid)
        with self._lock:
            data = self._meta()
            if data["sessions"].pop(sid, None) is not None:
                _write_json(self.state_dir / "agents.json", data)
            reviews = _read_json(self.state_dir / "reviews.json", {})
            if reviews.pop(sid, None) is not None:
                _write_json(self.state_dir / "reviews.json", reviews)

    def agent_name(self, sid: str) -> str | None:
        return self.session_meta(sid).get("name")

    def git_agent_session(self) -> str | None:
        for sid, m in self.sessions_meta().items():
            if m.get("role") == "git" and not m.get("archived"):
                return sid
        return None

    # ---- reviews -----------------------------------------------------------
    def reviews(self, sid: str) -> dict[str, str]:
        with self._lock:
            return _read_json(self.state_dir / "reviews.json", {}).get(sid, {})

    def set_review(self, sid: str, path: str, content_hash: str | None) -> None:
        with self._lock:
            data = _read_json(self.state_dir / "reviews.json", {})
            entry = data.setdefault(sid, {})
            if content_hash:
                entry[path] = content_hash
            else:
                entry.pop(path, None)
            _write_json(self.state_dir / "reviews.json", data)

    # ---- git graph view ----------------------------------------------------
    def git_view(self) -> dict[str, Any] | None:
        return _read_json(self.state_dir / "git_view.json", None)

    def set_git_view(self, view: dict[str, Any] | None) -> None:
        path = self.state_dir / "git_view.json"
        if view is None:
            path.unlink(missing_ok=True)
        else:
            _write_json(path, view)

    # ---- tracker state -----------------------------------------------------
    def tracker_state(self) -> dict[str, Any]:
        return _read_json(self.state_dir / "tracker.json", {"offsets": {}, "runs": []})

    def save_tracker_state(self, state: dict[str, Any]) -> None:
        _write_json(self.state_dir / "tracker.json", state)

    # ---- paths -------------------------------------------------------------
    def roots(self) -> list[str]:
        """The project directory plus all of its git worktrees."""
        out = [self.path]
        for w in gitops.worktree_paths_sync(self.path):
            if w not in out:
                out.append(w)
        return out

    def worktrees_sync(self) -> list[dict[str, Any]]:
        text = gitops._run_sync(self.path, "worktree", "list", "--porcelain")
        return gitops._parse_worktrees(text) if text else []

    # ---- worktrees seen ----------------------------------------------------
    def note_worktrees(self, wts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Remember the linked worktrees of ``git worktree list``: a session keeps its directory after
        the worktree is removed, and is still found there. Returns every worktree seen, by path:
        {branch, head, gone} — ``gone`` is when it was first missed (None while it exists)."""
        path = self.state_dir / "worktrees.json"
        with self._lock:
            seen: dict[str, dict[str, Any]] = _read_json(path, {})
            if not wts:  # not a repository, or git failed: nothing to learn
                return seen
            before = json.dumps(seen, sort_keys=True)
            live = {w["path"]: w for w in wts if not w.get("main") and not w.get("bare") and os.path.isdir(w["path"])}
            for p, w in live.items():
                seen[p] = {"branch": w.get("branch"), "head": w.get("head"), "gone": None}
            for p, e in seen.items():
                if p not in live and not e.get("gone"):
                    e["gone"] = time.time()
            if json.dumps(seen, sort_keys=True) != before:
                _write_json(path, seen)
            return seen

    def known_worktrees(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return _read_json(self.state_dir / "worktrees.json", {})

    def forget_worktrees(self, paths: list[str]) -> None:
        with self._lock:
            seen = _read_json(self.state_dir / "worktrees.json", {})
            for p in paths:
                seen.pop(p, None)
            _write_json(self.state_dir / "worktrees.json", seen)

    def resolve(self, rel: str, root: str | None = None) -> Path:
        """Resolve a path inside one of the project's roots; refuse anything outside."""
        base = os.path.realpath(root or self.path)
        roots = self.roots()
        if base not in roots and not any(base.startswith(r + os.sep) for r in roots):
            raise PermissionError(f"{base} is not part of this project")
        target = os.path.realpath(os.path.join(base, rel)) if not os.path.isabs(rel) else os.path.realpath(rel)
        if not any(target == r or target.startswith(r + os.sep) for r in roots):
            raise PermissionError(f"{rel} is outside the project")
        return Path(target)

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "name": self.name, "is_git": gitops.is_repo(self.path)}


class ProjectManager:
    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._lock = threading.Lock()
        self.on_create: list[Any] = []

    def get(self, pid: str) -> Project:
        with self._lock:
            if pid in self._projects:
                return self._projects[pid]
            path = config.project_path(pid)
            if not path or not os.path.isdir(path):
                raise ProjectNotFound(pid)
            proj = Project(pid, path)
            self._projects[pid] = proj
        for cb in self.on_create:
            cb(proj)
        return proj

    def open(self, path: str) -> Project:
        entry = config.add_project(path)
        return self.get(entry["id"])

    def loaded(self) -> list[Project]:
        return list(self._projects.values())

    def forget(self, pid: str) -> Project | None:
        with self._lock:
            return self._projects.pop(pid, None)


projects = ProjectManager()

__all__ = ["Project", "ProjectManager", "ProjectNotFound", "projects", "project_id_for"]
