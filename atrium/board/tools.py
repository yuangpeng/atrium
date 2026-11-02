"""Board tools handed to every session agent (harness-neutral ToolSpecs)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from ..harness.base import ToolSpec
from .template import is_suggestion

if TYPE_CHECKING:
    from ..project import Project

BOARD_SERVER = "board"
TODOS_READ = 30  # open TODOs board.read() lists, the human's first


def board_toolset(project: "Project", session_id: str | Callable[[], str | None],
                  on_status: Callable[[str], None] | None = None) -> list[ToolSpec]:
    """Board tools bound to one session. ``session_id`` may be a callable when the
    harness assigns the id only once the session has started (Codex threads).
    ``on_status`` hears the status the agent reports (done, waiting… — the review trigger)."""
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
        if on_status:
            on_status(args.get("status") or "working")
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

    async def todo_add(args: dict[str, Any]) -> str:
        mine = bool(args.get("requested"))
        try:
            t = board.add_todo(args.get("text") or "", sid=sid(), why=args.get("why") or "", source="human" if mine else "agent")
        except ValueError as e:
            return str(e)
        return f"TODO {t['id']} is on the board{'' if mine else ' as your suggestion; the human accepts or dismisses it'}."

    async def todo_done(args: dict[str, Any]) -> str:
        tid = str(args.get("id") or "")
        was = next((t for t in board.todos() if t["id"] == tid), None)
        if was is None:
            return f"No TODO {tid!r}: board.read() lists them."
        if was.get("done"):
            return f"TODO {tid} was checked off already."
        me = (board.agent(sid()) or {}).get("name") or project.agent_name(sid()) or "an agent"
        t = board.update_todo(tid, done=True, by=me)
        if t is None:  # deleted in between
            return f"No TODO {tid!r}: board.read() lists them."
        board.log(sid(), f"✓ {t['text']}" + (f" — {args['note']}" if args.get("note") else ""), kind="todo", source="self")
        return f"TODO {t['id']} checked off."

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
            gone = ", worktree removed" if (a.get("stats") or {}).get("workspace_gone") else ""
            lines.append(
                f"- {a.get('name')}{me}: {a.get('status')} {a.get('progress', 0)}% — {a.get('doing') or '—'}"
                f"{f' [branch {branch}{gone}]' if branch else ''} (updated {age}m ago)"
                + (f"\n  summary: {a['summary']}" if a.get("summary") else "")
                + (f"\n  cards: {', '.join(c.get('title', k) for k, c in (a.get('cards') or {}).items())}" if a.get("cards") else "")
            )
        todos = sorted((t for t in snap.get("todos") or [] if not t.get("done")), key=is_suggestion)
        if todos:
            lines.append("Open TODOs (the human's, then agents' suggestions they haven't accepted yet):")
            lines += [f"  · {t['id']}: {t['text']}" + (f" (suggested by {t.get('agent') or 'an agent'})" if is_suggestion(t) else "")
                      for t in todos[:TODOS_READ]]
            if len(todos) > TODOS_READ:
                lines.append(f"  · … and {len(todos) - TODOS_READ} more")
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
            name="todo_add",
            description=(
                "Add a TODO to the board's TODO list. By default it is your suggestion, which the human accepts "
                "or dismisses: follow-up work you recommend but didn't do. With requested=true it goes straight "
                "into the human's own list: only when they asked you to note it down."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The TODO itself: short, actionable, <= 120 chars"},
                    "why": {"type": "string", "description": "One sentence: why it matters (shown under it)"},
                    "requested": {"type": "boolean", "description": "true only when the human asked you to add it"},
                },
                "required": ["text"],
            },
            handler=todo_add,
        ),
        ToolSpec(
            name="todo_done",
            description="Check off a TODO on the board (the human's or a suggestion) that you have finished. board.read() lists their ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The TODO's id, e.g. t12"},
                    "note": {"type": "string", "description": "Optional: one line on how it was done, for the timeline"},
                },
                "required": ["id"],
            },
            handler=todo_done,
        ),
        ToolSpec(
            name="read",
            description="Read the shared board: what every agent in this project is doing, their progress and recent milestones.",
            input_schema={"type": "object", "properties": {}},
            handler=read,
            read_only=True,
        ),
    ]
