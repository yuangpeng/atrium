"""Desktop notifications when an agent needs you: it asks for approval, or its turn ends.

Skipped for a session whose chat is open in a visible, focused browser tab (``Hub.watching``),
so they arrive while the page is closed, in the background, or showing another agent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from typing import TYPE_CHECKING, Protocol

from .config import config
from .events import hub
from .harness.base import Item
from .harness.util import short

if TYPE_CHECKING:
    from .project import Project

log = logging.getLogger("atrium.notify")

THROTTLE_SECONDS = 5.0  # per session and kind: parallel tool calls ask for several approvals at once
INTERRUPTED = {"aborted_streaming", "aborted_tools", "interrupted"}  # turns the user stopped


class Session(Protocol):
    """What the notifier reads from ``sessions.LiveHandle`` (which imports this module)."""

    sid: str
    project: Project
    cwd: str
    name: str
    items: list[Item]


class Notifier:
    def __init__(self) -> None:
        self._last: dict[tuple[str, str], float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ---- events (called from the session emitter) ---------------------------
    def permission(self, h: Session, item: Item) -> None:
        if not self._due(h, "permission"):
            return
        inp = item.get("input") or {}
        if item.get("tool") == "AskUserQuestion":
            questions = inp.get("questions") or [{}]
            self._send(h, f"{_agent(h)} has a question", questions[0].get("question") or "Waiting for your answer")
        elif item.get("tool") == "ExitPlanMode":
            self._send(h, f"{_agent(h)} has a plan for review", inp.get("plan") or "Waiting for your review")
        else:
            self._send(h, f"{_agent(h)} needs approval", _describe(item, h.cwd))

    def turn_ended(self, h: Session, item: Item) -> None:
        if item.get("terminal_reason") in INTERRUPTED or not self._due(h, "turn_end"):
            return
        if item.get("is_error"):
            self._send(h, f"{_agent(h)} stopped with an error", item.get("text") or _last_text(h) or "The turn failed")
        else:
            self._send(h, f"{_agent(h)} finished", _last_text(h) or "Turn complete")

    def failed(self, h: Session) -> None:
        """The agent process died mid-turn, so no turn end will follow."""
        if not self._due(h, "turn_end"):
            return
        err = next((it.get("text") for it in reversed(h.items) if it.get("kind") == "error"), None)
        self._send(h, f"{_agent(h)} stopped unexpectedly", err or "The agent process exited")

    # ---- internals ---------------------------------------------------------
    def _due(self, h: Session, kind: str) -> bool:
        """Enabled for ``kind``, nobody is looking at the session, and not notified just now."""
        s = config.get("notifications") or {}
        if not s.get("enabled") or not s.get(kind) or hub.watching(f"session:{h.sid}"):
            return False
        now, key = time.monotonic(), (h.sid, kind)
        if key in self._last and now - self._last[key] < THROTTLE_SECONDS:
            return False
        self._last[key] = now
        return True

    def _send(self, h: Session, title: str, body: str) -> None:
        base = os.environ.get("ATRIUM_URL")
        url = f"{base.rstrip('/')}/?project={h.project.id}&session={h.sid}" if base else None
        cmd = _command(title, f"Atrium · {h.project.name}", short(body, 200), url=url, group=f"atrium-{h.sid}",
                       sound=bool(config.get("notifications.sound")))
        if not cmd:
            log.debug("no desktop notifier on %s", sys.platform)
            return
        task = asyncio.get_running_loop().create_task(_run(cmd))
        self._tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(self._tasks.discard)


def _command(title: str, subtitle: str, body: str, *, url: str | None, group: str, sound: bool) -> list[str] | None:
    if sys.platform == "darwin":
        if tn := shutil.which("terminal-notifier"):  # clicking opens the session; one notification per session
            # a leading zero-width space keeps text starting with "-" or "(" from being read as an option / plist
            title, subtitle, body = ("\u200b" + s for s in (title, subtitle, body))
            return [tn, "-title", title, "-subtitle", subtitle, "-message", body, "-group", group,
                    *(["-open", url] if url else []), *(["-sound", "default"] if sound else [])]
        show = "display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)"
        return ["osascript", "-e", "on run argv", "-e", show + (' sound name "Glass"' if sound else ""), "-e", "end run",
                "--", title, subtitle, body]
    if ns := shutil.which("notify-send"):
        return [ns, "--app-name=Atrium", "--", title, f"{subtitle}\n{body}"]
    return None


async def _run(cmd: list[str]) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        log.warning("desktop notification failed: %s", e)
        return
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        proc.kill()
        log.warning("desktop notification timed out (%s)", cmd[0])
        return
    if proc.returncode:
        log.warning("desktop notification failed (%s): %s", cmd[0], err.decode(errors="replace").strip())


def _agent(h: Session) -> str:
    return h.project.session_meta(h.sid).get("name") or h.name or "Agent"


def _last_text(h: Session) -> str:
    """The agent's last message in the current turn."""
    for it in reversed(h.items):
        if it.get("kind") == "user":
            break
        if it.get("kind") == "text":
            return it.get("text") or ""
    return ""


def _describe(item: Item, cwd: str) -> str:
    """One line for a permission request, like the brief on the chat's approval card."""
    inp = item.get("input") or {}
    if item.get("tool") == "Patch":
        detail = ", ".join(_rel(c.get("path") or "", cwd) for c in inp.get("changes") or [])
    else:
        path = _rel(inp.get("file_path") or inp.get("notebook_path") or "", cwd)
        detail = inp.get("description") or inp.get("command") or path or inp.get("url") or inp.get("query") or inp.get("pattern")
    tool = item.get("display_name") or item.get("tool") or "a tool"
    return f"{tool}: {short(detail, 160)}" if detail else item.get("title") or f"Allow {tool}?"


def _rel(path: str, cwd: str) -> str:
    return path[len(cwd) + 1:] if path.startswith(cwd + os.sep) else path


notifier = Notifier()
