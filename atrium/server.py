"""HTTP + WebSocket API, and static hosting of the built frontend."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, changes, configfiles, fs, gitops, lsp
from .auth import AUTH_PATH, AuthMiddleware, auth
from .auth import router as auth_router
from .config import UPLOADS_DIR, config
from .events import Subscriber, hub
from .gitagent import set_view
from .harness import all_harnesses, get_harness
from .project import Project, ProjectNotFound, projects
from .review import ReviewManager
from .sessions import WorkspaceGone, attachment_from_upload, sessions
from .terminal import TerminalError, terminals
from .tracker import Tracker
from .watch import ProjectWatcher, transcript_watcher

log = logging.getLogger("atrium.server")
STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Env vars that make a spawned Claude Code think it is nested inside another session.
_NESTING_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID",
)

_watchers: dict[str, ProjectWatcher] = {}


def _on_project_loaded(project: Project) -> None:
    project.tracker = Tracker(project, sessions)
    project.tracker.start()
    project.review = ReviewManager(project, sessions)
    w = ProjectWatcher(project)
    w.start()
    _watchers[project.id] = w
    transcript_watcher.add_project(project)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for v in _NESTING_VARS:
        os.environ.pop(v, None)
    # `atrium` has created auth.json already, unless the server was started some other way (uvicorn)
    if auth.enabled and auth.ensure().get("generated_password"):
        log.warning("login password: made up, in %s (set your own: atrium --set-password)", AUTH_PATH)
    hub.bind_loop(asyncio.get_running_loop())
    projects.on_create.append(_on_project_loaded)
    sessions.on_turn_end.append(lambda project, _sid: project.tracker and project.tracker.after_turn())
    sessions.on_turn_end.append(lambda project, sid: project.review and project.review.turn_ended(sid))
    sessions.start_background()
    transcript_watcher.start()
    for p in config.projects():
        if p["exists"] and not p.get("archived_at"):  # archived: loaded when opened
            try:
                projects.get(p["id"])
            except Exception:
                log.exception("failed to load project %s", p["path"])
    yield
    for p in projects.loaded():
        if p.tracker:
            p.tracker.stop()
    for w in _watchers.values():
        w.stop()
    transcript_watcher.stop()
    await sessions.shutdown()
    for h in all_harnesses():
        if hasattr(h, "shutdown"):
            await h.shutdown()
    await lsp.registry.shutdown()
    await terminals.shutdown()


app = FastAPI(title="Atrium", version=__version__, lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)  # before the SPA's catch-all


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
@app.exception_handler(ProjectNotFound)
async def _nf(_r: Request, e: ProjectNotFound):
    return JSONResponse({"error": f"unknown project {e}"}, status_code=404)


@app.exception_handler(PermissionError)
async def _perm(_r: Request, e: PermissionError):
    return JSONResponse({"error": str(e)}, status_code=403)


@app.exception_handler(WorkspaceGone)
async def _gone(_r: Request, e: WorkspaceGone):
    return JSONResponse({"error": str(e), "code": "workspace_gone", "cwd": e.cwd}, status_code=409)


@app.exception_handler(FileNotFoundError)
async def _fnf(_r: Request, e: FileNotFoundError):
    return JSONResponse({"error": f"not found: {e}"}, status_code=404)


@app.exception_handler(FileExistsError)
async def _fe(_r: Request, e: FileExistsError):
    return JSONResponse({"error": f"already exists: {e}"}, status_code=409)


@app.exception_handler(fs.Conflict)
async def _conflict(_r: Request, e: fs.Conflict):
    return JSONResponse({"error": "conflict", "current_sha": e.current_sha}, status_code=409)


@app.exception_handler(gitops.GitError)
async def _git(_r: Request, e: gitops.GitError):
    return JSONResponse({"error": str(e)}, status_code=400)


@app.exception_handler(configfiles.ConfigConflict)
async def _cfg_conflict(_r: Request, e: configfiles.ConfigConflict):
    return JSONResponse({"error": str(e)}, status_code=409)


@app.exception_handler(configfiles.ConfigInvalid)
async def _cfg_invalid(_r: Request, e: configfiles.ConfigInvalid):
    return JSONResponse({"error": str(e)}, status_code=422)


@app.exception_handler(RuntimeError)
async def _rt(_r: Request, e: RuntimeError):
    log.exception("runtime error")
    return JSONResponse({"error": str(e)}, status_code=500)


def P(pid: str) -> Project:
    return projects.get(pid)


async def _catalog(h: Any) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(h.catalog(), timeout=20)
    except Exception as e:
        d = h.describe()
        d["catalog_error"] = str(e) or type(e).__name__
        return d


# ---------------------------------------------------------------------------
# meta / settings / projects
# ---------------------------------------------------------------------------
@app.get("/api/meta")
async def meta():
    harnesses = await asyncio.gather(*(_catalog(h) for h in all_harnesses()))
    return {"version": __version__, "harnesses": harnesses, "settings": config.public_settings(), "lsp": await asyncio.to_thread(lsp.available),
            "terminal": terminals.available(), "auth": auth.enabled}


@app.get("/api/settings")
async def get_settings():
    return config.public_settings()


@app.patch("/api/settings")
async def patch_settings(body: dict[str, Any] = Body(...)):
    """Small UI preferences only (e.g. ``{"path": "ui.theme", "value": "dark"}``); everything
    else is edited as the raw settings file."""
    path = str(body.get("path") or "")
    if not path.startswith(("ui.", "defaults.")):
        raise HTTPException(400, "only ui.* and defaults.* can be patched; edit settings.json for the rest")
    config.set(path, body.get("value"))
    return config.public_settings()


# ---- raw config files (Atrium / Claude Code / Codex) ------------------------
def _config_project_dir(pid: str | None) -> str | None:
    return P(pid).path if pid else None


@app.get("/api/config-files")
async def list_config_files(pid: str | None = None):
    return [f.describe() for f in configfiles.all_config_files(_config_project_dir(pid))]


@app.get("/api/config-files/{fid}")
async def read_config_file(fid: str, pid: str | None = None):
    return configfiles.read(configfiles.find(fid, _config_project_dir(pid)))


@app.put("/api/config-files/{fid}")
async def write_config_file(fid: str, body: dict[str, Any] = Body(...)):
    f = configfiles.find(fid, _config_project_dir(body.get("pid")))
    out = await asyncio.to_thread(configfiles.write, f, body.get("content") or "", body.get("base_sha"))
    if f.harness == "atrium":
        config.reload()
    else:
        get_harness(f.harness).on_config_saved(fid)
    return out


# ---- composer attachments ---------------------------------------------------
@app.put("/api/uploads")
async def upload(request: Request, name: str = Query(...)):
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    safe = re.sub(r"[^\w.\-]+", "_", os.path.basename(name)).strip("._") or "file"
    fid = f"{hashlib.sha1(data).hexdigest()[:12]}__{safe[-80:]}"
    dest = UPLOADS_DIR / fid
    if not dest.exists():
        await asyncio.to_thread(dest.write_bytes, data)
    mime = request.headers.get("content-type") or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    if mime == "application/octet-stream":
        mime = mimetypes.guess_type(safe)[0] or mime
    return {"id": fid, "name": name, "path": str(dest), "mime": mime, "size": len(data), "url": f"/api/uploads/{fid}"}


@app.get("/api/uploads/{fid}")
async def get_upload(fid: str):
    f = UPLOADS_DIR / os.path.basename(fid)
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/projects")
async def list_projects():
    return config.projects()


@app.post("/api/projects")
async def add_project(body: dict[str, Any] = Body(...)):
    """Open a directory as a project; ``create``: make it first (in an existing directory), ``git``: and git init it."""
    path = os.path.realpath(os.path.expanduser(body["path"]))
    if body.get("create"):
        name = os.path.basename(path)
        if not name or name.startswith(".") or not os.path.isdir(os.path.dirname(path)):
            raise HTTPException(400, f"can't create {path}: give a new folder's name, in an existing directory")
        await asyncio.to_thread(os.mkdir, path)  # FileExistsError: 409
        if body.get("git"):
            await gitops.init(path)
    proj = projects.open(path)  # on the loop: loading starts tracker/watcher tasks
    return proj.describe()


@app.patch("/api/projects/{pid}")
async def mark_project(pid: str, body: dict[str, Any] = Body(...)):
    """Pin / unpin, archive / unarchive (home page)."""
    if body.get("archived"):
        loaded = next((p for p in projects.loaded() if p.id == pid), None)
        busy = sessions.busy_in(loaded, loaded.path) if loaded else []
        if busy:
            who = f"“{busy[0]}” is" if len(busy) == 1 else f"{len(busy)} agents are"
            raise HTTPException(409, f"{who} in the middle of a turn here: archive the project when it's done")
        running = await terminals.running(pid)
        if running:
            raise HTTPException(409, f"“{running[0]}” is still running in a terminal here: stop it or close the terminal first")
    out = config.mark_project(pid, pinned=body.get("pinned"), archived=body.get("archived"))
    if body.get("archived"):
        await terminals.close_project(pid)  # nothing runs in them: an archived project is put away
    return out


@app.delete("/api/projects/{pid}")
async def remove_project(pid: str):
    p = projects.forget(pid)
    if p and p.tracker:
        p.tracker.stop()
    w = _watchers.pop(pid, None)
    if w:
        w.stop()
    await terminals.close_project(pid)
    config.remove_project(pid)
    return {"ok": True}


@app.get("/api/browse")
async def browse(path: str | None = None):
    return fs.browse(path)


_HOME_AGENT_FIELDS = ("id", "harness", "name", "title", "live", "pending", "status", "progress", "doing", "created_at", "updated_at")
_git_briefs: dict[str, tuple[float, dict[str, Any] | None]] = {}
_git_brief_tasks: dict[str, asyncio.Task] = {}


async def _git_brief(p: Project) -> dict[str, Any] | None:
    """Branch and number of changed files, for a home card: fresh for 15 s, then the last one is returned
    while a new one is worked out (a big repository's git status mustn't hold up the whole page)."""

    async def compute() -> dict[str, Any] | None:
        brief = None
        if gitops.is_repo(p.path):
            try:
                st = await gitops.status(p.path)
                brief = {"branch": st["branch"], "changed": len(st["files"])}
            except gitops.GitError:
                pass
        _git_briefs[p.id] = (time.monotonic(), brief)
        return brief

    hit = _git_briefs.get(p.id)
    if hit and time.monotonic() - hit[0] < 15:
        return hit[1]
    task = _git_brief_tasks.get(p.id)
    if task is None:
        task = _git_brief_tasks[p.id] = asyncio.create_task(compute())
        task.add_done_callback(lambda _t: _git_brief_tasks.pop(p.id, None))
    return hit[1] if hit else await asyncio.shield(task)


@app.get("/api/home")
async def home():
    """Every project for the home page: pins, archive, how often it was opened. For the others, also the
    Tracker's headline, the git branch and changed files, and their agents (the session rows' status fields,
    so the page shows them the way the rail does)."""

    async def one(entry: dict[str, Any]) -> dict[str, Any]:
        out = {k: entry.get(k) for k in ("id", "path", "name", "exists", "last_opened", "open_count", "pinned_at", "archived_at")}
        if not entry["exists"] or entry.get("archived_at"):
            return out
        try:
            p = P(entry["id"])
            rows, git = await asyncio.gather(sessions.list_sessions(p, details=False), _git_brief(p))
        except Exception as e:  # one broken project mustn't take the home page down
            log.warning("home: %s: %s", entry["path"], e)
            return {**out, "error": str(e) or type(e).__name__}
        agents = [{k: r.get(k) for k in _HOME_AGENT_FIELDS} for r in rows if r["role"] == "worker" and not r["archived"]]
        return {**out, "headline": p.board.headline(), "git": git, "agents": agents}

    return await asyncio.gather(*(one(e) for e in config.projects()))


@app.get("/api/p/{pid}")
async def project_info(pid: str, opened: bool = False):
    p = P(pid)
    config.touch_project(pid, opened)
    info = p.describe()
    info["worktrees"] = await gitops.worktrees(p.path) if info["is_git"] else []
    return info


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
@app.get("/api/p/{pid}/sessions")
async def list_sessions(pid: str, archived: bool = False):
    return await sessions.list_sessions(P(pid), archived)


@app.post("/api/p/{pid}/sessions")
async def create_session(pid: str, body: dict[str, Any] = Body(...)):
    p = P(pid)
    cwd = body.get("cwd") or p.path
    wt = body.get("worktree")
    if wt and wt.get("branch"):
        created = await gitops.add_worktree(p.path, wt["branch"], base=wt.get("base") or None, create_branch=wt.get("create", True))
        cwd = created["path"]
        transcript_watcher.add_project(p)
    attachments = [attachment_from_upload(a) for a in body.get("attachments") or []]
    sid = await sessions.create(
        p, name=body.get("name") or "", prompt=body.get("prompt") or None, harness_id=body.get("harness"),
        cwd=cwd, model=body.get("model") or None, effort=body.get("effort") or None, mode=body.get("mode") or None,
        attachments=attachments,
    )
    return {"id": sid, "cwd": cwd}


@app.get("/api/p/{pid}/sessions/{sid}")
async def get_session(pid: str, sid: str):
    p = P(pid)
    snap = await sessions.snapshot(p, sid)
    rows = [r for r in await sessions.list_sessions(p, True) if r["id"] == sid]
    snap["session"] = rows[0] if rows else None
    snap["board"] = p.board.agent(sid)
    return snap


@app.get("/api/p/{pid}/sessions/{sid}/subagents/{aid}")
async def get_subagent(pid: str, sid: str, aid: str):
    return await sessions.subagent(P(pid), sid, aid)


@app.get("/api/p/{pid}/sessions/{sid}/compactions/{cid}")
async def get_compaction(pid: str, sid: str, cid: str):
    return await sessions.compaction(P(pid), sid, cid)


@app.delete("/api/p/{pid}/sessions/{sid}")
async def delete_session(pid: str, sid: str):
    p = P(pid)
    try:
        backup = await sessions.delete(p, sid)
    except (NotImplementedError, ValueError) as e:
        raise HTTPException(400, str(e))
    await p.review.forget(sid)  # its review session goes with it
    return {"ok": True, "backup": backup}


@app.post("/api/p/{pid}/sessions/{sid}/fork")
async def fork_session(pid: str, sid: str, body: dict[str, Any] = Body(default={})):
    p = P(pid)
    attachments = [attachment_from_upload(a) for a in body.get("attachments") or []]
    try:
        res = await sessions.fork(
            p, sid, at=body.get("at") or None, workspace=body.get("workspace") or None, carry=body.get("carry") is not False,
            name=body.get("name") or "", prompt=(body.get("prompt") or "").strip() or None, attachments=attachments,
            archive_source=bool(body.get("archive_source")),
        )
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(400, str(e))
    if res.get("worktree"):
        transcript_watcher.add_project(p)
        hub.publish(f"project:{pid}", {"type": "git_changed"})
    return res


@app.patch("/api/p/{pid}/sessions/{sid}")
async def patch_session(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    p = P(pid)
    cwd, harness_id = await sessions.session_info(p, sid)
    if body.get("name"):
        name = body["name"].strip()
        p.set_session_meta(sid, name=name, name_source="user")
        p.board.rename_agent(sid, name)
        await get_harness(harness_id).rename_session(sid, name, cwd)
        h = sessions.live.get(sid)
        if h:
            h.name = name
    out: dict[str, Any] = {"ok": True}
    if "archived" in body:
        out["worktree"] = await sessions.set_archived(p, sid, bool(body["archived"]))
    sessions.invalidate(p)
    hub.publish(f"project:{pid}", {"type": "sessions_changed"})
    return out


@app.post("/api/p/{pid}/sessions/{sid}/workspace")
async def move_session(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    """Carry on in another directory: {"kind": "root"} | {"kind": "worktree", "path"} | {"kind": "new", "branch", "base"?}."""
    p = P(pid)
    try:
        res = await sessions.move(p, sid, body)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(400, str(e))
    transcript_watcher.add_project(p)  # watch where its transcript now goes
    await p.review.follow(sid)  # its reviewer goes along
    if res.get("worktree"):
        hub.publish(f"project:{pid}", {"type": "git_changed"})
    return res


@app.post("/api/p/{pid}/sessions/{sid}/send")
async def send(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    text = (body.get("text") or "").strip()
    attachments = [attachment_from_upload(a) for a in body.get("attachments") or []]
    if not text and not attachments:
        raise HTTPException(400, "empty message")
    await sessions.send(P(pid), sid, text, attachments)
    return {"ok": True}


@app.get("/api/p/{pid}/commands")
async def slash_commands(pid: str, sid: str | None = None, harness: str | None = None, cwd: str | None = None):
    """Slash commands for the composer: a session's, or a new agent's (``harness`` in ``cwd``)."""
    return await sessions.commands(P(pid), sid=sid, harness_id=harness, cwd=cwd)


@app.post("/api/p/{pid}/sessions/{sid}/attach")
async def attach(pid: str, sid: str, body: dict[str, Any] = Body(default={})):
    h = await sessions.ensure_live(P(pid), sid, model=body.get("model"), mode=body.get("mode"))
    return {"state": h.state}


@app.post("/api/p/{pid}/sessions/{sid}/interrupt")
async def interrupt(pid: str, sid: str):
    await sessions.interrupt(sid)
    return {"ok": True}


@app.post("/api/p/{pid}/sessions/{sid}/mode")
async def set_mode(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    project = P(pid)
    try:
        await sessions.set_mode(project, sid, body["mode"])
    except Exception as e:  # e.g. the CLI refusing the mode: show its reason instead of a bare 500
        raise HTTPException(400, str(e) or type(e).__name__)
    return {"ok": True}


@app.post("/api/p/{pid}/sessions/{sid}/model")
async def set_model(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    await sessions.set_model(P(pid), sid, body.get("model") or None)
    return {"ok": True}


@app.post("/api/p/{pid}/sessions/{sid}/effort")
async def set_effort(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    await sessions.set_effort(P(pid), sid, body.get("effort") or None)
    return {"ok": True}


@app.post("/api/p/{pid}/sessions/{sid}/permission")
async def permission(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    ok = sessions.resolve_permission(sid, body["request_id"], body)
    if not ok:
        raise HTTPException(404, "no such pending request")
    return {"ok": True}


@app.post("/api/p/{pid}/sessions/{sid}/close")
async def close(pid: str, sid: str):
    await sessions.close(sid)
    return {"ok": True}


# ---- per-session changes (diff review) -------------------------------------
@app.get("/api/p/{pid}/sessions/{sid}/changes")
async def session_changes(pid: str, sid: str):
    p = P(pid)
    cwd, hid = await sessions.session_info(p, sid)
    return await asyncio.to_thread(changes.session_changes, p, sid, cwd, hid)


@app.get("/api/p/{pid}/sessions/{sid}/changes/file")
async def session_change_file(pid: str, sid: str, path: str):
    p = P(pid)
    cwd, hid = await sessions.session_info(p, sid)
    return await asyncio.to_thread(changes.file_change, p, sid, cwd, hid, path)


@app.post("/api/p/{pid}/sessions/{sid}/changes/revert")
async def session_change_revert(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    p = P(pid)
    cwd, hid = await sessions.session_info(p, sid)
    return await asyncio.to_thread(changes.revert, p, sid, cwd, hid, body["path"])


@app.post("/api/p/{pid}/sessions/{sid}/changes/review")
async def session_change_review(pid: str, sid: str, body: dict[str, Any] = Body(...)):
    changes.mark_reviewed(P(pid), sid, body["path"], bool(body.get("reviewed", True)))
    return {"ok": True}


@app.get("/api/p/{pid}/changes")
async def project_changes(pid: str):
    """Change counts per agent, for the review inbox."""
    p = P(pid)

    rows = await sessions.list_sessions(p)

    def compute() -> list[dict[str, Any]]:
        out = []
        for s in rows:
            if s["role"] != "worker":
                continue
            cs = changes.session_changes(p, s["id"], s["cwd"], s["harness"])
            if cs["files"]:
                out.append(
                    {
                        "session": s,
                        "files": len(cs["files"]),
                        "unreviewed": sum(1 for f in cs["files"] if not f["reviewed"] and not f["unchanged"]),
                        "added": sum(f["added"] for f in cs["files"]),
                        "removed": sum(f["removed"] for f in cs["files"]),
                    }
                )
        return out

    return await asyncio.to_thread(compute)


# ---- code review -------------------------------------------------------------
@app.get("/api/p/{pid}/reviews")
async def reviews_summary(pid: str):
    """Per agent: whether a review runs, findings left to decide on (the review inbox)."""
    return P(pid).review.summary()


@app.get("/api/p/{pid}/reviews/{wsid}")
async def review_view(pid: str, wsid: str):
    return P(pid).review.view(wsid)


@app.post("/api/p/{pid}/reviews/{wsid}/run")
async def review_run(pid: str, wsid: str):
    try:
        return await P(pid).review.run(wsid)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e) or type(e).__name__)


@app.post("/api/p/{pid}/reviews/{wsid}/findings")
async def review_findings(pid: str, wsid: str, body: dict[str, Any] = Body(...)):
    try:
        return P(pid).review.set_status(wsid, int(body["round"]), [str(i) for i in body.get("ids") or []], body.get("status") or "")
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/p/{pid}/reviews/{wsid}/send")
async def review_send(pid: str, wsid: str, body: dict[str, Any] = Body(...)):
    """Send review feedback to the agent (queued until its turn ends if it is busy)."""
    try:
        return await P(pid).review.send(wsid, body.get("text") or "", n=body.get("round"), draft=body.get("draft"))
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e) or type(e).__name__)


@app.post("/api/p/{pid}/reviews/{wsid}/drafts/{did}/cancel")
async def review_cancel_draft(pid: str, wsid: str, did: int):
    P(pid).review.cancel_draft(wsid, did)
    return {"ok": True}


@app.post("/api/p/{pid}/reviews/{wsid}/outbox")
async def review_deliver(pid: str, wsid: str):
    """Send the queued feedback now, without waiting for the agent's turn to end."""
    await P(pid).review.deliver_now(wsid)
    return {"ok": True}


@app.delete("/api/p/{pid}/reviews/{wsid}/outbox")
async def review_cancel_outbox(pid: str, wsid: str):
    P(pid).review.cancel_outbox(wsid)
    return {"ok": True}


@app.post("/api/p/{pid}/reviews/{wsid}/auto")
async def review_auto(pid: str, wsid: str, body: dict[str, Any] = Body(...)):
    """Automatic reviews for this agent: true / false, or null for the setting's default."""
    P(pid).review.set_auto(wsid, body.get("auto"))
    return {"ok": True}


# ---------------------------------------------------------------------------
# board + tracker
# ---------------------------------------------------------------------------
@app.get("/api/p/{pid}/board.html")
async def board_html(pid: str):
    p = P(pid)
    return FileResponse(p.board.html_path, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/api/p/{pid}/board")
async def board_json(pid: str):
    return P(pid).board.snapshot()


@app.delete("/api/p/{pid}/board/agents/{sid}/cards/{key}")
async def delete_card(pid: str, sid: str, key: str):
    return {"ok": P(pid).board.remove_card(sid, key)}


@app.delete("/api/p/{pid}/board/agents/{sid}")
async def delete_board_agent(pid: str, sid: str):
    P(pid).board.remove_agent(sid)
    return {"ok": True}


@app.get("/api/p/{pid}/tracker")
async def tracker_status(pid: str):
    return P(pid).tracker.status()


@app.post("/api/p/{pid}/tracker/run")
async def tracker_run(pid: str):
    P(pid).tracker.run_now()
    return {"ok": True}


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
@app.get("/api/p/{pid}/files")
async def files(pid: str, path: str = "", root: str | None = None, hidden: bool = False):
    return await asyncio.to_thread(fs.list_dir, P(pid), root, path, hidden)


@app.get("/api/p/{pid}/file")
async def read_file(pid: str, path: str, root: str | None = None):
    return await asyncio.to_thread(fs.read_file, P(pid), root, path)


@app.put("/api/p/{pid}/file")
async def write_file(pid: str, body: dict[str, Any] = Body(...)):
    return await asyncio.to_thread(
        fs.write_file, P(pid), body.get("root"), body["path"], body.get("content", ""), body.get("base_sha"), bool(body.get("force"))
    )


@app.post("/api/p/{pid}/file/create")
async def create_file(pid: str, body: dict[str, Any] = Body(...)):
    return fs.create(P(pid), body.get("root"), body["path"], body.get("kind", "file"))


@app.post("/api/p/{pid}/file/rename")
async def rename_file(pid: str, body: dict[str, Any] = Body(...)):
    return fs.rename(P(pid), body.get("root"), body["src"], body["dst"])


@app.post("/api/p/{pid}/file/delete")
async def delete_file(pid: str, body: dict[str, Any] = Body(...)):
    return fs.trash(P(pid), body.get("root"), body["path"])


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
def _git_root(p: Project, root: str | None) -> str:
    r = os.path.realpath(root or p.path)
    p.resolve(".", root=r)
    return r


@app.get("/api/p/{pid}/git/summary")
async def git_summary(pid: str, root: str | None = None):
    p = P(pid)
    r = _git_root(p, root)
    if not gitops.is_repo(r):
        return {"is_repo": False}
    status, branches, worktrees = await asyncio.gather(gitops.status(r), gitops.branches(r), gitops.worktrees(r))
    return {"is_repo": True, "root": r, "status": status, "branches": branches, "worktrees": worktrees, "view": p.git_view()}


@app.get("/api/p/{pid}/git/graph")
async def git_graph(pid: str, mode: str = "view", refs: str | None = None, limit: int = 300):
    p = P(pid)
    if not gitops.is_repo(p.path):
        return {"commits": [], "head": None, "refs": [], "mode": mode}
    view = p.git_view()
    if refs:
        ref_list = [r for r in refs.split(",") if r]
    elif mode == "all":
        ref_list = []
    elif mode == "view" and view and view.get("branches"):
        ref_list = list(view["branches"])
    else:
        mode = "smart"
        ref_list = await gitops.smart_refs(p.path)
    valid = {b["name"] for b in await gitops.branches(p.path)}
    ref_list = [r for r in ref_list if r in valid or len(r) >= 7]
    data = await gitops.graph(p.path, ref_list + (["HEAD"] if ref_list else []), limit=limit)
    return {**data, "refs": ref_list, "mode": mode, "view": view}


@app.get("/api/p/{pid}/git/commit/{sha}")
async def git_commit(pid: str, sha: str):
    return await gitops.commit_detail(P(pid).path, sha)


@app.get("/api/p/{pid}/git/commit/{sha}/file")
async def git_commit_file(pid: str, sha: str, path: str, orig: str | None = None):
    return await gitops.commit_file(P(pid).path, sha, path, orig)


@app.get("/api/p/{pid}/git/diff")
async def git_diff(pid: str, path: str, root: str | None = None, staged: bool = False):
    p = P(pid)
    r = _git_root(p, root)
    original = await gitops.show_file_at(r, "HEAD", path) or ""
    if staged:
        current = await gitops.show_file_at(r, "", path) or ""  # ":path" = index
    else:
        target = p.resolve(path, root=r)
        current = fs.read_text(target) if target.exists() else ""
    return {"path": path, "original": original, "current": current or "", "editable": not staged}


@app.post("/api/p/{pid}/git/init")
async def git_init(pid: str):
    p = P(pid)
    if not gitops.is_repo(p.path):
        await gitops.init(p.path)
    hub.publish(f"project:{pid}", {"type": "git_changed"})
    return {"ok": True}


@app.post("/api/p/{pid}/git/stage")
async def git_stage(pid: str, body: dict[str, Any] = Body(...)):
    await gitops.stage(_git_root(P(pid), body.get("root")), body["paths"])
    return {"ok": True}


@app.post("/api/p/{pid}/git/unstage")
async def git_unstage(pid: str, body: dict[str, Any] = Body(...)):
    await gitops.unstage(_git_root(P(pid), body.get("root")), body["paths"])
    return {"ok": True}


@app.post("/api/p/{pid}/git/discard")
async def git_discard(pid: str, body: dict[str, Any] = Body(...)):
    await gitops.discard(_git_root(P(pid), body.get("root")), body["paths"])
    return {"ok": True}


@app.post("/api/p/{pid}/git/commit")
async def git_commit_create(pid: str, body: dict[str, Any] = Body(...)):
    out = await gitops.commit(_git_root(P(pid), body.get("root")), body["message"], amend=bool(body.get("amend")))
    return {"ok": True, "output": out}


@app.post("/api/p/{pid}/git/checkout")
async def git_checkout(pid: str, body: dict[str, Any] = Body(...)):
    await gitops.checkout(_git_root(P(pid), body.get("root")), body["branch"])
    return {"ok": True}


@app.post("/api/p/{pid}/git/branch")
async def git_branch(pid: str, body: dict[str, Any] = Body(...)):
    await gitops.create_branch(P(pid).path, body["name"], body.get("base") or None)
    return {"ok": True}


@app.post("/api/p/{pid}/git/worktrees")
async def git_add_worktree(pid: str, body: dict[str, Any] = Body(...)):
    p = P(pid)
    res = await gitops.add_worktree(p.path, body["branch"], base=body.get("base") or None, create_branch=body.get("create", True), dest=body.get("path") or None)
    transcript_watcher.add_project(p)
    hub.publish(f"project:{pid}", {"type": "git_changed"})
    return res


@app.post("/api/p/{pid}/git/worktrees/remove")
async def git_remove_worktree(pid: str, body: dict[str, Any] = Body(...)):
    p = P(pid)
    busy = sessions.busy_in(p, body["path"])
    if busy:
        who = f"“{busy[0]}” is" if len(busy) == 1 else f"{len(busy)} agents are"
        raise HTTPException(409, f"{who} still working in this worktree: wait for the turn to end, or stop it, before removing the worktree")
    await gitops.remove_worktree(p.path, body["path"], force=bool(body.get("force")))
    await sessions.release_gone(p)  # its agents stay, and continue wherever they are moved to
    sessions.invalidate(p)
    hub.publish(f"project:{pid}", {"type": "git_changed"})
    hub.publish(f"project:{pid}", {"type": "sessions_changed"})
    return {"ok": True}


@app.post("/api/p/{pid}/git/view")
async def git_set_view(pid: str, body: dict[str, Any] = Body(...)):
    return set_view(P(pid), body.get("branches"), body.get("highlight"), body.get("note"), source="user")


@app.delete("/api/p/{pid}/git/view")
async def git_reset_view(pid: str):
    p = P(pid)
    p.set_git_view(None)
    hub.publish(f"project:{pid}", {"type": "git_view", "view": None})
    return {"ok": True}


@app.post("/api/p/{pid}/git/agent")
async def git_agent(pid: str):
    """Ensure the project's Git Agent session exists and is attached."""
    p = P(pid)
    sid = p.git_agent_session()
    if sid:
        await sessions.ensure_live(p, sid)
    else:
        sid = await sessions.create(p, name="Git Agent", prompt=None, role="git", harness_id=config.get("git_agent.harness") or None,
                                    model=config.get("git_agent.model") or None)
    return {"id": sid}


# ---------------------------------------------------------------------------
# terminals
# ---------------------------------------------------------------------------
@app.get("/api/p/{pid}/terminals")
async def list_terminals(pid: str):
    return terminals.list(P(pid).id)


@app.post("/api/p/{pid}/terminals")
async def create_terminal(pid: str, body: dict[str, Any] = Body(default={})):
    """A shell in the project's directory or one of its worktrees (``root``), at the page's size."""
    p = P(pid)
    try:
        t = await terminals.create(p.id, _git_root(p, body.get("root")), int(body.get("cols") or 80), int(body.get("rows") or 24))
    except TerminalError as e:
        raise HTTPException(400, str(e))
    return t.describe()


@app.delete("/api/p/{pid}/terminals/{tid}")
async def close_terminal(pid: str, tid: str, force: bool = False):
    """Close a terminal; while a command runs in it, only with ``force`` (the 409 names the command)."""
    t = terminals.get(pid, tid)
    cmd = await t.foreground() if t and not force else None
    if cmd:
        return JSONResponse({"error": f"“{cmd}” is still running in this terminal", "code": "terminal_busy", "command": cmd}, status_code=409)
    await terminals.close(pid, tid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# websocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    sub = Subscriber()

    async def pump() -> None:
        while True:
            msg = await sub.queue.get()
            await socket.send_json(msg)

    sender = asyncio.create_task(pump())
    try:
        while True:
            msg = await socket.receive_json()
            op, topic = msg.get("op"), msg.get("topic")
            if op == "sub" and topic:
                hub.subscribe(sub, topic)
            elif op == "unsub" and topic:
                hub.unsubscribe(sub, topic)
            elif op == "presence":
                sub.active = bool(msg.get("active"))
            elif op == "ping":
                await socket.send_json({"topic": "_", "type": "pong"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sender.cancel()
        hub.drop(sub)


def _lsp_roots() -> list[tuple[str, str]]:
    return [(r, p.path) for p in projects.loaded() for r in p.roots()]


@app.websocket("/ws/lsp")
async def ws_lsp(socket: WebSocket):
    """LSP for the editor; see atrium/lsp.py."""
    await socket.accept()
    client = lsp.LspClient(_lsp_roots)

    async def pump() -> None:
        while True:
            await socket.send_json(await client.queue.get())

    sender = asyncio.create_task(pump())
    try:
        while True:
            msg = await socket.receive_json()
            # attach waits for the server to initialize; keep reading meanwhile
            if msg.get("op") == "attach":
                asyncio.create_task(client.handle(msg))
            else:
                await client.handle(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sender.cancel()
        await client.close()


@app.websocket("/ws/terminal/{pid}/{tid}")
async def ws_terminal(socket: WebSocket, pid: str, tid: str):
    """One page attached to a terminal: its bytes both ways, JSON for the rest; see atrium/terminal.py."""
    await socket.accept()
    term = terminals.get(pid, tid) if terminals.available() else None
    if term is None:
        await socket.close(4404, "no such terminal")
        return
    client = term.attach()

    async def pump() -> None:
        while True:
            for item in await client.take():
                if item is None:  # closed, or its shell exited with 0
                    await socket.close(4000, "terminal closed")
                    return
                if isinstance(item, dict):
                    await socket.send_json(item)
                else:
                    await socket.send_bytes(bytes(item))

    sender = asyncio.create_task(pump())
    try:
        while True:
            msg = await socket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                term.write(msg["bytes"])
            elif msg.get("text"):
                ctl = json.loads(msg["text"])
                if ctl.get("type") == "resize":
                    term.resize(int(ctl["cols"]), int(ctl["rows"]))
    except (WebSocketDisconnect, RuntimeError, ValueError, KeyError, TypeError):
        pass
    finally:
        sender.cancel()
        term.detach(client)


# ---------------------------------------------------------------------------
# frontend
# ---------------------------------------------------------------------------
if (STATIC_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    f = STATIC_DIR / path
    if path and f.is_file():
        return FileResponse(f)
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-store"})
    return Response("Atrium's UI is not built yet. Run `make build` (or use `make dev-web`).", media_type="text/plain")
