"""Board tools handed to every session agent (harness-neutral ToolSpecs)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from ..harness.base import ToolSpec

if TYPE_CHECKING:
    from ..project import Project

BOARD_SERVER = "board"


def board_toolset(project: "Project", session_id: str | Callable[[], str | None]) -> list[ToolSpec]:
    """Board tools bound to one session. ``session_id`` may be a callable when the
    harness assigns the id only once the session has started (Codex threads)."""
    board = project.board

    def sid() -> str:
        return (session_id() if callable(session_id) else session_id) or ""

    async def status(args: dict[str, Any]) -> str:
        board.update_agent(
            sid(),
            status=args.get("status") or "working",
            progress=args.get("progress"),
            doing=args.get("doing") or "",
            source="self",
        )
        return "Status updated on the board."

    async def publish(args: dict[str, Any]) -> str:
        html = args.get("html") or ""
        if not html.strip():
            return "html must not be empty"
        board.publish_card(sid(), args.get("title") or "Result", html, key=args.get("key") or "main")
        return f"Card '{args.get('key') or 'main'}' published to the board."

    async def note(args: dict[str, Any]) -> str:
        board.log(sid(), args.get("text") or "", kind="note", source="self")
        return "Noted on the timeline."

    async def read(_args: dict[str, Any]) -> str:
        snap = board.snapshot()
        lines = []
        if snap["overview"].get("headline"):
            lines.append(f"Project headline: {snap['overview']['headline']}")
        now = time.time()
        for a in sorted(snap["agents"].values(), key=lambda a: -(a.get("updated_at") or 0)):
            if (a.get("stats") or {}).get("archived"):
                continue
            me = " (you)" if a.get("session_id") == sid() else ""
            age = int((now - (a.get("updated_at") or now)) / 60)
            branch = (a.get("stats") or {}).get("branch")
            lines.append(
                f"- {a.get('name')}{me}: {a.get('status')} {a.get('progress', 0)}% — {a.get('doing') or '—'}"
                f"{f' [branch {branch}]' if branch else ''} (updated {age}m ago)"
                + (f"\n  summary: {a['summary']}" if a.get("summary") else "")
                + (f"\n  cards: {', '.join(c.get('title', k) for k, c in (a.get('cards') or {}).items())}" if a.get("cards") else "")
            )
        recent = snap["timeline"][-8:]
        if recent:
            lines.append("Recent timeline:")
            lines += [f"  · {t.get('agent') or 'tracker'}: {t.get('text')}" for t in recent]
        return "\n".join(lines) or "The board is empty."

    return [
        ToolSpec(
            name="status",
            description="Update your status line on the shared project board (status, progress 0-100, what you are doing now).",
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["working", "waiting", "blocked", "done", "idle"]},
                    "progress": {"type": "integer", "minimum": 0, "maximum": 100},
                    "doing": {"type": "string", "description": "Short present-tense phrase, <= 80 chars"},
                },
                "required": ["status", "progress", "doing"],
            },
            handler=status,
        ),
        ToolSpec(
            name="publish",
            description=(
                "Publish or replace a result card (an HTML fragment) on the shared project board. "
                "Use this to present results to the human instead of a long chat answer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "html": {"type": "string", "description": "HTML fragment; no <html>/<head>/<body>, no external resources"},
                    "key": {"type": "string", "description": "Card id; same key replaces. Default 'main'."},
                },
                "required": ["title", "html"],
            },
            handler=publish,
        ),
        ToolSpec(
            name="note",
            description="Append a one-line milestone to the project timeline on the board.",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            handler=note,
        ),
        ToolSpec(
            name="read",
            description="Read the shared board: what every agent in this project is doing, their progress and recent milestones.",
            input_schema={"type": "object", "properties": {}},
            handler=read,
            read_only=True,
        ),
    ]
