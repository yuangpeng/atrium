"""Live session management: start/resume agents, stream items, permissions, idle reaping."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import snapshots
from .board.prompts import GIT_AGENT_APPEND, SESSION_AGENT_APPEND
from .board.tools import BOARD_SERVER, board_toolset
from .config import UPLOADS_DIR, config
from .events import hub
from .gitagent import GIT_AGENT_ALLOWED_TOOLS, GIT_SERVER, git_toolset
from .harness import all_harnesses, get_harness
from .harness.base import Attachment, Item, LiveSession, LiveSpec, SessionSummary
from .harness.util import upload_url
from .mcp.web import WEB_SERVER, web_toolset
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
        modes = [m["id"] for m in get_harness(harness_id).permission_modes]
        default_mode = ("read-only" if "read-only" in modes else "default") if role == "git" else hdef.get("permission_mode")
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
            if kind not in TRANSIENT:
                h.store(item)
            topic = f"session:{h.sid}"
            ptopic = f"project:{h.project.id}"
            if kind == "state":
                h.state = item["state"]
                hub.publish(ptopic, {"type": "session_state", "session_id": h.sid, "state": h.state, "pending": len(h.pending)})
                if h.role == "worker" and h.sid in self.live:
                    h.project.board.update_agent(h.sid, stats={"live": h.state if h.alive else None, "last_active": h.last_activity})
            elif kind == "permission":
                h.pending[item["id"]] = item
                hub.publish(ptopic, {"type": "session_state", "session_id": h.sid, "state": "waiting", "pending": len(h.pending)})
            elif kind == "permission_resolved":
                h.pending.pop(item["id"], None)
            elif kind == "result":
                asyncio.get_running_loop().create_task(self._turn_ended(h))
            hub.publish(topic, {"type": "item", "item": item})

        return emit

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
            }

        for s in summaries:
            seen.add(s.id)
            out.append(row(s.id, s.harness, s.title, s.cwd or project.path, s.updated_at, s.created_at, s.git_branch, s.first_prompt, s.transcript))
        for sid, h in self.live.items():
            if h.project.id == project.id and sid not in seen:
                out.append(row(sid, h.harness_id, h.name, h.cwd, h.last_activity, h.started_at, None, None, None))
        if not include_archived:
            out = [r for r in out if not r["archived"]]
        out.sort(key=lambda r: -(r["updated_at"] or 0))
        return out

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
            name = meta.get("name") or (summary.title if summary else "Agent")
            role = meta.get("role", "worker")
            harness = get_harness(harness_id)
            preload = await harness.load_history(sid, cwd) if summary else (h.items if h else [])
            return await self._start(project, sid, resume=summary is not None, harness_id=harness_id, cwd=cwd, name=name, role=role,
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

    async def snapshot(self, project: Project, sid: str) -> dict[str, Any]:
        meta = project.session_meta(sid)
        h = self.live.get(sid)
        if h and h.alive:
            return {"live": True, "state": h.state, "items": h.items, "pending": list(h.pending), "settings": h.settings()}
        settings = {"model": meta.get("model") or "", "effort": meta.get("effort") or "", "mode": meta.get("mode") or ""}
        summary = await self.find_summary(project, sid)
        if not summary:  # never reached the transcript (e.g. failed to start): show what we have
            return {"live": False, "state": h.state if h else None, "items": h.items if h else [], "pending": [], "settings": settings}
        harness = get_harness(meta.get("harness") or summary.harness)
        items = await harness.load_history(sid, summary.cwd or project.path)
        return {"live": False, "state": None, "items": items, "pending": [], "settings": settings}


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


def _branch_of(cwd: str) -> str | None:
    import subprocess

    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=5)
        b = r.stdout.strip()
        return b if r.returncode == 0 and b != "HEAD" else None
    except (OSError, subprocess.TimeoutExpired):
        return None


sessions = SessionManager()
