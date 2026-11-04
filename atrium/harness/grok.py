"""Grok Build harness: xAI's coding agent (the ``grok`` CLI) driven over the Agent Client Protocol.

Every live session runs its own ``grok agent stdio`` process (JSON-RPC on stdio, see ``acp.py``). The CLI comes
with Atrium: xAI's ``grok-cli-bin`` wheel bundles it. ``harnesses.grok.binary`` "" = the ``grok`` of xAI's
installer (``$GROK_HOME/bin``) or on PATH when there is one — the same CLI as the terminal's, writing the same
sessions, and newer than the wheel — else the bundled one; a path or another command = that one.

Sessions are Grok's own, in ``$GROK_HOME/sessions/<cwd, URL-encoded>/<id>/`` (``~/.grok`` by default), so
``grok --resume`` lists them too:

    summary.json          title, times, model
    updates.jsonl         what the agent streamed, as it replays it (history, tracker digest)
    rewind_points.jsonl   each prompt's files before and after the edits it made (per-session diffs)

Atrium picks a new session's id (``_meta.sessionId``) and adds its system prompt as rules (``_meta.rules``,
applied when a session is created, as Grok keeps a session's prompt). Atrium's tools are in-process MCP
servers the agent reaches over the same connection (``_meta["x.ai/mcp/servers"]``, called back with
``x.ai/mcp/sdk_call``); the model finds them with ``search_tool`` and calls them with ``use_tool``
(``board__status``…). Approvals arrive as ``session/request_permission`` and are asked in the chat as the
session's permission mode says. Settings are Grok's own ``config.toml``: models, custom endpoints and their
keys, MCP servers, permission rules.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from ..capture import loopback_env
from ..config import RUNTIME_DIR, atomic_write, config
from .acp import AcpClient, AcpError
from .base import Attachment, Emit, FileEdit, Harness, Item, LiveSession, LiveSpec, SessionSummary, SlashCommand, ToolSpec
from .util import attachment_note, clip, iso_to_epoch, parse_json_loose, short, short_tail, split_attachment_note, upload_url

log = logging.getLogger("atrium.grok")

# Grok's own permission modes ([ui] permission_mode): it asks for what isn't allowed already (reads and
# safe commands never are), or a classifier approves the safe ones, or nothing is asked.
PERMISSION_MODES = [
    {"id": "ask", "label": "Ask"},
    {"id": "auto", "label": "Auto"},
    {"id": "always-approve", "label": "Always approve"},
]
# Grok's own levels for a model that takes an effort without listing its levels (a custom one: supports_reasoning_effort)
EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
DEFAULT_WINDOW = 200_000  # Grok's context window for a custom model that doesn't set one
CLIENT_INFO = {"name": "atrium", "title": "Atrium", "version": "0.1.0"}
STOP_SUBTYPE = {"end_turn": "success", "cancelled": "interrupted", "max_tokens": "error_max_tokens",
                "max_turn_requests": "error_max_turns", "refusal": "refusal", "error": "error", "rate_limit": "error"}
# Grok's own turns (a command it moved to the background ended, a monitor saw something, /loop fired…) run prompts it
# makes up itself; these are reminders for its model alone, kept out of the chat as Grok keeps them out of its own
# (hideFromScrollback). The others (/loop's scheduled prompt…) show what they ask.
HIDDEN_PROMPTS = ("task-completed-", "subagent-completed-", "workflow-completed-", "notifications-", "goal-summary-",
                  "goal-classifier-nudge-")
TURN_UPDATES = {"agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan"}
READ_TOOLS = {"read_file", "list_dir", "grep", "search_tool", "get_command_or_subagent_output", "scheduler_list"}
EDIT_TOOLS = {"search_replace", "write"}
# not offered: the composer's permission picker sets the mode, and /context prints nothing over ACP (the terminal
# draws it; the composer has the context ring)
HIDDEN_COMMANDS = {"always-approve", "context"}
LOCAL_COMMANDS = {"compact", "session-info"}  # answered without a turn of the model's
SLASH_RE = re.compile(r"/([^\s/]+)(?:\s+(.*))?", re.S)
_CMD_RULE_RE = re.compile(r"Bash\((.+?)(?::\*)?\)$")  # Claude-style allow rules ("Bash(git log:*)")
# how Grok records a message sent while a turn ran (x.ai/interject, the terminal's mid-turn messages)
_INTERJECTED_RE = re.compile(r"\AThe user sent a message while you were working:\n<user_query>\n(.*)\n</user_query>\s*\Z", re.S)
_EXIT_RE = re.compile(r"\Aexit: (-?\d+)\n?")  # how Grok heads a command's output for the model
# what Atrium tells the agent ahead of a message (it was moved, what the user said stopping it): not the user's words
_NOTE_RE = re.compile(r"\A<system-reminder>\n.*?\n</system-reminder>\n\n", re.S)
# Turning down a call ends Grok's turn: the read-only agent is then told why, as its next message, and carries on.
READ_ONLY_NOTE = "That action isn't allowed: this agent only reads (it may run the read-only commands it was given). Carry on without it."


def grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok").expanduser()


def sessions_root() -> Path:
    return grok_home() / "sessions"


def _defaults() -> dict[str, Any]:
    """Atrium's defaults for new Grok agents (``harnesses.grok``): enabled, binary, mode, model, effort."""
    return config.harness_defaults("grok")


_tasks: set[asyncio.Task[Any]] = set()  # work that goes on after whoever started it returned (or gave up)


def _background(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------
DEFAULT_BINARIES = ("", "grok")  # xAI's installer's or PATH's CLI, else the bundled one


def bundled_grok() -> str | None:
    """The Grok CLI that comes with Atrium (the ``grok-cli-bin`` wheel), or None when it isn't installed."""
    try:
        from grok_cli_bin import bundled_grok_path

        return str(bundled_grok_path())
    except (ImportError, FileNotFoundError):
        return None


def resolve_binary(binary: str | None = None) -> str | None:
    binary = (_defaults().get("binary") or "") if binary is None else binary
    if binary in DEFAULT_BINARIES:
        installed = grok_home() / "bin" / "grok"  # where xAI's installer (and its npm package) put it
        return (str(installed) if os.access(installed, os.X_OK) else None) or shutil.which("grok") or bundled_grok()
    b = os.path.expanduser(binary or "")
    if os.path.isabs(b):
        return b if os.access(b, os.X_OK) else None
    return shutil.which(b)


def not_found(binary: str) -> str:
    if binary in DEFAULT_BINARIES:
        return "Grok CLI not found: there is no grok on PATH and the bundled one (grok-cli-bin) isn't installed (run make install)."
    return f"Grok CLI not found ({binary}). Install it, or clear harnesses.grok.binary in Atrium settings to use the bundled one."


def agent_env() -> dict[str, str]:
    """The environment of a ``grok`` process: Atrium's, with loopback never through a proxy, and no update checks."""
    return {**os.environ, **loopback_env(os.environ), "GROK_DISABLE_AUTOUPDATER": "1"}


# --------------------------------------------------------------------------
# Grok's own settings: $GROK_HOME/config.toml
# --------------------------------------------------------------------------
_toml_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def user_config() -> dict[str, Any]:
    """``config.toml``, re-read when it changes; {} if it doesn't exist or doesn't parse."""
    path = grok_home() / "config.toml"
    try:
        st = path.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    hit = _toml_cache.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        log.warning("can't read %s: %s", path, e)
        data = {}
    _toml_cache[str(path)] = (sig, data)
    return data


def acp_mcp_servers(servers: dict[str, Any]) -> list[dict[str, Any]]:
    """Atrium's ``mcp_servers`` (Claude Code's format) as ACP's: stdio (``command``) and HTTP (``url``) servers."""
    out: list[dict[str, Any]] = []
    for name, c in (servers or {}).items():
        if not isinstance(c, dict):
            continue
        if c.get("url"):
            out.append({"type": "sse" if c.get("type") == "sse" else "http", "name": name, "url": c["url"],
                        "headers": [{"name": k, "value": str(v)} for k, v in (c.get("headers") or {}).items()]})
        elif c.get("command"):
            out.append({"name": name, "command": c["command"], "args": [str(a) for a in c.get("args") or []],
                        "env": [{"name": k, "value": str(v)} for k, v in (c.get("env") or {}).items()]})
    return out


# --------------------------------------------------------------------------
# Tool calls as chat items (names and inputs after Claude Code's, which the UI renders)
# --------------------------------------------------------------------------


def _plain(args: Any) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k != "variant"} if isinstance(args, dict) else {}


def mcp_name(tool_name: str) -> tuple[str, str]:
    """``board__status`` → (``board``, ``status``): how ``use_tool`` names a server's tool."""
    server, _, tool = (tool_name or "").partition("__")
    return (server, tool) if tool else ("", tool_name or "")


def _abs(path: Any, cwd: str | None) -> Any:
    """A path the model gave relative to the session's directory (Grok takes either), made absolute."""
    return os.path.normpath(os.path.join(cwd, path)) if cwd and isinstance(path, str) and path and not os.path.isabs(path) else path


def tool_view(name: str, args: Any, cwd: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """A Grok tool call (its name, the model's arguments) → (name, input) for the chat; None: not shown. ``cwd``:
    the session's directory, which relative paths are in."""
    a = _plain(args)
    for key in ("file_path", "target_file", "path", "target_directory"):
        if key in a:
            a[key] = _abs(a[key], cwd)
    if name == "run_terminal_command":
        inp: dict[str, Any] = {"command": a.get("command") or ""}
        if a.get("description"):
            inp["description"] = a["description"]
        if a.get("background") or a.get("is_background"):
            inp["run_in_background"] = True
        return "Bash", inp
    if name == "read_file":
        inp = {"file_path": a.get("target_file") or a.get("file_path")}
        if a.get("offset"):
            inp["offset"] = a["offset"]
        if a.get("limit"):
            inp["limit"] = a["limit"]
        return "Read", inp
    if name == "search_replace":
        return "Edit", {"file_path": a.get("file_path"), "old_string": a.get("old_string") or "", "new_string": a.get("new_string") or "",
                        "replace_all": bool(a.get("replace_all"))}
    if name == "write":
        return "Write", {"file_path": a.get("file_path"), "content": a.get("content") or ""}
    if name == "grep":
        return "Grep", {k: v for k, v in a.items() if v is not None}
    if name == "web_search":
        return "WebSearch", {"query": a.get("query") or ""}
    if name == "web_fetch":
        return "WebFetch", {"url": a.get("url") or ""}
    if name == "use_tool":
        server, tool = mcp_name(str(a.get("tool_name") or ""))
        given = a.get("tool_input")
        return (f"mcp__{server}__{tool}" if server else tool), (given if isinstance(given, dict) else {"input": given})
    if name == "spawn_subagent":
        return "Agent", {"description": a.get("description") or short(a.get("prompt") or "", 200), "subagent_type": a.get("subagent_type") or "subagent",
                         "prompt": a.get("prompt") or ""}
    if name == "todo_write":
        return None  # often a partial update: the plan update that follows it has the whole list
    if name in ("image_gen", "image_edit"):
        return "ImageGen", {"prompt": a.get("prompt") or ""}
    if name == "exit_plan_mode":
        return "ExitPlanMode", a
    return name, a


def _content_text(blocks: Any) -> str:
    out = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        inner = b.get("content") if b.get("type") == "content" else b
        if isinstance(inner, dict) and inner.get("type") == "text" and inner.get("text"):
            out.append(inner["text"])
    return "\n".join(out)


def result_view(name: str, upd: dict[str, Any]) -> tuple[str, bool]:
    """(content, is_error) of a finished tool call (its last ``tool_call_update``)."""
    raw = upd.get("rawOutput")
    err = upd.get("status") == "failed"
    text = ""
    if isinstance(raw, dict):
        kind = raw.get("type")
        if kind == "Bash":
            text = raw.get("output_for_prompt") or ""
            m = _EXIT_RE.match(text)
            text = text[m.end():] if m else text
            code = raw.get("exit_code")
            if code not in (0, None):
                err = True
                text = text.rstrip() + f"\n[exit code {code}]"
            elif raw.get("timed_out"):
                err, text = True, text.rstrip() + "\n[timed out]"
        elif kind == "MCP":
            out = raw.get("output")
            if isinstance(out, dict) and len(out) == 1:
                key, val = next(iter(out.items()))
                err = err or "rror" in key
                text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            else:
                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        else:
            for v in raw.values():  # {"type": "SearchReplace", "EditsApplied": {"tool_output_for_prompt": …}} and kin
                if isinstance(v, dict) and isinstance(v.get("tool_output_for_prompt"), str):
                    text = v["tool_output_for_prompt"]
                    break
                if isinstance(v, dict) and isinstance(v.get("content"), str):
                    text = v["content"]
                    break
            if not text and isinstance(raw.get("content"), str):
                text = raw["content"]
            if not text and kind and any(k for k in raw if k not in ("type",)) and name not in EDIT_TOOLS:
                text = _content_text(upd.get("content")) or json.dumps({k: v for k, v in raw.items() if k != "type"}, ensure_ascii=False)
    if not text:
        text = _content_text(upd.get("content"))
    if not text and name in EDIT_TOOLS and not err:
        text = "Done."
    return clip(text), err


def _diffs(upd: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in upd.get("content") or [] if isinstance(b, dict) and b.get("type") == "diff" and b.get("path")]


# --------------------------------------------------------------------------
# History (updates.jsonl: what the agent streamed, one update per line)
# --------------------------------------------------------------------------


def _entries(path: Path) -> list[dict[str, Any]]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("params"), dict):
                out.append(d)
    return out


def _update(d: dict[str, Any]) -> dict[str, Any]:
    u = d["params"].get("update")
    return u if isinstance(u, dict) else {}


def _ts(d: dict[str, Any]) -> float | None:
    """When an update was made: the agent's own clock in ms when it says, else the line's whole seconds."""
    for meta in ((d["params"].get("_meta") or {}), (_update(d).get("_meta") or {})):
        if isinstance(meta, dict) and isinstance(meta.get("agentTimestampMs"), (int, float)):
            return meta["agentTimestampMs"] / 1000
    t = d.get("timestamp")
    return float(t) if isinstance(t, (int, float)) else None


def _event_id(d: dict[str, Any], fallback: str) -> str:
    meta = d["params"].get("_meta") or {}
    return str(meta.get("eventId") or fallback) if isinstance(meta, dict) else fallback


def _native(u: dict[str, Any]) -> str | None:
    """A tool call's Grok name: its ``x.ai/tool`` metadata, else the title the first update gives it."""
    meta = (u.get("_meta") or {}).get("x.ai/tool") if isinstance(u.get("_meta"), dict) else None
    return (meta.get("name") if isinstance(meta, dict) else None) or None


def user_message_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    m = _INTERJECTED_RE.match(text)
    text, files = split_attachment_note(_NOTE_RE.sub("", m.group(1) if m else text, count=1))
    return text, [{**f, "url": upload_url(f["path"])} for f in files]


def _hidden(u: dict[str, Any]) -> bool:
    """A message Grok gave its model itself (a background command ended…): not the user's, not in the chat."""
    meta = u.get("_meta")
    return isinstance(meta, dict) and bool(meta.get("hideFromScrollback"))


def plan_item(pid: str, entries: Any) -> list[Item]:
    status = {"in_progress": "in_progress", "completed": "completed"}
    todos = [{"content": e.get("content") or "", "status": status.get(e.get("status") or "", "pending")} for e in entries or [] if isinstance(e, dict)]
    return [{"kind": "tool", "id": pid, "name": "TodoWrite", "input": {"todos": todos}}, {"kind": "tool_result", "id": pid, "content": "", "is_error": False}]


def retry_item(iid: str, state: dict[str, Any]) -> Item:
    why = state.get("reason") or state.get("message") or state.get("error_type") or "error"
    return {"kind": "system", "id": iid, "subtype": "retry", "text": f"Model provider error ({short(why, 120)}), retrying", "detail": json.dumps(state, ensure_ascii=False)}


def history_items(entries: list[dict[str, Any]], cwd: str | None = None) -> list[Item]:
    """The chat items of a session's updates, in order. A rewind marker drops the prompts it went back over."""
    items: list[Item] = []
    prompts: list[int] = []  # the prompt index each item belongs to
    tools: dict[str, tuple[str, dict[str, Any]]] = {}  # tool call -> (Grok name, arguments)
    pos: dict[str, int] = {}  # item key -> index in items
    prompt, last = -1, None  # the prompt being read, and the kind of the update before
    command = False  # that prompt ran one of the agent's commands: what it printed is the command's
    hidden = False  # that prompt is one Grok gave its model itself (not shown; it still counts for rewinds)

    def put(it: Item, key: str | None = None) -> None:
        key = key or f"{it['kind']}:{it['id']}"
        if key in pos and pos[key] < len(items):
            items[pos[key]] = it
            return
        pos[key] = len(items)
        items.append(it)
        prompts.append(prompt)

    for n, d in enumerate(entries):
        u = _update(d)
        k = u.get("sessionUpdate")
        ts = _ts(d)
        eid = _event_id(d, f"e{n}")
        if k == "user_message_chunk":
            pi = (u.get("_meta") or {}).get("promptIndex") if isinstance(u.get("_meta"), dict) else None
            text = ((u.get("content") or {}).get("text") or "") if isinstance(u.get("content"), dict) else ""
            if not (last == k and (pi is None or pi == prompt)):  # a new message (else more blocks of the one before)
                prompt = pi if isinstance(pi, int) else prompt + 1
                hidden = _hidden(u)
                if not hidden:
                    put({"kind": "user", "id": f"u{prompt}-{eid}", "text": "", "ts": ts})
            command = False
            if not hidden and items and items[-1]["kind"] == "user":
                body, files = user_message_text(items[-1]["text"] + text)
                items[-1]["text"] = body
                if files:
                    items[-1]["attachments"] = files
                cmd = SLASH_RE.fullmatch(body.strip())
                command = bool(cmd and cmd.group(1) in LOCAL_COMMANDS)
                if command:
                    items[-1]["command"] = True
        elif k in ("agent_message_chunk", "agent_thought_chunk"):
            kind = ("command_output" if command else "text") if k == "agent_message_chunk" else "thinking"
            text = ((u.get("content") or {}).get("text") or "") if isinstance(u.get("content"), dict) else ""
            if last == k and items and items[-1]["kind"] == kind:
                items[-1]["text"] += text
            elif text:
                put({"kind": kind, "id": f"{kind[0]}-{eid}", "text": text, "ts": ts})
        elif k in ("tool_call", "tool_call_update"):
            tid = u.get("toolCallId")
            if not tid:
                continue
            name, args = tools.get(tid, (None, {}))
            name = name or _native(u) or (u.get("title") if k == "tool_call" else None) or "tool"
            if isinstance(u.get("rawInput"), dict):
                args = {**args, **_plain(u["rawInput"])}
            tools[tid] = (name, args)
            view = tool_view(name, args, cwd)
            if view and (k == "tool_call" or f"tool:{tid}" in pos or u.get("rawInput") is not None):
                put({"kind": "tool", "id": tid, "name": view[0], "input": view[1], "ts": items[pos[f"tool:{tid}"]].get("ts") if f"tool:{tid}" in pos else ts})
            if view and u.get("status") in ("completed", "failed"):
                content, err = result_view(name, u)
                put({"kind": "tool_result", "id": tid, "content": content, "is_error": err, "ts": ts})
        elif k == "plan":
            for it in plan_item(f"plan-{prompt}", u.get("entries")):
                put({**it, "ts": ts})
        elif k == "rewind_marker":
            target = u.get("target_prompt_index")
            if isinstance(target, int):
                keep = [i for i, p in enumerate(prompts) if p < target]
                items[:] = [items[i] for i in keep]
                prompts[:] = [prompts[i] for i in keep]
                pos = {}
                for i, it in enumerate(items):
                    pos[f"{it['kind']}:{it['id']}"] = i
                prompt = target - 1
        elif k in ("compaction_checkpoint", "auto_compact_completed"):
            put({"kind": "system", "id": f"compact-{eid}", "subtype": "compact", "text": "Conversation compacted", "ts": ts})
        elif k == "retry_state" and u.get("type") == "retrying":
            put(retry_item(f"retry-{eid}", u))
        elif k == "retry_state" or k == "auto_compact_failed":
            put({"kind": "error", "id": f"err-{eid}", "text": str(u.get("message") or u.get("reason") or u.get("error") or "Error"), "ts": ts})
        elif k == "turn_completed" and u.get("stop_reason") == "cancelled":
            put({"kind": "system", "id": f"interrupted-{eid}", "subtype": "interrupted", "text": "Interrupted", "ts": ts})
        if k not in (None, "hook_execution", "hook_run_started", "available_commands_update", "session_info_update", "current_mode_update",
                     "config_option_update", "usage_update", "tool_call_delta_chunk", "pending_interaction", "interaction_resolved"):
            last = k
    for it in items:
        if it.get("ts") is None:
            it.pop("ts", None)
    return items


def digest_update(d: dict[str, Any], tools: dict[str, str]) -> list[str]:
    u = _update(d)
    k = u.get("sessionUpdate")
    ts = _ts(d)
    pre = f"[{time.strftime('%H:%M', time.localtime(ts))}] " if ts else ""
    text = ((u.get("content") or {}).get("text") or "") if isinstance(u.get("content"), dict) else ""
    if k == "user_message_chunk" and text.strip():
        return [] if _hidden(u) else [f"{pre}USER: {short(user_message_text(text)[0], 600)}"]
    if k == "agent_message_chunk" and text.strip():
        return [f"{pre}AGENT: {short(text, 700)}"]
    if k == "agent_thought_chunk" and text.strip():
        return [f"{pre}THINKING: {short(text.replace('**', ''), 400)}"]
    if k == "tool_call":
        name = _native(u) or u.get("title") or ""
        tools[u.get("toolCallId") or ""] = name
        a = _plain(u.get("rawInput"))
        if name in READ_TOOLS:
            return []  # reads and searches are too noisy to matter for progress tracking
        if name == "run_terminal_command":
            return [f"{pre}TOOL Bash: {short(a.get('command') or '', 180)}"]
        if name == "write":
            return [f"{pre}wrote {a.get('file_path')} ({len(str(a.get('content') or '').splitlines())} lines)"]
        if name == "search_replace":
            return [f"{pre}edited {a.get('file_path')}"]
        if name == "use_tool":
            server, tool = mcp_name(str(a.get("tool_name") or ""))
            inp = x if isinstance(x := a.get("tool_input"), dict) else {}
            return [f"{pre}TOOL {server}.{tool}: {short({x: y for x, y in inp.items() if x != 'html'}, 200)}"]
        return [f"{pre}TOOL {name}: {short(a, 160)}"]
    if k == "tool_call_update" and u.get("status") in ("completed", "failed") and tools.get(u.get("toolCallId") or "") == "run_terminal_command":
        content, err = result_view("run_terminal_command", u)
        tail = short_tail(content, 200) if content.strip() else ""
        if err:
            return [f"{pre}(command failed){' ' + tail if tail else ''}"]
        return [f"{pre}→ {tail}"] if tail else []
    if k == "retry_state" and u.get("type") != "retrying":
        return [f"{pre}(error: {short(u.get('message') or u.get('reason') or '', 200)})"]
    return []


def _snapshot_edits(path: Path, cwd: str) -> tuple[list[FileEdit], int]:
    """The files each prompt changed with their edit tools, as they were before and after (rewind_points.jsonl),
    and how many prompts that covers."""
    edits: list[FileEdit] = []
    count = 0
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or not isinstance(d.get("file_snapshots"), dict):
                continue
            count = max(count, int(d.get("prompt_index") or 0) + 1)
            after = x if isinstance(x := d.get("after_snapshots"), dict) else {}
            for rel, snap in d["file_snapshots"].items():
                if not isinstance(snap, dict):
                    continue
                full = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
                before = snap.get("content")
                done = x if isinstance(x := after.get(rel), dict) else None
                now = done.get("content") if done else None
                kind = "create" if before is None and now is not None else "delete" if done and now is None else "update"
                edits.append(FileEdit(path=full, tool="Edit", ts=snap.get("captured_at") or d.get("created_at"), before=before, after=now, kind=kind))
    return edits, count


def _patch(details: Any, old: str, new: str) -> str:
    """Unified hunks for one replacement (Grok records where it was: ``old_line``); reverse-applied to find a baseline."""
    d = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
    start = max(int(d.get("old_line") or 1), 1)
    a, b = old.split("\n"), new.split("\n")
    return "\n".join([f"@@ -{start},{len(a)} +{start},{len(b)} @@", *("-" + x for x in a), *("+" + x for x in b)])


def update_edits(entries: list[dict[str, Any]], after_prompt: int) -> list[FileEdit]:
    """Edits the updates record for prompts from ``after_prompt`` on (a turn whose rewind point isn't written yet)."""
    edits: list[tuple[int, FileEdit]] = []
    prompt = -1
    for d in entries:
        u = _update(d)
        k = u.get("sessionUpdate")
        if k == "user_message_chunk":
            pi = (u.get("_meta") or {}).get("promptIndex")
            prompt = pi if isinstance(pi, int) else prompt + 1
        elif k == "rewind_marker" and isinstance(u.get("target_prompt_index"), int):
            edits = [(p, e) for p, e in edits if p < u["target_prompt_index"]]
        elif k == "tool_call_update" and u.get("status") == "completed" and prompt >= after_prompt:
            ts = _ts(d)
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None
            for b in _diffs(u):
                old, new = b.get("oldText"), b.get("newText") or ""
                details = (b.get("_meta") or {}).get("details") if isinstance(b.get("_meta"), dict) else None
                if old is None or old == "" and not details:  # a whole file written
                    edits.append((prompt, FileEdit(path=b["path"], tool="Write", ts=iso, before=None, after=new, kind="create" if old is None else "update")))
                else:
                    edits.append((prompt, FileEdit(path=b["path"], tool="Edit", ts=iso, before=None, after=None, kind="update",
                                                   patch=_patch(details, old or "", new))))
    return [e for _p, e in edits]


# --------------------------------------------------------------------------
# Session store: $GROK_HOME/sessions/<cwd>/<id>/
# --------------------------------------------------------------------------


def _cwd_of_dir(d: Path) -> str | None:
    """The working directory a ``sessions/<cwd>`` directory is for: its URL-decoded name, or (a long path, named by
    its hash) the ``.cwd`` file beside."""
    marker = d / ".cwd"
    if marker.is_file():
        try:
            return marker.read_text().strip() or None
        except OSError:
            return None
    name = urllib.parse.unquote(d.name)
    return name if name.startswith("/") or re.match(r"[A-Za-z]:[\\/]", name) else None


def session_dir(sid: str, cwd: str | None = None) -> Path | None:
    root = sessions_root()
    if cwd:
        p = root / urllib.parse.quote(cwd, safe="") / sid
        if p.is_dir():
            return p
    for p in root.glob(f"*/{sid}"):  # ids are unique: wherever it was started
        if p.is_dir():
            return p
    return None


def _bucket(cwd: str) -> Path:
    """The ``sessions/<cwd>`` directory Grok keeps a directory's sessions in: the URL-encoded path, or for a path
    too long to name a directory after, a hash Grok makes (blake3) — known only from one it made already."""
    name = urllib.parse.quote(cwd, safe="")
    if len(name.encode()) <= 255:
        return sessions_root() / name
    for d in sessions_root().iterdir():
        if d.is_dir() and _cwd_of_dir(d) == cwd:
            return d
    raise ValueError(f"Grok Build files the sessions of {cwd} under a name only it can make (the path is long): start a new agent there")


def _note_path(sid: str) -> Path:
    """Where moving a session leaves the note its agent is told with the next message (Grok keeps a session's
    system prompt as it started)."""
    return RUNTIME_DIR / "grok" / "notes" / f"{sid}.txt"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso_epoch(v: Any) -> float | None:
    return iso_to_epoch(v) if isinstance(v, str) else None


def _inside(path: str | None, root: str) -> bool:
    if not path:
        return False
    p = os.path.realpath(path)
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _command_allowed(command: str, rules: list[str]) -> bool:
    """Whether a Claude-style allow rule (``Bash(git log:*)``) covers the command; chains never are."""
    command = (command or "").strip()
    if not command or any(ch in command for ch in ";&|`$<>\n"):
        return False
    for rule in rules:
        m = _CMD_RULE_RE.match(rule)
        if m and (command == m.group(1) or command.startswith(m.group(1) + " ")):
            return True
    return False


# --------------------------------------------------------------------------
# Live session
# --------------------------------------------------------------------------


class GrokLiveSession(LiveSession):
    def __init__(self, harness: "GrokHarness", spec: LiveSpec, emit: Emit):
        self.harness = harness
        self.spec = spec
        self.emit = emit
        self.session_id = spec.session_id or str(uuid.uuid4())  # Grok takes the id Atrium picks (a UUID)
        self.state = "starting"
        self.model = spec.model or None
        self.effort = spec.effort or None
        self.mode = spec.permission_mode or "always-approve"
        self.client: AcpClient | None = None
        self._turn: asyncio.Task[None] | None = None
        self._queue: list[list[dict[str, Any]]] = []  # messages sent while a turn runs
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tools: dict[str, tuple[str, dict[str, Any]]] = {}  # tool call -> (Grok name, arguments so far)
        self._shown: dict[str, str] = {}  # tool call -> what was last emitted for it
        self._text: dict[str, Any] | None = None  # the assistant message (or thinking) being streamed
        self._toolsets: dict[str, dict[str, ToolSpec]] = {ns: {t.name: t for t in specs} for ns, specs in spec.toolsets.items()}
        self._models: list[dict[str, Any]] = []  # the agent's models (modelState)
        self._current_model: str | None = None  # the model the session runs, as Grok names it
        self._commands: list[dict[str, Any]] = []
        self._used: int | None = None  # context tokens after the last model call
        self._turns = 0
        self._created = spec.resume  # the session exists in Grok's store (a restarted agent resumes it)
        self._caps: dict[str, Any] = {}  # the agent's capabilities (initialize)
        self._command = False  # the turn runs one of the agent's commands (what it prints is the command's)
        self._interrupting = False  # Atrium stopped the turn
        self._rejected = False  # a call was turned down in this turn (which ends Grok's)
        self._errored = False  # the agent reported this turn's error already
        self._followups: list[str] = []  # what to tell the agent once a turned-down call ended its turn
        self._notes: list[str] = []  # what to tell it ahead of the next message (it was moved, why it was stopped)
        self._note_file: Path | None = None  # the move's note, deleted once told
        self._mine: set[str] = set()  # the prompts Atrium sent (their _meta.promptId); any other is a turn of Grok's own
        self._own: dict[str, Any] | None = None  # the turn Grok runs of its own accord: its prompt id, when it started
        self._own_done: set[str] = set()  # Grok's own turns that ended (an update about one may still come)
        self._loading = False  # session/load replays the conversation: nothing of it is new
        self._closed = False

    # ---- public API --------------------------------------------------------
    async def start(self) -> None:
        try:
            await self._open()
        except Exception as e:
            await self._shut()
            self._set_state("error")
            raise RuntimeError(f"Grok Build could not start: {e}") from e
        self.harness.live[self.session_id] = self
        note = _note_path(self.session_id)
        try:  # it was moved: its system prompt still has the old directory
            self._notes.append(note.read_text())
            self._note_file = note
        except OSError:
            pass
        self._connected("Connected")
        self._set_state("running" if self._own else "idle")  # Grok may carry on by itself as soon as it resumes

    async def send(self, text: str, attachments: list[Attachment] | None = None) -> None:
        prompt = self._prompt(text, attachments or [])
        if self._busy():
            self._queue.append(prompt)  # runs when the turn ends
            return
        self._turn = asyncio.create_task(self._turns_from(prompt), name=f"grok-{self.session_id[:8]}")

    async def interrupt(self) -> None:
        self._interrupting = True
        for fut in list(self._pending.values()):
            if not fut.done():  # ACP: a cancelled turn's open requests are answered "cancelled"
                fut.set_result({"behavior": "deny", "cancelled": True})
        if self.client and self.client.alive and self._busy():
            await self.client.notify("session/cancel", {"sessionId": self.session_id})

    async def set_permission_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        if self.client and self.client.alive:
            await self.client.notify("_x.ai/yolo_mode_changed", {"yolo_mode": mode == "always-approve", "auto_mode": mode == "auto",
                                                                  "permission_mode": mode if mode in ("auto", "always-approve") else "ask"})

    async def set_model(self, model: str | None) -> None:
        self.model = model or None
        if self.client and self.client.alive:
            target = self.model or self.harness.config_model()
            if target and target != self._current_model:
                await self.client.request("session/set_model", {"sessionId": self.session_id, "modelId": target}, timeout=60)
                self._current_model = target
                if self.effort:
                    await self._apply_effort()

    async def set_effort(self, effort: str | None) -> None:
        self.effort = effort or None
        if self.client and self.client.alive:
            await self._apply_effort()

    def resolve_permission(self, request_id: str, decision: dict[str, Any]) -> bool:
        fut = self._pending.get(request_id)
        if not fut or fut.done():
            return False
        fut.set_result(decision)
        return True

    def commands(self) -> list[SlashCommand] | None:
        return _slash_commands(self._commands) if self._commands else None

    def is_command(self, text: str) -> bool:
        m = SLASH_RE.fullmatch(text.strip())
        return bool(m) and m.group(1) in LOCAL_COMMANDS

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.harness.live.pop(self.session_id, None)
        if self._busy():
            try:
                await asyncio.wait_for(self.interrupt(), timeout=5)
                if self._turn and not self._turn.done():
                    await asyncio.wait_for(asyncio.shield(self._turn), timeout=10)
            except BaseException:
                if self._turn:
                    self._turn.cancel()
        await self._shut()
        self._set_state("closed")

    # ---- the agent process -------------------------------------------------
    def _servers(self) -> list[dict[str, str]]:
        return [{"name": ns, "serverId": f"atrium-{ns}"} for ns in self._toolsets]

    def _mode_meta(self) -> dict[str, Any]:
        return {"yoloMode": self.mode == "always-approve", "autoMode": self.mode == "auto"}

    async def _open(self) -> None:
        binary = resolve_binary()
        if not binary:
            raise FileNotFoundError(not_found(_defaults().get("binary") or ""))
        env = agent_env()  # not captured: only config.toml says where a model's calls go (GROK_CONFIG may not)
        self.client = AcpClient([binary, "--no-auto-update", "agent", "stdio"], cwd=self.spec.cwd, env=env,
                                on_notify=self._on_notify, on_request=self._on_request, name=f"grok {self.session_id[:8]}")
        await self.client.start()
        init = await self.client.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": CLIENT_INFO,
        }, timeout=90)
        meta = (init or {}).get("_meta") or {}
        self._caps = (init or {}).get("agentCapabilities") or {}
        self._read_models(meta.get("modelState"))
        self._commands = [c for c in meta.get("availableCommands") or [] if isinstance(c, dict)]
        if os.environ.get("XAI_API_KEY") and "xai.api_key" in {m.get("id") for m in (init or {}).get("authMethods") or []}:
            try:  # Grok reads the key itself; this only says which way it signs in
                await self.client.request("authenticate", {"methodId": "xai.api_key", "_meta": {"headless": True}}, timeout=60)
            except Exception as e:
                log.debug("grok authenticate: %s", e)
        servers = acp_mcp_servers(self.spec.extra_mcp_servers)
        meta_out: dict[str, Any] = {"x.ai/mcp/servers": self._servers(), **self._mode_meta(), "clientIdentifier": "atrium"}
        resume = self._created
        if resume:
            params = {"sessionId": self.session_id, "cwd": self.spec.cwd, "mcpServers": servers, "_meta": meta_out}
            if "resume" in (self._caps.get("sessionCapabilities") or {}):
                res = await self.client.request("session/resume", params, timeout=120)
            else:  # an older CLI: loading replays the conversation unless told not to (newer ones), and is ignored here
                self._loading = True
                try:
                    res = await self.client.request("session/load", {**params, "_meta": {**meta_out, "noReplay": True}}, timeout=300)
                finally:
                    self._loading = False
        else:
            meta_out.update(sessionId=self.session_id, rules=self.spec.system_append or None)
            if self.model:
                meta_out["modelId"] = self.model
            res = await self.client.request("session/new", {"cwd": self.spec.cwd, "mcpServers": servers,
                                                            "_meta": {k: v for k, v in meta_out.items() if v is not None}}, timeout=120)
            if (res or {}).get("sessionId") and res["sessionId"] != self.session_id:
                self.session_id = res["sessionId"]
            self._created = True
        self._read_models((res or {}).get("models"))
        if resume and self.model and self.model != self._current_model:
            try:
                await self.client.request("session/set_model", {"sessionId": self.session_id, "modelId": self.model}, timeout=60)
                self._current_model = self.model
            except Exception as e:
                log.warning("grok: can't switch %s to %s: %s", self.session_id, self.model, e)
        if self.effort:
            await self._apply_effort()

    async def _shut(self) -> None:
        client, self.client = self.client, None
        if client:
            await client.close()

    def _read_models(self, state: Any) -> None:
        if not isinstance(state, dict):
            return
        self._models = [m for m in state.get("availableModels") or [] if isinstance(m, dict)] or self._models
        self._current_model = state.get("currentModelId") or self._current_model

    def _model_entry(self) -> dict[str, Any]:
        return next((m for m in self._models if m.get("modelId") == self._current_model), {})

    def _window(self) -> int:
        return int((self._model_entry().get("_meta") or {}).get("totalContextTokens") or DEFAULT_WINDOW)

    async def _apply_effort(self) -> None:
        """The effort picked, else the model's own default; dropped for a model without efforts. Through
        ``session/set_model``'s _meta, which both CLIs take: 0.2.111 has no ``session/set_config_option``, and
        ignores an effort in ``session/new`` for a custom model."""
        meta = self._model_entry().get("_meta") or {}
        if not meta.get("supportsReasoningEffort"):
            return
        effort = self.effort or meta.get("reasoningEffort")
        if not effort or not self.client:
            return
        try:
            await self.client.request("session/set_model", {"sessionId": self.session_id, "modelId": self._current_model,
                                                            "_meta": {"reasoningEffort": effort}}, timeout=60)
        except Exception as e:
            log.warning("grok: can't set effort %s on %s: %s", effort, self.session_id, e)

    def _connected(self, what: str) -> None:
        entry = self._model_entry()
        label = entry.get("name") or self._current_model or "?"
        mode = next((m["label"] for m in PERMISSION_MODES if m["id"] == self.mode), self.mode)
        self.emit({
            "kind": "system", "id": f"init-{self.session_id}-{int(time.time() * 1000)}", "subtype": "init",
            "text": f"{what} · Grok Build · model {label}" + (f" · effort {self.effort}" if self.effort else "") + f" · {mode}",
            "model": self.model or self._current_model or "", "effort": self.effort, "permission_mode": self.mode,
        })

    def _prompt(self, text: str, attachments: list[Attachment]) -> list[dict[str, Any]]:
        # every attachment is listed (images too, so history can show them); images also go inline
        blocks: list[dict[str, Any]] = [{"type": "text", "text": (text or ("(see attached image)" if attachments else "")) + attachment_note(attachments)}]
        for a in attachments:
            if a.is_image and (self._caps.get("promptCapabilities") or {}).get("image"):
                try:
                    blocks.append({"type": "image", "data": base64.b64encode(Path(a.path).read_bytes()).decode(), "mimeType": a.mime})
                except OSError as e:
                    log.warning("grok: attachment %s not sent: %s", a.path, e)
        return blocks

    # ---- turns -------------------------------------------------------------
    def _busy(self) -> bool:
        """A turn runs: one of Atrium's (its ``session/prompt`` hasn't answered), or one Grok started itself."""
        return bool(self._turn and not self._turn.done()) or self._own is not None

    async def _turns_from(self, prompt: list[dict[str, Any]] | None) -> None:
        while prompt is not None and not self._closed:
            await self._turn_once(prompt)
            if self.state == "error":
                return
            prompt = self._queue.pop(0) if self._queue else None
        if not self._closed:
            self._set_state("waiting" if self._pending else "running" if self._own else "idle")

    async def _turn_once(self, prompt: list[dict[str, Any]]) -> None:
        """One turn as Atrium sees it. Turning a call down ends Grok's own turn: when the user said what to do
        instead (or the read-only agent has to be told why), that goes on as their next message, in the same turn."""
        started = time.time()
        self._flush()  # what Grok said in a turn of its own that this one cut short comes first
        self._set_state("running")
        self._turns += 1
        self._interrupting = self._rejected = self._errored = False
        self._command = self.is_command(str(prompt[0].get("text") or "")) if prompt else False
        subtype, error, reason, turns = "success", None, "end_turn", None
        while True:
            try:
                if not self.client or not self.client.alive:
                    await self._shut()
                    await self._open()
                    self._connected("Reconnected")
                assert self.client
                pid = f"atrium-{uuid.uuid4().hex[:12]}"  # so that a turn Grok starts itself is told apart
                self._mine.add(pid)
                sent, noted = self._with_notes(prompt)
                try:
                    res = await self.client.request("session/prompt", {"sessionId": self.session_id, "prompt": sent,
                                                                       "_meta": {"promptId": pid}}, timeout=None)
                except AcpError:
                    if noted:  # the agent had the message: it failed on the model's side
                        self._told()
                    raise
                if noted:
                    self._told()
                reason = (res or {}).get("stopReason") or "end_turn"
                subtype = STOP_SUBTYPE.get(reason, reason)
                usage = ((res or {}).get("_meta") or {}).get("usage")
                turns = usage.get("numTurns") if isinstance(usage, dict) else None
            except Exception as e:
                subtype, reason, error = "error", "error", str(e) or type(e).__name__
                log.warning("Grok turn failed in %s: %s", self.session_id, error)
                if not self._errored:  # else the agent said it already
                    self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": error})
            self._flush()
            notes, self._followups = self._followups, []
            if reason != "cancelled" or not notes or self._interrupting or self._closed:
                break
            text = "\n\n".join(dict.fromkeys(notes))
            self.emit({"kind": "user", "id": f"u-{uuid.uuid4().hex[:12]}", "text": text, "ts": time.time()})
            prompt = self._prompt(text, [])
        self._finish(reason, subtype, error, turns=turns, duration_ms=int((time.time() - started) * 1000))
        if subtype == "error" and not (self.client and self.client.alive):
            self._set_state("error")  # the agent is gone: the next message starts a new one

    def _with_notes(self, prompt: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """The message with what Atrium has to tell the agent ahead of it (the chat shows the message alone), and
        whether that went with it: not with one of the agent's commands, which Grok runs only when the text starts
        with it — the notes wait for the next message then."""
        text = prompt[0].get("text") if prompt and prompt[0].get("type") == "text" else None
        if not self._notes or not isinstance(text, str) or SLASH_RE.fullmatch(text.strip()):
            return prompt, False
        note = "\n\n".join(self._notes)
        return [{**prompt[0], "text": f"<system-reminder>\n{note}\n</system-reminder>\n\n{text}"}, *prompt[1:]], True

    def _told(self) -> None:
        self._notes = []
        if self._note_file:
            self._note_file.unlink(missing_ok=True)
            self._note_file = None

    def _finish(self, reason: str, subtype: str, error: str | None, *, turns: Any = None, duration_ms: int | None = None) -> None:
        """How a turn ends in the chat: why it stopped, the context it fills, its result."""
        if reason == "cancelled":
            text = "Stopped: an action was turned down" if self._rejected and not self._interrupting else "Interrupted"
            self.emit({"kind": "system", "id": f"interrupted-{uuid.uuid4().hex[:8]}", "subtype": "interrupted", "text": text})
        if self._used:
            self.emit({"kind": "usage", "id": "usage", "used": self._used, "max": self._window()})
        self.emit({
            "kind": "result", "id": f"result-{uuid.uuid4().hex[:8]}", "subtype": subtype, "is_error": subtype == "error",
            "duration_ms": duration_ms, "cost_usd": None, "turns": turns, "text": error,
            "terminal_reason": "interrupted" if reason == "cancelled" else reason,
        })

    def _own_started(self, pid: str, text: Any) -> None:
        """Grok started a turn of its own accord: a command it moved to the background ended, a monitor saw
        something, /loop fired… It is a turn like any: running until its ``turn_completed``, with its result."""
        if pid in self._own_done or (self._own and self._own["id"] == pid):
            return
        if self._own:  # the one before never said it ended
            self._own_done.add(self._own["id"])
            self._finish("end_turn", "success", None, duration_ms=int((time.time() - self._own["started"]) * 1000))
        self._flush()
        if not (self._turn and not self._turn.done()):  # else it runs while one of Atrium's ends, or ahead of one Grok queued
            self._turns += 1
            self._interrupting = self._rejected = self._errored = self._command = False
        self._own = {"id": pid, "started": time.time()}
        if isinstance(text, str) and text.strip() and not pid.startswith(HIDDEN_PROMPTS):
            body, files = user_message_text(text)
            item: Item = {"kind": "user", "id": f"u-{pid}", "text": body, "ts": time.time()}
            if files:
                item["attachments"] = files
            self.emit(item)
        if self.state != "waiting":
            self._set_state("running")

    def _turn_completed(self, u: dict[str, Any]) -> None:
        """A turn ended (``turn_completed``; for one of Atrium's, ahead of ``session/prompt``'s answer, which ends it)."""
        self._flush()  # its last message is whole: shown before whatever comes next
        own = self._own
        if not own or u.get("prompt_id") != own["id"]:
            return
        self._own = None
        self._own_done.add(own["id"])
        reason = str(u.get("stop_reason") or "end_turn")
        subtype = STOP_SUBTYPE.get(reason, reason)
        error = str(u.get("agent_result") or reason) if subtype == "error" else None
        if error and not self._errored:
            self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": error})
        elapsed = u.get("elapsed_ms")
        self._finish(reason, subtype, error, duration_ms=elapsed if isinstance(elapsed, int) else int((time.time() - own["started"]) * 1000))
        notes, self._followups = self._followups, []
        if self._closed:
            return
        if reason == "cancelled" and notes and not self._interrupting:  # a call turned down with a word: it carries on from that
            text = "\n\n".join(dict.fromkeys(notes))
            self.emit({"kind": "user", "id": f"u-{uuid.uuid4().hex[:12]}", "text": text, "ts": time.time()})
            self._queue.insert(0, self._prompt(text, []))
        self._next()

    def _next(self) -> None:
        """Grok's own turn is over: the messages that waited for it go, else the agent is idle."""
        if self._turn and not self._turn.done():
            return  # one of Atrium's runs: it carries on with them
        if self._queue:
            self._turn = asyncio.create_task(self._turns_from(self._queue.pop(0)), name=f"grok-{self.session_id[:8]}")
        else:
            self._set_state("waiting" if self._pending else "idle")

    # ---- what the agent streams --------------------------------------------
    def _flush(self) -> None:
        """Emit the message or thinking being streamed, now whole (the chat shows each text item once)."""
        t, self._text = self._text, None
        if t and t["text"].strip():
            kind = "command_output" if self._command and t["kind"] == "text" else t["kind"]
            self.emit({"kind": kind, "id": t["id"], "text": t["text"]})

    def _stream(self, kind: str, text: str, eid: str | None, prompt: Any = None) -> None:
        if self._text and (self._text["kind"] != kind or self._text["prompt"] != prompt):
            self._flush()  # another kind, or another turn's (Grok's own after Atrium's): another message
        if self._text is None:
            self._text = {"kind": kind, "id": f"{kind[0]}-{eid or uuid.uuid4().hex[:12]}", "text": "", "prompt": prompt}
        self._text["text"] += text
        if kind == "text" and not self._command:
            self.emit({"kind": "delta", "text": text})

    def _on_notify(self, method: str, params: dict[str, Any]) -> None:
        if self._closed:
            return
        if method == "atrium/disconnected":
            if params.get("client") is not self.client:
                return  # one Atrium stopped itself (closing, or starting afresh)
            if self.state not in ("closed", "error") and not (self._turn and not self._turn.done()):  # else the turn says it failed
                tail = self.client.stderr_tail[-1] if self.client and self.client.stderr_tail else ""
                self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": "Grok Build exited." + (f" {tail}" if tail else "")})
                own, self._own = self._own, None
                if own:  # in a turn of its own
                    self._flush()
                    self._own_done.add(own["id"])
                    self._finish("error", "error", "Grok Build exited", duration_ms=int((time.time() - own["started"]) * 1000))
                self._set_state("error")
                if own and self._queue:  # what waited for that turn starts the agent again
                    self._next()
            return
        if self._loading or params.get("sessionId") not in (None, self.session_id):
            return  # the conversation replayed, or a subagent's own session
        if method.endswith("queue/changed"):  # what Grok runs now: a prompt of Atrium's, or one of its own
            running = params.get("runningPromptId")
            if isinstance(running, str) and running and running not in self._mine:
                self._own_started(running, params.get("runningText"))
            return
        u = params.get("update")
        if not isinstance(u, dict):
            return
        meta = x if isinstance(x := params.get("_meta"), dict) else {}
        if isinstance(meta.get("totalTokens"), int) and meta["totalTokens"] > 0:
            self._used = meta["totalTokens"]  # what the context holds after the agent's last call
        k = u.get("sessionUpdate")
        pid = meta.get("promptId")
        if k in TURN_UPDATES and isinstance(pid, str) and pid and pid not in self._mine:
            self._own_started(pid, None)  # (the queue tells first, when the CLI does)
        if k == "turn_completed":
            self._turn_completed(u)
        elif method == "session/update":
            self._on_update(k, u, meta)
        elif k == "retry_state":
            if u.get("type") == "retrying":
                self.emit(retry_item(f"retry-{uuid.uuid4().hex[:8]}", u))
            else:
                self._errored = True
                self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": str(u.get("message") or u.get("reason") or "Model error")})
        elif k in ("auto_compact_completed", "compaction_checkpoint"):
            self._flush()
            cid = f"compact-{meta.get('eventId') or uuid.uuid4().hex[:8]}"
            self.emit({"kind": "system", "id": cid, "subtype": "compact", "text": "Conversation compacted"})
        elif k == "auto_compact_failed":
            self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": f"Compaction failed: {u.get('error') or ''}"})
        elif k == "response_completed":
            usage = u.get("usage") or {}
            used = sum(int(usage.get(x) or 0) for x in ("input_tokens", "cache_read_input_tokens", "output_tokens"))
            self._used = used or self._used

    def _on_update(self, k: Any, u: dict[str, Any], meta: dict[str, Any]) -> None:
        if k == "agent_message_chunk":
            self._stream("text", ((u.get("content") or {}).get("text") or ""), meta.get("eventId"), meta.get("promptId"))
        elif k == "agent_thought_chunk":
            self._stream("thinking", ((u.get("content") or {}).get("text") or ""), meta.get("eventId"), meta.get("promptId"))
        elif k in ("tool_call", "tool_call_update"):
            tid = u.get("toolCallId")
            if not tid:
                return
            self._flush()
            name, args = self._tools.get(tid, (None, {}))
            name = name or _native(u) or (u.get("title") if k == "tool_call" else None) or "tool"
            if isinstance(u.get("rawInput"), dict):
                args = {**args, **_plain(u["rawInput"])}
            self._tools[tid] = (name, args)
            view = tool_view(name, args, self.spec.cwd)
            if view is None:
                return
            shown = json.dumps(view, sort_keys=True, default=str)
            if self._shown.get(tid) != shown:
                self._shown[tid] = shown
                self.emit({"kind": "tool", "id": tid, "name": view[0], "input": view[1]})
            if u.get("status") in ("completed", "failed"):
                content, err = result_view(name, u)
                self.emit({"kind": "tool_result", "id": tid, "content": content, "is_error": err})
        elif k == "plan":
            self._flush()
            for it in plan_item(f"plan-{self.session_id[:8]}-{self._turns}", u.get("entries")):
                self.emit(it)
        elif k == "available_commands_update":
            self._commands = [c for c in u.get("availableCommands") or [] if isinstance(c, dict)]
        elif k == "config_option_update":
            for opt in u.get("configOptions") or []:
                if isinstance(opt, dict) and opt.get("id") == "model" and opt.get("currentValue"):
                    self._current_model = opt["currentValue"]
        elif k == "usage_update" and isinstance(u.get("used"), int):
            self._used = u["used"]

    # ---- what the agent asks -----------------------------------------------
    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method in ("_x.ai/mcp/sdk_call", "x.ai/mcp/sdk_call"):
            return await self._mcp(str(params.get("serverId") or ""), params.get("message") or {})
        if method == "session/request_permission":
            return await self._permission(params)
        if method in ("_x.ai/ask_user_question", "x.ai/ask_user_question"):
            return await self._question(params)
        raise NotImplementedError(f"Atrium does not handle {method}")

    async def _mcp(self, server_id: str, msg: dict[str, Any]) -> dict[str, Any]:
        """One MCP message to Atrium's tools of a namespace (``atrium-<namespace>``), answered as its server would."""
        ns = server_id.removeprefix("atrium-")
        specs = self._toolsets.get(ns)
        rid, method, p = msg.get("id"), msg.get("method"), msg.get("params") or {}

        def ok(result: dict[str, Any]) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        if specs is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown server {server_id}"}}
        if method == "initialize":
            return ok({"protocolVersion": p.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                       "serverInfo": {"name": ns, "version": CLIENT_INFO["version"]}})
        if method == "tools/list":
            return ok({"tools": [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in specs.values()]})
        if method == "tools/call":
            spec = specs.get(str(p.get("name") or ""))
            if spec is None:
                return ok({"content": [{"type": "text", "text": f"Unknown tool {ns}.{p.get('name')}"}], "isError": True})
            try:
                out = await spec.handler(p.get("arguments") or {})
            except Exception as e:  # reported to the model instead of failing the turn
                out = {"text": f"Error: {e}", "is_error": True}
            text, err = (str(out.get("text", "")), bool(out.get("is_error"))) if isinstance(out, dict) else (str(out), False)
            return ok({"content": [{"type": "text", "text": text}], "isError": err})
        if method == "ping":
            return ok({})
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}

    def _atrium_tool(self, native: str | None, raw: dict[str, Any]) -> bool:
        if native != "use_tool" and raw.get("variant") != "UseTool":
            return False
        server, tool = mcp_name(str(raw.get("tool_name") or ""))
        return tool in self._toolsets.get(server, {})

    async def _permission(self, params: dict[str, Any]) -> dict[str, Any]:
        tc = params.get("toolCall") or {}
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]
        raw = x if isinstance(x := tc.get("rawInput"), dict) else {}
        tid = tc.get("toolCallId") or ""
        native = _native(tc) or self._tools.get(tid, (None, {}))[0]
        view = tool_view(native or "", {**self._tools.get(tid, (None, {}))[1], **_plain(raw)}, self.spec.cwd) or (tc.get("title") or "tool", _plain(raw))

        def pick(*kinds: str) -> dict[str, Any]:
            for kind in kinds:
                o = next((o for o in options if o.get("kind") == kind), None)
                if o:
                    return {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}
            return {"outcome": {"outcome": "cancelled"}}

        command = str(raw.get("command") or "") if view[0] == "Bash" else None
        if self._atrium_tool(native, raw):
            return pick("allow_once", "allow_always")  # Atrium's own tools run unasked, and leave no rule behind
        if command is not None and _command_allowed(command, self.spec.allowed_tools):
            return pick("allow_once", "allow_always")
        if self.mode == "read-only":
            self._rejected = True
            self._followups.append(READ_ONLY_NOTE)
            return pick("reject_once", "reject_always")
        # the rest is the user's to decide, in always-approve too: Grok asks there only for what an `ask` rule or a
        # hook keeps for a person
        rid = tid or f"perm-{uuid.uuid4().hex[:10]}"
        while rid in self._pending:
            rid += "+"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        path = view[1].get("file_path") if isinstance(view[1], dict) else None
        self.emit({"kind": "permission", "id": rid, "tool": view[0], "input": view[1], "title": tc.get("title"),
                   "can_remember": any(o.get("kind") == "allow_always" for o in options),
                   "blocked_path": path if path and not _inside(path, os.path.realpath(self.spec.cwd)) else None})
        self._set_state("waiting")
        try:
            decision = await fut
        finally:
            self._pending.pop(rid, None)
        behavior = decision.get("behavior", "deny")
        self.emit({"kind": "permission_resolved", "id": rid, "decision": behavior})
        if self.state == "waiting" and not self._pending:
            self._set_state("running" if self._busy() else "idle")
        if decision.get("cancelled"):
            return {"outcome": {"outcome": "cancelled"}}
        if behavior == "allow":
            return pick("allow_always", "allow_once") if decision.get("remember") else pick("allow_once", "allow_always")
        self._rejected = True
        message = str(decision.get("message") or "").strip()
        if decision.get("interrupt"):  # turning it down ends Grok's turn already: Deny & stop has it stop there
            self._interrupting = True
            for other in list(self._pending.values()):
                if not other.done():  # the turn's other questions go with it
                    other.set_result({"behavior": "deny", "cancelled": True})
            if message:  # and why reaches the agent with the next message
                self._notes.append(f"The user stopped you, turning down {view[0]}: {message}")
        elif message:  # the turn ends here: what to do instead is the user's next message
            self._followups.append(message)
        return pick("reject_once", "reject_always")

    async def _question(self, params: dict[str, Any]) -> dict[str, Any]:
        """``ask_user_question``: the agent's questions, answered in the chat."""
        qs = [q for q in params.get("questions") or [] if isinstance(q, dict)]
        ask = [{"question": q.get("question") or "", "header": q.get("header"), "multiSelect": bool(q.get("multi_select")),
                "options": [{"label": o.get("label") or "", "description": o.get("description") or ""} for o in q.get("options") or [] if isinstance(o, dict)]}
               for q in qs]
        rid = str(params.get("toolCallId") or f"ask-{uuid.uuid4().hex[:10]}")
        while rid in self._pending:
            rid += "+"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.emit({"kind": "permission", "id": rid, "tool": "AskUserQuestion", "input": {"questions": ask}, "can_remember": False})
        self._set_state("waiting")
        try:
            decision = await fut
        finally:
            self._pending.pop(rid, None)
        self.emit({"kind": "permission_resolved", "id": rid, "decision": decision.get("behavior", "deny")})
        if self.state == "waiting" and not self._pending:
            self._set_state("running" if self._busy() else "idle")
        if decision.get("interrupt") or decision.get("cancelled"):
            return {"outcome": "cancelled"}
        answers = decision.get("answers") or {}
        if decision.get("behavior") != "allow" or not answers:
            return {"outcome": "skip_interview", "partial_answers": {}}
        return {"outcome": "accepted", "answers": {q: [a.strip() for a in str(v).split(",") if a.strip()] for q, v in answers.items() if v}}

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit({"kind": "state", "state": state})


def _slash_commands(raw: list[dict[str, Any]]) -> list[SlashCommand]:
    out = []
    for c in raw:
        name = str(c.get("name") or "")
        if not name or name in HIDDEN_COMMANDS:
            continue
        hint = (c.get("input") or {}).get("hint") if isinstance(c.get("input"), dict) else None
        out.append(SlashCommand(name, str(c.get("description") or ""), argument_hint=str(hint or "")))
    return out


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class GrokHarness(Harness):
    id = "grok"
    label = "Grok Build"
    permission_modes = PERMISSION_MODES
    git_agent_mode = "ask"  # reads freely; the Git agent's read-only git commands are allowed (allowed_tools)
    review_agent_mode = "read-only"  # not offered in the picker: reads and the pre-approved commands, refuses the rest unasked
    models = [{"id": "", "label": "Default"}]
    efforts = EFFORTS
    can_fork = False
    can_edit = False

    def __init__(self) -> None:
        self._catalog: tuple[tuple[str, float], dict[str, Any]] | None = None  # (binary, config.toml mtime) -> initialize's _meta
        self._catalog_lock = asyncio.Lock()
        self._prompts: dict[str, str] = {}  # updates.jsonl -> its first message
        self._summaries: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}  # summary.json -> (signature, content)
        self._cwds: dict[str, str | None] = {}  # session dir -> its cwd
        self._versions: dict[tuple[str, int, int], str | None] = {}  # (path, mtime, size) -> `grok --version`
        self.live: dict[str, GrokLiveSession] = {}

    def available(self) -> tuple[bool, str | None]:
        d = _defaults()
        if not d.get("enabled", True):
            return False, "Disabled in Atrium settings (harnesses.grok.enabled)."
        if not resolve_binary():
            return False, not_found(d.get("binary") or "")
        return True, None

    # ---- settings ----------------------------------------------------------
    def config_files(self, project_dir: str | None) -> list[Any]:
        from ..configfiles import ConfigFile

        files = [ConfigFile(
            id="grok-user", label="Grok Build", path=str(grok_home() / "config.toml"), language="toml", scope="user", harness=self.id,
            hint="Models ([models] default, [model.<id>] with base_url, api_key or env_key, api_backend), MCP servers, permission "
                 "rules… Applies to Grok sessions started afterwards.",
            secret=True,
        )]
        if project_dir:
            files.append(ConfigFile(
                id="grok-project", label="Grok Build · project", path=os.path.join(project_dir, ".grok", "config.toml"), language="toml",
                scope="project", harness=self.id, hint="Project MCP servers, plugins and permission rules.",
            ))
        return files

    def on_config_saved(self, file_id: str) -> None:
        self._catalog = None
        _toml_cache.clear()

    async def _probe(self) -> dict[str, Any]:
        """What a new agent process says it offers (``initialize``'s _meta: models, commands), re-asked when the
        CLI or config.toml change."""
        binary = resolve_binary()
        if not binary:
            return {}
        try:
            mtime = (grok_home() / "config.toml").stat().st_mtime
        except OSError:
            mtime = 0.0
        key = (binary, mtime)
        async with self._catalog_lock:
            if self._catalog and self._catalog[0] == key:
                return self._catalog[1]

            async def ignore(method: str, params: dict[str, Any]) -> Any:
                raise NotImplementedError(method)

            cwd = str(RUNTIME_DIR)
            os.makedirs(cwd, exist_ok=True)
            client = AcpClient([binary, "--no-auto-update", "agent", "stdio"], cwd=cwd, env=agent_env(), on_notify=lambda m, p: None,
                               on_request=ignore, name="grok catalog")
            await client.start()
            try:
                init = await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": CLIENT_INFO}, timeout=90)
            finally:
                await client.close()
            meta = (init or {}).get("_meta") or {}
            self._catalog = (key, meta)
            return meta

    def config_model(self) -> str | None:
        """The model a session gets when Atrium doesn't pick: ``[models] default``, else the catalog's current one."""
        models = user_config().get("models")
        default = models.get("default") if isinstance(models, dict) else None
        if default:
            return str(default)
        state = (self._catalog[1] if self._catalog else {}).get("modelState") or {}
        return state.get("currentModelId")

    async def catalog(self) -> dict[str, Any]:
        d = self.describe()
        if d["available"]:
            try:
                state = (await self._probe()).get("modelState") or {}
                models = []
                for m in state.get("availableModels") or []:
                    meta = m.get("_meta") or {}
                    efforts = [e.get("value") or e.get("id") for e in meta.get("reasoningEfforts") or [] if isinstance(e, dict)] or list(EFFORTS)
                    efforts.sort(key=lambda e: EFFORT_ORDER.index(e) if e in EFFORT_ORDER else len(EFFORT_ORDER))
                    models.append({"id": m.get("modelId"), "label": m.get("name") or m.get("modelId"), "description": m.get("description"),
                                   "efforts": efforts if meta.get("supportsReasoningEffort") else [], "default_effort": meta.get("reasoningEffort")})
                current = self.config_model() or state.get("currentModelId")
                entry = next((m for m in models if m["id"] == current), {})
                d["models"] = [{"id": "", "label": f"Default ({entry.get('label') or current})" if current else "Default", "resolves_to": current,
                                "efforts": entry.get("efforts") or [], "default_effort": entry.get("default_effort")}] + models
                d["config_model"], d["config_effort"] = current, entry.get("default_effort")
            except Exception as e:
                d["catalog_error"] = str(e)
        d["cli"] = await self._cli()
        return d

    async def _cli(self) -> dict[str, Any]:
        """Which Grok CLI runs the sessions, its version, and whether it is the bundled one."""
        path = resolve_binary()
        version = None
        if path:
            try:
                st = os.stat(path)
                key = (path, st.st_mtime_ns, st.st_size)
                if key not in self._versions:
                    self._versions[key] = None  # asked once per binary, even when that fails
                    out = await asyncio.to_thread(subprocess.run, [path, "--version"], capture_output=True, text=True, errors="replace",
                                                  timeout=30, env=agent_env())
                    words = out.stdout.split()  # "grok 0.2.111 (94172f2aa4e5)"
                    self._versions[key] = words[1] if len(words) > 1 else None
                version = self._versions[key]
            except (OSError, subprocess.SubprocessError):
                pass
        bundled = bundled_grok()
        return {"path": path, "version": version, "bundled": bool(path and bundled and path == bundled)}

    async def slash_commands(self, cwd: str, live: LiveSession | None = None) -> list[SlashCommand]:
        if live is not None and live.commands() is not None:
            return live.commands() or []
        if not self.available()[0]:
            return []
        try:
            return _slash_commands([c for c in (await self._probe()).get("availableCommands") or [] if isinstance(c, dict)])
        except Exception as e:
            log.debug("grok commands: %s", e)
            return []

    # ---- discovery ---------------------------------------------------------
    async def list_sessions(self, project_dir: str, roots: list[str]) -> list[SessionSummary]:
        return await asyncio.to_thread(self._list, roots)

    def _summary(self, path: Path) -> dict[str, Any]:
        try:
            st = path.stat()
        except OSError:
            return {}
        sig = (st.st_mtime_ns, st.st_size)
        hit = self._summaries.get(str(path))
        if hit and hit[0] == sig:
            return hit[1]
        data = _read_json(path)
        self._summaries[str(path)] = (sig, data)
        return data

    def _list(self, roots: list[str]) -> list[SessionSummary]:
        root = sessions_root()
        if not root.is_dir():
            return []
        real = [os.path.realpath(r) for r in roots]
        out: list[SessionSummary] = []
        for cdir in root.iterdir():
            cwd = _cwd_of_dir(cdir) if cdir.is_dir() else None
            if not cwd or not any(_inside(cwd, r) for r in real):
                continue
            for sdir in cdir.iterdir():
                updates = sdir / "updates.jsonl"
                try:
                    st = updates.stat()
                except OSError:
                    continue  # not a session, or no message yet
                s = self._summary(sdir / "summary.json")
                if s.get("parent_session_id") or s.get("session_kind") in ("subagent", "task"):
                    continue  # a subagent's own session: its parent's tool call stands for it
                prompt = self._first_prompt(updates)
                if not prompt and not st.st_size:
                    continue
                title = s.get("generated_title") or s.get("session_summary") or short(prompt, 80) or "Untitled"
                out.append(SessionSummary(
                    id=sdir.name, harness=self.id, title=str(title), updated_at=_iso_epoch(s.get("last_active_at") or s.get("updated_at")) or st.st_mtime,
                    created_at=_iso_epoch(s.get("created_at")), first_prompt=prompt, cwd=(s.get("info") or {}).get("cwd") or cwd,
                    git_branch=s.get("head_branch"), size=st.st_size, transcript=str(updates),
                ))
        return out

    def _first_prompt(self, path: Path) -> str:
        """The session's first message, read up to that line; once found it never changes, so it is kept."""
        hit = self._prompts.get(str(path))
        if hit:
            return hit
        prompt = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"user_message_chunk"' not in line:
                        continue
                    try:
                        u = _update(json.loads(line))
                    except (ValueError, KeyError, TypeError):
                        continue
                    text = ((u.get("content") or {}).get("text") or "") if isinstance(u.get("content"), dict) else ""
                    if text.strip() and not _hidden(u):
                        prompt = user_message_text(text)[0]
                        break
        except OSError:
            return ""
        if prompt:
            self._prompts[str(path)] = prompt
        return prompt

    def transcript_path(self, session_id: str, cwd: str) -> Path | None:
        d = session_dir(session_id, cwd)
        return d / "updates.jsonl" if d and (d / "updates.jsonl").exists() else None

    def watch_roots(self) -> list[Path]:
        root = sessions_root()
        return [Path(os.path.realpath(root))] if root.is_dir() else []

    def transcript_cwd(self, path: str) -> str | None:
        if not path.endswith("updates.jsonl"):
            return None
        sdir = Path(path).parent
        if str(sdir) not in self._cwds:
            s = _read_json(sdir / "summary.json")
            self._cwds[str(sdir)] = (s.get("info") or {}).get("cwd") or _cwd_of_dir(sdir.parent)
        return self._cwds[str(sdir)]

    async def rename_session(self, session_id: str, title: str, cwd: str) -> None:
        live = self.live.get(session_id)
        if live and live.client and live.client.alive:  # the agent keeps the title it holds over what is on disk
            try:
                await live.client.request("_x.ai/session/rename", {"sessionId": session_id, "title": title, "cwd": cwd}, timeout=30)
                return
            except Exception as e:
                log.debug("grok rename %s: %s", session_id, e)

        def rename() -> None:  # the title Grok's own session list shows
            d = session_dir(session_id, cwd)
            s = _read_json(d / "summary.json") if d else {}
            if d and s and s.get("generated_title") != title:
                atomic_write(d / "summary.json", json.dumps({**s, "generated_title": title}, indent=2, ensure_ascii=False))

        try:
            await asyncio.to_thread(rename)
        except OSError as e:
            log.debug("grok rename %s: %s", session_id, e)

    async def delete_session(self, session_id: str, cwd: str) -> None:
        binary = resolve_binary()
        if binary:
            try:  # the CLI also takes it out of its search index
                await asyncio.to_thread(subprocess.run, [binary, "--no-auto-update", "sessions", "delete", session_id], capture_output=True,
                                        timeout=60, env=agent_env(), cwd=cwd if os.path.isdir(cwd) else None)
            except (OSError, subprocess.SubprocessError) as e:
                log.debug("grok sessions delete %s: %s", session_id, e)
        d = session_dir(session_id, cwd)
        if d:
            await asyncio.to_thread(shutil.rmtree, d, True)

    async def move_session(self, session_id: str, cwd: str, target_cwd: str, note: str = "") -> None:
        # Grok opens a session by its id wherever it is filed; it lists it by the directory it is filed under, and
        # works where its summary says. The system prompt stays the one it started with: ``note`` goes with the next
        # message (GrokLiveSession.start).
        target = os.path.realpath(target_cwd)

        def move() -> None:
            src = session_dir(session_id, cwd)
            if not src:
                raise FileNotFoundError(f"Grok Build session {session_id}")
            bucket = _bucket(target)
            dest = bucket / session_id
            if src != dest:
                if dest.exists():
                    raise FileExistsError(f"{dest} exists already")
                bucket.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.rename(src, dest)
                try:
                    src.parent.rmdir()  # the old directory's last session
                except OSError:
                    pass
            s = _read_json(dest / "summary.json")
            if s:
                info = x if isinstance(x := s.get("info"), dict) else {}
                atomic_write(dest / "summary.json", json.dumps({**s, "info": {**info, "cwd": target}}, indent=2, ensure_ascii=False))
            path = _note_path(session_id)
            if note:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, note)
            else:
                path.unlink(missing_ok=True)

        await asyncio.to_thread(move)

    # ---- content -----------------------------------------------------------
    async def load_history(self, session_id: str, cwd: str) -> list[Item]:
        path = self.transcript_path(session_id, cwd)
        if not path:
            return []
        recorded = (_read_json(path.parent / "summary.json").get("info") or {}).get("cwd") or cwd
        return await asyncio.to_thread(lambda: history_items(_entries(path), recorded))

    def digest_lines(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        tools: dict[str, str] = {}
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("params"), dict):
                out += digest_update(d, tools)
        return out

    def file_edits(self, session_id: str, cwd: str) -> list[FileEdit]:
        d = session_dir(session_id, cwd)
        if not d:
            return []
        recorded = (_read_json(d / "summary.json").get("info") or {}).get("cwd") or cwd
        edits, covered = _snapshot_edits(d / "rewind_points.jsonl", recorded)
        return edits + update_edits(_entries(d / "updates.jsonl"), covered)

    # ---- execution ---------------------------------------------------------
    def create_live(self, spec: LiveSpec, emit: Emit) -> LiveSession:
        return GrokLiveSession(self, spec, emit)

    async def complete_json(self, *, system, prompt, schema, model, effort, cwd):
        binary = resolve_binary()
        if not binary:
            raise RuntimeError(not_found(_defaults().get("binary") or ""))
        started = time.time()
        os.makedirs(cwd, exist_ok=True)
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        sid = str(uuid.uuid4())  # the one-shot's session, named up front: it goes however the call ends
        # the prompt in a file of the user's alone, not on the command line: others can read that, and it holds
        # only so much (the Tracker's runs to 60 000 characters, 128 KiB on Linux)
        fd, prompt_file = tempfile.mkstemp(prefix="grok-prompt-", suffix=".txt", dir=RUNTIME_DIR)
        proc: asyncio.subprocess.Process | None = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(prompt)
            argv = [binary, "--no-auto-update", "--prompt-file", prompt_file, "--session-id", sid, "--output-format", "json",
                    "--json-schema", json.dumps(schema), "--system-prompt-override", system, "--max-turns", "4"]
            if model:
                argv += ["-m", model]
            if effort:
                argv += ["--reasoning-effort", effort]
            proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=agent_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                raise RuntimeError("Grok Build took too long to answer") from None
        finally:
            if proc and proc.returncode is None:  # it took too long, or the caller gave up first (the Tracker's own timeout)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            os.unlink(prompt_file)
            _background(self.delete_session(sid, cwd))  # a one-shot: nothing to keep in Grok's session list
        try:
            res = json.loads(out.decode(errors="replace") or "{}")
        except ValueError:
            res = {}
        data = res.get("structuredOutput")
        if not isinstance(data, dict):
            text = res.get("text") or ""
            if not text:
                raise RuntimeError((err.decode(errors="replace").strip().splitlines() or ["no answer from Grok Build"])[-1])
            data = parse_json_loose(text)
        usage = res.get("usage") or {}
        return data, {"cost_usd": None, "duration_ms": int((time.time() - started) * 1000), "turns": res.get("num_turns") or 1,
                      "tokens": usage.get("total_tokens")}
