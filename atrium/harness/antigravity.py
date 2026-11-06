"""Google Antigravity harness, built on the Antigravity SDK (``google-antigravity`` on PyPI).

The SDK's platform wheels bundle Antigravity's agent runtime (``localharness``, a Go binary), so
installing Atrium installs the harness. Every live session runs its own runtime process, which the
SDK drives over a local WebSocket. Model calls go to the Gemini API with a Gemini API key — Google's,
or any endpoint that speaks it, such as a relay — or to Gemini Enterprise (Vertex AI); never through
a Google account sign-in: Antigravity's terms don't allow third-party apps to use the consumer login
of the Antigravity app and CLI. Where they go is read from the Antigravity CLI's own settings file,
``~/.gemini/antigravity-cli/settings.json`` (see "Antigravity's own settings" below).

Conversations live in Atrium's own store, ``$ATRIUM_HOME/antigravity``:

    save/<id>.db         the runtime's state of a conversation (what it resumes from)
    data/brain/<id>/     the runtime's data directory, with its JSONL transcript
                         (``.system_generated/logs/transcript_full.jsonl``: history, diffs, tracker)
    sessions/<id>.json   Atrium's index of the conversation: working directory, title, start

Atrium's tools are Python tools of the SDK named ``<namespace>_<tool>`` (``board_status``…).
Approvals go through a pre-tool hook that asks in the chat, as the session's permission mode says.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import gitops
from ..config import ATRIUM_HOME, atomic_write, config
from .base import (
    Attachment, Compaction, Emit, FileEdit, ForkPoint, Harness, Item, LiveSession, LiveSpec, Replacement, Rewind, SessionSummary, ToolSpec,
)
from .util import TailScan, attachment_note, clip, hhmm, iso_to_epoch, parse_json_loose, short, short_tail, split_attachment_note, tail_scan, upload_url

log = logging.getLogger("atrium.antigravity")


class _QuietSdk(logging.Filter):
    """The SDK logs to the root logger: keep its INFO chatter (the runtime's stderr…) out of Atrium's log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or f"{os.sep}google{os.sep}antigravity{os.sep}" not in record.pathname


logging.getLogger().addFilter(_QuietSdk())

# After the Antigravity CLI's own presets (toolPermission / --mode accept-edits)
PERMISSION_MODES = [
    {"id": "request-review", "label": "Request review"},
    {"id": "accept-edits", "label": "Accept edits"},
    {"id": "always-proceed", "label": "Always proceed"},
]
EFFORTS = ["minimal", "low", "medium", "high"]  # Gemini thinking levels
DEFAULT_MODEL = "gemini-3.8-flash"  # the SDK's default (google.antigravity.models.DEFAULT_MODEL)
IMAGE_MODEL = "gemini-3.1-flash-lite-image"  # the SDK's image model (generate_image)
GEMINI_API = "https://generativelanguage.googleapis.com"  # the Gemini API's own address (an endpoint with no base URL)
CONTEXT_WINDOW = 1_048_576  # Gemini 3.x models
# Tools that only read: never asked about. Names as the pre-tool hook sees them (the SDK maps
# write_to_file to create_file and replace_file_content to edit_file).
READ_TOOLS = {
    "view_file", "list_directory", "search_directory", "find_file", "finish", "list_skills", "lookup_skill", "lookup_skills",
    "get_skill_resources", "skill_lookup", "invoke_subagent", "define_subagent", "manage_subagents", "send_message", "manage_task",
}
EDIT_TOOLS = {"write_to_file", "create_file", "replace_file_content", "multi_replace_file_content", "edit_file"}
META_ARGS = ("toolAction", "toolSummary")  # the runtime adds them to every tool's arguments
ATRIUM_NAMESPACES = ("board", "web", "graph", "review")  # Atrium's tools are <namespace>_<tool> here
TOOL_NAMES_NOTE = "Atrium's tools are named `<namespace>_<tool>` in this session: {examples}."
RULE_FILES = ("AGENTS.md", "GEMINI.md", ".agents/AGENTS.md", ".agents/GEMINI.md")
MAX_RULE_BYTES = 24_000  # Antigravity's per-file limit for rules
_STAMP_RE = re.compile(r"^(?:Created|Completed) At: .*\n?", re.M)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
_CMD_RULE_RE = re.compile(r"Bash\((.+?)(?::\*)?\)$")  # Claude-style allow rules ("Bash(git log:*)")
_API_ERROR_RE = re.compile(r"Error (\d{3}), Message: ([^\[\]\"(]+)")  # "Error 503, Message: Upstream service unavailable [trace…"
_HEAD_RE = re.compile(rb'\{"step_index":(\d+),"source":"(\w*)","type":"(\w*)"')  # how the runtime starts a transcript line
COMPACTED = "CHECKPOINT"  # a compaction's step: its content is the summary the model goes on from


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def ag_home() -> Path:
    return ATRIUM_HOME / "antigravity"


def _save_dir() -> Path:
    return ag_home() / "save"


def _data_dir() -> Path:
    return ag_home() / "data"


def _brain_dir() -> Path:
    return _data_dir() / "brain"


def _index_path(sid: str) -> Path:
    return ag_home() / "sessions" / f"{sid}.json"


def _logs(sid: str) -> Path:
    return _brain_dir() / sid / ".system_generated" / "logs"


def transcript_file(sid: str) -> Path | None:
    """The conversation's transcript: the full one (tool outputs as the model saw them), else the abridged one."""
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        p = _logs(sid) / name
        if p.exists():
            return p
    return None


def read_index(sid: str) -> dict[str, Any]:
    try:
        d = json.loads(_index_path(sid).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            return n, i


def _put_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        out.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)


def _file_uri(path: str) -> str:
    return "file://" + urllib.parse.quote(path, safe="/:@!$&'()*+,;=-._~")


def _within(path: str, root: str) -> str | None:
    """``path`` (resolved) if it is ``root`` (resolved) or inside it, else None."""
    real = os.path.realpath(path)
    return real if real == root or real.startswith(root.rstrip(os.sep) + os.sep) else None


def rewrite_workspace(sid: str, old: str, new: str) -> bool:
    """Point a stopped conversation's runtime state at another directory. The runtime records the
    workspaces in its state database (``trajectory_metadata_blob``: a protobuf with each workspace's
    file:// URI in field 7) and a resumed runtime keeps them, whatever it is given. Returns whether
    one was rewritten: False if there is no state yet (no database, no record) or it is already in
    ``new``. Raises ValueError, and changes nothing, if the records name no workspace in ``old`` (the
    runtime would go on working there) or have a format Atrium doesn't know."""
    import sqlite3

    db = _save_dir() / f"{sid}.db"
    if not db.exists():
        return False
    old, new = os.path.realpath(old), os.path.realpath(new)
    rows: list[Any] = []
    seen: list[str] = []  # the workspaces recorded
    changed = False
    con = sqlite3.connect(db)
    try:
        with con:
            rows = con.execute("SELECT id, data FROM trajectory_metadata_blob").fetchall()
            for rid, data in rows:
                out, i, data, hit = bytearray(), 0, bytes(data or b""), False
                while i < len(data):
                    key, j = _varint(data, i)
                    if key & 7 == 0:
                        _, j = _varint(data, j)
                    elif key & 7 == 2:
                        n, k = _varint(data, j)
                        value, j = data[k:k + n], k + n
                        if key >> 3 == 7 and value.startswith(b"file://"):
                            path = urllib.parse.unquote(value[len(b"file://"):].decode())
                            seen.append(path)
                            real = _within(path, old)
                            if real:
                                uri = _file_uri(new + real[len(old):]).encode()
                                out += _put_varint(key) + _put_varint(len(uri)) + uri
                                hit = True
                                i = j
                                continue
                    elif key & 7 == 1:
                        j += 8
                    elif key & 7 == 5:
                        j += 4
                    else:
                        raise ValueError(f"unexpected wire type {key & 7}")
                    if j > len(data):
                        raise ValueError("truncated record")
                    out += data[i:j]
                    i = j
                if hit:
                    con.execute("UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?", (bytes(out), rid))
                    changed = True
    except (ValueError, IndexError, UnicodeDecodeError, sqlite3.Error) as e:
        raise ValueError(f"Antigravity's state of {sid} has a format Atrium doesn't know ({e}): not moved") from e
    finally:
        con.close()
    if rows and not changed and not (seen and all(_within(p, new) for p in seen)):
        raise ValueError(f"Antigravity's state of {sid} names no workspace in {old} (it has: {', '.join(seen) or 'none'}): not moved")
    return changed


def write_index(sid: str, **fields: Any) -> None:
    atomic_write(_index_path(sid), json.dumps({**read_index(sid), **fields, "id": sid}, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# SDK
# --------------------------------------------------------------------------

def _runtime() -> str:
    """The runtime binary the SDK starts: its wheel's, unless ANTIGRAVITY_HARNESS_PATH says otherwise."""
    import importlib.resources

    name = "bin/localharness.exe" if os.name == "nt" else "bin/localharness"
    return os.environ.get("ANTIGRAVITY_HARNESS_PATH") or str(importlib.resources.files("google.antigravity").joinpath(name))


@functools.cache
def _sdk_problem() -> str | None:
    """Why the SDK can't be used here (not installed, no runtime for this platform), or None."""
    try:
        import google.antigravity  # noqa: F401

        return None if os.path.exists(_runtime()) else f"its runtime is missing ({_runtime()})"
    except Exception as e:  # not installed, or broken
        return f"{type(e).__name__}: {e}"


@functools.cache
def _warm_runtime() -> None:
    """Run the runtime once, off the event loop: its first start can be slow (macOS checks a new
    binary), and the SDK starts it synchronously."""
    try:  # with no config on stdin it exits right away
        subprocess.run([_runtime()], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("warming the Antigravity runtime failed: %s", e)


def _defaults() -> dict[str, Any]:
    """Atrium's defaults for new Antigravity agents (``harnesses.antigravity``): enabled, mode, model, effort."""
    return config.harness_defaults("antigravity")


# --------------------------------------------------------------------------
# Antigravity's own settings: the Antigravity CLI's file, ~/.gemini/antigravity-cli/settings.json
# --------------------------------------------------------------------------
# Atrium reads what decides where model calls go, in the CLI's own keys: ``model`` (a custom model's
# key or label, or a model id), ``customModelsConfig.customModels`` (``modelName``:
# ``gemini-api://<base_url>/models/<model>``, e.g. a relay), and ``env``: the environment variables the
# CLI reads (GEMINI_API_KEY, GOOGLE_GEMINI_BASE_URL, Vertex AI's…), kept in the file for Atrium; the
# environment fills in what it doesn't set, except a relay's key (see gemini_endpoint).

_cli_cache: dict[str, tuple[tuple[int, int], dict[str, Any], str | None]] = {}
_bad_max_tokens: set[tuple[str, str]] = set()  # (custom model, maxTokens) already warned about


def cli_settings_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def cli_settings() -> tuple[dict[str, Any], str | None]:
    """(settings, problem): the Antigravity CLI's settings file, re-read when it changes; {} if it doesn't exist."""
    path = cli_settings_path()
    try:
        st = path.stat()
    except OSError:
        return {}, None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _cli_cache.get(str(path))
    if hit and hit[0] == sig:
        return hit[1], hit[2]
    try:
        data = json.loads(path.read_text() or "{}")
        data, problem = (data, None) if isinstance(data, dict) else ({}, f"{path}: the top level must be a JSON object")
    except (OSError, ValueError) as e:
        data, problem = {}, f"{path}: {e}"
    _cli_cache[str(path)] = (sig, data, problem)
    return data, problem


def _file_env(name: str) -> str | None:
    """A variable set under the settings file's ``env`` (only there)."""
    env = cli_settings()[0].get("env")
    value = env.get(name) if isinstance(env, dict) else None
    return str(value) if value not in (None, "") else None


def setting_env(name: str) -> str | None:
    """An environment variable for Antigravity: the settings file's ``env``, else Atrium's environment."""
    return _file_env(name) or os.environ.get(name) or None


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes")


def _vertex() -> bool:
    """Gemini Enterprise (Vertex AI) instead of the Gemini API: GOOGLE_GENAI_USE_VERTEXAI, or the CLI's AGY_ADC_AUTH."""
    return _truthy(setting_env("GOOGLE_GENAI_USE_VERTEXAI")) or _truthy(setting_env("AGY_ADC_AUTH"))


def _gcp() -> dict[str, Any]:
    gcp = cli_settings()[0].get("gcp")
    return gcp if isinstance(gcp, dict) else {}


def _vertex_project() -> str | None:
    return setting_env("GOOGLE_CLOUD_PROJECT") or setting_env("GOOGLE_CLOUD_QUOTA_PROJECT") or _gcp().get("project") or None


@dataclass
class ModelChoice:
    """The model a session talks to, and where."""

    model: str  # the model id sent to the API
    base_url: str | None  # a custom model's endpoint; None: GOOGLE_GEMINI_BASE_URL, else Google's
    label: str
    thinking: bool = True  # takes a thinking level (modelFeatures.supportsThinking)
    images: bool = True  # takes images (modelFeatures.supportsImages)
    window: int = CONTEXT_WINDOW  # context window, in tokens (maxTokens)


def parse_model_name(name: str) -> tuple[str | None, str]:
    """A custom model's ``gemini-api://<base_url>/models/<model>`` → (base URL, model). The CLI writes the
    base URL without a scheme (https); an explicit http:// or https:// is kept. Anything else is a model id."""
    if not name.startswith("gemini-api:"):
        return None, name
    base, sep, model = name[len("gemini-api:"):].rpartition("/models/")
    if not sep or not model:
        return None, name
    base = base.removeprefix("//").rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base or None, model


def custom_models() -> dict[str, dict[str, Any]]:
    """The settings file's custom models (``customModelsConfig.customModels``), by key."""
    cmc = cli_settings()[0].get("customModelsConfig")
    found = cmc.get("customModels") if isinstance(cmc, dict) else None
    if not isinstance(found, dict):
        return {}
    return {str(k): m for k, m in found.items() if isinstance(m, dict) and isinstance(m.get("modelName"), str)}


def _max_tokens(key: str, m: dict[str, Any]) -> int:
    """A custom model's context window (``maxTokens``); the default, with a warning, if that isn't a number of tokens."""
    raw = m.get("maxTokens")
    if raw in (None, ""):
        return CONTEXT_WINDOW
    n = 0
    if not isinstance(raw, bool) and not (isinstance(raw, float) and not raw.is_integer()):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            pass
    if n > 0:
        return n
    if (key, repr(raw)) not in _bad_max_tokens:  # once: the catalog asks on every page load
        _bad_max_tokens.add((key, repr(raw)))
        log.warning("%s: maxTokens %r of custom model %s isn't a number of tokens; using %d", cli_settings_path(), raw, key, CONTEXT_WINDOW)
    return CONTEXT_WINDOW


def resolve_model(pick: str | None) -> ModelChoice:
    """Atrium's pick, else the settings file's ``model``, else the SDK's default. Either can name a custom
    model (by key or label), a model id, or one of the CLI's labels ("Gemini 3.8 Flash (High)")."""
    name = (pick or str(cli_settings()[0].get("model") or "")).strip() or DEFAULT_MODEL
    for key, m in custom_models().items():
        if name in (key, m.get("label"), m["modelName"]):
            base, model = parse_model_name(m["modelName"])
            feats = m.get("modelFeatures")
            feats = feats if isinstance(feats, dict) else {}
            return ModelChoice(model=model, base_url=base, label=str(m.get("label") or f"Custom Model: {model}"),
                               thinking=feats.get("supportsThinking") is not False, images=feats.get("supportsImages") is not False,
                               window=_max_tokens(key, m))
    base, model = parse_model_name(name)
    if " " in model:  # a built-in model's label, as the CLI saves "/model": "Gemini 3.8 Flash (High)" → gemini-3.8-flash
        model = re.sub(r"\s*\(.*\)$", "", model).strip().lower().replace(" ", "-")
    return ModelChoice(model=model, base_url=base, label=model)


def _google(url: str | None) -> bool:
    """Whether a Gemini API base URL is Google's own (None: the API's default, which is)."""
    host = (urllib.parse.urlsplit(url).hostname or "") if url else "generativelanguage.googleapis.com"
    return host == "googleapis.com" or host.endswith(".googleapis.com")


def gemini_endpoint(choice: ModelChoice) -> tuple[str | None, str | None, str | None]:
    """(base URL, API key, problem) for a model on the Gemini API. A relay set in the settings file (a custom
    model, or GOOGLE_GEMINI_BASE_URL under ``env``) only gets the key written there, never a GEMINI_API_KEY
    from Atrium's environment, which is most likely Google's. A key always goes with the endpoint: given
    none, the runtime takes GEMINI_API_KEY from its environment (Atrium's), and it doesn't start without one."""
    base, in_file = choice.base_url, bool(choice.base_url)
    if not base:
        base, in_file = setting_env("GOOGLE_GEMINI_BASE_URL"), bool(_file_env("GOOGLE_GEMINI_BASE_URL"))
    if in_file and not _google(base):
        key = _file_env("GEMINI_API_KEY")
        return base, key, None if key else (
            f"Needs the API key of {urllib.parse.urlsplit(base or '').netloc or base}: in {cli_settings_path()} put it under "
            "\"env\": {\"GEMINI_API_KEY\": …}. A relay set in that file only gets a key written there (a placeholder if it takes none).")
    key = setting_env("GEMINI_API_KEY")
    return base, key, None if key else (
        f"Needs a Gemini API key: in {cli_settings_path()} put it under \"env\": {{\"GEMINI_API_KEY\": …}} (or set "
        "GEMINI_API_KEY), and a relay as a custom model in customModelsConfig if you use one.")


def credentials_problem() -> str | None:
    """Why model calls can't be made (no key, no project, a settings file that can't be read or used), or None."""
    try:
        problem = cli_settings()[1]
        if problem:
            return f"Antigravity's settings can't be read: {problem}"
        choice = resolve_model(_defaults().get("model"))
        if choice.base_url or not _vertex():
            return gemini_endpoint(choice)[2]
        if _vertex_project() or setting_env("GOOGLE_API_KEY"):
            return None  # Application Default Credentials, checked on first use
        return ("Gemini Enterprise (Vertex AI) needs a project (GOOGLE_CLOUD_PROJECT, or \"gcp\": {\"project\": …} in "
                f"{cli_settings_path()}) or an API key (GOOGLE_API_KEY).")
    except Exception as e:  # the file belongs to another program: whatever it holds, say so rather than fail
        log.warning("Antigravity's settings can't be used", exc_info=True)
        return f"Antigravity's settings can't be used ({cli_settings_path()}): {type(e).__name__}: {e}"


def model_targets(model: str | None, effort: str | None, capture: Any = None, session: str | None = None) -> list[Any]:
    """The text model (with its thinking level) and the image model, on the endpoint the settings say. With
    ``capture`` (an agent's ``CaptureContext``), a Gemini API endpoint is reached through Atrium's capture proxy."""
    from google.antigravity import types

    choice = resolve_model(model)
    gemini = bool(choice.base_url) or not _vertex()
    base, key, problem = gemini_endpoint(choice) if gemini else (None, None, None)
    if problem:
        raise RuntimeError(problem)
    if gemini and capture:  # the runtime reaches loopback directly (Go skips the proxy for it)
        base = capture.base_url("antigravity", base or GEMINI_API, session=session) or base

    def endpoint(options: Any) -> Any:
        if gemini:
            return types.GeminiAPIEndpoint(api_key=key, base_url=base, options=options)
        project, vbase = _vertex_project(), setting_env("GOOGLE_VERTEX_BASE_URL")
        if project or not setting_env("GOOGLE_API_KEY"):  # project + location, with Application Default Credentials
            location = setting_env("GOOGLE_CLOUD_LOCATION") or _gcp().get("location") or "global"
            return types.VertexEndpoint(project=project, location=location, base_url=vbase, options=options)
        return types.VertexEndpoint(api_key=setting_env("GOOGLE_API_KEY"), base_url=vbase, options=options)  # express mode

    level = types.GeminiModelOptions(thinking_level=types.ThinkingLevel(effort)) if effort in EFFORTS and choice.thinking else None
    return [
        types.ModelTarget(name=choice.model, types=[types.ModelType.TEXT], endpoint=endpoint(level)),
        types.ModelTarget(name=IMAGE_MODEL, types=[types.ModelType.IMAGE], endpoint=endpoint(None)),
    ]


def mcp_configs(servers: dict[str, Any]) -> list[Any]:
    """Atrium's ``mcp_servers`` (Claude Code's format) as the SDK's MCP server configs."""
    from google.antigravity.types import McpStdioServer, McpStreamableHttpServer

    out: list[Any] = []
    for name, c in (servers or {}).items():
        if not isinstance(c, dict) or not re.fullmatch(r"[a-zA-Z0-9_-]+", str(name)):
            continue
        try:
            if c.get("url"):
                out.append(McpStreamableHttpServer(name=name, url=c["url"], headers=c.get("headers") or None))
            elif c.get("command"):
                out.append(McpStdioServer(name=name, command=c["command"], args=[str(a) for a in c.get("args") or []], env=c.get("env") or None))
        except Exception as e:
            log.warning("skipping MCP server %s for Antigravity: %s", name, e)
    return out


# --------------------------------------------------------------------------
# Rules: Antigravity's product surfaces load AGENTS.md / GEMINI.md, the SDK's runtime doesn't
# --------------------------------------------------------------------------


def _rule_text(path: Path) -> str:
    try:
        return path.read_bytes()[:MAX_RULE_BYTES].decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def workspace_rules(cwd: str) -> str:
    """The rules Antigravity would load for a session in ``cwd``: global AGENTS.md / GEMINI.md, then
    those of every directory from the repository root down to ``cwd``, with ``.agents/rules/*.md``
    (``always_on`` in full, ``model_decision`` as a pointer)."""
    root = os.path.realpath(gitops.repo_root(cwd) or cwd)
    dirs, d = [], os.path.realpath(cwd)
    while True:
        dirs.append(d)
        parent = os.path.dirname(d)
        if d == root or parent == d or not d.startswith(root):
            break
        d = parent
    files = [Path.home() / ".gemini" / n for n in ("AGENTS.md", "GEMINI.md")]
    for d in reversed(dirs):
        files += [Path(d) / n for n in RULE_FILES]
    sections, pointers = [], []
    for p in files:
        text = _rule_text(p) if p.is_file() else ""
        if text:
            sections.append(f"## {p}\n\n{text}")
    for d in reversed(dirs):
        rules = Path(d) / ".agents" / "rules"
        for p in sorted(rules.glob("*.md")) if rules.is_dir() else []:
            text = _rule_text(p)
            m = _FRONTMATTER_RE.match(text)
            front = dict(re.findall(r"^(\w+):\s*\"?(.*?)\"?\s*$", m.group(1), re.M)) if m else {}
            if front.get("trigger") == "always_on":
                sections.append(f"## {p}\n\n{text[m.end():].strip() if m else text}")
            elif front.get("trigger") == "model_decision" and front.get("description"):
                pointers.append(f"- {p}: {front['description']}")
    if pointers:
        sections.append("## More rules (read the file when its description matches the task)\n\n" + "\n".join(pointers))
    return "# Rules\n\n" + "\n\n".join(sections) if sections else ""


# --------------------------------------------------------------------------
# Tool calls as chat items (names and inputs after Claude Code's, which the UI renders)
# --------------------------------------------------------------------------


def _plain(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in (args or {}).items() if k not in META_ARGS}


def atrium_tool(name: str) -> str | None:
    """The chat name of one of Atrium's tools by its name here: ``board_status`` → ``mcp__board__status``."""
    ns, _, tool = name.partition("_")
    return f"mcp__{ns}__{tool}" if ns in ATRIUM_NAMESPACES and tool else None


def mcp_args(args: dict[str, Any]) -> dict[str, Any]:
    """An MCP tool's own arguments: ``call_mcp_tool`` (how a lazily loaded one is called) wraps them in Arguments."""
    a = _plain(args)
    if "ServerName" in a and "ToolName" in a:
        inner = a.get("Arguments")
        return inner if isinstance(inner, dict) else {"arguments": inner}
    return a


def call_view(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A tool call as the model made it (transcript, approval requests) → (name, input) for the chat."""
    a = _plain(args)
    shown = atrium_tool(name)
    if shown:
        return shown, a
    if name == "call_mcp_tool" and a.get("ServerName") and a.get("ToolName"):
        return f"mcp__{a['ServerName']}__{a['ToolName']}", mcp_args(a)
    if "CommandLine" in a:
        return "Bash", {"command": a.get("CommandLine") or "", "cwd": a.get("Cwd") or None}
    if "CodeContent" in a:
        return "Write", {"file_path": a.get("TargetFile"), "content": a.get("CodeContent") or ""}
    if "ReplacementChunks" in a:
        edits = [{"old_string": c.get("TargetContent") or "", "new_string": c.get("ReplacementContent") or ""}
                 for c in a.get("ReplacementChunks") or [] if isinstance(c, dict)]
        return "MultiEdit", {"file_path": a.get("TargetFile"), "edits": edits}
    if "TargetContent" in a:
        return "Edit", {"file_path": a.get("TargetFile"), "old_string": a.get("TargetContent") or "",
                        "new_string": a.get("ReplacementContent") or "", "replace_all": bool(a.get("AllowMultiple"))}
    if "AbsolutePath" in a:
        inp: dict[str, Any] = {"file_path": a["AbsolutePath"]}
        if a.get("StartLine"):
            inp["offset"] = a["StartLine"]
        return "Read", inp
    if name == "search_web":
        return "WebSearch", {"query": a.get("query") or "", **({"domain": a["domain"]} if a.get("domain") else {})}
    if name == "read_url_content":
        return "WebFetch", {"url": a.get("Url") or a.get("url") or ""}
    if name == "generate_image":
        return "ImageGen", {"prompt": a.get("Prompt") or ""}
    if name == "invoke_subagent":
        subs = [s for s in a.get("Subagents") or [] if isinstance(s, dict)]
        first = subs[0] if subs else {}
        desc = first.get("Prompt") or first.get("Task") or first.get("Description") or ""
        return "Agent", {"description": short(desc, 200), "subagent_type": first.get("Name") or first.get("Type") or "subagent",
                         "subagents": len(subs)}
    return name, a


def _hunks(blocks: list[dict[str, Any]]) -> tuple[str, str]:
    """``edit_file`` diff blocks → (kind, diff): "add" with the content for a new file, else unified hunks."""
    lines = [ln for b in blocks for ln in b.get("lines") or []]
    if lines and all(ln.get("action") == "LINE_ACTION_INSERT" for ln in lines) and all(not b.get("start_line") for b in blocks):
        return "add", "\n".join(ln.get("text", "") for ln in lines)
    out = []
    for b in blocks:
        ls = b.get("lines") or []
        old = sum(1 for ln in ls if ln.get("action") != "LINE_ACTION_INSERT")
        new = sum(1 for ln in ls if ln.get("action") != "LINE_ACTION_DELETE")
        start = int(b.get("start_line") or 0) + 1
        out.append(f"@@ -{start},{old} +{start},{new} @@")
        sign = {"LINE_ACTION_INSERT": "+", "LINE_ACTION_DELETE": "-"}
        out += [sign.get(ln.get("action") or "", " ") + (ln.get("text") or "") for ln in ls]
    return "update", "\n".join(out)


def step_view(call: Any) -> tuple[str, dict[str, Any]] | None:
    """A tool step of the live stream (the SDK's ToolCall, arguments as the runtime reports them)
    → (name, input) for the chat; None for steps that aren't shown."""
    name, a = str(call.name), dict(call.args or {})
    server = getattr(call, "server_name", None)
    if server:
        return f"mcp__{server}__{name}", _plain(a)
    if name == "run_command":
        return "Bash", {"command": a.get("command_line") or "", "cwd": a.get("working_dir") or None}
    if name == "view_file":
        inp: dict[str, Any] = {"file_path": a.get("file_path")}
        if a.get("start_line"):
            inp["offset"] = a["start_line"]
        return "Read", inp
    if name == "edit_file":
        kind, diff = _hunks(a.get("diff_block") or [])
        return "Patch", {"changes": [{"path": a.get("file_path"), "kind": kind, "diff": diff}]}
    if name == "create_file":
        return "Write", {"file_path": a.get("file_path"), "content": a.get("contents") or ""}
    if name == "find_file":
        return "Glob", {"pattern": a.get("query") or "", "path": a.get("directory_path")}
    if name == "search_directory":
        return "Grep", {"pattern": a.get("query") or "", "path": a.get("directory_path")}
    if name == "search_web":
        return "WebSearch", {"query": a.get("query") or "", **({"domain": a["domain"]} if a.get("domain") else {})}
    if name == "read_url_content":
        return "WebFetch", {"url": a.get("url") or ""}
    if name == "generate_image":
        return "ImageGen", {"prompt": a.get("prompt") or "", "file_path": a.get("output_path") or None}
    if name == "invoke_subagent":
        return "Agent", {"description": "", "subagent_type": "subagent"}
    if name == "run_workflow":
        return "Workflow", {"description": a.get("description") or "", "script_path": a.get("script_path") or None}
    if name == "skill_lookup":
        return "Skill", {"skills": a.get("requested_skill_names") or []}
    if name == "finish":
        return None
    return name, _plain(a)


def step_result(call: Any, step: Any) -> tuple[str, bool]:
    """(content, is_error) of a finished tool step of the live stream."""
    a = dict(call.args or {})
    status = str(getattr(step.status, "value", step.status))
    if status == "ERROR":
        return clip(step.error or "Failed."), True
    if status == "CANCELED":
        return "Cancelled.", True
    name = str(call.name)
    if name == "run_command":
        out, code = a.get("combined_output") or "", a.get("exit_code")
        if code not in (0, None):
            return clip(out + f"\n[exit code {code}]"), True
        return clip(out), False
    if name == "list_directory":
        rows = [f"{r.get('name')}{'/' if r.get('is_directory') else ''}" for r in a.get("results") or [] if isinstance(r, dict)]
        return clip("\n".join(rows)), False
    if name == "find_file":
        return clip(a.get("output") or ""), False
    if name == "search_directory":
        return f"{a.get('num_results') or 0} results", False
    if name in ("search_web", "read_url_content"):
        return clip("\n\n".join(x for x in (a.get("title"), a.get("summary")) if x) or "Done."), False
    if name == "run_workflow":
        return clip(a.get("output") or ""), False
    if name == "skill_lookup":
        return ", ".join(a.get("resolved_skill_names") or []) or "Done.", False
    if name == "edit_file":
        return "Edited.", False
    if name == "generate_image":
        return f"Saved {a.get('output_path')}" if a.get("output_path") else "Done.", False
    return "Done.", False  # the step's own text is the call's summary, not its result


# --------------------------------------------------------------------------
# Transcript (data/brain/<id>/.system_generated/logs/transcript_full.jsonl)
# --------------------------------------------------------------------------


def _count(v: Any) -> int:
    """A token count as the transcript records it (a string of digits)."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _step_of(line: bytes) -> dict[str, Any] | None:
    try:
        d = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("step_index"), int) else None


def _entries(path: Path, lo: int | None = None, hi: int | None = None, start: int = 0) -> list[dict[str, Any]]:
    """The transcript's steps in order (lines are not always written in order; a rewritten step wins): with ``lo`` and
    ``hi``, those from step ``lo`` up to ``hi`` (not included), read from byte ``start`` on. A line's step is read off its
    start, and only the lines of those steps are parsed: a conversation compacted many times is tens of megabytes."""

    def wanted(i: int) -> bool:
        return (lo is None or i >= lo) and (hi is None or i < hi)

    steps: dict[int, dict[str, Any]] = {}
    try:
        fh = path.open("rb")
    except OSError:
        return []
    with fh:
        fh.seek(start)
        for line in fh:
            head = _HEAD_RE.match(line)
            if head and not wanted(int(head.group(1))):
                continue
            d = _step_of(line)
            if d is not None and wanted(d["step_index"]):
                steps[d["step_index"]] = d
    return [steps[i] for i in sorted(steps)]


class _StepScan(TailScan):
    """Where a transcript's steps are, as far as it was read: where each one's line starts, the compactions (their
    steps) and the user's messages. The chat, an edit and the session list need only those and the conversation's
    end: a conversation compacted many times is tens of megabytes."""

    def __init__(self, ino: int):
        super().__init__(ino)
        self.lines: dict[int, int] = {}  # step -> where its line starts (a rewritten step: the last one)
        self.top = -1  # the highest step read so far
        self.started: float | None = None  # when the conversation began (its first line)
        # compaction step -> {at, tokens, start: where the steps from it on are read from (None: some came before its line)}
        self.compactions: dict[int, dict[str, Any]] = {}
        self.prompts: dict[int, tuple[float | None, str]] = {}  # the user's messages: step -> (when, what was typed, shortened)

    def line(self, raw: bytes) -> None:
        at = self.offset - len(raw)
        head = _HEAD_RE.match(raw)
        d = None if head else _step_of(raw)
        if head:
            step, source, kind = int(head.group(1)), head.group(2).decode(), head.group(3).decode()
        elif d is not None:
            step, source, kind = d["step_index"], str(d.get("source") or ""), str(d.get("type") or "")
        else:
            return  # not a step (a line cut short?)
        prompt = kind == "USER_INPUT" and source == "USER_EXPLICIT"
        if kind == COMPACTED or prompt or self.started is None:
            d = d or _step_of(raw) or {}
            ts = iso_to_epoch(d.get("created_at"))
            self.started = self.started if self.started is not None else ts
            if kind == COMPACTED:
                tokens = sum(_count(d.get(k)) for k in ("input_tokens", "cache_read_tokens"))  # what it summarized
                self.compactions[step] = {"at": ts, "tokens": tokens or None, "start": at if self.top < step else None}
            if prompt:
                self.prompts[step] = (ts, short(split_attachment_note(user_text(d.get("content") or ""))[0], 300))
        if kind != COMPACTED:
            self.compactions.pop(step, None)
        if not prompt:
            self.prompts.pop(step, None)
        self.lines[step] = at
        self.top = max(self.top, step)

    def copy(self) -> "_StepScan":
        out = copy.copy(self)
        out.lines, out.compactions, out.prompts = dict(self.lines), dict(self.compactions), dict(self.prompts)
        return out


_scans: dict[str, _StepScan] = {}  # transcript path -> what's read of it
_scan_lock = threading.Lock()


def _steps(path: Path | None) -> _StepScan | None:
    """The transcript's scan, read on to its end: a copy (they are read on in worker threads)."""
    if path is None:
        return None
    with _scan_lock:
        scan = tail_scan(_scans, path, _StepScan)
        return scan.copy() if scan else None


def _step_at(path: Path, steps: _StepScan, i: int) -> dict[str, Any]:
    """Step ``i`` as its line in the transcript has it ({} if it isn't there)."""
    try:
        with path.open("rb") as fh:
            fh.seek(steps.lines[i])
            d = _step_of(fh.readline())
    except (KeyError, OSError):
        return {}
    return d if d is not None and d["step_index"] == i else {}


def current_steps(path: Path) -> list[dict[str, Any]]:
    """The conversation since its last compaction, which opens it (what the model still sees, as the chat shows it): its
    steps from the last compaction's on; all of them if there is none."""
    steps = _steps(path)
    if not steps or not steps.compactions:
        return _entries(path)
    last = max(steps.compactions)
    return _entries(path, lo=last, start=steps.compactions[last]["start"] or 0)


def compacted_steps(path: Path, cid: str) -> list[dict[str, Any]] | None:
    """The part of the conversation compaction ``cid`` closed: from the compaction before it (which opens it) or the
    start, up to this one. None if there is no such compaction."""
    steps = _steps(path)
    m = re.fullmatch(r"s(\d+)", cid)
    end = int(m.group(1)) if m else None
    if not steps or end not in steps.compactions:
        return None
    before = max((c for c in steps.compactions if c < end), default=None)
    start = (steps.compactions[before]["start"] or 0) if before is not None else 0
    return _entries(path, lo=before, hi=end, start=start)


def compactions_of(path: Path) -> list[Compaction]:
    """The conversation's compactions, oldest first: each with when the part it closed began, and its first message."""
    steps = _steps(path)
    out: list[Compaction] = []
    prev = -1
    if steps is None:
        return out
    for c in sorted(steps.compactions):
        found = steps.compactions[c]
        first = next((text for i, (_ts, text) in sorted(steps.prompts.items()) if prev < i < c), "")
        out.append(Compaction(id=f"s{c}", at=found["at"], started_at=out[-1].at if out else steps.started, tokens=found["tokens"],
                              first_prompt=first))
        prev = c
    return out


def _targs(raw: Any) -> dict[str, Any]:
    """Tool-call arguments as the transcript records them: every value JSON-encoded."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items() if isinstance(raw, dict) else []:
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


def user_text(content: str) -> str:
    """What the user typed: the runtime wraps it in <USER_REQUEST> and adds metadata."""
    a, b = content.find("<USER_REQUEST>"), content.rfind("</USER_REQUEST>")
    if a < 0 or b < a:
        return content
    return content[a + len("<USER_REQUEST>") : b].strip("\n")


def _result_text(d: dict[str, Any]) -> tuple[str, bool]:
    """(content, is_error) of a tool's result step."""
    text = _STAMP_RE.sub("", d.get("content") or "").strip("\n")
    err = d.get("status") == "ERROR" or d.get("type") == "ERROR_MESSAGE"  # (the runtime refused the call: invalid arguments…)
    if err:
        return clip(str(d.get("error") or text or "Failed.")), True
    if d.get("type") == "RUN_COMMAND":
        head, sep, out = text.partition("Output:\n")
        text = out if sep else text
        if d.get("exit_code") not in (0, None):
            return clip(text.rstrip() + f"\n[exit code {d['exit_code']}]"), True
    m = re.match(r"Tool response for [^\n]*:\n```(?:json)?\n(.*)\n```\s*$", text, re.S)
    if m:  # one of Atrium's tools: {"result": ...}
        try:
            res = json.loads(m.group(1))
            text = str(res["result"]) if isinstance(res, dict) and isinstance(res.get("result"), str) else m.group(1)
        except ValueError:
            pass
    return clip(text), False


def _calls(entries: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int, dict[str, Any], dict[str, Any] | None]]:
    """(planner step, index in its tool_calls, call, result step or None): each tool call with the step
    that carries its result — the steps right after the planner step, in order."""
    out: list[tuple[dict[str, Any], int, dict[str, Any], dict[str, Any] | None]] = []
    waiting: list[int] = []  # positions in out still without a result
    for d in entries:
        t = d.get("type")
        if t == "PLANNER_RESPONSE":
            for n, c in enumerate(d.get("tool_calls") or []):
                if isinstance(c, dict):
                    waiting.append(len(out))
                    out.append((d, n, c, None))
        elif t == "USER_INPUT" or t == COMPACTED:
            waiting = []
        elif waiting:
            i = waiting.pop(0)
            out[i] = (*out[i][:3], d)
    return out


def history_items(entries: list[dict[str, Any]]) -> list[Item]:
    calls = {(id(p), n): r for p, n, _c, r in _calls(entries)}
    results = {id(r) for r in calls.values() if r is not None}
    items: list[Item] = []
    for d in entries:
        t, iid = d.get("type"), f"s{d['step_index']}"
        made = len(items)
        if t == "USER_INPUT":
            text, files = split_attachment_note(user_text(d.get("content") or ""))
            if d.get("source") != "USER_EXPLICIT":
                items.append({"kind": "system", "id": iid, "subtype": "notice", "text": short(text, 2000)})
            else:
                it: Item = {"kind": "user", "id": iid, "text": text}
                if files:
                    it["attachments"] = [{**f, "url": upload_url(f["path"])} for f in files]
                items.append(it)
        elif t == "PLANNER_RESPONSE":
            if (d.get("thinking") or "").strip():
                items.append({"kind": "thinking", "id": f"{iid}:think", "text": d["thinking"]})
            if (d.get("content") or "").strip():
                items.append({"kind": "text", "id": iid, "text": d["content"]})
            for n, c in enumerate(d.get("tool_calls") or []):
                if not isinstance(c, dict):
                    continue
                name, inp = call_view(c.get("name") or "", _targs(c.get("args")))
                items.append({"kind": "tool", "id": f"{iid}:{n}", "name": name, "input": inp})
                res = calls.get((id(d), n))
                if res is not None:
                    content, err = _result_text(res)
                    items.append({"kind": "tool_result", "id": f"{iid}:{n}", "content": content, "is_error": err})
        elif t == COMPACTED:
            items += compaction_items(iid, d.get("content") or "")
        elif id(d) not in results and t == "ERROR_MESSAGE" and d.get("status") != "ERROR":  # the call was retried
            items.append(retry_item(iid, str(d.get("error") or _result_text(d)[0])))
        elif id(d) not in results and (d.get("status") == "ERROR" or t == "ERROR_MESSAGE"):
            items.append({"kind": "error", "id": iid, "text": d.get("error") or _result_text(d)[0]})
        if ts := iso_to_epoch(d.get("created_at")):
            for it in items[made:]:
                it["ts"] = ts
    return items


def _command_output(d: dict[str, Any]) -> str:
    """What a RUN_COMMAND step printed (the runtime keeps the end of a long output)."""
    text = _STAMP_RE.sub("", d.get("content") or "")
    _head, sep, out = text.partition("Output:\n")
    if not sep:  # a failed command: "The command exited with code 1.\nStdout:\n…\nStderr:\n…"
        out = re.sub(r"(?m)^(?:The command exited with code -?\d+\.|Stdout:|Stderr:)", "", text)
    return out.strip()


def digest_entry(d: dict[str, Any]) -> list[str]:
    ts = hhmm(d.get("created_at"))
    pre = f"[{ts}] " if ts else ""
    t = d.get("type")
    if t == "USER_INPUT" and d.get("source") == "USER_EXPLICIT":
        text = split_attachment_note(user_text(d.get("content") or ""))[0]
        return [f"{pre}USER: {short(text, 600)}"] if text.strip() else []
    if t == "PLANNER_RESPONSE":
        if (d.get("content") or "").strip():
            out = [f"{pre}AGENT: {short(d['content'], 700)}"]
        elif (d.get("thinking") or "").strip():  # Gemini seldom writes between tool calls: what it has found is in its thoughts
            out = [f"{pre}THINKING: {short(d['thinking'].replace('**', ''), 400)}"]
        else:
            out = []
        for c in d.get("tool_calls") or []:
            if not isinstance(c, dict):
                continue
            name, a = c.get("name") or "", _targs(c.get("args"))
            if "CommandLine" in a:
                out.append(f"{pre}TOOL Bash: {short(a['CommandLine'], 180)}")
            elif "CodeContent" in a:
                out.append(f"{pre}wrote {a.get('TargetFile')} ({len(str(a['CodeContent']).splitlines())} lines)")
            elif "TargetContent" in a or "ReplacementChunks" in a:
                out.append(f"{pre}edited {a.get('TargetFile')}")
            elif atrium_tool(name):
                out.append(f"{pre}TOOL {name.replace('_', '.', 1)}: {short({k: v for k, v in _plain(a).items() if k != 'html'}, 200)}")
            elif name not in READ_TOOLS:
                out.append(f"{pre}TOOL {name}: {short(_plain(a), 160)}")
        return out
    if t == "RUN_COMMAND":
        printed = _command_output(d)
        tail = short_tail(printed, 200) if printed else ""
        if d.get("exit_code") not in (0, None):
            return [f"{pre}(command failed: exit {d['exit_code']}){' ' + tail if tail else ''}"]
        return [f"{pre}→ {tail}"] if tail else []
    if t == "ERROR_MESSAGE":
        return [f"{pre}(error: {short(d.get('error') or '', 200)})"]
    return []


def _patch(chunks: list[tuple[Any, str, str]]) -> str:
    """Unified hunks for replacements (start line, old text, new text); reverse-applied to find a baseline."""
    out = []
    for start, old, new in chunks:
        a, b = old.split("\n"), new.split("\n")
        start = max(int(start or 1), 1)
        out.append(f"@@ -{start},{len(a)} +{start},{len(b)} @@")
        out += ["-" + x for x in a] + ["+" + x for x in b]
    return "\n".join(out)


def _replacement(x: dict[str, Any]) -> Replacement:
    """One replacement of the edit tools as the runtime makes it: the exact TargetContent, searched for in the lines
    StartLine–EndLine; with AllowMultiple, every occurrence there."""
    try:
        lines: tuple[int, int] | None = (int(x["StartLine"]), int(x.get("EndLine") or x["StartLine"]))
    except (KeyError, TypeError, ValueError):
        lines = None
    return Replacement(old=str(x.get("TargetContent") or ""), new=str(x.get("ReplacementContent") or ""), all=bool(x.get("AllowMultiple")),
                       lines=lines)


def file_edits_of(entries: list[dict[str, Any]]) -> list[FileEdit]:
    edits: list[FileEdit] = []
    for p, _n, c, res in _calls(entries):
        a = _targs(c.get("args"))
        path = a.get("TargetFile")
        if not path or res is None or res.get("status") == "ERROR" or res.get("type") == "ERROR_MESSAGE":
            continue
        ts = p.get("created_at")
        if "CodeContent" in a:
            append = bool(a.get("Append"))
            edits.append(FileEdit(path=path, tool="Write", ts=ts, before=None, after=None if append else str(a["CodeContent"]),
                                  kind="update" if append or a.get("Overwrite") else "create", append=str(a["CodeContent"]) if append else None))
        elif "ReplacementChunks" in a:
            raw = [x for x in a["ReplacementChunks"] or [] if isinstance(x, dict)]
            chunks = [(x.get("StartLine"), str(x.get("TargetContent") or ""), str(x.get("ReplacementContent") or "")) for x in raw]
            reps = [_replacement(x) for x in raw]
            if all(r.lines for r in reps):  # their lines are the file's before the edit: the bottom one first, the others' still hold
                reps.sort(key=lambda r: -(r.lines or (0, 0))[0])
            edits.append(FileEdit(path=path, tool="MultiEdit", ts=ts, before=None, after=None, kind="update", patch=_patch(chunks),
                                  replacements=reps))
        elif "TargetContent" in a:
            chunk = (a.get("StartLine"), str(a.get("TargetContent") or ""), str(a.get("ReplacementContent") or ""))
            edits.append(FileEdit(path=path, tool="Edit", ts=ts, before=None, after=None, kind="update", patch=_patch([chunk]),
                                  replacements=[_replacement(a)]))
    return edits


def turn_edits(sid: str, cut: int) -> list[FileEdit]:
    """The file edits made in the turn that step ``cut`` is in, up to that step (``Harness.fork_edits``)."""
    path = transcript_file(sid)
    steps = _steps(path)
    if path is None or steps is None:
        return []
    start = max((i for i in steps.prompts if i <= cut), default=-1)
    return file_edits_of(_entries(path, lo=start + 1, hi=cut + 1))


def retry_item(iid: str, error: str) -> Item:
    """A model call that failed and is tried again (the runtime retries): a line, not an error."""
    m = _API_ERROR_RE.search(error)
    why = f"{m.group(1)} {m.group(2).strip()}" if m else short(error, 120)
    return {"kind": "system", "id": iid, "subtype": "retry", "text": f"Model provider error ({why}), retried", "detail": error}


def compaction_items(iid: str, summary: str) -> list[Item]:
    """Where a compaction cut the conversation, and the summary the model goes on from, folded. ``compaction`` opens the
    part it closed when ``list_compactions`` has that id: a step of the transcript's (``s<step>``). The live stream's ids
    never are (its numbers aren't the transcript's once a conversation is long): its compaction opens once the chat is
    loaded again."""
    items: list[Item] = [{"kind": "system", "id": iid, "subtype": "compact", "text": "Conversation compacted", "compaction": iid}]
    summary = _STAMP_RE.sub("", summary).strip()
    if summary:
        items.append({"kind": "system", "id": f"{iid}:summary", "subtype": "compact_summary", "text": "Summary of the earlier conversation",
                      "detail": summary})
    return items


def _last_prompt(path: Path | None) -> int | None:
    """The step of the user's last message in the transcript."""
    steps = _steps(path)
    return max(steps.prompts, default=None) if steps else None


def _last_usage(path: Path | None) -> int | None:
    """Tokens in context after the last model call: its input (cached or not) plus output."""
    if not path:
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - 262_144))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("input_tokens"):
            return int(d["input_tokens"]) + int(d.get("cache_read_tokens") or 0) + int(d.get("output_tokens") or 0)
    return None


# --------------------------------------------------------------------------
# Forks: a copy of the conversation under a new id, cut after a step
# --------------------------------------------------------------------------
# The runtime has no fork of its own. A conversation is its state database (save/<id>.db) and its data
# directory (data/brain/<id>/), so a fork is a copy of both under the new id, with every step after the cut
# taken out. In the database (version 0.1.23's layout, read from its protos): ``steps`` (one row per step,
# ``idx`` = the transcript's step_index), ``gen_metadata`` (one row per model call: #2 the steps it made,
# #4 its execution), ``executor_metadata`` (one row per execution, i.e. per message the runtime answered:
# #1 why it ended, #2 its model calls, #3 its last step, #9 its id), ``trajectory_meta`` and
# ``trajectory_metadata_blob`` (#6 the id, #7 the workspaces). The id also appears inside the steps' and
# model calls' records; the new id has the same length, so it is swapped byte for byte.

# history ids s<step>[:…]; live ones <conversation>:<number>@<run>[:…], numbered by the stream of that run (_Run)
_STEP_RE = re.compile(r"^(?:s|(?P<conv>[0-9A-Za-z-]{32,}):)(?P<step>\d+)(?:@(?P<run>[0-9a-f]+))?(?::.*)?$")
_FINISHED = {3, 4, 5, 6, 7, 12}  # CortexStepStatus: done, invalid, cleared, canceled, error, interrupted
_CANCELED = 2  # ExecutorTerminationReason USER_CANCELED: how an execution cut short ends
_CHUNK = 102_400  # the runtime also keeps each transcript in chunks of this many bytes (logs/chunks/<name>/<n>.jsonl)


def _pb_fields(data: bytes) -> list[tuple[int, int, Any, bytes]]:
    """A protobuf message's top-level fields: (number, wire type, value, the field's bytes)."""
    out, i = [], 0
    while i < len(data):
        key, j = _varint(data, i)
        wt = key & 7
        if wt == 0:
            value, j = _varint(data, j)
        elif wt == 2:
            n, k = _varint(data, j)
            value, j = data[k:k + n], k + n
        elif wt in (1, 5):
            value, j = data[j:j + (8 if wt == 1 else 4)], j + (8 if wt == 1 else 4)
        else:
            raise ValueError(f"unexpected wire type {wt}")
        if j > len(data):
            raise ValueError("truncated record")
        out.append((key >> 3, wt, value, data[i:j]))
        i = j
    return out


def _gen_steps(data: bytes) -> tuple[list[int], bytes]:
    """A model call's record (gen_metadata): the steps it made (#2, packed or not) and its execution (#4)."""
    steps: list[int] = []
    execution = b""
    for num, wt, value, _raw in _pb_fields(data):
        if num == 2 and wt == 2:
            i = 0
            while i < len(value):
                n, i = _varint(value, i)
                steps.append(n)
        elif num == 2 and wt == 0:
            steps.append(value)
        elif num == 4 and wt == 2:
            execution = value
    return steps, execution


def _db_steps(sid: str) -> tuple[dict[int, int], list[list[int]], dict[int, float]]:
    """A conversation's steps in its state database ({idx: status}), its model calls' steps, and when each step was
    made, to the microsecond (the transcript has whole seconds: a fast turn's steps would look simultaneous)."""
    import sqlite3

    db = _save_dir() / f"{sid}.db"
    if not db.exists():
        raise FileNotFoundError(f"Antigravity's state of {sid}")
    con = sqlite3.connect(db)
    times: dict[int, float] = {}
    try:
        status: dict[int, int] = {}
        for idx, st, meta in con.execute("SELECT idx, status, metadata FROM steps"):
            status[idx] = st
            made = next((v for n, wt, v, _raw in _pb_fields(bytes(meta or b"")) if n == 1 and wt == 2), None)  # created_at
            if made is not None:
                ts = {n: v for n, wt, v, _raw in _pb_fields(made) if wt == 0}  # a Timestamp: seconds, nanos
                times[idx] = ts.get(1, 0) + ts.get(2, 0) / 1e9
        gens = [_gen_steps(bytes(d or b""))[0] for (d,) in con.execute("SELECT data FROM gen_metadata ORDER BY idx")]
    except (sqlite3.Error, ValueError, IndexError) as e:
        raise ValueError(f"Antigravity's state of {sid} has a format Atrium doesn't know ({e})") from e
    finally:
        con.close()
    return status, gens, times


def _made_with(entries: list[dict[str, Any]], at: int) -> list[int]:
    """The steps of the model call that made step ``at``, from the transcript: its response, and the steps that hold
    what its tool calls returned (when the database doesn't list them)."""
    for p, _n, _c, res in reversed(_calls(entries)):
        if p["step_index"] == at or res is not None and res["step_index"] == at:
            planner = p["step_index"]
            return [planner] + [r["step_index"] for q, _n2, _c2, r in _calls(entries) if q["step_index"] == planner and r is not None]
    return [at]


# Items shown live. The runtime's stream numbers steps its own way: a response that only calls tools has no number of
# its own there (each call has one), and a resumed runtime numbers what it reloads its own way too. So the number of an
# item shown live is no step of the transcript: it is placed by its turn, through the turns' messages (numbered by the
# stream and by the transcript), and within its turn by what it is. Its id says which run of the runtime numbered it.

_STALE = "That part of the chat is out of date: open the agent again, then try again"
RUNS_KEPT = 64  # runs of a conversation's runtime whose live items can still be placed
# a tool call's kind, by its name in the transcript or on the stream (where the SDK renames some): calls are lined up by it
_TOOL_KINDS = {"write_to_file": "edit", "create_file": "edit", "replace_file_content": "edit", "multi_replace_file_content": "edit",
               "edit_file": "edit", "list_dir": "list_directory", "grep_search": "search_directory"}


@dataclass
class _Run:
    """One start of a conversation's runtime, as far as its live items can be placed in the transcript."""

    start: int  # the transcript's last message when it started (-1: none)
    turns: dict[int, int | None] = field(default_factory=dict)  # its turns' messages: their number -> their step (None: not known)
    marks: dict[int, tuple[str, str]] = field(default_factory=dict)  # replies and tool calls: number -> ("text", digest) | ("tool", kind)
    running: int | None = None  # the turn in progress (its message's number)

    def copy(self) -> "_Run":
        return _Run(self.start, dict(self.turns), dict(self.marks), self.running)

    def ended(self, live: int, step: int | None) -> None:
        """Turn ``live`` ended, the transcript's last message being ``step``: its own, if it came after the turn before's."""
        known = [s for i, s in self.turns.items() if s is not None and i != live]
        self.turns[live] = step if step is not None and step > max(known, default=self.start) else None
        self.running = None


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _tool_kind(name: str, server: str | None = None) -> str:
    return f"mcp:{server}:{name}" if server else _TOOL_KINDS.get(name, name)


def _call_kind(c: dict[str, Any]) -> str:
    """A transcript tool call's kind (``_tool_kind``)."""
    name = str(c.get("name") or "")
    if name == "call_mcp_tool":
        a = _targs(c.get("args"))
        return _tool_kind(str(a.get("ToolName") or ""), str(a.get("ServerName") or "") or None)
    return _tool_kind(name)


def _turn_message(run: _Run, prompts: list[int], n: int) -> int | None:
    """The transcript step of the message of the turn that live item ``n`` of ``run`` is in (None: not known)."""
    opened = [live for live in run.turns if live <= n]
    if not opened:
        return None
    live = max(opened)
    if run.turns[live] is None and live == run.running:  # its message is the first after the run's last known
        return next((u for u in prompts if u > max([s for s in run.turns.values() if s is not None], default=run.start)), None)
    return run.turns[live]


def _after_live(run: _Run | None, prompts: list[int], n: int) -> int | None:
    """The first of the user's messages after live item ``n`` of ``run`` (None: none yet)."""
    if run is None:  # numbered by a run that is gone (or before runs were told apart)
        raise ValueError(_STALE)
    step = _turn_message(run, prompts, n)
    if step is None:
        raise ValueError("Couldn't find that point of the conversation in its transcript")
    return next((u for u in prompts if u > step), None)


def _live_step(run: _Run | None, entries: list[dict[str, Any]], prompts: list[int], n: int) -> int | None:
    """The transcript step of live item ``n`` of ``run``, a reply or a tool call (None: not found, or not written yet).
    Within its turn, a reply is found by its text, a tool call by its place among the turn's calls of its kind: the
    transcript's are matched in order, those the stream didn't show skipped."""
    if run is None:
        raise ValueError(_STALE)
    mark, step = run.marks.get(n), _turn_message(run, prompts, n)
    if mark is None or step is None:
        return None
    first = max(live for live in run.turns if live <= n)
    end = next((u for u in prompts if u > step), None)
    turn = [d for d in entries if d["step_index"] > step and (end is None or d["step_index"] < end)]
    mine = [m for i, m in sorted(run.marks.items()) if first < i <= n and m[0] == mark[0]]  # the turn's, up to it
    if mark[0] == "text":
        found = [d["step_index"] for d in turn if d.get("type") == "PLANNER_RESPONSE" and _digest(d.get("content") or "") == mark[1]]
        k = mine.count(mark) - 1  # replies with the same text before it in its turn
        return found[k] if k < len(found) else None
    calls = [(p["step_index"], _call_kind(c)) for p, _i, c, _r in _calls(turn)]
    at, pos = None, 0
    for _tag, kind in mine:
        while pos < len(calls) and calls[pos][1] != kind:
            pos += 1
        if pos == len(calls):
            return None
        at, pos = calls[pos][0], pos + 1
    return at


def fork_cut(sid: str, anchor: str | None, user_index: int | None, after: bool, run: _Run | None = None) -> ForkPoint:
    """Where a fork of conversation ``sid`` cuts it (``Harness.fork_point``): ``cut`` is the last step kept. ``run``: the run
    that numbered ``anchor``, when it is an item shown live."""
    path = transcript_file(sid)
    entries = _entries(path) if path else []
    if not entries:
        raise ValueError("That conversation has nothing to fork yet")
    by_step = {d["step_index"]: d for d in entries}
    prompts = [d["step_index"] for d in entries if d.get("type") == "USER_INPUT" and d.get("source") == "USER_EXPLICIT"]
    m = _STEP_RE.match(anchor or "")
    live = int(m.group("step")) if m and m.group("conv") == sid else None  # an item shown live, numbered by its run
    at = int(m.group("step")) if m and m.group("conv") is None else None  # (a subagent's steps are its own conversation's)
    status, gens, times = _db_steps(sid)
    called = False  # the step called tools: its turn goes on after it, recorded yet or not (the agent may still be working)
    if after:
        if live is not None:
            at = _live_step(run, entries, prompts, live)
            if at is None and run is not None and run.running is not None and live >= run.running:
                raise ValueError("That step is still running: wait for its tools to finish, or fork from an earlier one")
        if at is None or at not in by_step:  # (a step is written to the transcript once it is done)
            raise ValueError("That step is still running: wait for its tools to finish, or fork from an earlier one"
                             if at is not None and at in status and status[at] not in _FINISHED
                             else "Couldn't find that point of the conversation in its transcript")
        # the model call that made the step: its response and the results of the tools it called
        made = next((g for g in gens if at in g), None) or _made_with(entries, at)
        cut = max(made)
        if any(status.get(i) not in _FINISHED for i in made):
            raise ValueError("That step is still running: wait for its tools to finish, or fork from an earlier one")
        called = any((by_step.get(i) or {}).get("tool_calls") for i in made)
    else:
        if live is not None:
            nxt = _after_live(run, prompts, live)
        else:
            if at is None and user_index and user_index < len(prompts):  # nothing before it to find it by (or no step: an Atrium tool's)
                if any(d.get("type") == COMPACTED for d in entries):  # counted from where the chat starts: the start, or a compaction
                    raise ValueError("Couldn't find that message in the transcript: fork from one of the agent's steps instead")
                at = prompts[user_index] - 1
            if at is None or at < 0:
                raise ValueError("Couldn't find that point of the conversation in its transcript")
            nxt = next((u for u in prompts if u > at), None)
        cut = nxt - 1 if nxt is not None else max(by_step)
    later = [i for i in by_step if i > cut]
    nxt_step = min(later) if later else None
    mid_turn = nxt_step not in prompts if nxt_step is not None else called
    turn = max((u for u in prompts if u <= cut), default=None) if mid_turn else None

    def when(i: int | None) -> float | None:
        return None if i is None else times.get(i) or iso_to_epoch((by_step.get(i) or {}).get("created_at"))

    return ForkPoint(cut=str(cut), cut_ts=when(cut), next_ts=when(nxt_step), turn_ts=when(turn))


def edit_cut(sid: str, item: str | None, anchor: str | None, user_index: int, run: _Run | None = None) -> ForkPoint:
    """Where editing one of the user's messages cuts conversation ``sid`` (``Harness.edit_point``): just before
    that message's step; ``drop`` is that step, ``cut`` None if nothing comes before it. ``run``: the run that numbered
    ``anchor``, when it is an item shown live."""
    path = transcript_file(sid)
    steps = _steps(path)
    prompts = sorted(steps.prompts) if steps else []
    m, a = _STEP_RE.match(item or ""), _STEP_RE.match(anchor or "")
    target = int(m.group("step")) if m and m.group("conv") is None and int(m.group("step")) in prompts else None
    if target is None and anchor:  # sent live, the message has an id of Atrium's: the first prompt after the anchor
        if a and a.group("conv") == sid:
            target = _after_live(run, prompts, int(a.group("step")))
        elif a and a.group("conv") is None:
            target = next((u for u in prompts if u > int(a.group("step"))), None)
    elif target is None and user_index < len(prompts):  # nothing before it to find it by (0: the conversation's first)
        first = min(steps.compactions, default=None) if steps else None
        if first is None or prompts[user_index] < first:  # (after one, the chat shows that compaction before it)
            target = prompts[user_index]
    if target is None or path is None or steps is None:
        raise ValueError("Couldn't find that message in the transcript")
    kept = max((i for i in steps.lines if i < target), default=None)
    return ForkPoint(cut=str(target - 1) if kept is not None else None,
                     cut_ts=iso_to_epoch(_step_at(path, steps, kept).get("created_at")) if kept is not None else None,
                     next_ts=steps.prompts[target][0], drop=str(target))


def cut_conversation(sid: str, cut: int | None, backup: Path) -> bool:
    """Make stopped conversation ``sid`` end after step ``cut``. What the cut changes goes to ``backup`` first: its state
    database, its transcripts and the chunks of them written again. The rest of its data directory stays as it is, and
    isn't copied: over NFS, its hundreds of files took most of an edit's time. With ``cut`` None nothing is kept: both
    are moved there, and it starts over under its id. Returns whether anything is left."""
    db, brain = _save_dir() / f"{sid}.db", _brain_dir() / sid
    backup.mkdir(parents=True, exist_ok=True)
    if cut is None:
        for p in [*_save_dir().glob(f"{sid}.db*"), brain]:
            if p.exists():
                shutil.move(p, backup / p.name)
        return False
    if not db.exists():
        raise FileNotFoundError(f"Antigravity's state of {sid}")
    _backup_state(db, backup / db.name)
    _cut_in_place(db, cut)
    if brain.is_dir():
        _cut_transcripts(_logs(sid), _logs(sid), sid, sid, cut, backup=backup / brain.name / _logs(sid).relative_to(brain))
    return True


def new_conversation_id(like: str) -> str:
    """A fresh conversation id of the same length as ``like`` (the runtime takes 32+ of [a-zA-Z0-9-]): the id is
    swapped byte for byte in the copied records."""
    out = uuid.uuid4().hex
    while len(out) < len(like):
        out += uuid.uuid4().hex
    return out[: max(len(like), 32)]


def copy_conversation(sid: str, new: str, cut: int | None, cwd: str, target_cwd: str, title: str) -> None:
    """Copy conversation ``sid`` to ``new`` — its state, its data directory with the transcripts, Atrium's index —
    keeping the steps up to ``cut`` (None: all of them), working in ``target_cwd``."""
    if len(new) != len(sid):
        raise ValueError("a copy's id must have the length of the original's")
    db, dest = _save_dir() / f"{sid}.db", _save_dir() / f"{new}.db"
    if not db.exists():
        raise FileNotFoundError(f"Antigravity's state of {sid}")
    brain, new_brain = _brain_dir() / sid, _brain_dir() / new
    tmp = _save_dir() / f".{new}.db.tmp"
    try:
        _copy_state(db, tmp, sid.encode(), new.encode(), cut)
        os.replace(tmp, dest)
        if brain.is_dir():  # (its transcripts written as they are cut)
            shutil.copytree(brain, new_brain, symlinks=True, ignore=_without_transcripts(_logs(sid)))
            _cut_transcripts(_logs(sid), _logs(new), sid, new, cut)
        rewrite_workspace(new, cwd, target_cwd)
        write_index(new, cwd=os.path.realpath(target_cwd), created_at=time.time(), title=title, forked_from=sid)
    except BaseException:
        for p in _save_dir().glob(f"{new}.db*"):
            p.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
        shutil.rmtree(new_brain, ignore_errors=True)
        _index_path(new).unlink(missing_ok=True)
        raise


def _copy_state(db: Path, dest: Path, old: bytes, new: bytes, cut: int | None) -> None:
    """Copy a conversation's state database (consistently, even while its runtime writes it), cut after step ``cut``,
    under the new id."""
    import sqlite3

    dest.unlink(missing_ok=True)
    src = sqlite3.connect(db)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        src.close()
        with out:
            if cut is not None:
                _cut_state(out, cut)
            tables = {name for (name,) in out.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table, key, cols in (("steps", "idx", ("metadata", "error_details", "permissions", "task_details", "render_info", "step_payload")),
                                     ("gen_metadata", "idx", ("data",)), ("executor_metadata", "idx", ("data",)),
                                     ("parent_references", "idx", ("data",)), ("battle_mode_infos", "idx", ("data",)),
                                     ("trajectory_metadata_blob", "id", ("data",))):
                if table not in tables:
                    continue
                for row in out.execute(f"SELECT {key}, {', '.join(cols)} FROM {table}").fetchall() if old != new else []:
                    changed = {c: bytes(v).replace(old, new) for c, v in zip(cols, row[1:]) if isinstance(v, bytes) and old in v}
                    if changed:
                        out.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in changed)} WHERE {key} = ?", (*changed.values(), row[0]))
            if old != new:
                out.execute("UPDATE trajectory_meta SET trajectory_id = ? WHERE trajectory_id = ?", (new.decode(), old.decode()))
                out.execute("UPDATE trajectory_meta SET cascade_id = ? WHERE cascade_id = ?", (new.decode(), old.decode()))
        out.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except (sqlite3.Error, ValueError, IndexError) as e:
        raise ValueError(f"Antigravity's state ({db.name}) has a format Atrium doesn't know ({e})") from e
    finally:
        src.close()
        out.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{dest}{suffix}").unlink(missing_ok=True)


def _cut_state(con: Any, cut: int) -> None:
    """Take every step after ``cut`` out of a state database, with the model calls that made them, and end the
    execution the cut falls in there."""
    con.execute("DELETE FROM steps WHERE idx > ?", (cut,))
    kept: dict[bytes, int] = {}  # execution -> model calls kept
    for idx, data in con.execute("SELECT idx, data FROM gen_metadata").fetchall():
        steps, execution = _gen_steps(bytes(data or b""))
        if steps and min(steps) > cut:
            con.execute("DELETE FROM gen_metadata WHERE idx = ?", (idx,))
        elif steps and max(steps) > cut:
            raise ValueError(f"step {cut} is in the middle of a model call")
        else:
            kept[execution] = kept.get(execution, 0) + 1
    for idx, data in con.execute("SELECT idx, data FROM executor_metadata").fetchall():
        fields = _pb_fields(bytes(data or b""))
        get = {num: value for num, _wt, value, _raw in fields}
        if int(get.get(3) or 0) <= cut:
            continue
        calls = kept.get(get.get(9) or b"", 0)
        if not calls:
            con.execute("DELETE FROM executor_metadata WHERE idx = ?", (idx,))
            continue
        set_to = {1: _CANCELED, 2: calls, 3: cut}  # cut short, as a stop would have
        out = b"".join(_put_varint(num << 3) + _put_varint(set_to[num]) if num in set_to and wt == 0 else raw for num, wt, _v, raw in fields)
        out += b"".join(_put_varint(num << 3) + _put_varint(v) for num, v in set_to.items() if num not in get)
        con.execute("UPDATE executor_metadata SET data = ? WHERE idx = ?", (out, idx))


def _backup_state(db: Path, dest: Path) -> None:
    """Copy a stopped conversation's state database: its log folded into it, then the file as it is (SQLite's own backup
    goes page by page, five times slower over NFS); page by page if something still has it open."""
    import sqlite3

    con = sqlite3.connect(db)
    try:
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()  # (a journal left behind is rolled back)
        busy = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    except sqlite3.Error:
        busy = 1
    finally:
        con.close()
    if busy:
        _copy_state(db, dest, b"", b"", None)
    else:
        shutil.copyfile(db, dest)


def _cut_in_place(db: Path, cut: int) -> None:
    """Take every step after ``cut`` out of a stopped conversation's state database, in one transaction (``_cut_state``)."""
    import sqlite3

    con = sqlite3.connect(db)
    try:
        with con:
            _cut_state(con, cut)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except (sqlite3.Error, ValueError, IndexError) as e:
        raise ValueError(f"Antigravity's state ({db.name}) has a format Atrium doesn't know ({e})") from e
    finally:
        con.close()


_TRANSCRIPTS: tuple[str, ...] = ("transcript_full", "transcript")


def _without_transcripts(logs: Path) -> Callable[[str, list[str]], set[str]]:
    """``shutil.copytree``'s ``ignore`` for a data directory whose transcripts and their chunks ``_cut_transcripts`` writes."""

    def ignore(d: str, names: list[str]) -> set[str]:
        if d == str(logs):
            return {f"{n}.jsonl" for n in _TRANSCRIPTS} & set(names)
        return set(_TRANSCRIPTS) & set(names) if d == str(logs / "chunks") else set()

    return ignore


def _cut_transcripts(src: Path, dest: Path, old: str, new: str, cut: int | None, backup: Path | None = None) -> None:
    """Write the transcripts in logs directory ``src`` (and their chunks) to ``dest``, holding the steps up to ``cut``,
    under the new id. ``dest`` is ``src`` for a conversation cut in place: a transcript then starts as it did up to the
    first line it drops, and only the chunks from there on are written again, what they and it held kept in ``backup``."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in _TRANSCRIPTS:
        path = src / f"{name}.jsonl"
        if not path.exists():
            continue
        raw = path.read_bytes()
        kept: list[bytes] = []
        same = len(raw)  # how much of it the new one starts with as it is
        pos = 0
        for line in raw.splitlines(keepends=True):
            whole = line.endswith(b"\n")  # else cut short as it was written (its runtime was stopped): kept if it is all there
            head = _HEAD_RE.match(line) if whole else None
            d = None if head else _step_of(line)
            step = int(head.group(1)) if head else d["step_index"] if d else None
            if step is not None and (cut is None or step <= cut):
                line_ = line if whole else line + b"\n"  # (what the runtime writes next starts a line of its own)
                kept.append(line_.replace(old.encode(), new.encode()) if old != new else line_)
            if step is None or cut is not None and step > cut or not whole:
                same = min(same, pos)
            pos += len(line)
        data = b"".join(kept)
        if dest == src and data == raw:
            continue  # nothing of it is dropped
        if old != new:
            same = 0
        chunks, out = src / "chunks" / name, dest / "chunks" / name
        have = {int(f.stem) for f in out.glob("*.jsonl") if f.stem.isdigit()} if out.is_dir() else set()
        first = min([same // _CHUNK, *(n for n in range(same // _CHUNK) if n not in have)])  # the first chunk that changes
        if backup is not None:
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, backup / path.name)
            for n in sorted(n for n in have if n >= first):
                (backup / "chunks" / name).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(out / f"{n:08d}.jsonl", backup / "chunks" / name / f"{n:08d}.jsonl")
        tmp = dest / f".{path.name}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, dest / path.name)
        if chunks.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            end = -(-len(data) // _CHUNK)  # how many it has now
            for n in sorted(n for n in have if n >= end):
                (out / f"{n:08d}.jsonl").unlink(missing_ok=True)
            for n in range(first, end):
                (out / f"{n:08d}.jsonl").write_bytes(data[n * _CHUNK : (n + 1) * _CHUNK])


# --------------------------------------------------------------------------
# Live session
# --------------------------------------------------------------------------


def _int_args(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Whole numbers back to ints where the schema wants an integer (the runtime sends floats)."""
    props = schema.get("properties") or {}
    return {k: int(v) if isinstance(v, float) and v.is_integer() and (props.get(k) or {}).get("type") == "integer" else v for k, v in args.items()}


def _inside(path: str | None, root: str) -> bool:
    if not path:
        return False
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


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


def _mcp_allowed(server: str, tool: str, rules: list[str]) -> bool:
    """Whether a Claude-style allow rule names an MCP server's tool (``mcp__<server>__<tool>``) or the whole
    server (``mcp__<server>``, ``mcp__<server>__*``)."""
    whole = f"mcp__{server}"
    return any(rule in (f"{whole}__{tool}", whole, f"{whole}__*") for rule in rules)


class AntigravityLiveSession(LiveSession):
    def __init__(self, harness: "AntigravityHarness", spec: LiveSpec, emit: Emit):
        self.harness = harness
        self.spec = spec
        self.emit = emit
        self.session_id = spec.session_id or uuid.uuid4().hex  # the runtime takes ids of 32+ [a-zA-Z0-9-]
        self.state = "starting"
        self.model = spec.model or None
        self.effort = spec.effort or None
        self.mode = spec.permission_mode or "always-proceed"
        self._agent: Any = None
        self._choice = resolve_model(self.model)  # the model and its endpoint, as of the runtime's start
        self._stale = False  # model / effort changed: the runtime restarts before the next turn
        self._turn: asyncio.Task[None] | None = None
        self._queue: list[Any] = []  # messages sent while a turn runs
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._remembered: set[str] = set()  # approvals the user gave for the rest of the session
        self._shown: set[str] = set()  # finished text steps already emitted
        self._tool_seen: dict[str, str] = {}  # tool step -> what was last emitted for it (steps repeat)
        self._atrium_steps: dict[str, list[str]] = {}  # one of Atrium's tools -> the steps of its calls the gate let through
        self._errored = False  # this turn already showed an error step
        self._message: int | None = None  # this turn's message as the stream numbers it
        self._run_id, self._run = "", _Run(-1)  # the runtime's current run: its items' ids end with @<run id> (_Run)
        self._closed = False
        self._atrium = {f"{ns}_{t.name}" for ns, specs in spec.toolsets.items() for t in specs}  # names here
        self._tools = [self._python_tool(ns, t) for ns, specs in spec.toolsets.items() for t in specs]

    # ---- public API --------------------------------------------------------
    async def start(self) -> None:
        try:
            await self._open()
        except Exception as e:
            self._set_state("error")
            raise RuntimeError(f"Antigravity could not start: {e}") from e
        if not _index_path(self.session_id).exists():
            write_index(self.session_id, cwd=self.spec.cwd, created_at=time.time())
        self._connected("Connected")
        self._set_state("idle")

    async def send(self, text: str, attachments: list[Attachment] | None = None) -> None:
        content = self._content(text, attachments or [])
        if self._turn and not self._turn.done():
            self._queue.append(content)  # runs when the turn ends (the runtime takes one message per turn)
            return
        self._turn = asyncio.create_task(self._turns(content), name=f"antigravity-{self.session_id[:8]}")

    async def interrupt(self) -> None:
        if self._agent is not None and self._turn and not self._turn.done():
            await self._agent.conversation.connection.cancel()

    async def set_permission_mode(self, mode: str) -> None:
        self.mode = mode  # the pre-tool hook reads it for every call

    async def set_model(self, model: str | None) -> None:
        if (model or None) != self.model:
            self.model, self._stale = model or None, True

    async def set_effort(self, effort: str | None) -> None:
        if (effort or None) != self.effort:
            self.effort, self._stale = effort or None, True

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
                fut.set_result({"behavior": "deny", "message": "Session closed", "interrupt": True})
        if self._turn and not self._turn.done():
            try:
                await asyncio.wait_for(self.interrupt(), timeout=5)
            except Exception:
                pass
            self._turn.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._turn), timeout=5)
            except BaseException:
                pass
        await self._shut()
        run, live = self._run, self._run.running
        if live is not None:  # a turn cut short (cancelled, it didn't end): its message, if it was written
            run.ended(live, await asyncio.to_thread(_last_prompt, transcript_file(self.session_id)))
        self._set_state("closed")

    # ---- runtime -----------------------------------------------------------
    def _config(self) -> Any:
        from google.antigravity import CapabilitiesConfig, LocalAgentConfig
        from google.antigravity.hooks import policy
        from google.antigravity.types import BuiltinTools, SessionContinuationMode

        spec = self.spec
        disabled = [BuiltinTools.SCHEDULE]  # wake-ups only work while Atrium keeps the runtime open
        if "WebSearch" in spec.disallowed_tools:  # Atrium's web tools replace the built-in ones
            disabled.append(BuiltinTools.SEARCH_WEB)
        if "WebFetch" in spec.disallowed_tools:
            disabled.append(BuiltinTools.READ_URL_CONTENT)
        notes = [spec.system_append, workspace_rules(spec.cwd)]
        if self._atrium:
            notes.append(TOOL_NAMES_NOTE.format(examples=", ".join(f"`{n}` is `{n.replace('_', '.', 1)}`" for n in sorted(self._atrium))))
        return LocalAgentConfig(
            system_instructions="\n\n".join(n for n in notes if n) or None,
            capabilities=CapabilitiesConfig(disabled_tools=disabled),
            # every call is allowed by policy and decided by the pre-tool hook (mode, approvals)
            policies=[policy.allow_all()],
            hooks=[self.harness.gate_hook(self)],
            tools=self._tools,
            mcp_servers=mcp_configs(spec.extra_mcp_servers),
            models=model_targets(self.model, self.effort, spec.capture, self.session_id),
            workspaces=[spec.cwd],
            conversation_id=self.session_id,
            session_continuation_mode=SessionContinuationMode.CREATE_OR_RESUME,
            save_dir=str(_save_dir()),
            app_data_dir=str(_data_dir()),
        )

    async def _open(self) -> None:
        from google.antigravity import Agent

        _save_dir().mkdir(parents=True, exist_ok=True)
        _brain_dir().mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_warm_runtime)
        self._choice = resolve_model(self.model)  # the settings file may have changed since
        start = await asyncio.to_thread(_last_prompt, transcript_file(self.session_id))
        self._run_id, self._run = self.harness.new_run(self.session_id, -1 if start is None else start)
        agent = Agent(self._config())
        await agent.__aenter__()
        self._agent = agent

    async def _shut(self) -> None:
        agent, self._agent = self._agent, None
        if agent is None:
            return
        conn = agent.conversation.connection if agent.is_started else None
        proc = getattr(conn, "_process", None)
        # the SDK waits for the runtime to exit, blocking: make sure it does, soon
        guard = threading.Timer(5, lambda: proc.poll() is None and proc.kill()) if proc else None
        if guard:
            guard.start()
        try:
            await agent.__aexit__(None, None, None)
        except Exception as e:
            log.debug("closing Antigravity session %s: %s", self.session_id, e)
        finally:
            if guard:
                guard.cancel()

    def _lid(self, step_id: str) -> str:
        """The id of the chat item a step of the stream shows as: its own, and the run that numbered it."""
        return f"{step_id}@{self._run_id}"

    def _alive(self) -> bool:
        proc = getattr(self._agent.conversation.connection, "_process", None) if self._agent is not None else None
        return proc is None or proc.poll() is None

    def _connected(self, what: str) -> None:
        c = self._choice
        mode = next((m["label"] for m in PERMISSION_MODES if m["id"] == self.mode), self.mode)
        via = f" via {urllib.parse.urlsplit(c.base_url).netloc}" if c.base_url else ""
        self.emit({
            "kind": "system", "id": f"init-{self.session_id}-{int(time.time() * 1000)}", "subtype": "init",
            "text": f"{what} · Antigravity · model {c.model}{via}" + (f" · effort {self.effort}" if self.effort else "") + f" · {mode}",
            "model": c.model, "effort": self.effort, "permission_mode": self.mode,
        })

    def _content(self, text: str, attachments: list[Attachment]) -> list[Any]:
        from google.antigravity.types import Image

        # every attachment is listed (images too, so history can show them); images also go inline, if the model takes them
        parts: list[Any] = [(text or ("(see attached image)" if attachments else "")) + attachment_note(attachments)]
        for a in attachments:
            if a.is_image and self._choice.images:
                parts.append(Image(data=Path(a.path).read_bytes(), mime_type=a.mime))
        return parts

    async def _turns(self, content: Any) -> None:
        while content is not None and not self._closed:
            await self._turn_once(content)
            if self.state == "error":
                return
            content = self._queue.pop(0) if self._queue else None
        if not self._closed:
            self._set_state("waiting" if self._pending else "idle")

    async def _turn_once(self, content: Any) -> None:
        from google.antigravity import types

        started = time.time()
        self._set_state("running")
        self._errored, self._message = False, None
        subtype, error = "success", None
        try:
            if self._stale or self._agent is None:
                self._stale = False
                await self._shut()
                await self._open()
                self._connected("Reconnected")
            assert self._agent is not None
            conv = self._agent.conversation
            await conv.send(content)
            async for step in conv.receive_steps():
                try:
                    self._on_step(step)
                except Exception:
                    log.exception("Antigravity step not shown")
        except types.AntigravityCancelledError:
            subtype = "interrupted"
            self.emit({"kind": "system", "id": f"interrupted-{uuid.uuid4().hex[:8]}", "subtype": "interrupted", "text": "Interrupted"})
        except Exception as e:
            subtype, error = "error", str(e) or type(e).__name__
            log.warning("Antigravity turn failed in %s: %s", self.session_id, error)
            if not self._errored:  # else the error step said it already
                self.emit({"kind": "error", "id": f"err-{uuid.uuid4().hex[:8]}", "text": error})
            self._stale = True  # a failed turn can leave the runtime's connection closed: start afresh (and re-read the settings)
        if self._message is not None:  # where its message is in the transcript: the last one there
            run, live = self._run, self._message
            run.ended(live, await asyncio.to_thread(_last_prompt, transcript_file(self.session_id)))
        used = await asyncio.to_thread(_last_usage, transcript_file(self.session_id))
        if used:
            self.emit({"kind": "usage", "id": "usage", "used": used, "max": self._choice.window})
        stop = getattr(getattr(self._agent, "conversation", None), "connection", None)
        reason = str(getattr(getattr(stop, "_last_turn_stop_reason", None), "value", "") or "") if stop else ""
        self.emit({
            "kind": "result", "id": f"result-{uuid.uuid4().hex[:8]}", "subtype": subtype, "is_error": subtype == "error",
            "duration_ms": int((time.time() - started) * 1000), "cost_usd": None, "turns": None, "text": error,
            "terminal_reason": reason if reason and reason != "UNSPECIFIED" else subtype,
        })
        if subtype == "error" and not self._alive():
            self._set_state("error")  # the runtime is gone: the next message starts a new one

    def _on_step(self, step: Any) -> None:
        from google.antigravity.types import StepSource, StepStatus, StepType

        if step.depth or step.parent_trajectory_id:
            return  # a subagent's own steps: its invoke_subagent call stands for them
        if step.source == StepSource.USER:
            if self._message is None:
                self._message = self._run.running = step.step_index
                self._run.turns[step.step_index] = None
            return  # Atrium showed the message when it was sent
        status, lid = step.status, self._lid(step.id)
        if step.type == StepType.TOOL_CALL:
            call = step.tool_calls[0] if step.tool_calls else None
            if call is not None:
                self._run.marks.setdefault(step.step_index, ("tool", _tool_kind(str(call.name), call.server_name)))
            if call is None or not call.server_name and str(call.name) in self._atrium:
                return  # Atrium's own tools show themselves (see _python_tool); an MCP server's may share their names
            view = step_view(call)
            if view is None:
                return
            shown = json.dumps(view, sort_keys=True, default=str)
            if self._tool_seen.get(lid) != shown:
                self._tool_seen[lid] = shown
                self.emit({"kind": "tool", "id": lid, "name": view[0], "input": view[1]})
            if status in (StepStatus.DONE, StepStatus.ERROR, StepStatus.CANCELED):
                content, err = step_result(call, step)
                self.emit({"kind": "tool_result", "id": lid, "content": content, "is_error": err})
            return
        if step.type == StepType.COMPACTION:
            if status == StepStatus.DONE and lid not in self._shown:
                self._shown.add(lid)
                for it in compaction_items(lid, step.content or ""):
                    self.emit(it)
            return
        if status == StepStatus.ERROR:
            error = step.error or step.content or "Error"
            if "retryable error" in error:  # the runtime tries the call again
                self.emit(retry_item(lid, error))
            elif "denied by pre-tool hook" not in error:  # refusals show on their approval card
                self._errored = True
                self.emit({"kind": "error", "id": lid, "text": error})
            return
        if step.source != StepSource.MODEL or step.type not in (StepType.TEXT_RESPONSE, StepType.THINKING):
            return
        if status == StepStatus.ACTIVE and step.content_delta:
            self.emit({"kind": "delta", "text": step.content_delta})
        if status == StepStatus.DONE and lid not in self._shown:
            self._shown.add(lid)  # chat items are shown once: thinking and text when the step ends
            if step.thinking.strip():
                self.emit({"kind": "thinking", "id": f"{lid}:think", "text": step.thinking})
            if step.content.strip():
                self._run.marks[step.step_index] = ("text", _digest(step.content))
                self.emit({"kind": "text", "id": lid, "text": step.content})

    # ---- tools and approvals -----------------------------------------------
    def _python_tool(self, ns: str, spec: ToolSpec) -> Any:
        from google.antigravity.tools.tool_runner import ToolWithSchema

        shown = f"mcp__{ns}__{spec.name}"

        async def call(**kwargs: Any) -> str:
            args = _int_args(_plain(kwargs), spec.input_schema)
            queued = self._atrium_steps.get(f"{ns}_{spec.name}")
            tid = queued.pop(0) if queued else f"t-{uuid.uuid4().hex[:12]}"  # its step, as the other tools' items have
            self.emit({"kind": "tool", "id": tid, "name": shown, "input": args})
            try:
                out = await spec.handler(args)
            except Exception as e:  # reported to the model instead of failing the turn
                out = {"text": f"Error: {e}", "is_error": True}
            text, err = (str(out.get("text", "")), bool(out.get("is_error"))) if isinstance(out, dict) else (str(out), False)
            self.emit({"kind": "tool_result", "id": tid, "content": clip(text), "is_error": err})
            return text

        tool = ToolWithSchema(call, spec.input_schema)
        tool.__name__ = f"{ns}_{spec.name}"
        tool.__doc__ = spec.description
        return tool

    async def decide(self, tc: Any) -> tuple[bool, str]:
        """The pre-tool hook: (allow, why not) for a tool call, asking the user when the mode says so. An MCP
        server's tool (``server_name``) is never taken for a built-in or one of Atrium's by its name: only the
        mode, an allow rule (``mcp__<server>__<tool>``) or the user lets it run."""
        name, args = str(tc.name), dict(tc.args or {})
        mcp = f"mcp__{tc.server_name}__{name}" if tc.server_name else None
        if not mcp and name in self._atrium and tc.step_id:
            self._atrium_steps.setdefault(name, []).append(self._lid(str(tc.step_id)))  # the item its call shows as (see _python_tool)
        if self.mode == "always-proceed" or not mcp and (name in self._atrium or name in READ_TOOLS):
            return True, ""
        command = None if mcp or "CommandLine" not in args else str(args["CommandLine"])
        rules = self.spec.allowed_tools
        if mcp and _mcp_allowed(str(tc.server_name), name, rules) or command is not None and _command_allowed(command, rules):
            return True, ""
        if self.mode == "read-only":
            return False, "Not allowed: this agent only reads (it may run the read-only commands it was given)."
        key = mcp or (f"{name}:{command}" if command is not None else name)
        if key in self._remembered:
            return True, ""
        path = None if mcp else tc.canonical_path or args.get("TargetFile") or args.get("AbsolutePath")
        edit = not mcp and name in EDIT_TOOLS
        if self.mode == "accept-edits" and edit and _inside(path, self.spec.cwd):
            return True, ""
        rid = tc.id or f"perm-{uuid.uuid4().hex[:10]}"
        while rid in self._pending:
            rid += "+"
        tool, inp = (mcp, mcp_args(args)) if mcp else call_view(name, args)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.emit({"kind": "permission", "id": rid, "tool": tool, "input": inp, "can_remember": True,
                   "blocked_path": path if edit and not _inside(path, self.spec.cwd) else None})
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
            if decision.get("remember"):
                self._remembered.add(key)
            return True, ""
        if decision.get("interrupt") and not self._closed:
            asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(self.interrupt()))
        return False, decision.get("message") or "The user declined this action."

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit({"kind": "state", "state": state})


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class AntigravityHarness(Harness):
    id = "antigravity"
    label = "Antigravity"
    permission_modes = PERMISSION_MODES
    git_agent_mode = "request-review"  # reads freely; the Git agent's read-only git commands are allowed (allowed_tools)
    review_agent_mode = "read-only"  # not offered in the picker: reads and the pre-approved commands, refuses the rest unasked
    models = [{"id": "", "label": "Default"}]
    efforts = EFFORTS
    can_edit = True

    def __init__(self) -> None:
        self._prompts: dict[str, str] = {}  # transcript -> its first message
        self._cwds: dict[str, str | None] = {}  # session id -> cwd (index)
        self._runs: dict[str, dict[str, _Run]] = {}  # session id -> the latest runs of its runtime (RUNS_KEPT), by id

    def available(self) -> tuple[bool, str | None]:
        if not _defaults().get("enabled", True):
            return False, "Disabled in Atrium settings (harnesses.antigravity.enabled)."
        problem = _sdk_problem()
        if problem:
            return False, f"The Antigravity SDK isn't usable: {problem}. Run `make install` (it installs google-antigravity)."
        cred = credentials_problem()
        return (False, cred) if cred else (True, None)

    async def catalog(self) -> dict[str, Any]:
        d = self.describe()
        c = resolve_model(None)  # what a session gets when Atrium doesn't pick: the settings file's model
        d["models"] = [{"id": "", "label": f"Default ({c.label})", "resolves_to": c.model, "efforts": EFFORTS}] + [
            {"id": key, "label": str(m.get("label") or key), "efforts": EFFORTS} for key, m in custom_models().items()]
        d["config_model"], d["config_effort"] = c.model, None
        return d

    # ---- settings ----------------------------------------------------------
    def config_files(self, project_dir: str | None) -> list[Any]:
        from ..configfiles import ConfigFile

        return [ConfigFile(
            id="antigravity-user", label="Antigravity", path=str(cli_settings_path()), language="json", scope="user", harness=self.id,
            hint="The Antigravity CLI's settings: model, custom models (a relay: customModelsConfig), and \"env\" for the "
                 "Gemini API key (GEMINI_API_KEY). Applies to sessions started afterwards.",
            secret=True,
        )]

    def on_config_saved(self, file_id: str) -> None:
        _cli_cache.clear()

    def new_run(self, session_id: str, start: int) -> tuple[str, _Run]:
        """A session's runtime starts (``start``: the transcript's last message then): the run's id, and its record."""
        runs = self._runs.setdefault(session_id, {})
        rid = uuid.uuid4().hex[:8]
        runs[rid] = _Run(start)
        while len(runs) > RUNS_KEPT:
            del runs[next(iter(runs))]
        return rid, runs[rid]

    def _run_of(self, session_id: str, anchor: str | None) -> _Run | None:
        """The run that numbered chat item ``anchor``, if it is one shown live and the run is known: a copy (it is read in a
        worker thread while the stream goes on)."""
        m = _STEP_RE.match(anchor or "")
        run = self._runs.get(session_id, {}).get(m.group("run") or "") if m else None
        return run.copy() if run else None

    def gate_hook(self, session: AntigravityLiveSession) -> Any:
        from google.antigravity.hooks import hooks
        from google.antigravity.types import HookResult

        class Gate(hooks.PreToolCallDecideHook):
            async def run(self, context: Any, data: Any) -> Any:
                allow, why = await session.decide(data)
                return HookResult(allow=allow, message=why)

        return Gate()

    # ---- discovery ---------------------------------------------------------
    async def list_sessions(self, project_dir: str, roots: list[str]) -> list[SessionSummary]:
        return await asyncio.to_thread(self._list, roots)

    def _list(self, roots: list[str]) -> list[SessionSummary]:
        real = [os.path.realpath(r) for r in roots]
        out: list[SessionSummary] = []
        for f in (ag_home() / "sessions").glob("*.json"):
            sid = f.stem
            meta = read_index(sid)
            cwd = meta.get("cwd")
            if not cwd or not any(_inside(cwd, r) for r in real):
                continue
            path = transcript_file(sid)
            try:
                st = path.stat() if path else None
            except OSError:
                st = None
            if path is None or st is None:
                continue  # no message yet
            prompt = self._first_prompt(path)
            out.append(SessionSummary(
                id=sid, harness=self.id, title=meta.get("title") or short(prompt, 80) or "Untitled", updated_at=st.st_mtime,
                created_at=meta.get("created_at"), first_prompt=prompt, cwd=cwd, size=st.st_size, transcript=str(path),
            ))
        return out

    def _first_prompt(self, path: Path) -> str:
        """The conversation's first message, read up to that line; once found it never changes, so it is kept."""
        hit = self._prompts.get(str(path))
        if hit:
            return hit
        prompt = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"USER_EXPLICIT"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("type") == "USER_INPUT" and d.get("source") == "USER_EXPLICIT":
                        prompt = split_attachment_note(user_text(d.get("content") or ""))[0]
                        break
        except OSError:
            return ""
        if prompt:
            self._prompts[str(path)] = prompt
        return prompt

    def transcript_path(self, session_id: str, cwd: str) -> Path | None:
        return transcript_file(session_id)

    def watch_roots(self) -> list[Path]:
        root = _brain_dir()
        root.mkdir(parents=True, exist_ok=True)
        return [Path(os.path.realpath(root))]

    def transcript_cwd(self, path: str) -> str | None:
        rel = None
        for root in (_brain_dir(), Path(os.path.realpath(_brain_dir()))):
            try:
                rel = Path(path).relative_to(root)
                break
            except ValueError:
                continue
        if rel is None or not rel.parts:
            return None
        sid = rel.parts[0]
        if sid not in self._cwds:
            self._cwds[sid] = read_index(sid).get("cwd")  # None for a subagent's own conversation
        return self._cwds[sid]

    async def rename_session(self, session_id: str, title: str, cwd: str) -> None:
        if _index_path(session_id).exists():
            await asyncio.to_thread(write_index, session_id, title=title)

    # ---- forks: a copy of the runtime's state and data under a new id, cut after a step (see "Forks" above)
    async def fork_point(self, session_id: str, cwd: str, anchor: str | None, user_index: int | None, *, after: bool = False) -> ForkPoint:
        return await asyncio.to_thread(fork_cut, session_id, anchor, user_index, after, self._run_of(session_id, anchor))

    async def fork_edits(self, session_id: str, cwd: str, point: ForkPoint) -> list[FileEdit] | None:
        if point.cut is None or point.turn_ts is None:
            return None
        return await asyncio.to_thread(turn_edits, session_id, int(point.cut))

    async def fork_session(self, session_id: str, cwd: str, point: ForkPoint, *, target_cwd: str, title: str, note: str = "") -> str:
        # ``note`` reaches the model through the system prompt, which a resumed runtime takes anew
        new = new_conversation_id(session_id)
        cut = int(point.cut) if point.cut is not None else None
        await asyncio.to_thread(copy_conversation, session_id, new, cut, read_index(session_id).get("cwd") or cwd, target_cwd, title)
        return new

    # ---- edits: the conversation is cut in place just before the message (its old state kept in the backup)
    async def edit_point(self, session_id: str, cwd: str, item: str | None, anchor: str | None, user_index: int) -> ForkPoint:
        return await asyncio.to_thread(edit_cut, session_id, item, anchor, user_index, self._run_of(session_id, anchor))

    async def rewind_session(self, session_id: str, cwd: str, point: ForkPoint, backup: Path) -> Rewind:
        left = await asyncio.to_thread(cut_conversation, session_id, int(point.cut) if point.cut is not None else None, backup)
        self._runs.pop(session_id, None)  # (the steps after the cut are numbered anew: the chat loads the conversation again)
        return Rewind(fresh=not left, cut=True)

    async def move_session(self, session_id: str, cwd: str, target_cwd: str, note: str = "") -> None:
        # ``note`` reaches the model through the system prompt, which a resumed runtime takes anew
        if not _index_path(session_id).exists():
            raise FileNotFoundError(f"Antigravity session {session_id}")

        def move() -> None:  # the index follows only once the runtime's own state does (rewrite_workspace raises otherwise)
            rewrite_workspace(session_id, read_index(session_id).get("cwd") or cwd, target_cwd)
            write_index(session_id, cwd=os.path.realpath(target_cwd))

        await asyncio.to_thread(move)
        self._cwds.pop(session_id, None)

    async def delete_session(self, session_id: str, cwd: str) -> None:
        def delete() -> None:
            for p in _save_dir().glob(f"{session_id}.db*"):
                p.unlink(missing_ok=True)
            shutil.rmtree(_brain_dir() / session_id, ignore_errors=True)
            _index_path(session_id).unlink(missing_ok=True)

        await asyncio.to_thread(delete)
        self._cwds.pop(session_id, None)
        self._runs.pop(session_id, None)

    # ---- content -----------------------------------------------------------
    async def load_history(self, session_id: str, cwd: str) -> list[Item]:
        path = transcript_file(session_id)
        if not path:
            return []
        return await asyncio.to_thread(lambda: history_items(current_steps(path)))

    # ---- compactions: CHECKPOINT steps (the chat shows the conversation since the last, as the runtime resumes it)
    async def list_compactions(self, sessions: list[SessionSummary]) -> dict[str, list[Compaction]]:
        def scan() -> dict[str, list[Compaction]]:
            found = {s.id: compactions_of(Path(s.transcript)) for s in sessions if s.harness == self.id and s.transcript}
            return {sid: cs for sid, cs in found.items() if cs}

        return await asyncio.to_thread(scan)

    async def load_compacted_history(self, session_id: str, compaction_id: str, cwd: str) -> list[Item]:
        def load() -> list[Item]:
            path = transcript_file(session_id)
            steps = compacted_steps(path, compaction_id) if path else None
            if steps is None:
                raise FileNotFoundError(f"no compaction {compaction_id} in session {session_id}")
            return history_items(steps)

        return await asyncio.to_thread(load)

    def digest_lines(self, lines: list[str]) -> list[str]:
        steps: dict[int, dict[str, Any]] = {}  # in order, as in _entries: a planner step is written after its tools' results
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("step_index"), int):
                steps[d["step_index"]] = d
        return [e for i in sorted(steps) for e in digest_entry(steps[i])]

    def file_edits(self, session_id: str, cwd: str, since: float | None = None) -> list[FileEdit]:
        path = transcript_file(session_id)
        return file_edits_of(_entries(path)) if path else []

    # ---- execution ---------------------------------------------------------
    def create_live(self, spec: LiveSpec, emit: Emit) -> LiveSession:
        return AntigravityLiveSession(self, spec, emit)

    async def complete_json(self, *, system, prompt, schema, model, effort, cwd):
        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
        from google.antigravity.types import BuiltinTools

        started = time.time()
        scratch = tempfile.mkdtemp(prefix="atrium-antigravity-")  # one-shot: nothing kept
        try:
            await asyncio.to_thread(_warm_runtime)
            cfg = LocalAgentConfig(
                system_instructions=system, capabilities=CapabilitiesConfig(enabled_tools=[BuiltinTools.FINISH], enable_subagents=False),
                models=model_targets(model, effort), workspaces=[cwd], response_schema=schema,
                save_dir=os.path.join(scratch, "save"), app_data_dir=os.path.join(scratch, "data"),
            )
            async with Agent(cfg) as agent:
                response = await agent.chat(prompt)
                text = await response.text()
                data = agent.conversation.get_last_structured_output()
                usage = agent.conversation.connection.cumulative_usage
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if not isinstance(data, dict):
            data = parse_json_loose(text)
        return data, {"cost_usd": None, "duration_ms": int((time.time() - started) * 1000), "turns": 1, "tokens": usage.total_token_count}
