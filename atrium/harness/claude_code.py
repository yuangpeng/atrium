"""Claude Code harness, built on ``claude-agent-sdk`` (Python)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    delete_session,
    get_session_messages,
    list_sessions,
    project_key_for_directory,
    query,
    rename_session,
    tool,
)

from .. import gitops
from ..config import UPLOADS_DIR
from .base import Attachment, Emit, FileEdit, Harness, Item, LiveSession, LiveSpec, SessionSummary, ToolSpec
from .util import attachment_note, hhmm, parse_json_loose, split_attachment_note

log = logging.getLogger("atrium.claude")

MAX_RESULT_CHARS = 20_000
MAX_INLINE_IMAGE_B64 = 3_000_000  # history images above this are shown as a placeholder
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))


def _claude_projects_dir() -> Path:
    return _claude_home() / "projects"


def _effort_settings(effort: str | None) -> str | None:
    """Flag-level settings pinning the effort.

    ``--effort`` alone loses against ``env.CLAUDE_CODE_EFFORT_LEVEL`` in the user's
    settings.json; flag settings sit above every other settings source.
    """
    return json.dumps({"env": {EFFORT_ENV: effort}}) if effort else None


def _short(s: Any, n: int) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _block_text(content: Any) -> str:
    """Flatten tool_result / message content into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False)[:2000])
            else:
                parts.append(str(b))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, indent=1)
    return str(content)


def _clip(s: str, n: int = MAX_RESULT_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n} more chars]"


_CMD_RE = re.compile(r"<(command-name|command-message|command-args|local-command-stdout|local-command-stderr)>(.*?)</\1>", re.S)


def _user_text_item(uid: str, text: str, ts: str | None = None) -> Item:
    """User-role text, recognising CLI command wrappers and interrupt markers."""
    stripped = text.strip()
    if stripped.startswith("<command-") or stripped.startswith("<local-command"):
        parts = {m.group(1): m.group(2).strip() for m in _CMD_RE.finditer(stripped)}
        label = parts.get("command-name") or ""
        out = parts.get("local-command-stdout") or parts.get("local-command-stderr") or parts.get("command-args") or ""
        return {"kind": "system", "id": uid, "subtype": "command", "text": f"{label} {out}".strip() or stripped}
    if stripped.startswith("[Request interrupted by user"):
        return {"kind": "system", "id": uid, "subtype": "interrupted", "text": "Interrupted by user"}
    text, atts = split_attachment_note(text)
    item: Item = {"kind": "user", "id": uid, "text": text}
    if atts:
        item["attachments"] = atts
    if ts:
        item["ts"] = ts
    return item


def _image_attachment(block: dict[str, Any], idx: int) -> dict[str, Any]:
    src = block.get("source") or {}
    att: dict[str, Any] = {"name": f"image-{idx + 1}", "mime": src.get("media_type") or "image/png"}
    if src.get("type") == "base64" and len(src.get("data") or "") <= MAX_INLINE_IMAGE_B64:
        att["url"] = f"data:{att['mime']};base64,{src['data']}"
    return att


def _tool_brief(name: str, inp: dict[str, Any]) -> str:
    if name.startswith(("mcp__board__", "mcp__atrium_board__")):
        keep = {k: v for k, v in inp.items() if k != "html"}
        return _short(keep, 200)
    for k in ("file_path", "notebook_path", "path", "command", "pattern", "url", "query", "description", "prompt"):
        if k in inp and inp[k]:
            return f"{k}={_short(inp[k], 180)}"
    return _short(inp, 160)


def _patch_stats(patch: Any) -> tuple[int, int]:
    add = rem = 0
    for h in patch or []:
        for line in h.get("lines", []):
            if line.startswith("+"):
                add += 1
            elif line.startswith("-"):
                rem += 1
    return add, rem


# --------------------------------------------------------------------------
# Live session
# --------------------------------------------------------------------------


class ClaudeLiveSession(LiveSession):
    def __init__(self, spec: LiveSpec, emit: Emit):
        self.spec = spec
        self.session_id = spec.session_id or str(uuid.uuid4())
        self.emit = emit
        self.state = "starting"
        self._inbox: asyncio.Queue[tuple[str, Any, asyncio.Future]] = asyncio.Queue()
        self._pending: dict[str, tuple[asyncio.Future, ToolPermissionContext, str]] = {}
        self._ready = asyncio.Event()
        self._error: str | None = None
        self._task: asyncio.Task | None = None
        self._stderr_tail: list[str] = []

    # ---- public API --------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"claude-live-{self.session_id[:8]}")
        waiter = asyncio.create_task(self._ready.wait())
        done, _ = await asyncio.wait({waiter, self._task}, timeout=120, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if self._error or not self._ready.is_set():
            raise RuntimeError(self._error or "Claude Code did not start in time")

    async def send(self, text: str, attachments: list[Attachment] | None = None) -> None:
        await self._call("send", (text, attachments or []))

    async def interrupt(self) -> None:
        await self._call("interrupt", None)

    async def set_permission_mode(self, mode: str) -> None:
        await self._call("mode", mode)

    async def set_model(self, model: str | None) -> None:
        await self._call("model", model or None)

    async def set_effort(self, effort: str | None) -> None:
        await self._call("effort", effort or None)

    def resolve_permission(self, request_id: str, decision: dict[str, Any]) -> bool:
        entry = self._pending.get(request_id)
        if not entry or entry[0].done():
            return False
        entry[0].set_result(decision)
        return True

    def pending_permissions(self) -> list[str]:
        return list(self._pending)

    async def close(self) -> None:
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._call("close", None), timeout=5)
            except Exception:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except Exception:
                self._task.cancel()
        self._set_state("closed")

    # ---- internals ---------------------------------------------------------
    async def _call(self, op: str, arg: Any) -> Any:
        if self._task is None or self._task.done():
            raise RuntimeError("session is not running")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._inbox.put((op, arg, fut))
        return await fut

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit({"kind": "state", "state": state})

    def _stderr(self, line: str) -> None:
        self._stderr_tail = (self._stderr_tail + [line])[-30:]
        log.debug("[%s] %s", self.session_id[:8], line.rstrip())

    def _options(self) -> ClaudeAgentOptions:
        spec = self.spec
        mcp_servers: dict[str, Any] = dict(spec.extra_mcp_servers)
        allowed = list(spec.allowed_tools)
        for server_name, specs in spec.toolsets.items():
            mcp_servers[server_name] = to_sdk_server(server_name, specs)
            allowed += [f"mcp__{server_name}__{s.name}" for s in specs]
        system_prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
        if spec.system_append:
            # snapshot=False: the append is re-applied on resume, so sessions started
            # from the CLI also learn the board protocol once Atrium resumes them.
            system_prompt.update(append=spec.system_append, snapshot=False)
        return ClaudeAgentOptions(
            cwd=spec.cwd,
            system_prompt=system_prompt,  # type: ignore[arg-type]
            permission_mode=spec.permission_mode or None,  # type: ignore[arg-type]
            model=spec.model or None,
            effort=spec.effort or None,  # type: ignore[arg-type]
            settings=_effort_settings(spec.effort),
            resume=self.session_id if spec.resume else None,
            session_id=None if spec.resume else self.session_id,
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            disallowed_tools=spec.disallowed_tools,
            add_dirs=[str(UPLOADS_DIR)],  # composer attachments are readable without prompts
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
            stderr=self._stderr,
        )

    async def _run(self) -> None:
        try:
            async with ClaudeSDKClient(self._options()) as client:
                self._set_state("idle")
                self._ready.set()
                reader = asyncio.create_task(self._read_loop(client))
                try:
                    while True:
                        getter = asyncio.create_task(self._inbox.get())
                        done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                        if getter not in done:
                            getter.cancel()
                            break  # CLI process ended
                        op, arg, fut = getter.result()
                        try:
                            if op == "send":
                                self._set_state("running")
                                await client.query(_user_message(*arg))
                            elif op == "interrupt":
                                await client.interrupt()
                            elif op == "mode":
                                await client.set_permission_mode(arg)
                            elif op == "model":
                                await client.set_model(arg)
                            elif op == "effort":
                                # shallow-merged into flag settings; {} drops our override
                                await client._query._send_control_request(  # type: ignore[union-attr]
                                    {"subtype": "apply_flag_settings", "settings": {"env": {EFFORT_ENV: arg} if arg else {}}}
                                )
                            elif op == "close":
                                fut.set_result(None)
                                break
                            if not fut.done():
                                fut.set_result(None)
                        except Exception as e:  # surface to caller
                            if not fut.done():
                                fut.set_exception(e)
                finally:
                    reader.cancel()
                    for fut, _ctx, _tool in list(self._pending.values()):
                        if not fut.done():
                            fut.set_result({"behavior": "deny", "message": "Session closed"})
                    self._fail_inbox()
        except Exception as e:
            tail = "".join(self._stderr_tail[-8:]).strip()
            self._error = f"{type(e).__name__}: {e}" + (f"\n{tail}" if tail else "")
            log.exception("live session %s failed", self.session_id)
            self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": self._error})
            self._set_state("error")
            self._ready.set()
            self._fail_inbox()
            return
        self._set_state("closed")

    def _fail_inbox(self) -> None:
        while not self._inbox.empty():
            _op, _arg, fut = self._inbox.get_nowait()
            if not fut.done():
                fut.set_exception(RuntimeError("session closed"))

    async def _read_loop(self, client: ClaudeSDKClient) -> None:
        async for msg in client.receive_messages():
            try:
                for item in live_message_items(msg):
                    self.emit(item)
                if isinstance(msg, ResultMessage):
                    self._set_state("waiting" if self._pending else "idle")
            except Exception:
                log.exception("failed to normalize message")

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext):
        rid = ctx.tool_use_id or f"perm-{uuid.uuid4().hex[:10]}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = (fut, ctx, tool_name)
        self.emit(
            {
                "kind": "permission",
                "id": rid,
                "tool": tool_name,
                "input": tool_input,
                "title": ctx.title,
                "display_name": ctx.display_name,
                "description": ctx.description,
                "reason": ctx.decision_reason,
                "blocked_path": ctx.blocked_path,
                "can_remember": bool(ctx.suggestions),
            }
        )
        self._set_state("waiting")
        try:
            decision = await fut
        finally:
            self._pending.pop(rid, None)
        behavior = decision.get("behavior", "deny")
        self.emit({"kind": "permission_resolved", "id": rid, "decision": behavior})
        if self.state == "waiting" and not self._pending:
            self._set_state("running")
        if behavior == "allow":
            updated = decision.get("updated_input") or tool_input
            if decision.get("answers"):
                updated = {**tool_input, "answers": decision["answers"]}
            perms = ctx.suggestions if decision.get("remember") else None
            return PermissionResultAllow(updated_input=updated, updated_permissions=perms)
        return PermissionResultDeny(
            message=decision.get("message") or "The user declined this action.",
            interrupt=bool(decision.get("interrupt")),
        )


def _user_message(text: str, attachments: list[Attachment]) -> str | Any:
    """Plain text, or an async stream with one multimodal user message."""
    images = [a for a in attachments if a.is_image]
    files = [a for a in attachments if not a.is_image]
    text = text + attachment_note(files)
    if not images:
        return text
    content: list[dict[str, Any]] = []
    for a in images:
        data = base64.b64encode(Path(a.path).read_bytes()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": a.mime, "data": data}})
    content.append({"type": "text", "text": text or "(see attached image)"})

    async def stream():
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    return stream()


def live_message_items(msg: Any) -> list[Item]:
    """Normalize one SDK stream message into chat items."""
    items: list[Item] = []
    if isinstance(msg, StreamEvent):
        ev = msg.event or {}
        if msg.parent_tool_use_id is None and ev.get("type") == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta":
                items.append({"kind": "delta", "text": d.get("text", "")})
        return items
    if isinstance(msg, AssistantMessage):
        base = msg.uuid or msg.message_id or uuid.uuid4().hex
        for i, b in enumerate(msg.content):
            iid = f"{base}:{i}"
            if isinstance(b, TextBlock):
                if msg.parent_tool_use_id is None and b.text.strip():
                    items.append({"kind": "text", "id": iid, "text": b.text})
            elif isinstance(b, ThinkingBlock):
                if b.thinking.strip():
                    items.append({"kind": "thinking", "id": iid, "text": b.thinking})
            elif isinstance(b, (ToolUseBlock, ServerToolUseBlock)):
                it: Item = {"kind": "tool", "id": b.id, "name": b.name, "input": b.input}
                if msg.parent_tool_use_id:
                    it["parent"] = msg.parent_tool_use_id
                items.append(it)
            elif isinstance(b, ServerToolResultBlock):
                items.append({"kind": "tool_result", "id": b.tool_use_id, "content": _clip(_block_text(b.content)), "is_error": False})
        if msg.error:
            items.append({"kind": "error", "id": f"{base}:err", "text": f"API error: {msg.error}"})
        return items
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            if msg.content.strip():
                kind = (msg.origin or {}).get("kind")
                if kind and kind != "human":
                    items.append({"kind": "system", "id": msg.uuid or uuid.uuid4().hex, "subtype": kind, "text": _short(msg.content, 2000)})
                else:
                    items.append(_user_text_item(msg.uuid or uuid.uuid4().hex, msg.content))
        else:
            for i, b in enumerate(msg.content):
                if isinstance(b, ToolResultBlock):
                    items.append(
                        {"kind": "tool_result", "id": b.tool_use_id, "content": _clip(_block_text(b.content)), "is_error": bool(b.is_error)}
                    )
                elif isinstance(b, TextBlock) and b.text.strip():
                    items.append(_user_text_item(f"{msg.uuid}:{i}", b.text))
        return items
    if isinstance(msg, ResultMessage):
        items.append(
            {
                "kind": "result",
                "id": msg.uuid or f"result-{uuid.uuid4().hex[:8]}",
                "subtype": msg.subtype,
                "is_error": msg.is_error,
                "duration_ms": msg.duration_ms,
                "cost_usd": msg.total_cost_usd,
                "turns": msg.num_turns,
                "text": msg.result if msg.is_error else None,
                "terminal_reason": msg.terminal_reason,
            }
        )
        return items
    if isinstance(msg, TaskStartedMessage):
        items.append({"kind": "system", "id": msg.uuid, "subtype": "task_started", "text": f"Subagent started: {msg.description}"})
        return items
    if isinstance(msg, TaskNotificationMessage):
        items.append({"kind": "system", "id": msg.uuid, "subtype": "task_done", "text": f"Subagent {msg.status}: {_short(msg.summary, 300)}"})
        return items
    if isinstance(msg, SystemMessage):
        if msg.subtype == "init":
            d = msg.data or {}
            mcp = ", ".join(f"{s.get('name')}:{s.get('status')}" for s in d.get("mcp_servers", []) or [])
            items.append(
                {
                    "kind": "system",
                    "id": f"init-{d.get('session_id', '')}-{int(time.time())}",
                    "subtype": "init",
                    "text": f"Connected · model {d.get('model', '?')} · mode {d.get('permissionMode', '?')}" + (f" · MCP {mcp}" if mcp else ""),
                    "model": d.get("model"),
                    "permission_mode": d.get("permissionMode"),
                }
            )
        elif msg.subtype == "compact_boundary":
            items.append({"kind": "system", "id": f"compact-{uuid.uuid4().hex[:8]}", "subtype": "compact", "text": "Conversation compacted"})
        return items
    return items


def history_items(messages: list[Any]) -> list[Item]:
    """Normalize ``get_session_messages`` output."""
    items: list[Item] = []
    for m in messages:
        raw = m.message or {}
        content = raw.get("content")
        if m.type == "user":
            if isinstance(content, str):
                if content.strip():
                    items.append(_user_text_item(m.uuid, content))
                continue
            images = [b for b in content or [] if isinstance(b, dict) and b.get("type") == "image"]
            if images:  # one user bubble: text + attached images
                text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                it = _user_text_item(m.uuid, text)
                it["attachments"] = [_image_attachment(b, i) for i, b in enumerate(images)] + it.get("attachments", [])
                items.append(it)
                continue
            for i, b in enumerate(content or []):
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "tool_result":
                    items.append(
                        {
                            "kind": "tool_result",
                            "id": b.get("tool_use_id"),
                            "content": _clip(_block_text(b.get("content"))),
                            "is_error": bool(b.get("is_error")),
                        }
                    )
                elif t == "text" and b.get("text", "").strip():
                    items.append(_user_text_item(f"{m.uuid}:{i}", b["text"]))
        elif m.type == "assistant":
            for i, b in enumerate(content or []):
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                iid = f"{m.uuid}:{i}"
                if t == "text" and b.get("text", "").strip():
                    items.append({"kind": "text", "id": iid, "text": b["text"]})
                elif t == "thinking" and b.get("thinking", "").strip():
                    items.append({"kind": "thinking", "id": iid, "text": b["thinking"]})
                elif t in ("tool_use", "server_tool_use"):
                    items.append({"kind": "tool", "id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}})
                elif t and t.endswith("_tool_result"):
                    items.append({"kind": "tool_result", "id": b.get("tool_use_id"), "content": _clip(_block_text(b.get("content"))), "is_error": False})
    return items


def to_sdk_server(server_name: str, specs: list[ToolSpec]) -> Any:
    """Expose harness-neutral ToolSpecs as an in-process SDK MCP server."""
    sdk_tools = []
    for spec in specs:

        def make(spec: ToolSpec):
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                try:
                    out = await spec.handler(args or {})
                except Exception as e:  # report to the model instead of crashing the turn
                    return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
                if isinstance(out, dict):
                    return {"content": [{"type": "text", "text": out.get("text", "")}], "is_error": bool(out.get("is_error"))}
                return {"content": [{"type": "text", "text": str(out)}]}

            return handler

        sdk_tools.append(tool(spec.name, spec.description, spec.input_schema)(make(spec)))
    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=sdk_tools)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class ClaudeCodeHarness(Harness):
    id = "claude-code"
    label = "Claude Code"
    permission_modes = [
        {"id": "default", "label": "Ask permissions"},
        {"id": "acceptEdits", "label": "Accept edits"},
        {"id": "plan", "label": "Plan mode"},
        {"id": "auto", "label": "Auto (classifier)"},
        {"id": "bypassPermissions", "label": "Bypass permissions"},
    ]
    models = [
        {"id": "", "label": "Default"},
        {"id": "opus", "label": "Opus"},
        {"id": "sonnet", "label": "Sonnet"},
        {"id": "haiku", "label": "Haiku"},
    ]
    efforts = EFFORTS

    def config_files(self, project_dir: str | None) -> list[Any]:
        from ..configfiles import ConfigFile

        files = [
            ConfigFile(
                id="claude-user", label="Claude Code", path=str(_claude_home() / "settings.json"), language="json",
                scope="user", harness=self.id, hint="Model, env, permissions, hooks, MCP… for every Claude Code session. Applies to sessions started afterwards.",
            )
        ]
        if project_dir:
            files += [
                ConfigFile(
                    id="claude-project", label="Claude Code · project", path=os.path.join(project_dir, ".claude", "settings.json"),
                    language="json", scope="project", harness=self.id, hint="Shared project settings (usually committed).",
                ),
                ConfigFile(
                    id="claude-local", label="Claude Code · local", path=os.path.join(project_dir, ".claude", "settings.local.json"),
                    language="json", scope="local", harness=self.id, hint="Personal project overrides (git-ignored).",
                ),
            ]
        return files

    # ---- discovery ---------------------------------------------------------
    async def list_sessions(self, project_dir: str, roots: list[str]) -> list[SessionSummary]:
        return await asyncio.to_thread(self._list_sessions, project_dir)

    def _list_sessions(self, project_dir: str) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        try:
            infos = list_sessions(directory=project_dir, include_worktrees=True)
        except Exception:
            log.exception("list_sessions failed")
            return out
        for s in infos:
            cwd = s.cwd or project_dir
            path = self.transcript_path(s.session_id, cwd)
            out.append(
                SessionSummary(
                    id=s.session_id,
                    harness=self.id,
                    title=s.custom_title or s.summary or (s.first_prompt or "")[:80] or "Untitled",
                    updated_at=s.last_modified / 1000.0,
                    created_at=(s.created_at / 1000.0) if s.created_at else None,
                    first_prompt=s.first_prompt,
                    cwd=cwd,
                    git_branch=s.git_branch,
                    size=s.file_size,
                    transcript=str(path) if path else None,
                )
            )
        return out

    def transcript_path(self, session_id: str, cwd: str) -> Path | None:
        p = _claude_projects_dir() / project_key_for_directory(cwd) / f"{session_id}.jsonl"
        if p.exists():
            return p
        for cand in _claude_projects_dir().glob(f"*/{session_id}.jsonl"):
            return cand
        return None

    def transcript_dirs(self, project_dir: str) -> list[Path]:
        roots = [project_dir] + [w for w in gitops.worktree_paths_sync(project_dir) if w != project_dir]
        return [_claude_projects_dir() / project_key_for_directory(r) for r in roots]

    async def rename_session(self, session_id: str, title: str, cwd: str) -> None:
        try:
            await asyncio.to_thread(rename_session, session_id, title, directory=cwd)
        except Exception as e:  # transcript may not exist yet
            log.debug("rename_session(%s) skipped: %s", session_id, e)

    # ---- content -----------------------------------------------------------
    async def load_history(self, session_id: str, cwd: str) -> list[Item]:
        def load() -> list[Item]:
            msgs = get_session_messages(session_id, directory=cwd)
            if not msgs:
                msgs = get_session_messages(session_id)
            return history_items(msgs)

        return await asyncio.to_thread(load)

    def digest_lines(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        for line in lines:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            t = d.get("type")
            ts = hhmm(d.get("timestamp"))
            pre = f"[{ts}] " if ts else ""
            if t == "user" and not d.get("isMeta"):
                tur = d.get("toolUseResult")
                if isinstance(tur, dict) and tur.get("filePath") and "structuredPatch" in tur:
                    a, r = _patch_stats(tur.get("structuredPatch"))
                    verb = "created" if tur.get("type") == "create" else "edited"
                    out.append(f"{pre}{verb} {tur['filePath']} (+{a} -{r})")
                    continue
                content = (d.get("message") or {}).get("content")
                if isinstance(content, str):
                    if content.strip() and not content.lstrip().startswith("<"):
                        out.append(f"{pre}USER: {_short(content, 600)}")
                    continue
                for b in content or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        out.append(f"{pre}USER: {_short(b['text'], 600)}")
                    elif b.get("type") == "tool_result" and b.get("is_error"):
                        out.append(f"{pre}TOOL ERROR: {_short(_block_text(b.get('content')), 240)}")
            elif t == "assistant":
                for b in (d.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        out.append(f"{pre}AGENT: {_short(b['text'], 700)}")
                    elif b.get("type") == "tool_use":
                        name = b.get("name", "")
                        if name in ("Read", "Glob", "Grep", "LS"):
                            continue  # too noisy to matter for progress tracking
                        out.append(f"{pre}TOOL {name}: {_tool_brief(name, b.get('input') or {})}")
            elif t == "custom-title":
                out.append(f"{pre}(session renamed to '{d.get('customTitle')}')")
        return out

    def file_edits(self, session_id: str, cwd: str) -> list[FileEdit]:
        path = self.transcript_path(session_id, cwd)
        if not path:
            return []
        files = [path] + sorted((path.parent / session_id / "subagents").glob("*.jsonl"))
        edits: list[FileEdit] = []
        for f in files:
            try:
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"toolUseResult"' not in line or '"filePath"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tur = d.get("toolUseResult")
                    if not isinstance(tur, dict) or not tur.get("filePath"):
                        continue
                    edit = _edit_from_result(tur, d.get("timestamp"))
                    if edit:
                        edits.append(edit)
        edits.sort(key=lambda e: e.ts or "")
        return edits

    # ---- execution ---------------------------------------------------------
    def create_live(self, spec: LiveSpec, emit: Emit) -> LiveSession:
        return ClaudeLiveSession(spec, emit)

    async def complete_json(self, *, system, prompt, schema, model, effort, cwd):
        opts = ClaudeAgentOptions(
            system_prompt=system,
            tools=[],
            max_turns=4,
            model=model or None,
            effort=effort or None,
            settings=_effort_settings(effort),
            cwd=cwd,
            output_format={"type": "json_schema", "schema": schema},
            strict_mcp_config=True,
            setting_sources=["user"],
            permission_mode="dontAsk",
        )
        started = time.time()
        result: ResultMessage | None = None
        texts: list[str] = []
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result = msg
        if result is None:
            raise RuntimeError("no result from model")
        try:  # one-shot calls should not pile up as sessions
            delete_session(result.session_id, directory=cwd)
        except Exception:
            pass
        if result.is_error:
            raise RuntimeError(result.result or f"model error ({result.subtype})")
        data = result.structured_output
        if not isinstance(data, dict):
            data = parse_json_loose(result.result or "\n".join(texts))
        usage = {"cost_usd": result.total_cost_usd, "duration_ms": int((time.time() - started) * 1000), "turns": result.num_turns}
        return data, usage


def _edit_from_result(tur: dict[str, Any], ts: str | None) -> FileEdit | None:
    fp = tur["filePath"]
    if "oldString" in tur:  # Edit
        before = tur.get("originalFile")
        after = None
        if isinstance(before, str):
            old, new = tur.get("oldString") or "", tur.get("newString") or ""
            after = before.replace(old, new) if tur.get("replaceAll") else before.replace(old, new, 1)
        return FileEdit(path=fp, tool="Edit", ts=ts, before=before, after=after)
    if "edits" in tur:  # MultiEdit (older CLIs)
        before = tur.get("originalFile")
        after = before
        if isinstance(after, str):
            for e in tur.get("edits") or []:
                old, new = e.get("old_string", ""), e.get("new_string", "")
                after = after.replace(old, new) if e.get("replace_all") else after.replace(old, new, 1)
        return FileEdit(path=fp, tool="MultiEdit", ts=ts, before=before, after=after)
    if tur.get("type") in ("create", "update") and "content" in tur:  # Write
        kind = "create" if tur.get("type") == "create" else "update"
        return FileEdit(path=fp, tool="Write", ts=ts, before=tur.get("originalFile"), after=tur.get("content"), kind=kind)
    return None
