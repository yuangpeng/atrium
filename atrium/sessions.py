"""Live session management: start/resume agents, stream items, permissions, idle reaping."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import gitops, snapshots
from .board.prompts import FORK_FILES, FORK_MOVED_APPEND, FORK_SAME_DIR_APPEND, GIT_AGENT_APPEND, SESSION_AGENT_APPEND
from .board.tools import BOARD_SERVER, board_toolset
from .config import UPLOADS_DIR, config
from .events import hub
from .gitagent import GIT_AGENT_ALLOWED_TOOLS, GIT_SERVER, git_toolset
from .harness import all_harnesses, get_harness
from .harness.base import Attachment, ForkPoint, Item, LiveSession, LiveSpec, SessionSummary, SubagentSummary
from .harness.util import upload_url
from .mcp.web import WEB_SERVER, web_toolset
from .notify import notifier
from .project import Project

log = logging.getLogger("atrium.sessions")

TRANSIENT = {"delta", "state"}
MAX_SNAPSHOTS = 200


@dataclass
class LiveHandle:
    sid: str
    project: Project
    harness_id: str
    role: str
    cwd: str
    name: str
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    session: LiveSession | None = None
    items: list[Item] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)
    state: str = "starting"
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    pending: dict[str, Item] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)  # subagents seen on the stream, by spawning tool call
    title_synced: bool = False

    @property
    def alive(self) -> bool:
        return self.state not in ("closed", "error")

    def store(self, item: Item) -> None:
        """Append, or replace an earlier item with the same id and kind (tool calls get upserted)."""
        iid = item.get("id")
        pos = self.index.get(iid) if iid else None
        if pos is not None and pos < len(self.items) and self.items[pos].get("kind") == item.get("kind"):
            self.items[pos] = item
            return
        if iid:
            self.index[iid] = len(self.items)
        self.items.append(item)

    def settings(self) -> dict[str, Any]:
        return {"model": self.model or "", "effort": self.effort or "", "mode": self.mode or ""}


class SessionManager:
    def __init__(self) -> None:
        self.live: dict[str, LiveHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.on_turn_end: list[Callable[[Project, str], None]] = []
        self._reaper: asyncio.Task | None = None
        self._summary_cache: dict[str, tuple[float, list[SessionSummary]]] = {}
        self._summary_inflight: dict[str, asyncio.Task] = {}

    # ---- lifecycle ---------------------------------------------------------
    def start_background(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        await asyncio.gather(*(self.close(sid) for sid in list(self.live)), return_exceptions=True)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            limit = float(config.get("sessions.idle_close_minutes") or 30) * 60
            now = time.time()
            for sid, h in list(self.live.items()):
                if h.alive and h.state == "idle" and not h.pending and now - h.last_activity > limit:
                    log.info("closing idle session %s", sid)
                    await self.close(sid)

    # ---- spec --------------------------------------------------------------
    def _spec(self, project: Project, sid: str | None, *, resume: bool, harness_id: str, cwd: str, name: str, role: str,
              model: str | None, effort: str | None, mode: str | None) -> LiveSpec:
        web = config.get("web") or {}
        hdef = config.harness_defaults(harness_id)
        disallowed: list[str] = []
        if web.get("replace_builtin_web_tools"):
            disallowed.append("WebFetch")
            if web.get("serper_api_key"):
                disallowed.append("WebSearch")
        toolsets = {WEB_SERVER: web_toolset(web)}
        allowed = [f"mcp__{WEB_SERVER}__search", f"mcp__{WEB_SERVER}__fetch"]
        sid_box = [sid]
        if role == "git":
            toolsets[GIT_SERVER] = git_toolset(project)
            append = GIT_AGENT_APPEND.format(project=project.name, path=project.path)
            allowed += GIT_AGENT_ALLOWED_TOOLS
        else:
            toolsets[BOARD_SERVER] = board_toolset(project, lambda: sid_box[0])
            append = SESSION_AGENT_APPEND.format(name=name, project=project.name, path=project.path, cwd=cwd)
            fork = project.session_meta(sid).get("forked_from") if sid else None
            if fork:
                append += "\n\n" + _fork_note(fork, cwd)
        harness = get_harness(harness_id)
        modes = [m["id"] for m in harness.permission_modes]
        default_mode = harness.git_agent_mode if role == "git" else hdef.get("permission_mode")
        mode = mode or default_mode or (modes[0] if modes else "default")
        spec = LiveSpec(
            session_id=sid,
            resume=resume,
            cwd=cwd,
            name=name,
            role=role,
            model=model or hdef.get("model") or None,
            effort=effort or hdef.get("effort") or None,
            permission_mode=mode,
            system_append=append,
            toolsets=toolsets,
            extra_mcp_servers=dict(config.get("mcp_servers") or {}),
            disallowed_tools=disallowed,
            allowed_tools=allowed,
        )
        spec._sid_box = sid_box  # type: ignore[attr-defined]  # board tools learn the id once the harness assigns it
        return spec

    def _emitter(self, h: LiveHandle):
        def emit(item: Item) -> None:
            kind = item.get("kind")
            h.last_activity = time.time()
            if kind == "task":
                self._track_task(h, item)
                return
            if kind not in TRANSIENT:
                h.store(item)
            topic = f"session:{h.sid}"
            ptopic = f"project:{h.project.id}"
            if kind == "state":
                prev, h.state = h.state, item["state"]
                hub.publish(ptopic, {"type": "session_state", "session_id": h.sid, "state": h.state, "pending": len(h.pending)})
                if h.role == "worker" and h.sid in self.live:
                    h.project.board.update_agent(h.sid, stats={"live": h.state if h.alive else None, "last_active": h.last_activity})
                if h.state == "error" and prev in ("running", "waiting"):
                    notifier.failed(h)
            elif kind == "permission":
                h.pending[item["id"]] = item
                hub.publish(ptopic, {"type": "session_state", "session_id": h.sid, "state": "waiting", "pending": len(h.pending)})
                notifier.permission(h, item)
            elif kind == "permission_resolved":
                h.pending.pop(item["id"], None)
            elif kind == "result":
                if "usage" in h.index:  # the context gauge outlives the live session
                    u = h.items[h.index["usage"]]
                    h.project.set_session_meta(h.sid, usage={"used": u.get("used"), "max": u.get("max")})
                asyncio.get_running_loop().create_task(self._turn_ended(h))
                notifier.turn_ended(h, item)
            hub.publish(topic, {"type": "item", "item": item})

        return emit

    def _track_task(self, h: LiveHandle, item: Item) -> None:
        """A subagent started / ended (harness ``task`` items): its status, ahead of the transcript."""
        key = item.get("tool_use_id")
        if not key:  # end-of-task updates may only carry the task id
            key = next((k for k, t in h.tasks.items() if t["task_id"] == item.get("task_id")), item.get("task_id"))
        if not key:
            return
        now = time.time()
        t = h.tasks.setdefault(key, {"task_id": item.get("task_id"), "tool_use_id": item.get("tool_use_id"), "created_at": now})
        t.update({k: item[k] for k in ("description", "agent_type") if item.get(k)})
        t["status"] = item.get("status") or "running"
        t["updated_at"] = now
        hub.publish(f"project:{h.project.id}", {"type": "sessions_changed", "session_id": h.sid, "source": "subagents"})

    async def _turn_ended(self, h: LiveHandle) -> None:
        await self._snapshot(h, "end")
        if not h.title_synced and h.role == "worker" and h.project.session_meta(h.sid).get("name"):
            h.title_synced = True
            await get_harness(h.harness_id).rename_session(h.sid, h.name, h.cwd)
        for cb in self.on_turn_end:
            try:
                cb(h.project, h.sid)
            except Exception:
                log.exception("turn-end callback failed")
        self.invalidate(h.project)
        hub.publish(f"project:{h.project.id}", {"type": "sessions_changed"})

    async def _snapshot(self, h: LiveHandle, phase: str) -> None:
        snap = await asyncio.to_thread(snapshots.take, h.cwd, phase)
        if not snap:
            return
        snaps = list(h.project.session_meta(h.sid).get("snapshots") or [])
        if phase == "start" and snaps and snaps[-1].get("phase") == "start":
            snaps.pop()  # a turn that never reported its end
        snaps.append(snap)
        h.project.set_session_meta(h.sid, snapshots=snaps[-MAX_SNAPSHOTS:])

    # ---- discovery ---------------------------------------------------------
    async def summaries(self, project: Project, max_age: float = 3.0) -> list[SessionSummary]:
        cached = self._summary_cache.get(project.id)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        task = self._summary_inflight.get(project.id)
        if task is None:
            task = asyncio.create_task(self._discover(project))
            self._summary_inflight[project.id] = task
            task.add_done_callback(lambda _t: self._summary_inflight.pop(project.id, None))
        return await asyncio.shield(task)

    async def _discover(self, project: Project) -> list[SessionSummary]:
        roots = await asyncio.to_thread(project.roots)
        results = await asyncio.gather(*(h.list_sessions(project.path, roots) for h in all_harnesses()), return_exceptions=True)
        out: list[SessionSummary] = []
        for h, r in zip(all_harnesses(), results):
            if isinstance(r, BaseException):
                log.warning("%s: listing sessions failed: %s", h.id, r)
            else:
                out += r
        self._summary_cache[project.id] = (time.time(), out)
        return out

    def invalidate(self, project: Project) -> None:
        self._summary_cache.pop(project.id, None)

    async def find_summary(self, project: Project, sid: str) -> SessionSummary | None:
        for s in await self.summaries(project):
            if s.id == sid:
                return s
        for s in await self.summaries(project, max_age=0):
            if s.id == sid:
                return s
        return None

    async def session_info(self, project: Project, sid: str) -> tuple[str, str]:
        """(cwd, harness_id) of a session."""
        h = self.live.get(sid)
        if h:
            return h.cwd, h.harness_id
        meta = project.session_meta(sid)
        if meta.get("cwd") and meta.get("harness"):
            return meta["cwd"], meta["harness"]
        s = await self.find_summary(project, sid)
        return meta.get("cwd") or (s.cwd if s else None) or project.path, meta.get("harness") or (s.harness if s else "claude-code")

    async def list_sessions(self, project: Project, include_archived: bool = False) -> list[dict[str, Any]]:
        summaries = await self.summaries(project)
        wts = await asyncio.to_thread(project.worktrees_sync)
        meta = project.sessions_meta()
        board = project.board.snapshot()["agents"]
        wt_branch = {w["path"]: w.get("branch") for w in wts}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def row(sid: str, harness: str, title: str, cwd: str, updated: float, created: float | None, branch: str | None,
                first_prompt: str | None, transcript: str | None) -> dict[str, Any]:
            m = meta.get(sid, {})
            b = board.get(sid, {})
            h = self.live.get(sid)
            if h and h.alive:
                updated = max(updated, h.last_activity)
            real = os.path.realpath(cwd) if cwd else ""
            fork = m.get("forked_from")
            if fork:  # the copied transcript starts with the source's timestamps
                created = m.get("created_at") or created
            return {
                "id": sid,
                "harness": harness,
                "name": m.get("name"),
                "title": title,
                "role": m.get("role", "worker"),
                "cwd": real or cwd,
                "worktree": bool(real) and real in wt_branch and real != project.path,
                "branch": wt_branch.get(real) or branch,
                "created_at": created or m.get("created_at"),
                "updated_at": updated,
                "live": h.state if h and h.alive else None,
                "pending": len(h.pending) if h else 0,
                "status": b.get("status"),
                "progress": b.get("progress"),
                "doing": b.get("doing"),
                "first_prompt": (first_prompt or "")[:300],
                "archived": bool(m.get("archived")),
                "created_by": m.get("created_by", "external"),
                "effort": (h.effort if h else None) or m.get("effort") or "",
                "transcript": transcript,
                "forked_from": {"session": fork.get("session"), "name": fork.get("name")} if fork else None,
            }

        for s in summaries:
            seen.add(s.id)
            out.append(row(s.id, s.harness, s.title, s.cwd or project.path, s.updated_at, s.created_at, s.git_branch, s.first_prompt, s.transcript))
        for sid, h in self.live.items():
            if h.project.id == project.id and sid not in seen:
                seen.add(sid)
                out.append(row(sid, h.harness_id, h.name, h.cwd, h.last_activity, h.started_at, None, None, None))
        for sid, m in meta.items():  # forks the harness doesn't list yet (Codex: until their first turn)
            fork = m.get("forked_from")
            if fork and sid not in seen and m.get("cwd"):
                title = m.get("name") or f"{fork.get('name') or 'Agent'} (fork)"
                out.append(row(sid, m.get("harness") or "claude-code", title, m["cwd"], m.get("forked_at") or 0, m.get("created_at"), None, None, None))
        ids = {r["id"] for r in out}
        for r in out:
            if r["forked_from"]:
                r["forked_from"]["exists"] = r["forked_from"]["session"] in ids  # the original may have been deleted since
        if not include_archived:
            out = [r for r in out if not r["archived"]]
        roots = [project.path] + [w["path"] for w in wts if w["path"] != project.path and not w.get("bare")]
        listed = {r["id"] for r in out}
        found = await self._subagents([s for s in summaries if s.id in listed], roots)
        for r in out:
            r["subagents"] = _subagent_rows(found.get(r["id"], []), self.live.get(r["id"]))
        out.sort(key=lambda r: -(r["updated_at"] or 0))
        return out

    async def _subagents(self, summaries: list[SessionSummary], roots: list[str]) -> dict[str, list[SubagentSummary]]:
        groups: dict[str, list[SessionSummary]] = {}
        for s in summaries:
            groups.setdefault(s.harness, []).append(s)
        results = await asyncio.gather(*(get_harness(hid).list_subagents(ss, roots) for hid, ss in groups.items()), return_exceptions=True)
        found: dict[str, list[SubagentSummary]] = {}
        for hid, r in zip(groups, results):
            if isinstance(r, BaseException):
                log.warning("%s: listing subagents failed: %s", hid, r)
            else:
                found.update(r)
        return found

    async def subagent(self, project: Project, sid: str, aid: str) -> dict[str, Any]:
        """A subagent's row and conversation (read-only: only its parent talks to it)."""
        summary = await self.find_summary(project, sid)
        found = await self._subagents([summary], await asyncio.to_thread(project.roots)) if summary else {}
        sub = next((a for a in _subagent_rows(found.get(sid, []), self.live.get(sid)) if a["id"] == aid), None)
        if sub is None:
            raise FileNotFoundError(f"no subagent {aid} in session {sid}")
        cwd, harness_id = await self.session_info(project, sid)
        items = await get_harness(harness_id).load_subagent_history(sid, aid, cwd)
        return {"subagent": sub, "items": items}

    # ---- operations --------------------------------------------------------
    def _lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    async def _start(self, project: Project, sid: str | None, *, resume: bool, harness_id: str, cwd: str, name: str, role: str,
                     model: str | None, effort: str | None, mode: str | None, preload: list[Item] | None = None) -> LiveHandle:
        harness = get_harness(harness_id)
        ok, reason = harness.available()
        if not ok:
            raise RuntimeError(reason or f"{harness.label} is not available")
        spec = self._spec(project, sid, resume=resume, harness_id=harness_id, cwd=cwd, name=name, role=role, model=model, effort=effort, mode=mode)
        h = LiveHandle(sid=sid or f"pending-{uuid.uuid4().hex[:8]}", project=project, harness_id=harness_id, role=role, cwd=cwd, name=name,
                       model=spec.model, effort=spec.effort, mode=spec.permission_mode)
        for it in preload or []:
            h.store(it)
        h.title_synced = resume
        if sid:
            self.live[sid] = h
        h.session = harness.create_live(spec, self._emitter(h))
        try:
            await h.session.start()
        except Exception:
            h.state = "error"
            if sid:
                self.live.pop(sid, None)
            raise
        if h.session.session_id != h.sid:  # the harness assigned the id (e.g. a new Codex thread)
            self.live.pop(h.sid, None)
            h.sid = h.session.session_id
        spec._sid_box[0] = h.sid  # type: ignore[attr-defined]
        self.live[h.sid] = h
        self.invalidate(project)
        hub.publish(f"project:{project.id}", {"type": "sessions_changed"})
        return h

    async def create(self, project: Project, *, name: str, prompt: str | None, harness_id: str | None = None, cwd: str | None = None,
                     model: str | None = None, effort: str | None = None, mode: str | None = None, role: str = "worker",
                     attachments: list[Attachment] | None = None) -> str:
        harness_id = get_harness(harness_id or config.get("defaults.harness")).id
        cwd = os.path.realpath(cwd or project.path)
        project.resolve(".", root=cwd)  # validates cwd belongs to the project
        # An empty name leaves the session unnamed; the Tracker Agent will name it ("<Topic> Agent").
        name = (name or "").strip()
        h = await self._start(project, None, resume=False, harness_id=harness_id, cwd=cwd, name=name or "Agent", role=role,
                              model=model, effort=effort, mode=mode)
        sid = h.sid
        project.set_session_meta(sid, name=name or None, name_source="user" if name else None, harness=harness_id, role=role,
                                 created_by="atrium", cwd=cwd, effort=h.effort, model=h.model, mode=h.mode)
        if role == "worker":
            project.board.update_agent(
                sid, name=name or None, status="working" if prompt else "idle", progress=0, doing="Starting…" if prompt else "",
                source="atrium", stats={"branch": _branch_of(cwd), "worktree": cwd != project.path, "harness": harness_id, "live": h.state},
            )
        if prompt or attachments:
            await self.send(project, sid, prompt or "", attachments)
        return sid

    async def fork(self, project: Project, sid: str, *, at: dict[str, Any] | None = None, workspace: dict[str, Any] | None = None,
                   carry: bool = True, name: str = "", prompt: str | None = None, attachments: list[Attachment] | None = None,
                   archive_source: bool = False) -> dict[str, Any]:
        """Copy a conversation into a new session — in the same directory or in a worktree.

        ``at`` = {anchor, user_index}: fork before that user message (``Harness.fork_point``); None
        keeps the whole conversation. ``workspace``: {"kind": "same"} (default), {"kind":
        "worktree", "path"} or {"kind": "new", "branch"} — a new worktree branches from the
        source's commit and, with ``carry``, gets the source's files as they were at the fork point.
        """
        src_cwd, harness_id = await self.session_info(project, sid)
        harness = get_harness(harness_id)
        h = self.handle(sid)
        point = ForkPoint()
        if at:
            point = await harness.fork_point(sid, src_cwd, at.get("anchor") or None, int(at.get("user_index") or 0) or None)
        if point.next_ts is None and h and h.state in ("starting", "running", "waiting"):  # the kept part ends with the turn in progress
            raise ValueError("The agent is still working on that turn: wait for it to end (or stop it), or fork from an earlier message")
        meta = project.session_meta(sid)
        summary = None if meta.get("name") else await self.find_summary(project, sid)
        src_name = meta.get("name") or (summary.title if summary else None) or "Agent"
        name = name.strip()
        title = name or f"{src_name} (fork)"  # unnamed forks keep it until the Tracker names them

        target, created, files = src_cwd, None, None
        ws = workspace or {}
        kind = ws.get("kind") or "same"
        if kind != "same":
            src_root = gitops.repo_root(src_cwd)
            if not src_root:
                raise ValueError("Forking into a worktree needs a git repository")
            if kind == "new":
                branch = str(ws.get("branch") or "").strip()
                if not branch:
                    raise ValueError("Give the new worktree a branch name")
                head = (await gitops.run_git(src_root, "rev-parse", "HEAD")).strip()
                created = await gitops.add_worktree(project.path, branch, base=head)
                root = created["path"]
            else:
                root = os.path.realpath(str(ws.get("path") or ""))
            sub = os.path.join(root, os.path.relpath(src_cwd, src_root))  # the same subdirectory, if the agent works in one
            target = os.path.realpath(sub if os.path.isdir(sub) else root)
        try:
            project.resolve(".", root=target)  # must belong to the project
            history = _history_snapshots(meta, point)
            if created:
                files = await asyncio.to_thread(self._carry, history, point, src_cwd, created["path"]) if carry else "committed"
            note = _fork_note({"name": src_name, "cwd": src_cwd, "files": files}, target)
            new_sid = await harness.fork_session(sid, src_cwd, point, target_cwd=target, title=title, note=note)
        except BaseException:
            if created:  # nothing refers to it yet
                await gitops.run_git(project.path, "worktree", "remove", "--force", created["path"], check=False)
                await gitops.run_git(project.path, "branch", "-D", created["branch"], check=False)
            raise

        now = time.time()
        project.set_session_meta(
            new_sid, name=name or None, name_source="user" if name else None, harness=harness_id, role="worker",
            created_by="atrium", cwd=target, created_at=now, forked_at=now,
            model=(h.model if h else None) or meta.get("model") or None, effort=(h.effort if h else None) or meta.get("effort") or None,
            mode=(h.mode if h else None) or meta.get("mode") or None,
            forked_from={"session": sid, "name": src_name, "cwd": src_cwd, "files": files, "user_index": (at or {}).get("user_index")},
            fork_snapshots=history or None,
        )
        branch = _branch_of(target)
        starting = bool(prompt or attachments)
        project.board.update_agent(
            new_sid, name=title, status="working" if starting else "idle", progress=0, doing="Starting…" if starting else "", source="atrium",
            stats={"branch": branch, "worktree": target != project.path, "harness": harness_id, "live": None, "forked_from": sid},
        )
        project.board.log(new_sid, f"Forked from {src_name}" + (f" into worktree {branch}" if created else ""), source="atrium")
        transcript = harness.transcript_path(new_sid, target)
        if transcript and project.tracker:
            project.tracker.skip_existing(str(transcript))  # the copied history was tracked as the source's
        if archive_source:  # "move": the fork carries on, the original is put away
            project.set_session_meta(sid, archived=True)
            if project.board.agent(sid):
                project.board.update_agent(sid, stats={"archived": True})
            await self.close(sid)
        self.invalidate(project)
        hub.publish(f"project:{project.id}", {"type": "sessions_changed"})
        out: dict[str, Any] = {"id": new_sid, "cwd": target, "files": files, "worktree": created}
        if starting:
            try:
                await self.send(project, new_sid, prompt or "", attachments)
            except Exception as e:  # the fork exists either way; let the UI open it and show why
                log.exception("first message to fork %s failed", new_sid)
                out["error"] = str(e) or type(e).__name__
        return out

    @staticmethod
    def _carry(snaps: list[dict[str, Any]], point: ForkPoint, src_cwd: str, dest_root: str) -> str:
        """Give a forked worktree its source's files at the fork point. Returns what it got:
        "fork-point", "current" (that moment has no snapshot) or "committed" (no snapshot at all)."""
        snap, files = None, "fork-point"
        if point.next_ts is not None:  # forking before a later message: the files back then
            snap = _snapshot_at(snaps, point, dest_root)
            files = "fork-point" if snap else "current"
        snap = snap or snapshots.take(src_cwd, "fork")
        if not snap:
            return "committed"
        snapshots.restore(dest_root, snap["tree"])
        return files

    async def ensure_live(self, project: Project, sid: str, *, model: str | None = None, mode: str | None = None) -> LiveHandle:
        async with self._lock(sid):
            h = self.live.get(sid)
            if h and h.alive:
                return h
            meta = project.session_meta(sid)
            summary = await self.find_summary(project, sid)
            harness_id = meta.get("harness") or (summary.harness if summary else None) or "claude-code"
            cwd = meta.get("cwd") or (summary.cwd if summary else None) or project.path
            if not os.path.isdir(cwd):
                raise FileNotFoundError(f"session directory no longer exists: {cwd}")
            fork = meta.get("forked_from")
            name = meta.get("name") or (summary.title if summary else None) or (f"{fork.get('name') or 'Agent'} (fork)" if fork else "Agent")
            role = meta.get("role", "worker")
            harness = get_harness(harness_id)
            known = summary is not None or bool(fork)  # a fork exists before discovery lists it
            preload = _with_usage(await harness.load_history(sid, cwd), meta) if known else (h.items if h else [])
            return await self._start(project, sid, resume=known, harness_id=harness_id, cwd=cwd, name=name, role=role,
                                     model=model or meta.get("model"), effort=meta.get("effort"), mode=mode or meta.get("mode"), preload=preload)

    async def send(self, project: Project, sid: str, text: str, attachments: list[Attachment] | None = None) -> None:
        h = await self.ensure_live(project, sid)
        attachments = attachments or []
        item: Item = {"kind": "user", "id": f"u-{uuid.uuid4().hex[:12]}", "text": text, "ts": time.time()}
        if attachments:
            item["attachments"] = [{"name": a.name, "path": a.path, "mime": a.mime, "url": upload_url(a.path)} for a in attachments]
        if h.state == "idle":
            await self._snapshot(h, "start")
        self._emitter(h)(item)
        assert h.session
        await h.session.send(text, attachments)
        if h.role == "worker":
            a = project.board.agent(sid)
            if not a or a.get("status") in (None, "idle", "done"):
                project.board.update_agent(sid, status="working", doing=_short(text, 80) or "Working…", source="atrium")

    def handle(self, sid: str) -> LiveHandle | None:
        h = self.live.get(sid)
        return h if h and h.alive else None

    async def interrupt(self, sid: str) -> None:
        h = self.handle(sid)
        if h and h.session:
            await h.session.interrupt()

    async def set_mode(self, project: Project, sid: str, mode: str) -> None:
        h = await self.ensure_live(project, sid, mode=mode)
        assert h.session
        await h.session.set_permission_mode(mode)
        h.mode = mode
        project.set_session_meta(sid, mode=mode)

    async def set_model(self, project: Project, sid: str, model: str | None) -> None:
        h = await self.ensure_live(project, sid, model=model)
        assert h.session
        await h.session.set_model(model)
        h.model = model
        project.set_session_meta(sid, model=model or "")

    async def set_effort(self, project: Project, sid: str, effort: str | None) -> None:
        project.set_session_meta(sid, effort=effort or "")
        h = self.handle(sid)
        if h and h.session:  # otherwise applied when the session next starts
            await h.session.set_effort(effort)
            h.effort = effort
            hub.publish(f"session:{sid}", {"type": "settings", "settings": h.settings()})

    def resolve_permission(self, sid: str, request_id: str, decision: dict[str, Any]) -> bool:
        h = self.handle(sid)
        return bool(h and h.session and h.session.resolve_permission(request_id, decision))

    async def close(self, sid: str) -> None:
        h = self.live.get(sid)
        if not h:
            return
        if h.session:
            try:
                await h.session.close()
            except Exception:
                log.exception("close failed for %s", sid)
        h.state = "closed"
        self.live.pop(sid, None)
        if h.role == "worker":
            h.project.board.update_agent(sid, stats={"live": None})
        hub.publish(f"project:{h.project.id}", {"type": "session_state", "session_id": sid, "state": "closed", "pending": 0})

    async def delete(self, project: Project, sid: str) -> str | None:
        """Delete a session everywhere: stop it, back up its transcript into
        ``.atrium/trash/sessions/``, remove it from the harness's store, forget Atrium's state.
        Returns the backup directory (None when there was no transcript yet)."""
        cwd, harness_id = await self.session_info(project, sid)
        harness = get_harness(harness_id)
        await self.close(sid)
        backup = None
        transcript = harness.transcript_path(sid, cwd)
        if transcript and transcript.exists():
            dest = project.state_dir / "trash" / "sessions" / f"{time.strftime('%Y%m%d-%H%M%S')}-{harness_id}-{sid}"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(transcript, dest / transcript.name)
            sidecar = transcript.with_suffix("")  # Claude keeps subagent transcripts in <sid>/
            if sidecar.is_dir():
                shutil.copytree(sidecar, dest / sidecar.name, dirs_exist_ok=True)
            backup = str(dest)
        await harness.delete_session(sid, cwd)
        project.forget_session(sid)
        project.board.remove_agent(sid, timeline=True)
        self.invalidate(project)
        hub.publish(f"project:{project.id}", {"type": "sessions_changed"})
        return backup

    async def snapshot(self, project: Project, sid: str) -> dict[str, Any]:
        meta = project.session_meta(sid)
        h = self.live.get(sid)
        if h and h.alive:
            return {"live": True, "state": h.state, "items": h.items, "pending": list(h.pending), "settings": h.settings()}
        settings = {"model": meta.get("model") or "", "effort": meta.get("effort") or "", "mode": meta.get("mode") or ""}
        summary = await self.find_summary(project, sid)
        if not summary and not meta.get("forked_from"):  # never reached the transcript (e.g. failed to start): show what we have
            return {"live": False, "state": h.state if h else None, "items": h.items if h else [], "pending": [], "settings": settings}
        harness = get_harness(meta.get("harness") or (summary.harness if summary else "claude-code"))
        items = _with_usage(await harness.load_history(sid, (summary.cwd if summary else None) or meta.get("cwd") or project.path), meta)
        return {"live": False, "state": None, "items": items, "pending": [], "settings": settings}


def _with_usage(items: list[Item], meta: dict[str, Any]) -> list[Item]:
    """History plus the context gauge as of the last turn Atrium saw (transcripts don't record the window)."""
    return [*items, {"kind": "usage", "id": "usage", **meta["usage"]}] if meta.get("usage") else items


def _subagent_rows(found: list[SubagentSummary], h: LiveHandle | None) -> list[dict[str, Any]]:
    """Discovered subagents, with the live stream's word on their status when the parent runs in Atrium."""
    rows = [asdict(a) for a in found]
    if h and h.alive and h.tasks:
        index: dict[str, dict[str, Any]] = {}
        for r in rows:
            index.setdefault(r["id"], r)
            if r["tool_use_id"]:
                index[r["tool_use_id"]] = r
        for key, t in h.tasks.items():
            r = index.get(key) or index.get(t["task_id"] or "")
            if r is None:  # no transcript yet
                r = asdict(SubagentSummary(id=t["task_id"] or key, agent_type=t.get("agent_type"), description=t.get("description") or "Subagent",
                                           status=t["status"], updated_at=t["updated_at"], created_at=t["created_at"], tool_use_id=t["tool_use_id"]))
                rows.append(r)
            r["status"] = t["status"]
            r["updated_at"] = max(r["updated_at"], t["updated_at"])
    rows.sort(key=lambda r: r["created_at"] or r["updated_at"])
    return rows


def attachment_from_upload(name: str) -> Attachment:
    """Resolve an uploaded file id (its name in ~/.atrium/uploads) to an Attachment."""
    import mimetypes

    path = os.path.realpath(UPLOADS_DIR / os.path.basename(name))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"attachment not found: {name}")
    label = path.split("__", 1)[1] if "__" in os.path.basename(path) else os.path.basename(path)
    return Attachment(path=path, name=label, mime=mimetypes.guess_type(path)[0] or "application/octet-stream")


def _short(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _history_snapshots(meta: dict[str, Any], point: ForkPoint) -> list[dict[str, Any]]:
    """The turn snapshots of a session's history up to a fork point: its own and those it inherited
    as a fork, so a fork of a fork can restore files from before it existed. A fork keeps them as
    ``fork_snapshots``, apart from ``snapshots`` (its own turns, what its Changes count)."""
    end = (point.next_ts if point.next_ts is not None else time.time()) + 1
    snaps = [s for s in (meta.get("fork_snapshots") or []) + (meta.get("snapshots") or []) if s.get("tree") and (s.get("ts") or 0) <= end]
    return sorted(snaps, key=lambda s: s.get("ts") or 0)[-MAX_SNAPSHOTS:]


def _snapshot_at(snaps: list[dict[str, Any]], point: ForkPoint, repo: str) -> dict[str, Any] | None:
    """The turn snapshot with the files at a fork point: the last one taken after the kept part
    ended and before the first dropped message was sent (± 1 s: Codex times are whole seconds)."""
    lo, hi = (point.cut_ts or 0) - 1, (point.next_ts or time.time()) + 1
    found = [s for s in snaps if s.get("tree") and lo <= (s.get("ts") or 0) <= hi]
    if found and snapshots.tree_exists(repo, found[-1]["tree"]):  # every checkout of the repository has its objects
        return found[-1]
    return None


def _fork_note(fork: dict[str, Any], cwd: str) -> str:
    """System-prompt note for a forked session: where it comes from and which files it has."""
    source = fork.get("name") or "another agent"
    old = fork.get("cwd") or cwd
    if os.path.realpath(old) == os.path.realpath(cwd):
        return FORK_SAME_DIR_APPEND.format(source=source)
    return FORK_MOVED_APPEND.format(source=source, old=old, cwd=cwd, files=FORK_FILES.get(fork.get("files") or "", FORK_FILES[""]))


def _branch_of(cwd: str) -> str | None:
    import subprocess

    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=5)
        b = r.stdout.strip()
        return b if r.returncode == 0 and b != "HEAD" else None
    except (OSError, subprocess.TimeoutExpired):
        return None


sessions = SessionManager()
