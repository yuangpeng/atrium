"""Codex harness, built on ``codex app-server`` (JSON-RPC over stdio).

One app-server process serves every Codex session (thread) Atrium runs — see
``codex_rpc.py``. Atrium's tools are handed to Codex as *dynamic tools* in
namespaces (``board.status``, ``web.search``, ``graph.set_view``) and come back
as ``item/tool/call`` requests. Items are normalized to the same chat items as
Claude Code (``Bash``, ``mcp__board__status``…); patches become ``Patch``.

Sessions are discovered with ``thread/list`` (filtered by the project's
directories); transcripts are the rollout files under ``~/.codex/sessions``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import config
from .base import Attachment, Emit, FileEdit, Harness, Item, LiveSession, LiveSpec, SessionSummary, ToolSpec
from .codex_rpc import AppServerClient, RpcError
from .util import attachment_note, clip, hhmm, parse_json_loose, short, split_attachment_note, strict_schema, upload_url

log = logging.getLogger("atrium.codex")

PERMISSION_MODES = [
    {"id": "untrusted", "label": "Ask permissions"},
    {"id": "auto", "label": "Auto (workspace write)"},
    {"id": "read-only", "label": "Read only"},
    {"id": "full-access", "label": "Full access"},
    {"id": "config", "label": "From config.toml"},
]
# mode -> (approvalPolicy, sandbox mode); "config" sends nothing and lets config.toml decide
MODE_POLICY = {
    "untrusted": ("untrusted", "workspace-write"),
    "auto": ("on-request", "workspace-write"),
    "read-only": ("on-request", "read-only"),
    "full-access": ("never", "danger-full-access"),
}
SANDBOX_POLICY = {
    "workspace-write": {"type": "workspaceWrite"},
    "read-only": {"type": "readOnly"},
    "danger-full-access": {"type": "dangerFullAccess"},
}
SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "unknown"]
# Atrium namespace -> Codex dynamic-tool namespace ("web" is reserved by the Responses API)
NS_ALIAS = {"web": "internet"}
NS_UNALIAS = {v: k for k, v in NS_ALIAS.items()}
DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh"]
SKIP_DIGEST_ACTIONS = {"read", "listFiles", "search", "list_files"}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def unwrap_command(cmd: Any) -> str:
    """``/bin/zsh -lc 'cat x'`` → ``cat x`` (Codex runs every command through a login shell)."""
    argv = cmd if isinstance(cmd, list) else None
    if argv is None:
        try:
            argv = shlex.split(cmd or "")
        except ValueError:
            return str(cmd or "")
    if len(argv) == 3 and argv[1] in ("-lc", "-c") and argv[0].rsplit("/", 1)[-1] in ("sh", "bash", "zsh", "fish"):
        return argv[2]
    return " ".join(shlex.quote(a) for a in argv) if isinstance(cmd, list) else str(cmd)


def _kind(k: Any) -> str:
    return (k.get("type") if isinstance(k, dict) else k) or "update"


def _changes(raw: Any) -> list[dict[str, Any]]:
    out = []
    for c in raw or []:
        k = c.get("kind")
        out.append({"path": c.get("path"), "kind": _kind(k), "move_path": k.get("move_path") if isinstance(k, dict) else None, "diff": c.get("diff") or ""})
    return out


def _text_of(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    out = []
    for p in parts or []:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            out.append(p.get("text") or "")
    return "\n".join(x for x in out if x)


def _attachment(path: str | None) -> dict[str, Any]:
    att: dict[str, Any] = {"name": os.path.basename(path or "image"), "path": path, "mime": mimetypes.guess_type(path or "")[0] or "image/png"}
    url = upload_url(path)
    if url:
        att["url"] = url
    return att


# --------------------------------------------------------------------------
# Item normalization (app-server v2 items, shared by live events and history)
# --------------------------------------------------------------------------


def user_item(item: dict[str, Any]) -> Item:
    texts, atts = [], []
    for c in item.get("content") or []:
        t = c.get("type")
        if t == "text":
            texts.append(c.get("text") or "")
        elif t == "localImage":
            atts.append(_attachment(c.get("path")))
        elif t == "image":
            atts.append({"name": "image", "mime": "image/*", "url": c.get("url")})
        elif t in ("mention", "skill"):
            texts.append(f"@{c.get('name') or c.get('path')}")
    text, files = split_attachment_note("\n".join(texts))
    it: Item = {"kind": "user", "id": item["id"], "text": text}
    if atts or files:
        it["attachments"] = atts + files
    return it


def tool_item(item: dict[str, Any]) -> Item | None:
    t, iid = item.get("type"), item.get("id")
    if t == "commandExecution":
        return {"kind": "tool", "id": iid, "name": "Bash", "input": {"command": unwrap_command(item.get("command")), "cwd": item.get("cwd")}}
    if t == "fileChange":
        return {"kind": "tool", "id": iid, "name": "Patch", "input": {"changes": _changes(item.get("changes"))}}
    if t == "mcpToolCall":
        return {"kind": "tool", "id": iid, "name": f"mcp__{item.get('server')}__{item.get('tool')}", "input": item.get("arguments") or {}}
    if t == "dynamicToolCall":
        ns = NS_UNALIAS.get(item.get("namespace") or "", item.get("namespace"))
        name = f"mcp__{ns}__{item.get('tool')}" if ns else str(item.get("tool"))
        return {"kind": "tool", "id": iid, "name": name, "input": item.get("arguments") or {}}
    if t == "webSearch":
        action = item.get("action") or {}
        inp = {"query": item.get("query") or action.get("query") or ""}
        if action.get("url"):
            inp["url"] = action["url"]
        return {"kind": "tool", "id": iid, "name": "WebSearch", "input": inp}
    if t == "imageView":
        return {"kind": "tool", "id": iid, "name": "ViewImage", "input": {"file_path": item.get("path")}}
    if t == "collabAgentToolCall":
        return {"kind": "tool", "id": iid, "name": "Agent", "input": {"description": short(item.get("prompt") or "", 200), "subagent_type": item.get("tool")}}
    if t == "imageGeneration":
        return {"kind": "tool", "id": iid, "name": "ImageGen", "input": {"prompt": item.get("revisedPrompt") or "", "file_path": item.get("savedPath")}}
    return None


def result_item(item: dict[str, Any]) -> Item | None:
    t, st = item.get("type"), item.get("status")
    if st == "inProgress":
        return None
    err = st in ("failed", "declined")
    content = ""
    if t == "commandExecution":
        content = item.get("aggregatedOutput") or ""
        code = item.get("exitCode")
        if code not in (0, None):
            err = True
            content += f"\n[exit code {code}]"
        if st == "declined":
            content = "Declined by the user."
    elif t == "fileChange":
        content = {"completed": "Patch applied.", "declined": "Patch declined by the user.", "failed": "Patch failed."}.get(st or "", "")
    elif t == "mcpToolCall":
        if item.get("error"):
            content, err = (item["error"] or {}).get("message") or "error", True
        else:
            content = _text_of(((item.get("result") or {}).get("content")))
    elif t == "dynamicToolCall":
        content = _text_of(item.get("contentItems"))
        err = err or item.get("success") is False
    elif t == "webSearch":
        results = item.get("results") or []
        content = "\n".join(f"- {r.get('title') or ''} {r.get('url') or ''}".strip() for r in results if isinstance(r, dict)) or "Searched."
    elif t in ("imageView", "collabAgentToolCall", "imageGeneration"):
        content = "Done." if not err else "Failed."
    else:
        return None
    return {"kind": "tool_result", "id": item.get("id"), "content": clip(content), "is_error": err}


def message_items(item: dict[str, Any]) -> list[Item]:
    """Non-tool items (assistant text, reasoning, plans, compaction)."""
    t, iid = item.get("type"), item.get("id")
    if t == "agentMessage":
        text = item.get("text") or ""
        return [{"kind": "text", "id": iid, "text": text}] if text.strip() else []
    if t == "reasoning":
        text = _text_of(item.get("summary")) or _text_of(item.get("content"))
        return [{"kind": "thinking", "id": iid, "text": text}] if text.strip() else []
    if t == "plan":
        return [{"kind": "text", "id": iid, "text": item.get("text") or ""}]
    if t == "contextCompaction":
        return [{"kind": "system", "id": iid, "subtype": "compact", "text": "Conversation compacted"}]
    if t in ("enteredReviewMode", "exitedReviewMode"):
        return [{"kind": "system", "id": iid, "subtype": t, "text": f"{'Entered' if t.startswith('entered') else 'Exited'} review mode"}]
    return []


def plan_items(turn_id: str, plan: list[dict[str, Any]]) -> list[Item]:
    status = {"inProgress": "in_progress", "completed": "completed"}
    todos = [{"content": p.get("step") or "", "status": status.get(p.get("status") or "", "pending")} for p in plan]
    tid = f"plan-{turn_id}"
    return [{"kind": "tool", "id": tid, "name": "TodoWrite", "input": {"todos": todos}}, {"kind": "tool_result", "id": tid, "content": "", "is_error": False}]


def turn_items(turn: dict[str, Any]) -> list[Item]:
    out: list[Item] = []
    for it in turn.get("items") or []:
        if it.get("type") == "userMessage":
            out.append(user_item(it))
            continue
        out += message_items(it)
        tool = tool_item(it)
        if tool:
            out.append(tool)
            res = result_item(it)
            if res:
                out.append(res)
    st = turn.get("status")
    if st in ("failed", "interrupted"):
        err = turn.get("error") or {}
        out.append({"kind": "system", "id": f"turn-{turn.get('id')}", "subtype": "interrupted" if st == "interrupted" else "error",
                    "text": "Interrupted" if st == "interrupted" else f"Turn failed: {err.get('message') or 'unknown error'}"})
    return out


# --------------------------------------------------------------------------
# Live session
# --------------------------------------------------------------------------


class CodexLiveSession(LiveSession):
    def __init__(self, harness: "CodexHarness", spec: LiveSpec, emit: Emit):
        self.harness = harness
        self.client = harness.client
        self.spec = spec
        self.emit = emit
        self.session_id = spec.session_id or ""
        self.state = "starting"
        self.model = spec.model or None
        self.effort = spec.effort or None
        self.mode = spec.permission_mode or "auto"
        self._mode_dirty = False
        self.turn_id: str | None = None
        self._turn_started = 0.0
        self._pending: dict[str, asyncio.Future] = {}
        self._file_changes: dict[str, Any] = {}
        self._usage: dict[str, Any] | None = None
        self._tools: dict[tuple[str, str], ToolSpec] = {(ns, t.name): t for ns, specs in spec.toolsets.items() for t in specs}
        self._closed = False

    # ---- public API --------------------------------------------------------
    async def start(self) -> None:
        spec = self.spec
        params: dict[str, Any] = {"cwd": spec.cwd, "developerInstructions": spec.system_append or None}
        if self.model:
            params["model"] = self.model
        if self.mode in MODE_POLICY:
            params["approvalPolicy"], params["sandbox"] = MODE_POLICY[self.mode]
        try:
            if spec.resume:
                res = await self.client.request("thread/resume", {"threadId": self.session_id, "excludeTurns": True, **params})
            else:
                res = await self.client.request("thread/start", {**params, "dynamicTools": self._dynamic_tools(), "serviceName": "atrium"})
        except Exception as e:
            self._set_state("error")
            raise RuntimeError(f"Codex could not start the thread: {e}") from e
        thread = res.get("thread") or {}
        self.session_id = thread["id"]
        self.client.add_thread(self.session_id, self._on_event, self._on_request)
        self.harness.live_threads.add(self.session_id)
        model = res.get("model") or thread.get("model") or "?"
        effort = res.get("reasoningEffort") or thread.get("reasoningEffort")
        mode = next((m["label"] for m in PERMISSION_MODES if m["id"] == self.mode), self.mode)
        self.emit({
            "kind": "system", "id": f"init-{self.session_id}-{int(time.time())}", "subtype": "init",
            "text": f"Connected · Codex · model {model}" + (f" · effort {effort}" if effort else "") + f" · {mode}",
            "model": self.model or "", "permission_mode": self.mode,
        })
        self._set_state("idle")

    async def send(self, text: str, attachments: list[Attachment] | None = None) -> None:
        attachments = attachments or []
        files = [a for a in attachments if not a.is_image]
        inp: list[dict[str, Any]] = [{"type": "text", "text": text + attachment_note(files), "text_elements": []}]
        inp += [{"type": "localImage", "path": a.path} for a in attachments if a.is_image]
        if self.turn_id and self.state in ("running", "waiting"):
            try:  # mid-turn messages steer the running turn
                await self.client.request("turn/steer", {"threadId": self.session_id, "expectedTurnId": self.turn_id, "input": inp})
                return
            except RpcError as e:
                log.debug("steer failed (%s); starting a new turn", e)
        params: dict[str, Any] = {"threadId": self.session_id, "input": inp}
        if self.model:
            params["model"] = self.model
        if self.effort:
            params["effort"] = self.effort
        if self._mode_dirty and self.mode in MODE_POLICY:
            approval, sandbox = MODE_POLICY[self.mode]
            params["approvalPolicy"], params["sandboxPolicy"] = approval, SANDBOX_POLICY[sandbox]
            self._mode_dirty = False
        self._set_state("running")
        try:
            res = await self.client.request("turn/start", params)
        except Exception:
            self._set_state("idle")
            raise
        self.turn_id = (res.get("turn") or {}).get("id") or self.turn_id
        self._turn_started = time.time()

    async def interrupt(self) -> None:
        if self.turn_id:
            await self.client.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id})

    async def set_permission_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self._mode_dirty = True  # applied with the next turn

    async def set_model(self, model: str | None) -> None:
        self.model = model or None

    async def set_effort(self, effort: str | None) -> None:
        self.effort = effort or None

    def resolve_permission(self, request_id: str, decision: dict[str, Any]) -> bool:
        fut = self._pending.get(request_id)
        if not fut or fut.done():
            return False
        fut.set_result(decision)
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"behavior": "deny", "interrupt": True})
        self.client.remove_thread(self.session_id)
        self.harness.live_threads.discard(self.session_id)
        if self.client.alive and self.session_id:
            try:
                if self.turn_id:
                    await self.client.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id}, timeout=5)
                await self.client.request("thread/unsubscribe", {"threadId": self.session_id}, timeout=5)
            except Exception:
                pass
        self._set_state("closed")

    # ---- internals ---------------------------------------------------------
    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit({"kind": "state", "state": state})

    def _dynamic_tools(self) -> list[dict[str, Any]]:
        out = []
        for ns, specs in self.spec.toolsets.items():
            out.append({
                "type": "namespace",
                "name": NS_ALIAS.get(ns, ns),
                "description": {"board": "Atrium's shared project board", "web": "Web search (Serper) and page fetch (Jina Reader)",
                                "graph": "Atrium's commit-graph view"}.get(ns, ns),
                "tools": [{"type": "function", "name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in specs],
            })
        return out

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if self._closed:
            return
        if method == "turn/started":
            self.turn_id = (params.get("turn") or {}).get("id") or self.turn_id
            self._turn_started = self._turn_started or time.time()
            self._set_state("waiting" if self._pending else "running")
        elif method == "item/agentMessage/delta":
            self.emit({"kind": "delta", "text": params.get("delta") or ""})
        elif method == "item/started":
            item = params.get("item") or {}
            if item.get("type") == "fileChange":
                self._file_changes[item.get("id")] = item.get("changes")
            tool = tool_item(item)
            if tool:
                self.emit(tool)
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "userMessage":
                return  # Atrium emitted the user item when it was sent
            for it in message_items(item):
                self.emit(it)
            tool = tool_item(item)
            if tool:
                self.emit(tool)  # upsert: inputs such as the web query are final only now
                res = result_item(item)
                if res:
                    self.emit(res)
        elif method == "turn/plan/updated":
            for it in plan_items(params.get("turnId") or "turn", params.get("plan") or []):
                self.emit(it)
        elif method == "thread/tokenUsage/updated":
            self._usage = params.get("tokenUsage")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            st = turn.get("status")
            err = turn.get("error") or {}
            dur = turn.get("durationMs") or (int((time.time() - self._turn_started) * 1000) if self._turn_started else None)
            self.emit({
                "kind": "result", "id": f"result-{turn.get('id') or uuid.uuid4().hex[:8]}", "subtype": st or "completed",
                "is_error": st == "failed", "duration_ms": dur, "cost_usd": None, "turns": None,
                "text": err.get("message") if st == "failed" else None, "terminal_reason": st,
            })
            self.turn_id = None
            self._turn_started = 0.0
            self._set_state("waiting" if self._pending else "idle")
        elif method == "error":
            if not params.get("willRetry"):
                err = params.get("error") or {}
                self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": err.get("message") or json.dumps(err)[:500]})
        elif method == "model/rerouted":
            self.emit({"kind": "system", "id": f"reroute-{uuid.uuid4().hex[:8]}", "subtype": "model", "text": f"Model rerouted: {short(params, 200)}"})
        elif method == "atrium/disconnected":
            self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": "Codex app-server exited." + (f" {self.client.stderr_tail[-1]}" if self.client.stderr_tail else "")})
            self.harness.live_threads.discard(self.session_id)
            self._set_state("error")

    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "item/tool/call":
            return await self._call_tool(params)
        if method == "item/commandExecution/requestApproval":
            d = await self._ask(params, "Bash", {"command": unwrap_command(params.get("command")), "cwd": params.get("cwd")})
            return {"decision": self._decision(d)}
        if method == "item/fileChange/requestApproval":
            changes = _changes(self._file_changes.get(params.get("itemId")))
            d = await self._ask(params, "Patch", {"changes": changes}, blocked_path=params.get("grantRoot"))
            return {"decision": self._decision(d)}
        if method == "item/permissions/requestApproval":
            d = await self._ask(params, "Permissions", params.get("permissions") or {})
            if d.get("behavior") == "allow":
                return {"permissions": params.get("permissions") or {}, "scope": "session" if d.get("remember") else "turn"}
            return {"permissions": {}}
        if method == "item/tool/requestUserInput":
            qs = params.get("questions") or []
            ask = [{"question": q.get("question"), "header": q.get("header"), "options": q.get("options") or [], "multiSelect": False} for q in qs]
            d = await self._ask(params, "AskUserQuestion", {"questions": ask}, can_remember=False)
            answers = d.get("answers") or {}
            return {"answers": {q.get("id"): {"answers": [answers[q.get("question")]] if answers.get(q.get("question")) else []} for q in qs}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}
        raise RuntimeError(f"Atrium does not handle {method}")

    async def _ask(self, params: dict[str, Any], tool: str, inp: dict[str, Any], *, blocked_path: str | None = None, can_remember: bool = True) -> dict[str, Any]:
        rid = params.get("itemId") or f"perm-{uuid.uuid4().hex[:10]}"
        while rid in self._pending:
            rid = f"{rid}+"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.emit({"kind": "permission", "id": rid, "tool": tool, "input": inp, "reason": params.get("reason"),
                   "blocked_path": blocked_path, "can_remember": can_remember})
        self._set_state("waiting")
        try:
            decision = await fut
        finally:
            self._pending.pop(rid, None)
        self.emit({"kind": "permission_resolved", "id": rid, "decision": decision.get("behavior", "deny")})
        if self.state == "waiting" and not self._pending:
            self._set_state("running" if self.turn_id else "idle")
        if decision.get("behavior") != "allow" and decision.get("message") and not decision.get("interrupt"):
            asyncio.create_task(self._steer_text(decision["message"]))
        return decision

    @staticmethod
    def _decision(d: dict[str, Any]) -> str:
        if d.get("behavior") == "allow":
            return "acceptForSession" if d.get("remember") else "accept"
        return "cancel" if d.get("interrupt") else "decline"

    async def _steer_text(self, text: str) -> None:
        await asyncio.sleep(0.2)
        if self.turn_id:
            try:
                await self.client.request("turn/steer", {"threadId": self.session_id, "expectedTurnId": self.turn_id,
                                                         "input": [{"type": "text", "text": text, "text_elements": []}]})
            except Exception as e:
                log.debug("steer after decline failed: %s", e)

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        ns, name = params.get("namespace") or "", params.get("tool") or ""
        ns = NS_UNALIAS.get(ns, ns)
        spec = self._tools.get((ns, name)) or next((t for (n, tn), t in self._tools.items() if tn == name and not ns), None)
        if spec is None:
            return {"contentItems": [{"type": "inputText", "text": f"Unknown tool {ns}.{name}"}], "success": False}
        try:
            out = await spec.handler(params.get("arguments") or {})
        except Exception as e:  # report to the model instead of failing the turn
            return {"contentItems": [{"type": "inputText", "text": f"Error: {e}"}], "success": False}
        if isinstance(out, dict):
            return {"contentItems": [{"type": "inputText", "text": out.get("text", "")}], "success": not out.get("is_error")}
        return {"contentItems": [{"type": "inputText", "text": str(out)}], "success": True}


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class CodexHarness(Harness):
    id = "codex"
    label = "Codex"
    permission_modes = PERMISSION_MODES
    models = [{"id": "", "label": "Default"}]
    efforts = DEFAULT_EFFORTS

    def __init__(self) -> None:
        self._client: AppServerClient | None = None
        self.live_threads: set[str] = set()
        self._paths: dict[str, str] = {}
        self._cwd_cache: dict[str, str | None] = {}
        self._models: tuple[float, list[dict[str, Any]]] | None = None
        self._list_warned = False

    @property
    def client(self) -> AppServerClient:
        binary = config.get("harnesses.codex.binary") or "codex"
        if self._client is None or (self._client.binary != binary and not self._client.alive):
            self._client = AppServerClient(binary)
        return self._client

    def available(self) -> tuple[bool, str | None]:
        if not config.get("harnesses.codex.enabled", True):
            return False, "Disabled in Atrium settings (harnesses.codex.enabled)."
        if not self.client.resolve_binary():
            return False, f"Codex CLI not found ({self.client.binary}). Install it, or set harnesses.codex.binary in Atrium settings."
        return True, None

    async def shutdown(self) -> None:
        if self._client:
            await self._client.close()

    # ---- settings ----------------------------------------------------------
    def config_files(self, project_dir: str | None) -> list[Any]:
        from ..configfiles import ConfigFile

        files = [
            ConfigFile(
                id="codex-user", label="Codex", path=str(codex_home() / "config.toml"), language="toml", scope="user", harness=self.id,
                hint="Model, provider, approval/sandbox defaults, MCP servers, profiles… Applies to Codex sessions started afterwards.",
            )
        ]
        if project_dir:
            files.append(ConfigFile(
                id="codex-project", label="Codex · project", path=os.path.join(project_dir, ".codex", "config.toml"), language="toml",
                scope="project", harness=self.id, hint="Project overrides (read when the project is trusted in ~/.codex/config.toml).",
            ))
        return files

    def on_config_saved(self, file_id: str) -> None:
        self._models = None
        if self._client and self._client.alive and not self.live_threads:
            asyncio.get_running_loop().create_task(self._client.close())  # restarted with the new config on next use

    async def catalog(self) -> dict[str, Any]:
        d = self.describe()
        if d["available"]:
            try:
                d["models"] = await self._model_list()
            except Exception as e:
                d["catalog_error"] = str(e)
        return d

    async def _model_list(self) -> list[dict[str, Any]]:
        if self._models and time.time() - self._models[0] < 600:
            return self._models[1]
        res = await self.client.request("model/list", {"limit": 100}, timeout=30)
        models: list[dict[str, Any]] = []
        default = None
        for m in res.get("data") or []:
            if m.get("hidden"):
                continue
            entry = {
                "id": m.get("model") or m.get("id"),
                "label": m.get("displayName") or m.get("model") or m.get("id"),
                "description": m.get("description"),
                "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or [] if e.get("reasoningEffort")],
                "default_effort": m.get("defaultReasoningEffort"),
            }
            if m.get("isDefault"):
                default = entry
            models.append(entry)
        head = {"id": "", "label": f"Default ({default['label']})" if default else "Default",
                "efforts": (default or {}).get("efforts") or DEFAULT_EFFORTS, "default_effort": (default or {}).get("default_effort")}
        self._models = (time.time(), [head] + models)
        return self._models[1]

    # ---- discovery ---------------------------------------------------------
    async def list_sessions(self, project_dir: str, roots: list[str]) -> list[SessionSummary]:
        if not self.available()[0]:
            return []
        cwds: set[str] = set()
        for r in roots:
            cwds.add(r)
            if r.startswith("/private/"):  # macOS: /tmp → /private/tmp; threads record whichever the shell used
                cwds.add(r[len("/private"):])
        out: list[SessionSummary] = []
        cursor = None
        for _ in range(10):
            params: dict[str, Any] = {"cwd": sorted(cwds), "sourceKinds": SOURCE_KINDS, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            try:
                res = await self.client.request("thread/list", params, timeout=30)
            except Exception as e:
                if not self._list_warned:
                    log.warning("codex thread/list failed: %s", e)
                    self._list_warned = True
                break
            for t in res.get("data") or []:
                out.append(self._summary(t))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return out

    def _summary(self, t: dict[str, Any]) -> SessionSummary:
        if t.get("path"):
            self._paths[t["id"]] = t["path"]
        preview = t.get("preview") or ""
        return SessionSummary(
            id=t["id"],
            harness=self.id,
            title=t.get("name") or short(preview, 80) or "Untitled",
            updated_at=float(t.get("updatedAt") or t.get("createdAt") or 0),
            created_at=float(t["createdAt"]) if t.get("createdAt") else None,
            first_prompt=preview,
            cwd=t.get("cwd"),
            git_branch=(t.get("gitInfo") or {}).get("branch"),
            transcript=t.get("path"),
        )

    def transcript_path(self, session_id: str, cwd: str) -> Path | None:
        known = self._paths.get(session_id)
        if known and os.path.exists(known):
            return Path(known)
        for base in (codex_home() / "sessions", codex_home() / "archived_sessions"):
            for cand in base.glob(f"**/rollout-*-{session_id}.jsonl"):
                self._paths[session_id] = str(cand)
                return cand
        return None

    def watch_roots(self) -> list[Path]:
        p = codex_home() / "sessions"
        return [p] if p.is_dir() else []

    def transcript_cwd(self, path: str) -> str | None:
        if not path.endswith(".jsonl"):
            return None
        if path in self._cwd_cache:
            return self._cwd_cache[path]
        cwd = None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = json.loads(f.readline() or "{}")
            cwd = (head.get("payload") or {}).get("cwd") if head.get("type") == "session_meta" else None
        except (OSError, json.JSONDecodeError):
            return None  # not written yet; retry next time
        self._cwd_cache[path] = cwd
        return cwd

    async def delete_session(self, session_id: str, cwd: str) -> None:
        await self.client.request("thread/delete", {"threadId": session_id}, timeout=30)
        self._paths.pop(session_id, None)

    async def rename_session(self, session_id: str, title: str, cwd: str) -> None:
        try:
            await self.client.request("thread/name/set", {"threadId": session_id, "name": title}, timeout=15)
        except Exception as e:
            log.debug("thread/name/set(%s) failed: %s", session_id, e)

    # ---- content -----------------------------------------------------------
    async def load_history(self, session_id: str, cwd: str) -> list[Item]:
        items: list[Item] = []
        cursor = None
        for _ in range(100):
            params: dict[str, Any] = {"threadId": session_id, "itemsView": "full", "sortDirection": "asc", "limit": 50}
            if cursor:
                params["cursor"] = cursor
            res = await self.client.request("thread/turns/list", params, timeout=60)
            for turn in res.get("data") or []:
                items += turn_items(turn)
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return items

    def digest_lines(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        for line in lines:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("type") != "event_msg":
                continue
            p = d.get("payload") or {}
            ts = hhmm(d.get("timestamp"))
            pre = f"[{ts}] " if ts else ""
            pt = p.get("type")
            if pt == "item_completed":
                out += [pre + e for e in _digest_item(p.get("item") or {})]
            elif pt == "user_message" and p.get("message"):  # older rollouts
                out.append(f"{pre}USER: {short(p['message'], 600)}")
            elif pt == "agent_message" and p.get("message"):
                out.append(f"{pre}AGENT: {short(p['message'], 700)}")
            elif pt == "turn_aborted":
                out.append(f"{pre}(turn interrupted)")
        return out

    def file_edits(self, session_id: str, cwd: str) -> list[FileEdit]:
        path = self.transcript_path(session_id, cwd)
        if not path:
            return []
        edits: list[FileEdit] = []
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            return []
        with fh:
            for line in fh:
                if '"FileChange"' not in line and '"patch_apply_end"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = d.get("payload") or {}
                item = p.get("item") if p.get("type") == "item_completed" else p if p.get("type") == "patch_apply_end" else None
                if not item or (item.get("type") == "FileChange" and item.get("status") != "completed"):
                    continue
                if p.get("type") == "patch_apply_end" and not item.get("success", True):
                    continue
                for fp, c in (item.get("changes") or {}).items():
                    edits += _edits_from_change(fp, c, d.get("timestamp"))
        return edits

    # ---- execution ---------------------------------------------------------
    def create_live(self, spec: LiveSpec, emit: Emit) -> LiveSession:
        return CodexLiveSession(self, spec, emit)

    async def complete_json(self, *, system, prompt, schema, model, effort, cwd):
        started = time.time()
        params: dict[str, Any] = {"cwd": cwd, "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                                  "developerInstructions": system, "serviceName": "atrium"}
        if model:
            params["model"] = model
        res = await self.client.request("thread/start", params)
        tid = res["thread"]["id"]
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        texts: list[str] = []
        usage: dict[str, Any] = {}

        def on_event(method: str, p: dict[str, Any]) -> None:
            if method == "item/completed" and (p.get("item") or {}).get("type") == "agentMessage":
                texts.append(p["item"].get("text") or "")
            elif method == "thread/tokenUsage/updated":
                usage.update(p.get("tokenUsage") or {})
            elif method == "turn/completed" and not done.done():
                done.set_result(p.get("turn") or {})
            elif method == "atrium/disconnected" and not done.done():
                done.set_exception(RuntimeError("codex app-server exited"))

        async def on_request(method: str, p: dict[str, Any]) -> Any:
            if "requestApproval" in method:
                return {"decision": "decline"} if "permissions" not in method else {"permissions": {}}
            raise RuntimeError("tools are disabled for this call")

        self.client.add_thread(tid, on_event, on_request)
        try:
            turn_params: dict[str, Any] = {"threadId": tid, "input": [{"type": "text", "text": prompt, "text_elements": []}],
                                           "outputSchema": strict_schema(schema)}
            if effort:
                turn_params["effort"] = effort
            await self.client.request("turn/start", turn_params)
            turn = await asyncio.wait_for(done, timeout=600)
        finally:
            self.client.remove_thread(tid)
            try:
                await self.client.request("thread/unsubscribe", {"threadId": tid}, timeout=5)
            except Exception:
                pass
        if turn.get("status") == "failed":
            raise RuntimeError((turn.get("error") or {}).get("message") or "Codex turn failed")
        if not texts:
            raise RuntimeError("no answer from Codex")
        data = parse_json_loose(texts[-1])
        total = (usage.get("total") or {}) if isinstance(usage.get("total"), dict) else {}
        return data, {"cost_usd": None, "duration_ms": int((time.time() - started) * 1000), "turns": 1,
                      "tokens": total.get("totalTokens") or total.get("total_tokens")}


def _digest_item(it: dict[str, Any]) -> list[str]:
    t = it.get("type")
    if t == "UserMessage":
        text = _text_of([c for c in it.get("content") or [] if isinstance(c, dict) and c.get("type") in ("text", "Text")])
        text = split_attachment_note(text)[0]
        return [f"USER: {short(text, 600)}"] if text.strip() else []
    if t == "AgentMessage":
        text = _text_of(it.get("content"))
        return [f"AGENT: {short(text, 700)}"] if text.strip() else []
    if t == "CommandExecution":
        actions = {a.get("type") for a in it.get("parsed_cmd") or [] if isinstance(a, dict)}
        if actions and actions <= SKIP_DIGEST_ACTIONS:
            return []  # reads / searches are too noisy to matter for progress tracking
        cmd = unwrap_command(it.get("command"))
        line = f"TOOL Bash: {short(cmd, 180)}"
        if it.get("exit_code") not in (0, None):
            line += f" → exit {it.get('exit_code')}: {short(it.get('aggregated_output') or '', 200)}"
        return [line]
    if t == "FileChange":
        out = []
        for fp, c in (it.get("changes") or {}).items():
            kind = c.get("type")
            if kind == "add":
                out.append(f"created {fp} (+{len((c.get('content') or '').splitlines())})")
            elif kind == "delete":
                out.append(f"deleted {fp}")
            else:
                diff = c.get("unified_diff") or ""
                a = sum(1 for x in diff.splitlines() if x.startswith("+") and not x.startswith("+++"))
                r = sum(1 for x in diff.splitlines() if x.startswith("-") and not x.startswith("---"))
                out.append(f"edited {fp} (+{a} -{r})")
        return out
    if t == "DynamicToolCall":
        args = {k: v for k, v in (it.get("arguments") or {}).items() if k != "html"}
        return [f"TOOL {it.get('namespace') or ''}.{it.get('tool')}: {short(args, 200)}"]
    if t == "McpToolCall":
        return [f"TOOL {it.get('server')}.{it.get('tool')}: {short(it.get('arguments') or {}, 160)}"]
    if t == "WebSearch":
        return [f"TOOL WebSearch: {short(it.get('query') or '', 160)}"]
    return []


def _edits_from_change(fp: str, c: dict[str, Any], ts: str | None) -> list[FileEdit]:
    kind = c.get("type")
    if kind == "add":
        content = c.get("content")
        if content is None and c.get("unified_diff"):
            content = "".join(x[1:] + "\n" for x in c["unified_diff"].splitlines() if x.startswith("+") and not x.startswith("+++"))
        return [FileEdit(path=fp, tool="Patch", ts=ts, before=None, after=content, kind="create")]
    if kind == "delete":
        return [FileEdit(path=fp, tool="Patch", ts=ts, before=c.get("content"), after=None, kind="delete")]
    move = c.get("move_path")
    edits = [FileEdit(path=fp, tool="Patch", ts=ts, before=None, after=None, kind="update", patch=c.get("unified_diff") or "")]
    if move:
        edits.append(FileEdit(path=move, tool="Patch", ts=ts, before=None, after=None, kind="update", patch=c.get("unified_diff") or ""))
    return edits
