"""Harness abstraction.

A *harness* is an agent runtime that works on a directory: Claude Code today,
Codex / pi / … later. Atrium talks to every harness through this interface so
the UI, the board, the tracker, diffs and git tooling stay harness-agnostic.

Contract summary
----------------
* Sessions are identified by an id unique within a harness and are discovered
  per *project directory* (including its git worktrees). A harness may assign
  the id itself when a session starts (Codex threads); ``LiveSession.session_id``
  is authoritative after ``start()``.
* Conversation content is exchanged as **normalized chat items** (plain dicts,
  see ``Item`` below) so the frontend renders every harness the same way.
* Atrium-provided tools (board, web, git view) are described once as
  ``ToolSpec``s grouped into namespaces (``board``, ``web``, ``graph``); each
  harness exposes them natively (in-process MCP servers for the Claude Agent
  SDK → ``mcp__board__status``; dynamic tools for Codex → ``board.status``).
* Settings are the harness's own config files (``config_files``), edited raw.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

# --------------------------------------------------------------------------
# Normalized chat items
# --------------------------------------------------------------------------
# Every item is a dict with a ``kind`` and (except transient kinds) an ``id``:
#
#   user        {id, text, ts?, attachments?: [{name, path, mime, url?}]}
#   text        {id, text}                      assistant prose (markdown)
#   thinking    {id, text}
#   tool        {id, name, input, parent?}      a tool call; names follow Claude Code
#                                               (Bash, Edit, Write, …), Atrium tools are
#                                               mcp__<namespace>__<tool>; Codex patches
#                                               are "Patch" {changes: [{path, kind, diff}]}
#   tool_result {id, content, is_error}         ``id`` == the tool call id
#   permission  {id, tool, input, title?, description?, can_remember}
#   permission_resolved {id, decision}
#   result      {id, subtype, is_error, duration_ms, cost_usd, turns, text?}
#   system      {id, subtype, text}
#   error       {id, text}
#   -- transient (not stored in history) --
#   delta       {text}                          streaming assistant text
#   state       {state}                         live session state change

Item = dict[str, Any]
Emit = Callable[[Item], None]

LIVE_STATES = ("starting", "running", "idle", "waiting", "closed", "error")


@dataclass
class ToolSpec:
    """A harness-neutral tool description (JSON-schema input, async handler).

    The handler receives the validated arguments and returns plain text (or a
    dict ``{"text": ..., "is_error": bool}``).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str | dict[str, Any]]]
    read_only: bool = False


@dataclass
class SessionSummary:
    id: str
    harness: str
    title: str
    updated_at: float  # epoch seconds
    created_at: float | None = None
    first_prompt: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    size: int | None = None
    transcript: str | None = None  # absolute path of the on-disk transcript, if any


@dataclass
class FileEdit:
    """One file modification recorded in a transcript (for per-session diffs)."""

    path: str
    tool: str
    ts: str | None
    before: str | None  # content before this edit (None = did not exist, or unknown when ``patch`` is set)
    after: str | None  # content after this edit (None = unknown / deleted)
    kind: str = "update"  # create | update | delete
    patch: str | None = None  # unified-diff hunks, when the transcript records a patch instead of contents


@dataclass
class Attachment:
    """A file attached to a user message (uploaded to ~/.atrium/uploads)."""

    path: str
    name: str
    mime: str

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


@dataclass
class LiveSpec:
    session_id: str | None  # None = new session, id assigned by the harness
    resume: bool
    cwd: str
    name: str
    role: str = "worker"  # worker | git
    model: str | None = None
    effort: str | None = None
    permission_mode: str = "default"
    system_append: str = ""
    toolsets: dict[str, list[ToolSpec]] = field(default_factory=dict)
    extra_mcp_servers: dict[str, Any] = field(default_factory=dict)
    disallowed_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)


class LiveSession(ABC):
    """A running, interactive agent session owned by Atrium."""

    session_id: str
    state: str = "starting"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def send(self, text: str, attachments: list[Attachment] | None = None) -> None: ...

    @abstractmethod
    async def interrupt(self) -> None: ...

    @abstractmethod
    async def set_permission_mode(self, mode: str) -> None: ...

    @abstractmethod
    async def set_model(self, model: str | None) -> None: ...

    @abstractmethod
    async def set_effort(self, effort: str | None) -> None: ...

    @abstractmethod
    def resolve_permission(self, request_id: str, decision: dict[str, Any]) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...


class Harness(ABC):
    id: str
    label: str
    permission_modes: list[dict[str, str]] = []
    models: list[dict[str, Any]] = []  # [{id, label, efforts?: [..], default_effort?}]
    efforts: list[str] = []  # reasoning-effort levels when a model does not list its own

    def available(self) -> tuple[bool, str | None]:
        """Whether the harness can run here (binary installed, enabled in settings…)."""
        return True, None

    # ---- discovery ---------------------------------------------------------
    @abstractmethod
    async def list_sessions(self, project_dir: str, roots: list[str]) -> list[SessionSummary]:
        """Sessions whose cwd is the project or one of its worktrees (``roots``)."""

    @abstractmethod
    def transcript_path(self, session_id: str, cwd: str) -> Path | None: ...

    def transcript_dirs(self, project_dir: str) -> list[Path]:
        """Per-project directories whose files change when this project's sessions progress."""
        return []

    def watch_roots(self) -> list[Path]:
        """Shared transcript trees (not per project); changes are mapped with ``transcript_cwd``."""
        return []

    def transcript_cwd(self, path: str) -> str | None:
        """Working directory of the session a transcript file under ``watch_roots`` belongs to."""
        return None

    # ---- content -----------------------------------------------------------
    @abstractmethod
    async def load_history(self, session_id: str, cwd: str) -> list[Item]: ...

    @abstractmethod
    def digest_lines(self, lines: list[str]) -> list[str]:
        """Compress raw transcript lines into short human-readable events (tracker input)."""

    @abstractmethod
    def file_edits(self, session_id: str, cwd: str) -> list[FileEdit]: ...

    async def rename_session(self, session_id: str, title: str, cwd: str) -> None:  # optional
        return None

    async def delete_session(self, session_id: str, cwd: str) -> None:
        """Remove the session from the harness's own store (so its CLI no longer lists it either)."""
        raise NotImplementedError(f"{self.label} sessions can't be deleted from Atrium")

    def config_files(self, project_dir: str | None) -> list[Any]:
        """The harness's own settings files (``configfiles.ConfigFile``), edited raw in the UI."""
        return []

    def on_config_saved(self, file_id: str) -> None:
        """Hook after one of ``config_files`` was saved from the UI."""

    # ---- execution ---------------------------------------------------------
    @abstractmethod
    def create_live(self, spec: LiveSpec, emit: Emit) -> LiveSession: ...

    @abstractmethod
    async def complete_json(
        self, *, system: str, prompt: str, schema: dict[str, Any], model: str | None, effort: str | None, cwd: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One-shot structured completion (used by the tracker). Returns (data, usage_info)."""

    async def catalog(self) -> dict[str, Any]:
        """``describe()`` plus anything that must be fetched (e.g. the model list)."""
        return self.describe()

    def describe(self) -> dict[str, Any]:
        ok, reason = self.available()
        return {
            "id": self.id,
            "label": self.label,
            "available": ok,
            "unavailable_reason": reason,
            "permission_modes": self.permission_modes,
            "models": self.models,
            "efforts": self.efforts,
        }
