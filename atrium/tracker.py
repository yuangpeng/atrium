"""Tracker Agent — one per project, runs in the background.

Proactivity comes from diffing transcripts over time: each run reads only the
bytes appended to every session transcript since the previous run, compresses
them into a short digest, and asks a model to update the board (status,
progress, "doing", summary, names for unnamed sessions, overview, timeline).

Runs happen periodically (``tracker.interval_seconds``), shortly after any
live session finishes a turn (``tracker.after_turn_seconds``), or on demand.
The model call goes to the harness configured in ``tracker.harness``.
No model call is made when nothing changed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from . import changes
from .board.prompts import TRACKER_SCHEMA, TRACKER_SYSTEM
from .config import RUNTIME_DIR, config
from .events import hub
from .harness import get_harness

if TYPE_CHECKING:
    from .project import Project
    from .sessions import SessionManager

log = logging.getLogger("atrium.tracker")

FIRST_RUN_MAX_AGE = 3 * 86400  # on first sight, only digest sessions active in the last 3 days
FIRST_RUN_TAIL_BYTES = 256_000
MAX_EVENTS_PER_SESSION = 70
MAX_CHARS_PER_SESSION = 7000
MAX_PROMPT_CHARS = 60_000


def _ago(ts: float | None) -> str:
    if not ts:
        return "unknown"
    m = int((time.time() - ts) / 60)
    if m < 1:
        return "just now"
    if m < 120:
        return f"{m} min ago"
    if m < 48 * 60:
        return f"{m // 60} h ago"
    return f"{m // 1440} d ago"


def _trim_events(events: list[str]) -> list[str]:
    if len(events) > MAX_EVENTS_PER_SESSION:
        head = events[:10]
        tail = events[-(MAX_EVENTS_PER_SESSION - 10) :]
        events = head + [f"… ({len(events) - len(head) - len(tail)} earlier events omitted) …"] + tail
    out, size = [], 0
    for e in reversed(events):
        size += len(e) + 1
        if size > MAX_CHARS_PER_SESSION:
            out.append("… (older events omitted) …")
            break
        out.append(e)
    return list(reversed(out))


class Tracker:
    def __init__(self, project: "Project", sessions: "SessionManager"):
        self.project = project
        self.sessions = sessions
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._force = False
        self._next_due = time.time() + 20
        self._turn_due: float | None = None
        self.running = False
        self._edits_cache: dict[str, tuple[tuple[int, int], int]] = {}  # transcript -> ((size, snapshots), files_changed)
        self.cwd = str(RUNTIME_DIR / "tracker")
        os.makedirs(self.cwd, exist_ok=True)

    # ---- scheduling --------------------------------------------------------
    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._loop(), name=f"tracker-{self.project.id}")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def run_now(self) -> None:
        self._force = True
        self._wake.set()

    def after_turn(self) -> None:
        delay = float(config.get("tracker.after_turn_seconds") or 45)
        self._turn_due = time.time() + delay
        self._wake.set()

    @staticmethod
    def _harness() -> Any:
        h = get_harness(config.get("tracker.harness"))
        return h if h.available()[0] else get_harness("claude-code")

    def status(self) -> dict[str, Any]:
        st = self.project.tracker_state()
        return {
            "enabled": bool(config.get("tracker.enabled")),
            "interval": int(config.get("tracker.interval_seconds") or 300),
            "harness": self._harness().id,
            "running": self.running,
            "next_run": min(self._next_due, self._turn_due or self._next_due),
            "runs": list(reversed(st.get("runs", [])[-15:])),
        }

    async def _loop(self) -> None:
        while True:
            due = min(self._next_due, self._turn_due or self._next_due)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(1.0, due - time.time()))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            now = time.time()
            forced, self._force = self._force, False
            turn = self._turn_due is not None and now >= self._turn_due
            if not (forced or turn or now >= self._next_due):
                continue
            self._next_due = now + max(30, int(config.get("tracker.interval_seconds") or 300))
            if turn:
                self._turn_due = None
            if not forced and not config.get("tracker.enabled"):
                continue
            try:
                await self.run_once("manual" if forced else "after-turn" if turn else "periodic", force=forced)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tracker run failed for %s", self.project.path)

    # ---- one run -----------------------------------------------------------
    def _read_delta(self, path: str, offset: int | None, updated_at: float) -> tuple[list[str], int]:
        try:
            size = os.path.getsize(path)
        except OSError:
            return [], offset or 0
        if offset is None:
            if time.time() - updated_at > FIRST_RUN_MAX_AGE:
                return [], size
            start = max(0, size - FIRST_RUN_TAIL_BYTES)
        else:
            start = 0 if size < offset else offset
        if start >= size:
            return [], size
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
        end = data.rfind(b"\n")
        if end < 0:
            return [], start
        chunk = data[: end + 1].decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        if offset is None and start > 0:
            lines = lines[1:]  # first line is probably partial
        return lines, start + end + 1

    def _files_changed(self, s: dict[str, Any], transcript: str | None) -> int:
        """Files the session changed: transcript edits plus snapshot-detected changes."""
        try:
            size = os.path.getsize(transcript) if transcript else 0
        except OSError:
            size = 0
        key = transcript or s["id"]
        version = (size, len(self.project.session_meta(s["id"]).get("snapshots") or []))
        cached = self._edits_cache.get(key)
        if cached and cached[0] == version:
            return cached[1]
        try:
            n = len(changes.session_changes(self.project, s["id"], s["cwd"], s["harness"])["files"])
        except Exception:
            log.debug("counting changes failed for %s", s["id"], exc_info=True)
            n = 0
        self._edits_cache[key] = (version, n)
        return n

    async def run_once(self, reason: str, force: bool = False) -> dict[str, Any]:
        if self.running:
            return {"skipped": "already running"}
        self.running = True
        hub.publish(f"project:{self.project.id}", {"type": "tracker", "running": True})
        started = time.time()
        record: dict[str, Any] = {"ts": started, "reason": reason}
        try:
            record.update(await self._run(force))
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"[:500]
            log.warning("tracker error (%s): %s", self.project.name, e)
        finally:
            self.running = False
            record["duration_ms"] = int((time.time() - started) * 1000)
            st = self.project.tracker_state()
            st["runs"] = (st.get("runs", []) + [record])[-30:]
            if "offsets" in record:
                st["offsets"] = record.pop("offsets")
                st["runs"][-1] = record
            self.project.save_tracker_state(st)
            hub.publish(f"project:{self.project.id}", {"type": "tracker", "running": False, "run": record})
        return record

    async def _run(self, force: bool) -> dict[str, Any]:
        try:
            return await self._run_inner(force)
        finally:
            self.project.board.rerender()  # flush batched (quiet) board updates

    async def _run_inner(self, force: bool) -> dict[str, Any]:
        project = self.project
        state = project.tracker_state()
        offsets: dict[str, int] = dict(state.get("offsets", {}))
        sessions = [s for s in await self.sessions.list_sessions(project) if s["role"] == "worker"]
        board = project.board

        digests: dict[str, list[str]] = {}
        facts: list[str] = []
        harness_by_sid: dict[str, Any] = {}
        for s in sessions:
            harness = get_harness(s["harness"])
            harness_by_sid[s["id"]] = harness
            transcript = s.get("transcript")
            if not transcript:
                path = harness.transcript_path(s["id"], s["cwd"])
                transcript = str(path) if path else None
            files_changed = await asyncio.to_thread(self._files_changed, s, transcript)
            board_agent = board.agent(s["id"])
            if board_agent is not None or s["live"] or (time.time() - (s["updated_at"] or 0) < FIRST_RUN_MAX_AGE):
                board.update_agent(
                    s["id"],
                    name=s["name"] or ((board_agent or {}).get("name") if (board_agent or {}).get("name") not in (None, "Agent") else None) or s["title"] or None,
                    stats={"live": s["live"], "last_active": s["updated_at"], "files_changed": files_changed, "branch": s["branch"], "worktree": s["worktree"], "harness": s["harness"]},
                    quiet=True,
                )
            if transcript:
                lines, new_off = await asyncio.to_thread(self._read_delta, transcript, offsets.get(transcript), s["updated_at"] or 0)
                offsets[transcript] = new_off
                events = harness.digest_lines(lines) if lines else []
                if events:
                    digests[s["id"]] = _trim_events(events)
            if s["id"] in digests or (board_agent and board_agent.get("status") in ("working", "waiting", "blocked")):
                label = f'name="{s["name"]}"' if s["name"] else f'UNNAMED (title: "{(s["title"] or "")[:80]}")'
                facts.append(
                    f'- [{s["id"]}] {label} | live={s["live"] or "detached"} | last activity {_ago(s["updated_at"])}'
                    f' | files changed: {files_changed} | branch: {s["branch"] or "?"}{" (worktree)" if s["worktree"] else ""}'
                )

        result: dict[str, Any] = {"offsets": offsets, "sessions": len(digests), "events": sum(len(v) for v in digests.values())}
        if not digests and not force:
            result["skipped"] = "no new activity"
            return result
        if not digests and not facts:
            result["skipped"] = "no sessions"
            return result

        snap = board.snapshot()
        board_lines = []
        for sid, a in snap["agents"].items():
            if (a.get("stats") or {}).get("archived"):
                continue
            board_lines.append(
                f'- [{sid}] "{a.get("name")}" status={a.get("status")} progress={a.get("progress")}% '
                f'doing="{a.get("doing", "")}" summary="{(a.get("summary") or "")[:200]}" (updated {_ago(a.get("updated_at"))}, by {a.get("source") or "?"})'
            )
        names = {s["id"]: (s["name"] or s["title"] or "?") for s in sessions}
        activity = []
        for sid, events in digests.items():
            activity.append(f"### [{sid}] {names.get(sid, '?')}\n" + "\n".join(events))
        prompt = (
            f"Project: {project.name} ({project.path})\nLocal time now: {time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"## Current board\nHeadline: {snap['overview'].get('headline') or '(none)'}\n" + ("\n".join(board_lines) or "(empty)") + "\n\n"
            "## Session facts\n" + ("\n".join(facts) or "(none)") + "\n\n"
            "## Activity since the previous run\n" + ("\n\n".join(activity) or "(no new transcript activity)")
        )
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS] + "\n… (truncated)"
        result["prompt_chars"] = len(prompt)

        harness = self._harness()
        data, usage = await harness.complete_json(
            system=TRACKER_SYSTEM, prompt=prompt, schema=TRACKER_SCHEMA, model=config.get("tracker.model") or None,
            effort=config.get("tracker.effort") or None, cwd=self.cwd,
        )
        result["harness"] = harness.id
        result.update(usage)

        known = {s["id"]: s for s in sessions}
        updated = 0
        for a in data.get("agents") or []:
            sid = a.get("session_id")
            if sid not in known:
                continue
            name = None
            if not known[sid]["name"] and (a.get("name") or "").strip():
                name = a["name"].strip()[:60]
                project.set_session_meta(sid, name=name, name_source="tracker")
                live = self.sessions.live.get(sid)
                if live:
                    live.name = name
            board.update_agent(
                sid, name=name, status=a.get("status"), progress=a.get("progress"), doing=a.get("doing"),
                summary=a.get("summary"), source="tracker", quiet=True,
            )
            updated += 1
        board.set_overview(data.get("headline"), data.get("overview_html"), quiet=True)
        for t in data.get("timeline") or []:
            if t.get("text"):
                board.log(t.get("session_id") if t.get("session_id") in known else None, t["text"], kind="tracker", source="tracker", quiet=True)
        hub.publish(f"project:{project.id}", {"type": "sessions_changed"})
        result["updated_agents"] = updated
        return result
