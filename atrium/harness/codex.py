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
import re
import shlex
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import config
from .base import Attachment, Compaction, Emit, FileEdit, ForkPoint, Harness, Item, LiveSession, LiveSpec, SessionSummary, SlashCommand, SubagentSummary, ToolSpec
from .codex_rpc import AppServerClient, RpcError
from .util import TailScan, attachment_note, clip, hhmm, iso_to_epoch, parse_json_loose, short, split_attachment_note, strict_schema, tail_scan, upload_url

log = logging.getLogger("atrium.codex")

# The Codex app's three choices. "auto" and "full-access" are Codex's own approval presets;
# "auto-review" is "auto" with approval requests routed to Codex's auto-reviewer instead of the user.
PERMISSION_MODES = [
    {"id": "auto", "label": "Ask for approval"},
    {"id": "auto-review", "label": "Approve for me"},
    {"id": "full-access", "label": "Full access"},
]
# mode -> (approvalPolicy, sandbox mode, approvalsReviewer); any other mode sends nothing and lets
# config.toml decide. "read-only" isn't offered in the picker: it's the Git agent's mode.
MODE_POLICY = {
    "auto": ("on-request", "workspace-write", "user"),
    "auto-review": ("on-request", "workspace-write", "auto_review"),
    "full-access": ("never", "danger-full-access", "user"),
    "read-only": ("on-request", "read-only", "user"),
}
SANDBOX_POLICY = {
    "workspace-write": {"type": "workspaceWrite"},
    "read-only": {"type": "readOnly"},
    "danger-full-access": {"type": "dangerFullAccess"},
}
SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "unknown"]
SUBAGENT_STATUS = {"active": "running", "systemError": "failed"}  # thread status -> subagent status (else done)
# subAgentActivity kind -> subagent status
SUBAGENT_ACTIVITY_STATUS = {"started": "running", "interacted": "running", "interrupted": "stopped", "completed": "done"}
# Atrium namespace -> Codex dynamic-tool namespace ("web" is reserved by the Responses API)
NS_ALIAS = {"web": "internet"}
NS_UNALIAS = {v: k for k, v in NS_ALIAS.items()}
DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh"]
SKIP_DIGEST_ACTIONS = {"read", "listFiles", "search", "list_files"}
# The Codex TUI's slash commands that Atrium runs through the app-server, in the TUI's words. The TUI
# handles slash commands itself; sent to a thread as text they'd only reach the model.
COMMANDS = [
    SlashCommand("init", "create an AGENTS.md file with instructions for Codex"),
    SlashCommand("compact", "summarize conversation to prevent hitting the context limit"),
    SlashCommand("review", "review my current changes and find issues", argument_hint="[instructions]"),
    SlashCommand("status", "show current session configuration and token usage"),
    SlashCommand("mcp", "list configured MCP tools"),
    SlashCommand("skills", "list skills; mention one with $name to use it"),
]
LOCAL_COMMANDS = {"status", "mcp", "skills"}  # answered by Atrium, no turn
TURN_COMMANDS = {"compact", "review"}  # turns of their own, started by their own app-server methods
SLASH_RE = re.compile(r"/([^\s/]+)(?:\s+(.*))?", re.S)
SKILL_RE = re.compile(r"(?<![\w$])\$([A-Za-z0-9][\w.:-]*)")  # "$name" mentions a skill
SKILLS_TTL = 60  # seconds a skills/list answer is reused
# the prompt Codex records for a review of uncommitted changes (a turn of its own, beside the review's)
REVIEW_UNCOMMITTED = "Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings."
# What the TUI's /init sends (codex-cli 0.160)
INIT_PROMPT = """Generate a file named AGENTS.md that serves as a contributor guide for this repository.
Before writing, check whether AGENTS.md already exists in the current working directory. If it does, do not overwrite or modify it.
Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.
Follow the outline below, but adapt as needed — add sections if relevant, and omit those that do not apply to this project.

Document Requirements

- Title the document "Repository Guidelines".
- Use Markdown headings (#, ##, etc.) for structure.
- Keep the document concise. 200-400 words is optimal.
- Keep explanations short, direct, and specific to this repository.
- Provide examples where helpful (commands, directory paths, naming patterns).
- Maintain a professional, instructional tone.

Recommended Sections

Project Structure & Module Organization

- Outline the project structure, including where the source code, tests, and assets are located.

Build, Test, and Development Commands

- List key commands for building, testing, and running locally (e.g., npm test, make build).
- Briefly explain what each command does.

Coding Style & Naming Conventions

- Specify indentation rules, language-specific style preferences, and naming patterns.
- Include any formatting or linting tools used.

Testing Guidelines

- Identify testing frameworks and coverage requirements.
- State test naming conventions and how to run tests.

Commit & Pull Request Guidelines

- Summarize commit message conventions found in the project’s Git history.
- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).

(Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions."""


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


def _skill_summary(skill: dict[str, Any]) -> str:
    return (skill.get("interface") or {}).get("shortDescription") or skill.get("shortDescription") or skill.get("description") or ""


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{round(n / 1e3)}k" if n >= 1e3 else str(n)


# --------------------------------------------------------------------------
# Item normalization (app-server v2 items, shared by live events and history)
# --------------------------------------------------------------------------


def user_item(item: dict[str, Any]) -> Item:
    texts, atts, refs = [], [], []
    for c in item.get("content") or []:
        t = c.get("type")
        if t == "text":
            texts.append(c.get("text") or "")
        elif t == "localImage":
            atts.append(_attachment(c.get("path")))
        elif t == "image":
            atts.append({"name": "image", "mime": "image/*", "url": c.get("url")})
        elif t in ("mention", "skill"):
            refs.append(f"{'$' if t == 'skill' else '@'}{c.get('name') or c.get('path')}")
    # skills and files the text doesn't name already ("$skill" mentions do)
    text, files = split_attachment_note("\n".join(texts + [r for r in refs if not any(r in x for x in texts)]))
    if text.startswith(INIT_PROMPT):  # sent by /init
        text = f"/init {text[len(INIT_PROMPT):].strip()}".strip()
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
        receivers = item.get("receiverThreadIds") or []
        desc = short(item.get("prompt") or "", 200) or ("for sub-agents" if item.get("tool") == "wait" and not receivers else "")
        return {"kind": "tool", "id": iid, "name": "Agent", "input": {"description": desc, "subagent_type": item.get("tool"), "agent_ids": receivers}}
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
        return [{"kind": "system", "id": iid, "subtype": "compact", "text": "Conversation compacted", "compaction": iid}]
    if t == "enteredReviewMode":
        return [{"kind": "system", "id": iid, "subtype": t, "text": f"Reviewing {item.get('review') or 'changes'}"}]
    if t == "exitedReviewMode":  # the review itself also comes as an agent message
        return [{"kind": "system", "id": iid, "subtype": t, "text": "Exited review mode"}]
    return []


def plan_items(turn_id: str, plan: list[dict[str, Any]]) -> list[Item]:
    status = {"inProgress": "in_progress", "completed": "completed"}
    todos = [{"content": p.get("step") or "", "status": status.get(p.get("status") or "", "pending")} for p in plan]
    tid = f"plan-{turn_id}"
    return [{"kind": "tool", "id": tid, "name": "TodoWrite", "input": {"todos": todos}}, {"kind": "tool_result", "id": tid, "content": "", "is_error": False}]


def review_prompts(turns: list[dict[str, Any]]) -> set[int]:
    """Turns that only hold a review's prompt, which Codex records beside the review's turn: the /review
    that asked for it, not a prompt of the user's."""
    def reviewing(i: int) -> bool:
        return 0 <= i < len(turns) and any(it.get("type") == "enteredReviewMode" for it in turns[i].get("items") or [])

    return {i for i, t in enumerate(turns) if [it.get("type") for it in t.get("items") or []] == ["userMessage"] and (reviewing(i - 1) or reviewing(i + 1))}


def turn_items(turn: dict[str, Any], review: bool = False) -> list[Item]:
    """``review``: the turn holds a review's prompt only (``review_prompts``), shown as the /review command."""
    if review:
        it = user_item(turn["items"][0])
        text = it["text"].strip()
        return [{**it, "text": "/review" if text == REVIEW_UNCOMMITTED else f"/review {text}", "command": True}]
    out: list[Item] = []
    for it in turn.get("items") or []:
        if it.get("type") == "userMessage":
            out.append(user_item(it))
            continue
        out += message_items(it)
        out += subagent_items(it)
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


def _task_name(agent_path: str | None) -> str:
    """``/root/inspect_calc`` → ``inspect_calc`` (the task name the parent gave its sub-agent)."""
    return (agent_path or "").rstrip("/").rsplit("/", 1)[-1] or "sub-agent"


def subagent_items(item: dict[str, Any]) -> list[Item]:
    """A ``subAgentActivity`` item: spawning a sub-agent or messaging it is shown like an Agent call,
    its end as a line; both link to the sub-agent (``agent_ids`` / ``agent_id`` = its thread)."""
    if item.get("type") != "subAgentActivity":
        return []
    iid, kind, aid, name = item.get("id"), item.get("kind"), item.get("agentThreadId"), _task_name(item.get("agentPath"))
    if kind in ("started", "interacted"):
        spawn = kind == "started"
        return [
            {"kind": "tool", "id": iid, "name": "Agent", "input": {"description": name, "subagent_type": "spawn" if spawn else "message", "agent_ids": [aid]}},
            {"kind": "tool_result", "id": iid, "content": "Started." if spawn else "Sent.", "is_error": False},
        ]
    text = "Sub-agent interrupted" if kind == "interrupted" else "Sub-agent finished"
    return [{"kind": "system", "id": iid, "subtype": "task_done", "text": f"{text}: {name}", "agent_id": aid}]


def subagent_task(item: dict[str, Any]) -> Item | None:
    """The live ``task`` item for a ``subAgentActivity`` (the sub-agent's status, ahead of discovery)."""
    status = SUBAGENT_ACTIVITY_STATUS.get(item.get("kind") or "")
    if item.get("type") != "subAgentActivity" or not status or not item.get("agentThreadId"):
        return None
    spawn = item.get("kind") == "started"
    return {"kind": "task", "task_id": item["agentThreadId"], "tool_use_id": item.get("id") if spawn else None, "status": status,
            "description": _task_name(item.get("agentPath"))}


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
        self._config_effort: str | None = None  # the thread's effort from config.toml, reported when it starts
        self._thread_model: str | None = None  # the model the thread started with
        self._usage: dict[str, Any] = {}  # the last context-window reading (used, max)
        self.mode = spec.permission_mode or "full-access"
        self._mode_dirty = False
        self.turn_id: str | None = None
        self._turn_started = 0.0
        self._pending: dict[str, asyncio.Future] = {}
        self._file_changes: dict[str, Any] = {}
        self._tools: dict[tuple[str, str], ToolSpec] = {(ns, t.name): t for ns, specs in spec.toolsets.items() for t in specs}
        self._closed = False

    # ---- public API --------------------------------------------------------
    async def start(self) -> None:
        spec = self.spec
        params: dict[str, Any] = {"cwd": spec.cwd, "developerInstructions": spec.system_append or None}
        if self.model:
            params["model"] = self.model
        if self.mode in MODE_POLICY:
            params["approvalPolicy"], params["sandbox"], params["approvalsReviewer"] = MODE_POLICY[self.mode]
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
        self._thread_model = model if model != "?" else None
        effort = self._config_effort = res.get("reasoningEffort") or thread.get("reasoningEffort")
        mode = next((m["label"] for m in PERMISSION_MODES if m["id"] == self.mode), self.mode)
        self.emit({
            "kind": "system", "id": f"init-{self.session_id}-{int(time.time())}", "subtype": "init",
            "text": f"Connected · Codex · model {model}" + (f" · effort {effort}" if effort else "") + f" · {mode}",
            "model": self.model or (model if model != "?" else ""), "effort": effort, "permission_mode": self.mode,
        })
        self._set_state("idle")

    async def send(self, text: str, attachments: list[Attachment] | None = None) -> None:
        attachments = attachments or []
        cmd = SLASH_RE.fullmatch(text.strip())
        name, args = (cmd.group(1), (cmd.group(2) or "").strip()) if cmd else ("", "")
        if name in LOCAL_COMMANDS:
            self.emit({"kind": "command_output", "id": f"cmd-{uuid.uuid4().hex[:12]}", "text": await self._local_command(name)})
            return
        if name in TURN_COMMANDS:
            await self._turn_command(name, args)
            return
        if name == "init":
            text = f"{INIT_PROMPT}\n\n{args}" if args else INIT_PROMPT
        files = [a for a in attachments if not a.is_image]
        inp: list[dict[str, Any]] = [{"type": "text", "text": text + attachment_note(files), "text_elements": []}]
        inp += [{"type": "localImage", "path": a.path} for a in attachments if a.is_image]
        inp += await self._skill_inputs(text)
        if self.turn_id and self.state in ("running", "waiting"):
            try:  # mid-turn messages steer the running turn
                await self.client.request("turn/steer", {"threadId": self.session_id, "expectedTurnId": self.turn_id, "input": inp})
                return
            except RpcError as e:
                log.debug("steer failed (%s); starting a new turn", e)
        params: dict[str, Any] = {"threadId": self.session_id, "input": inp}
        if self.model:
            params["model"] = self.model
        # a turn's effort sticks to the turns after it, so going back to the config's level must be said too
        if self.effort or self._config_effort:
            params["effort"] = self.effort or self._config_effort
        if self._mode_dirty and self.mode in MODE_POLICY:
            approval, sandbox, reviewer = MODE_POLICY[self.mode]
            params["approvalPolicy"], params["sandboxPolicy"], params["approvalsReviewer"] = approval, SANDBOX_POLICY[sandbox], reviewer
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

    def is_command(self, text: str) -> bool:
        m = SLASH_RE.fullmatch(text.strip())
        return bool(m) and m.group(1) in LOCAL_COMMANDS | TURN_COMMANDS  # /init is a prompt

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

    async def _turn_command(self, name: str, args: str) -> None:
        """/compact and /review: turns of their own, started by their app-server methods."""
        if self.turn_id and self.state in ("running", "waiting"):
            raise RuntimeError(f"/{name} has to wait until the current turn ends (or stop it)")
        self._set_state("running")
        try:
            if name == "compact":
                await self.client.request("thread/compact/start", {"threadId": self.session_id})
            else:  # the TUI's review presets, short of a picker: uncommitted changes, or what the arguments ask for
                target = {"type": "custom", "instructions": args} if args else {"type": "uncommittedChanges"}
                res = await self.client.request("review/start", {"threadId": self.session_id, "target": target, "delivery": "inline"})
                self.turn_id = (res.get("turn") or {}).get("id") or self.turn_id
        except Exception:
            self._set_state("idle")
            raise
        self._turn_started = time.time()

    async def _local_command(self, name: str) -> str:
        """/status, /mcp and /skills, answered from what Atrium and the app-server know (markdown)."""
        if name == "status":
            mode = next((m["label"] for m in PERMISSION_MODES if m["id"] == self.mode), self.mode)
            effort = self.effort or self._config_effort
            lines = [
                f"Model: `{self.model or self._thread_model or 'default'}`" + (f" · reasoning effort {effort}" if effort else ""),
                f"Permissions: {mode}" + (" (from the next turn)" if self._mode_dirty else ""),
                f"Directory: `{self.spec.cwd}`",
                f"Thread: `{self.session_id}`",
            ]
            used, mx = self._usage.get("used"), self._usage.get("max")
            if used and mx:
                lines.append(f"Context window: {round(100 * used / mx)}% used ({_tokens(used)} / {_tokens(mx)} tokens)")
            return "\n".join(f"- {x}" for x in lines)
        if name == "mcp":
            servers: list[dict[str, Any]] = []
            cursor = None
            for _ in range(10):
                res = await self.client.request("mcpServerStatus/list", {"threadId": self.session_id, "detail": "toolsAndAuthOnly", "cursor": cursor}, timeout=60)
                servers += res.get("data") or []
                cursor = res.get("nextCursor")
                if not cursor:
                    break
            if not servers:
                return "No MCP servers are configured. Add them under `[mcp_servers.<name>]` in `~/.codex/config.toml`."
            out = []
            for sv in servers:
                tools = sorted(sv.get("tools") or {})
                line = f"- **{sv.get('name')}** · {sv.get('runtimeStatus') or 'not started'}"
                line += f" · {len(tools)} tool{'s' if len(tools) != 1 else ''}" + (f": {', '.join(f'`{t}`' for t in tools)}" if tools else "")
                if sv.get("toolsError"):
                    line += f" · error: {sv['toolsError']}"
                out.append(line)
            return "\n".join(out)
        skills = await self.harness.skills(self.spec.cwd)
        if not skills:
            return "No skills found. Codex looks for `SKILL.md` files in `~/.codex/skills`, `~/.agents/skills` and the repository's `.agents/skills`."
        rows = [f"- **${sk['name']}** — {_skill_summary(sk)}" + (f" ({sk['scope']})" if sk.get("scope") else "") for sk in skills]
        return "Mention a skill with `$name` in a message to use it.\n\n" + "\n".join(rows)

    async def _skill_inputs(self, text: str) -> list[dict[str, Any]]:
        """The skills a message mentions ("$name"), as the input items Codex loads them from."""
        names = set(SKILL_RE.findall(text))
        if not names:
            return []
        try:
            skills = await self.harness.skills(self.spec.cwd)
        except Exception as e:
            log.debug("skills/list failed: %s", e)
            return []
        return [{"type": "skill", "name": sk["name"], "path": sk["path"]} for sk in skills if sk.get("name") in names and sk.get("path")]

    def _dynamic_tools(self) -> list[dict[str, Any]]:
        out = []
        for ns, specs in self.spec.toolsets.items():
            out.append({
                "type": "namespace",
                "name": NS_ALIAS.get(ns, ns),
                "description": {"board": "Atrium's shared project board", "web": "Web search (Serper) and page fetch (Jina Reader)",
                                "graph": "Atrium's commit-graph view", "review": "Atrium's code review: findings and drafts for the human"}.get(ns, ns),
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
            for it in message_items(item) + subagent_items(item):
                self.emit(it)
            task = subagent_task(item)
            if task:
                self.emit(task)
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
            u = params.get("tokenUsage") or {}
            self._usage = {"used": (u.get("last") or {}).get("totalTokens"), "max": u.get("modelContextWindow")}
            self.emit({"kind": "usage", "id": "usage", **self._usage})
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
    git_agent_mode = "read-only"
    review_agent_mode = "read-only"
    models = [{"id": "", "label": "Default"}]
    efforts = DEFAULT_EFFORTS

    def __init__(self) -> None:
        self._client: AppServerClient | None = None
        self.live_threads: set[str] = set()
        self._paths: dict[str, str] = {}
        self._cwd_cache: dict[str, str | None] = {}
        self._models: tuple[float, list[dict[str, Any]]] | None = None
        self._list_warned = False
        self._spawns_cache: dict[str, tuple[int, list[tuple[str, str]]]] = {}  # rollout -> (size, [(sub-agent thread, spawn call)])
        self._thread_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}  # thread/read, briefly
        self._compaction_cache: dict[str, _CompactionScan] = {}  # rollout path -> what's read of it
        self._compaction_lock = threading.Lock()
        self._skills_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # cwd -> (when, skills/list)

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
        self._skills_cache.clear()
        if self._client and self._client.alive and not self.live_threads:
            asyncio.get_running_loop().create_task(self._client.close())  # restarted with the new config on next use

    async def catalog(self) -> dict[str, Any]:
        d = self.describe()
        if d["available"]:
            try:
                d["models"] = await self._model_list()
                # what a thread gets when Atrium doesn't pick: config.toml, else the default model and its effort
                cfg = (await self.client.request("config/read", {}, timeout=30)).get("config") or {}
                model = cfg.get("model") or d["models"][0].get("resolves_to")
                entry = next((m for m in d["models"][1:] if m["id"] in (model, (model or "").split(":")[0])), {})  # "id:routing-tag"
                d["config_model"], d["config_effort"] = model, cfg.get("model_reasoning_effort") or entry.get("default_effort")
            except Exception as e:
                d["catalog_error"] = str(e)
        return d

    async def skills(self, cwd: str) -> list[dict[str, Any]]:
        """The enabled skills a thread in ``cwd`` can use (``skills/list``), briefly cached."""
        hit = self._skills_cache.get(cwd)
        if hit and time.time() - hit[0] < SKILLS_TTL:
            return hit[1]
        res = await self.client.request("skills/list", {"cwds": [cwd]}, timeout=30)
        skills = [sk for e in res.get("data") or [] for sk in e.get("skills") or [] if sk.get("enabled", True)]
        self._skills_cache[cwd] = (time.time(), skills)
        return skills

    async def slash_commands(self, cwd: str, live: LiveSession | None = None) -> list[SlashCommand]:
        skills: list[dict[str, Any]] = []
        if self.available()[0]:
            try:
                skills = await self.skills(cwd)
            except Exception as e:
                log.debug("skills/list failed: %s", e)
        return COMMANDS + [SlashCommand(sk["name"], _skill_summary(sk), source="skill", prefix="$") for sk in skills if sk.get("name")]

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
        head = {"id": "", "label": f"Default ({default['label']})" if default else "Default", "resolves_to": (default or {}).get("id"),
                "efforts": (default or {}).get("efforts") or DEFAULT_EFFORTS, "default_effort": (default or {}).get("default_effort")}
        self._models = (time.time(), [head] + models)
        return self._models[1]

    # ---- discovery ---------------------------------------------------------
    async def list_sessions(self, project_dir: str, roots: list[str]) -> list[SessionSummary]:
        if not self.available()[0]:
            return []
        out: list[SessionSummary] = []
        cursor = None
        for _ in range(10):
            params: dict[str, Any] = {"cwd": _thread_cwds(roots), "sourceKinds": SOURCE_KINDS, "limit": 100}
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

    async def list_subagents(self, sessions: list[SessionSummary], roots: list[str]) -> dict[str, list[SubagentSummary]]:
        # thread/list doesn't return sub-agent threads: follow each thread's own spawn events instead
        mine = [s for s in sessions if s.harness == self.id]
        if not mine or not self.available()[0]:
            return {}
        out: dict[str, list[SubagentSummary]] = {}
        for s in mine:
            path = s.transcript or str(self.transcript_path(s.id, s.cwd or "") or "")
            subs = await self._subagents_of(path, 1, set())
            if subs:
                out[s.id] = subs
        return out

    async def _subagents_of(self, rollout: str | None, depth: int, seen: set[str]) -> list[SubagentSummary]:
        """The sub-agents a thread spawned, then theirs (nested ones are listed under the top-level session)."""
        out: list[SubagentSummary] = []
        for aid, spawn_id in await asyncio.to_thread(self._spawns, rollout) if rollout else []:
            if aid in seen or len(seen) >= 200:
                continue
            seen.add(aid)
            t = await self._read_thread(aid)
            if not t:
                continue
            out.append(_subagent_summary(t, depth, spawn_id))
            if depth < 16:
                out += await self._subagents_of(t.get("path"), depth + 1, seen)
        return out

    def _spawns(self, path: str) -> list[tuple[str, str]]:
        """(sub-agent thread, spawning call) for each sub-agent a rollout started, in order."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        hit = self._spawns_cache.get(path)
        if hit and hit[0] == size:
            return hit[1]
        out: list[tuple[str, str]] = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"SubAgentActivity"' not in line:
                    continue
                try:
                    it = (json.loads(line).get("payload") or {}).get("item") or {}
                except (json.JSONDecodeError, AttributeError):
                    continue
                if it.get("type") == "SubAgentActivity" and it.get("kind") == "started" and it.get("agent_thread_id"):
                    out.append((it["agent_thread_id"], it.get("id") or ""))
        self._spawns_cache[path] = (size, out)
        return out

    async def _read_thread(self, thread_id: str) -> dict[str, Any] | None:
        hit = self._thread_cache.get(thread_id)
        if hit and time.time() - hit[0] < 2:
            return hit[1]
        try:
            t = (await self.client.request("thread/read", {"threadId": thread_id}, timeout=15)).get("thread")
        except Exception as e:
            log.debug("codex thread/read %s failed: %s", thread_id, e)
            t = None
        if t and t.get("path"):
            self._paths[thread_id] = t["path"]
        self._thread_cache[thread_id] = (time.time(), t)
        return t

    async def load_subagent_history(self, session_id: str, agent_id: str, cwd: str) -> list[Item]:
        return await self._thread_items(agent_id)  # a sub-agent is a thread of its own, shown whole

    # ---- compactions: "compacted" rollout entries, each followed by its contextCompaction item
    async def list_compactions(self, sessions: list[SessionSummary]) -> dict[str, list[Compaction]]:
        def scan() -> dict[str, list[Compaction]]:
            found = {s.id: self._compactions(Path(s.transcript)) for s in sessions if s.harness == self.id and s.transcript}
            return {sid: cs for sid, cs in found.items() if cs}

        return await asyncio.to_thread(scan)

    def _compactions(self, path: Path) -> list[Compaction]:
        with self._compaction_lock:
            scan = tail_scan(self._compaction_cache, path, _CompactionScan)
            return scan.compactions() if scan else []

    def _rollout_compactions(self, path: Path) -> tuple[list[Compaction], list[str]]:
        return self._compactions(path), _compaction_summaries(path)

    async def _parts(self, session_id: str, cwd: str) -> tuple[list[Item], list[int]]:
        """A thread's whole conversation and where its compactions are in it: each with its id as listed
        (``compaction``), and followed by its summary when the rollout has it."""
        items = await self._thread_items(session_id)
        count = sum(1 for it in items if _is_compact(it))
        path = self.transcript_path(session_id, cwd) if count else None
        listed, summaries = await asyncio.to_thread(self._rollout_compactions, path) if path else ([], [])
        out: list[Item] = []
        marks: list[int] = []
        for it in items:
            out.append(it)
            if not _is_compact(it):
                continue
            n = len(marks)
            marks.append(len(out) - 1)
            if len(listed) == count:  # by position: an older rollout's compactions have no item ids
                it["compaction"] = listed[n].id
            if len(summaries) == count and summaries[n]:  # a remote compaction leaves nothing readable
                out.append({"kind": "system", "id": f"{it['id']}:summary", "subtype": "compact_summary", "text": "Summary of the earlier conversation", "detail": summaries[n]})
        return out, marks

    async def load_compacted_history(self, session_id: str, compaction_id: str, cwd: str) -> list[Item]:
        items, marks = await self._parts(session_id, cwd)
        at = next((n for n, i in enumerate(marks) if items[i].get("compaction") == compaction_id), None)
        if at is None:
            raise FileNotFoundError(f"no compaction {compaction_id} in session {session_id}")
        return items[marks[at - 1] if at else 0 : marks[at]]

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
        try:
            await self.client.request("thread/delete", {"threadId": session_id}, timeout=30)
        except RpcError as e:
            if "forked history" in str(e):  # a fork reads the start of its history from this thread's rollout
                raise ValueError("Forks of this conversation still read its history: delete them first, or archive this one instead") from e
            raise
        self._paths.pop(session_id, None)

    async def rename_session(self, session_id: str, title: str, cwd: str) -> None:
        try:
            await self.client.request("thread/name/set", {"threadId": session_id, "name": title}, timeout=15)
        except Exception as e:
            log.debug("thread/name/set(%s) failed: %s", session_id, e)

    # ---- forks -------------------------------------------------------------
    async def fork_point(self, session_id: str, cwd: str, anchor: str | None, user_index: int | None) -> ForkPoint:
        turns = await self._turns(session_id)
        at = _turn_of(turns, anchor) if anchor else None
        if at is None and user_index:
            reviews = review_prompts(turns)  # shown as commands, which don't count
            starts = [i for i, t in enumerate(turns) if i not in reviews and any(it.get("type") == "userMessage" for it in t.get("items") or [])]
            if user_index < len(starts):
                at = starts[user_index] - 1
        if at is None or at < 0:
            raise ValueError("Couldn't find that point of the conversation in the thread")
        if turns[at].get("status") == "inProgress":
            raise ValueError("That turn is still running")
        nxt = turns[at + 1] if at + 1 < len(turns) else None
        return ForkPoint(cut=turns[at].get("id"), cut_ts=_epoch(turns[at].get("completedAt")), next_ts=_epoch((nxt or {}).get("startedAt")))

    async def fork_session(self, session_id: str, cwd: str, point: ForkPoint, *, target_cwd: str, title: str, note: str = "") -> str:
        params: dict[str, Any] = {"threadId": session_id, "cwd": target_cwd, "excludeTurns": True}
        if point.cut:
            params["lastTurnId"] = point.cut
        res = await self.client.request("thread/fork", params, timeout=60)
        thread = res.get("thread") or {}
        tid = thread["id"]
        if thread.get("path"):
            self._paths[tid] = thread["path"]
        if note:  # thread/resume doesn't show the model changed developerInstructions, so the note goes into the history
            item = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": note}]}
            try:
                await self.client.request("thread/inject_items", {"threadId": tid, "items": [item]}, timeout=15)
            except Exception as e:
                log.warning("couldn't add the fork note to thread %s: %s", tid, e)
        try:  # loaded again (thread/resume) when the fork is first used
            await self.client.request("thread/unsubscribe", {"threadId": tid}, timeout=5)
        except Exception:
            pass
        await self.rename_session(tid, title, target_cwd)
        return tid

    async def move_session(self, session_id: str, cwd: str, target_cwd: str, note: str = "") -> None:
        # the thread takes the new cwd when it resumes; loading it here records it now and carries the note
        await self.client.request("thread/resume", {"threadId": session_id, "cwd": target_cwd, "excludeTurns": True}, timeout=60)
        try:
            if note:  # as for forks: a changed developerInstructions isn't shown to the model on resume
                item = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": note}]}
                try:
                    await self.client.request("thread/inject_items", {"threadId": session_id, "items": [item]}, timeout=15)
                except Exception as e:
                    log.warning("couldn't add the move note to thread %s: %s", session_id, e)
        finally:
            try:
                await self.client.request("thread/unsubscribe", {"threadId": session_id}, timeout=5)
            except Exception:
                pass

    # ---- content -----------------------------------------------------------
    async def _turns(self, session_id: str) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        cursor = None
        for _ in range(100):
            params: dict[str, Any] = {"threadId": session_id, "itemsView": "full", "sortDirection": "asc", "limit": 50}
            if cursor:
                params["cursor"] = cursor
            res = await self.client.request("thread/turns/list", params, timeout=60)
            turns += res.get("data") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return turns

    async def _thread_items(self, session_id: str) -> list[Item]:
        turns = await self._turns(session_id)
        reviews = review_prompts(turns)
        return [it for i, turn in enumerate(turns) for it in turn_items(turn, i in reviews)]

    async def load_history(self, session_id: str, cwd: str) -> list[Item]:
        items, marks = await self._parts(session_id, cwd)
        return items[marks[-1] :] if marks else items

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


def _thread_cwds(roots: list[str]) -> list[str]:
    cwds: set[str] = set()
    for r in roots:
        cwds.add(r)
        if r.startswith("/private/"):  # macOS: /tmp → /private/tmp; threads record whichever the shell used
            cwds.add(r[len("/private"):])
    return sorted(cwds)


def _subagent_summary(t: dict[str, Any], depth: int, tool_use_id: str | None = None) -> SubagentSummary:
    source = t.get("source")
    spawn = source.get("subAgent") if isinstance(source, dict) else None
    spawn = (spawn.get("thread_spawn") if isinstance(spawn, dict) else None) or {}
    return SubagentSummary(
        id=t["id"],
        agent_type=t.get("agentRole") or spawn.get("agent_role"),
        name=t.get("agentNickname") or spawn.get("agent_nickname") or t.get("name"),
        description=short(t.get("preview") or "", 120) or (_task_name(spawn["agent_path"]) if spawn.get("agent_path") else t.get("name") or "Sub-agent"),
        status=SUBAGENT_STATUS.get((t.get("status") or {}).get("type") or "", "done"),
        updated_at=float(t.get("updatedAt") or t.get("createdAt") or 0),
        created_at=float(t["createdAt"]) if t.get("createdAt") else None,
        tool_use_id=tool_use_id or None,
        model=t.get("model"),
        depth=int(spawn.get("depth") or depth),
    )


def _epoch(v: Any) -> float | None:
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _is_compact(it: Item) -> bool:
    return it["kind"] == "system" and it.get("subtype") == "compact"


class _CompactionScan(TailScan):
    """A rollout's compactions ("compacted" entries, named by the contextCompaction item that follows
    each), with when each part began, its first prompt and the context's size before."""

    def __init__(self, ino: int):
        super().__init__(ino)
        self.thread: str | None = None  # the rollout's own thread (others' items show up in it too)
        self.started: float | None = None  # when the part being read began
        self.prompt: str | None = None  # its first prompt, once read
        self.tokens: int | None = None  # the context sent with the last model call
        self.found: list[Compaction] = []
        self.named = 0  # how many of them have their item id

    def line(self, raw: bytes) -> None:
        wanted = (b'"compacted"', b'"ContextCompaction"', b'"token_count"') + ((b'"UserMessage"', b'"user_message"') if self.prompt is None else ())
        if self.started is not None and not any(w in raw for w in wanted):
            return
        try:
            d = json.loads(raw)
        except ValueError:
            return
        p = d.get("payload") if isinstance(d, dict) else None
        if not isinstance(p, dict):
            return
        ts = iso_to_epoch(d.get("timestamp"))
        if self.started is None:
            self.started = ts
        item = p.get("item")
        item = item if isinstance(item, dict) else {}
        own = not self.thread or p.get("thread_id") in (None, self.thread)
        if d.get("type") == "session_meta":
            self.thread = p.get("id")
        elif d.get("type") == "compacted":
            self.found.append(Compaction(id="", at=ts, started_at=self.started, tokens=self.tokens, first_prompt=self.prompt or ""))
            self.started, self.prompt = ts, None
        elif p.get("type") == "item_completed" and item.get("type") == "ContextCompaction" and own:
            if self.named < len(self.found):
                self.found[self.named].id = item.get("id") or ""
                self.named += 1
        elif p.get("type") == "token_count":
            self.tokens = ((p.get("info") or {}).get("last_token_usage") or {}).get("input_tokens") or self.tokens
        elif self.prompt is None and own:
            text = None
            if p.get("type") == "item_completed" and item.get("type") == "UserMessage":
                text = "\n".join(c.get("text") or "" for c in item.get("content") or [] if isinstance(c, dict) and c.get("type") == "text")
            elif p.get("type") == "user_message":  # older rollouts
                text = p.get("message")
            if isinstance(text, str) and text.strip():
                self.prompt = short(split_attachment_note(text)[0], 300)

    def compactions(self) -> list[Compaction]:
        """As listed: an older rollout's compactions (no item ids) are numbered."""
        return [c if c.id else replace(c, id=f"compaction-{n}") for n, c in enumerate(self.found, 1)]


def _compaction_summaries(path: Path) -> list[str]:
    """What each compaction in a rollout left the model, in order ("" for a remote compaction's, which
    isn't readable)."""
    out: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type":"compacted"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("type") == "compacted":
                out.append(((d.get("payload") or {}).get("message") or "").strip())
    return out


def _turn_of(turns: list[dict[str, Any]], anchor: str) -> int | None:
    """Index of the turn holding chat item ``anchor``: a thread item id, or one of the per-turn ids
    Atrium makes up (``result-<turn>``, ``turn-<turn>``, ``plan-<turn>``)."""
    for i, t in enumerate(turns):
        if anchor in (f"result-{t.get('id')}", f"turn-{t.get('id')}", f"plan-{t.get('id')}"):
            return i
        if any(it.get("id") == anchor for it in t.get("items") or []):
            return i
    return None


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
