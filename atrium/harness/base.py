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
#   user        {id, text, ts?, attachments?: [{name, path, mime, url?}], command?}
#                                               ``command``: a slash command the harness ran rather
#                                               than a prompt (no turn to fork from)
#   text        {id, text}                      assistant prose (markdown)
#   command_output {id, text}                   what a slash command printed (markdown)
#   thinking    {id, text}
#   tool        {id, name, input, parent?}      a tool call; names follow Claude Code
#                                               (Bash, Edit, Write, …), Atrium tools are
#                                               mcp__<namespace>__<tool>; Codex patches
#                                               are "Patch" {changes: [{path, kind, diff}]}.
#                                               ``parent`` (also on thinking / tool_result):
#                                               made by the subagent of that Agent call
#   tool_result {id, content, is_error}         ``id`` == the tool call id
#   permission  {id, tool, input, title?, description?, can_remember}
#   permission_resolved {id, decision}
#   result      {id, subtype, is_error, duration_ms, cost_usd, turns, text?}
#   system      {id, subtype, text, detail?}   a subagent's end (subtype task_done) carries
#                                               ``tool_use_id`` (its Agent call) or ``agent_id``;
#                                               a compaction (subtype compact) its id as ``compaction``
#                                               (the part of the conversation it closed, see
#                                               ``Compaction``); ``detail``: more text, shown folded
#                                               (a compaction's summary, subtype compact_summary)
#   error       {id, text}
#   -- transient (not stored in history) --
#   delta       {text}                          streaming assistant text
#   state       {state}                         live session state change
#   task        {task_id, tool_use_id, status, description?, agent_type?}
#                                               a subagent started / ended; consumed by the
#                                               session manager (status: SUBAGENT_STATES)

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


SUBAGENT_STATES = ("running", "done", "failed", "stopped")


@dataclass
class SubagentSummary:
    """An agent spawned by a session (Claude's Agent tool, a Codex sub-agent thread).

    Subagents are listed under their top-level session, nested ones included
    (``depth`` > 1). They are read-only in Atrium: only the parent talks to them.
    """

    id: str  # agent id (Claude) / thread id (Codex)
    agent_type: str | None  # Explore, general-purpose, a Codex role…
    description: str
    status: str  # one of SUBAGENT_STATES
    updated_at: float
    created_at: float | None = None
    name: str | None = None  # Codex nickname
    tool_use_id: str | None = None  # the parent's tool call that spawned it
    model: str | None = None
    depth: int = 1


@dataclass
class Compaction:
    """A compaction of a session's context: from then on the model sees the conversation before it
    only as a summary.

    The chat shows the conversation since the last compaction (as the harness itself does on resume).
    The part each compaction closed is listed under its session, like a subagent, and opens read-only.
    """

    id: str  # Claude Code: its compact_boundary entry; Codex: its contextCompaction item
    at: float | None  # when it happened (epoch seconds)
    started_at: float | None = None  # when the part it closed began (the session's start, or the previous compaction)
    trigger: str | None = None  # "manual" (asked for) or "auto" (the context filled up), when the harness records it
    tokens: int | None = None  # the context's size just before
    first_prompt: str = ""  # the first message the user sent in the part it closed


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
class SlashCommand:
    """A command the composer offers when a message starts with "/": the harness's built-ins, custom
    commands, skills… Sent as typed ("/name args"); the harness runs it instead of prompting the model."""

    name: str  # without the prefix
    description: str = ""
    argument_hint: str = ""
    aliases: list[str] = field(default_factory=list)
    source: str | None = None  # where a custom one is defined: project, user, a plugin, skill… (None: built in)
    prefix: str = "/"  # what invokes it: "/", or "$" for Codex skills (mentioned anywhere in a message)


@dataclass
class ForkPoint:
    """Where a fork cuts a conversation: everything up to ``cut`` is kept.

    ``cut`` is harness-specific (Claude: the last kept transcript entry; Codex: the last kept
    turn); None keeps the whole conversation. The timestamps (epoch seconds) bracket the moment
    of the fork, to find the turn snapshot with the files as they were then.
    """

    cut: str | None = None
    cut_ts: float | None = None  # when the kept part ended
    next_ts: float | None = None  # when the first dropped message was sent (None: nothing dropped)


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

    def commands(self) -> list[SlashCommand] | None:
        """The slash commands this session accepts, if it knows them (None: ``Harness.slash_commands`` finds out)."""
        return None

    def is_command(self, text: str) -> bool:
        """Whether sending ``text`` runs a slash command rather than prompting the model: the message is
        then shown as a command, and it opens no turn to fork from."""
        return False

    @abstractmethod
    async def close(self) -> None: ...


class Harness(ABC):
    id: str
    label: str
    permission_modes: list[dict[str, str]] = []
    git_agent_mode: str = "default"  # the Git agent's mode: reads freely, asks before anything else
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
    async def load_history(self, session_id: str, cwd: str) -> list[Item]:
        """The conversation since the last compaction, opening with its ``compact`` item: what the model
        still sees. The parts before are ``load_compacted_history``'s."""

    async def list_compactions(self, sessions: list[SessionSummary]) -> dict[str, list[Compaction]]:
        """Compactions of these sessions (this harness's own), oldest first, keyed by session id. Called
        on every session-list request, so implementations cache what is expensive to compute."""
        return {}

    async def load_compacted_history(self, session_id: str, compaction_id: str, cwd: str) -> list[Item]:
        """The part of a conversation that a compaction closed: from the previous compaction (opening
        with its ``compact`` item) or the start, up to this one. Raises FileNotFoundError if unknown."""
        raise FileNotFoundError(f"no compaction {compaction_id} in session {session_id}")

    @abstractmethod
    def digest_lines(self, lines: list[str]) -> list[str]:
        """Compress raw transcript lines into short human-readable events (tracker input)."""

    @abstractmethod
    def file_edits(self, session_id: str, cwd: str) -> list[FileEdit]: ...

    async def list_subagents(self, sessions: list[SessionSummary], roots: list[str]) -> dict[str, list[SubagentSummary]]:
        """Subagents of these sessions (this harness's own), keyed by session id. Called on every
        session-list request, so implementations cache what is expensive to compute."""
        return {}

    async def load_subagent_history(self, session_id: str, agent_id: str, cwd: str) -> list[Item]:
        """A subagent's conversation (``agent_id`` as in ``list_subagents``)."""
        return []

    async def rename_session(self, session_id: str, title: str, cwd: str) -> None:  # optional
        return None

    async def delete_session(self, session_id: str, cwd: str) -> None:
        """Remove the session from the harness's own store (so its CLI no longer lists it either)."""
        raise NotImplementedError(f"{self.label} sessions can't be deleted from Atrium")

    async def fork_point(self, session_id: str, cwd: str, anchor: str | None, user_index: int | None) -> ForkPoint:
        """Resolve "fork before the ``user_index``-th user message" (0-based): keep the turn that
        holds chat item ``anchor`` — the last item shown before that message — and all before it."""
        raise NotImplementedError(f"{self.label} sessions can't be forked from an earlier message")

    async def fork_session(self, session_id: str, cwd: str, point: ForkPoint, *, target_cwd: str, title: str, note: str = "") -> str:
        """Copy the conversation up to ``point`` into a new session that works in ``target_cwd``.
        Returns the new id; the copy is not started — it resumes like any other session.

        ``note`` tells the fork's model where it comes from and where it now works. Atrium also
        appends it to the system prompt each time the session starts; a harness that doesn't
        apply a changed system prompt on resume (Codex) must put it into the copy's history."""
        raise NotImplementedError(f"{self.label} sessions can't be forked")

    async def move_session(self, session_id: str, cwd: str, target_cwd: str, note: str = "") -> None:
        """Make the (stopped) session resume in ``target_cwd`` instead of ``cwd`` — which may no
        longer exist — so that discovery lists it there. ``note`` tells the model what changed;
        like a fork's note, a harness that doesn't apply a changed system prompt on resume (Codex)
        must put it into the history."""
        raise NotImplementedError(f"{self.label} sessions can't be moved to another directory")

    def config_files(self, project_dir: str | None) -> list[Any]:
        """The harness's own settings files (``configfiles.ConfigFile``), edited raw in the UI."""
        return []

    def on_config_saved(self, file_id: str) -> None:
        """Hook after one of ``config_files`` was saved from the UI."""

    async def slash_commands(self, cwd: str, live: LiveSession | None = None) -> list[SlashCommand]:
        """What the composer offers after "/" for a session working in ``cwd`` (``live``: that session,
        when it runs). Commands the UI can't use (bound to the CLI's terminal) are left out."""
        return []

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
