"""Code Review Agent — reviews what each agent changed, and helps the human decide what goes back to it.

Every worker agent gets a review session of its own (role ``review``): read-only, in the agent's
directory, resumed round after round so it remembers earlier rounds and what the human decided.
A round starts shortly after a turn in which the agent reported its task done or waiting for the
human (``board.status``, see ``LiveHandle.reported``), or when the human asks for one.

The reviewer gets a materials file, attached to its request: the task, the agent's last reply, the
changed files and their diff since the previous round (between git snapshots, see ``snapshots.py``)
and what was sent to the agent last time. It records structured findings with ``review.report``;
the human adopts or dismisses them, in the chat or with a click. Nothing reaches the agent until the
human presses Send — on a draft the reviewer wrote (``review.draft_message``) or one made from the
adopted findings. A message for an agent that is busy waits until its turn ends.

State: ``.atrium/code_review.json`` — per agent: its review session, the rounds with their findings,
the reviewer's drafts, and the message waiting to be delivered (``outbox``).
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, Callable

from . import changes, gitops, snapshots
from .config import UPLOADS_DIR, config
from .events import hub
from .harness import get_harness
from .harness.base import Attachment, Item, ToolSpec

if TYPE_CHECKING:
    from .project import Project
    from .sessions import LiveHandle, SessionManager

log = logging.getLogger("atrium.review")

REVIEW_SERVER = "review"
REVIEW_AGENT_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git diff:*)",
    "Bash(git show:*)",
    "Bash(git log:*)",
    "Bash(git blame:*)",
    "Bash(git status:*)",
    "Bash(git grep:*)",
    "Bash(git ls-files:*)",
    "Bash(git rev-parse:*)",
    "Bash(git cat-file:*)",
    "Bash(git merge-base:*)",
]
REVIEW_AGENT_DISALLOWED_TOOLS = ["Edit", "Write", "NotebookEdit", "ReportFindings"]  # findings go through review.report
SEVERITIES = ("P0", "P1", "P2", "P3")
TRIGGERS = ("done", "waiting")  # what an agent reports with board.status that starts an automatic review
INTERRUPTED = {"aborted_streaming", "aborted_tools", "interrupted"}  # turns the human stopped
MAX_ROUNDS = 30
MAX_DRAFTS = 30
MAX_FINDINGS = 12
MAX_FILE_DIFF = 60_000  # characters of one file's diff in the materials
MAX_DIFF = 240_000  # … and of all of them
# lockfiles and build output: listed in the materials, their diff left out
GENERATED = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.lock|go\.sum)$|\.(min\.js|min\.css|map)$|(^|/)(dist|build)/")


def review_language() -> str:
    return config.get("review_agent.language") or "the language the human writes in (Chinese if their messages are in Chinese)"


def _short(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _active(r: dict[str, Any]) -> bool:
    return r.get("state") in ("queued", "running")


class ReviewManager:
    """One per project. Review sessions are ordinary sessions with role ``review``; this keeps
    which agent each one reviews, the rounds, and decides when to start one."""

    def __init__(self, project: "Project", sessions: "SessionManager"):
        self.project = project
        self.sessions = sessions
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._again: set[str] = set()  # agents that reported done again while their review ran
        self._starting: set[str] = set()  # agents whose round is gathering its materials
        data = self._load()  # rounds a previous Atrium process left queued or running died with it
        stale = [r for e in data.values() for r in (e.get("rounds") or [])[-1:] if _active(r)]
        for r in stale:
            r.update(state="failed", ended_at=time.time(), error="Atrium restarted during the review")
        if stale:
            self.project.save_code_review_state(data)

    # ---- state -------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        return self.project.code_review_state()

    def _save(self, data: dict[str, Any], wsid: str | None = None) -> None:
        self.project.save_code_review_state(data)
        hub.publish(f"project:{self.project.id}", {"type": "review", "session_id": wsid})

    def _entry(self, data: dict[str, Any], wsid: str) -> dict[str, Any]:
        e = data.setdefault(wsid, {})
        e.setdefault("rounds", [])
        e.setdefault("drafts", [])
        return e

    def worker_of(self, review_sid: str) -> str | None:
        return self.project.session_meta(review_sid).get("reviews")

    def auto_enabled(self, wsid: str) -> bool:
        own = self._load().get(wsid, {}).get("auto")
        return bool(config.get("review_agent.auto")) if own is None else bool(own)

    def _reconcile(self, data: dict[str, Any]) -> bool:
        """A running round whose reviewer is no longer working (its process died) failed."""
        changed = False
        for wsid, e in data.items():
            rounds = e.get("rounds") or []
            if rounds and rounds[-1].get("state") == "running" and wsid not in self._starting:
                h = self.sessions.handle(e.get("session") or "")
                if not h or h.state not in ("starting", "running", "waiting"):
                    rounds[-1].update(state="failed", ended_at=time.time(), error="The reviewer stopped before it finished")
                    changed = True
        if changed:
            self._save(data)
        return changed

    def view(self, wsid: str) -> dict[str, Any]:
        data = self._load()
        self._reconcile(data)
        e = data.get(wsid) or {}
        return {
            "worker": wsid,
            "session": e.get("session"),
            "auto": self.auto_enabled(wsid),
            "auto_own": e.get("auto"),
            "rounds": e.get("rounds") or [],
            "drafts": e.get("drafts") or [],
            "outbox": e.get("outbox"),
        }

    def summary(self) -> dict[str, dict[str, Any]]:
        """Per agent, what the review inbox shows: whether a review runs, findings left to decide on."""
        out = {}
        data = self._load()
        self._reconcile(data)
        for wsid, e in data.items():
            rounds = e.get("rounds") or []
            last = rounds[-1] if rounds else None
            out[wsid] = {
                "session": e.get("session"),
                "state": last.get("state") if last else None,
                "round": last.get("n") if last else 0,
                "open": sum(1 for f in (last or {}).get("findings") or [] if f.get("status") in ("open", "adopted")),
                "findings": len((last or {}).get("findings") or []),
                "outbox": bool(e.get("outbox")),
                "auto": self.auto_enabled(wsid),
            }
        return out

    def set_auto(self, wsid: str, auto: bool | None) -> None:
        data = self._load()
        e = self._entry(data, wsid)
        if auto is None:
            e.pop("auto", None)
        else:
            e["auto"] = bool(auto)
        self._save(data, wsid)

    # ---- triggers ----------------------------------------------------------
    def turn_ended(self, sid: str) -> None:
        """``SessionManager.on_turn_end``: a worker that reported done / waiting gets reviewed; a review
        session's turn closes its round; a message waiting for a worker goes out."""
        h = self.sessions.live.get(sid)
        if not h:
            return
        if h.role == "review":
            self._round_ended(h)
            return
        if h.role != "worker":
            return
        if (self._load().get(sid) or {}).get("outbox"):
            asyncio.get_running_loop().create_task(self._deliver(sid))
            return  # it is about to work on that: review what comes out of it
        if h.reported in TRIGGERS and not _interrupted(h) and self.auto_enabled(sid):
            self._schedule(sid, f"reported {h.reported}")

    def _schedule(self, wsid: str, reason: str) -> None:
        old = self._timers.pop(wsid, None)
        if old:
            old.cancel()
        delay = float(config.get("review_agent.delay_seconds") or 0)

        def fire() -> None:
            self._timers.pop(wsid, None)
            h = self.sessions.handle(wsid)
            if h and h.state != "idle":
                return  # busy again: the next report brings these changes along
            asyncio.get_running_loop().create_task(self._auto(wsid, reason))

        self._timers[wsid] = asyncio.get_running_loop().call_later(delay, fire)

    async def _auto(self, wsid: str, reason: str) -> None:
        try:
            await self.run(wsid, trigger="auto", reason=reason)
        except Exception:
            log.exception("automatic review of %s failed", wsid)

    # ---- rounds ------------------------------------------------------------
    async def run(self, wsid: str, *, trigger: str = "manual", reason: str = "") -> dict[str, Any]:
        """Start a round for an agent. It waits ("queued") while the reviewer is busy, or when it is
        automatic and enough reviews run already. Returns the round, or ``{"skipped": why}``."""
        meta = self.project.session_meta(wsid)
        if meta.get("role", "worker") != "worker":
            raise ValueError("only agents doing the work get reviewed")
        if not meta and not await self.sessions.find_summary(self.project, wsid):
            raise FileNotFoundError(f"no agent {wsid}")
        cwd, _hid = await self.sessions.session_info(self.project, wsid)
        if not os.path.isdir(cwd):  # its worktree was removed: reviewed again once it is moved
            if trigger == "auto":
                return {"skipped": "its worktree was removed"}
            raise ValueError("Its worktree was removed: move the agent (from its chat) before reviewing it")
        if wsid in self._starting:
            return {"skipped": "a review is starting"}
        data = self._load()
        self._reconcile(data)
        rounds = self._entry(data, wsid)["rounds"]
        if rounds and _active(rounds[-1]):
            if trigger == "auto":
                self._again.add(wsid)  # its newer changes get a round of their own once this one ends
            return rounds[-1]
        r = {"n": (rounds[-1]["n"] if rounds else 0) + 1, "state": "queued", "trigger": trigger, "reason": reason,
             "created_at": time.time(), "findings": [], "summary": ""}
        if not self._can_start(wsid, r):
            rounds.append(r)
            self._save(data, wsid)
            return r
        return await self._launch(wsid, r)

    def _can_start(self, wsid: str, r: dict[str, Any]) -> bool:
        rh = self.sessions.handle((self._load().get(wsid) or {}).get("session") or "")
        if rh and rh.state in ("starting", "running", "waiting"):
            return False  # its request would land in the middle of a conversation turn
        limit = int(config.get("review_agent.max_concurrent") or 0)
        return r["trigger"] != "auto" or not limit or self._running() < limit

    def _running(self) -> int:
        return sum(1 for e in self._load().values() for r in (e.get("rounds") or [])[-1:] if r.get("state") == "running")

    async def _launch(self, wsid: str, r: dict[str, Any]) -> dict[str, Any]:
        """Gather the materials, then send the round's request to the agent's review session."""
        self._starting.add(wsid)
        try:
            return await self._launch_inner(wsid, r)
        except Exception as ex:
            data = self._load()
            e = self._entry(data, wsid)
            failed = {**r, "state": "failed", "ended_at": time.time(), "error": _short(str(ex) or type(ex).__name__, 500)}
            e["rounds"] = sorted([x for x in e["rounds"] if x["n"] != r["n"]] + [failed], key=lambda x: x["n"])[-MAX_ROUNDS:]
            self._save(data, wsid)
            raise
        finally:
            self._starting.discard(wsid)

    async def _launch_inner(self, wsid: str, r: dict[str, Any]) -> dict[str, Any]:
        project = self.project
        cwd, hid = await self.sessions.session_info(project, wsid)
        name = await self._name(wsid)
        # the last round the reviewer got through: what it has seen ends at that round's snapshot
        prev = next((x for x in reversed(self._load().get(wsid, {}).get("rounds") or [])
                     if x["n"] < r["n"] and x.get("state") == "done" and x.get("head")), None)
        mat = await asyncio.to_thread(_materials, project, wsid, cwd, hid, prev)
        if not mat["files"] and r["trigger"] == "auto":
            self._drop(wsid, r)
            return {"skipped": "no new changes"}
        intent = await self._intent(wsid, cwd, hid)
        sent = [f for f in (prev or {}).get("findings") or [] if f.get("status") == "sent"]
        r.update(state="running", started_at=time.time(), scope=mat["scope"], head=mat["head"], base=mat["base"],
                 files=[{k: f[k] for k in ("rel", "status", "added", "removed")} for f in mat["files"]])
        att = _write_materials(_document(r, name, cwd, hid, intent, mat, sent, prev), f"review-{name}-r{r['n']}.md")
        why = {"auto": f"the agent {r['reason']}", "manual": "requested by the human"}.get(r["trigger"], r["trigger"])
        if mat["files"]:
            what = "everything this agent changed" if mat["scope"] == "all" else f"its changes since round {prev['n'] if prev else '?'}"
            text = (f"Review round {r['n']} for “{name}”: {what} — {len(mat['files'])} file(s), "
                    f"+{sum(f['added'] for f in mat['files'])} −{sum(f['removed'] for f in mat['files'])} ({why}). "
                    "The attached materials have the task, the diff and what was sent to it last time.")
        else:
            text = f"Review round {r['n']} for “{name}”: it has no file changes to review ({why}). The attached materials have the task."
        data = self._load()
        e = self._entry(data, wsid)
        e["rounds"] = sorted([x for x in e["rounds"] if x["n"] != r["n"]] + [r], key=lambda x: x["n"])[-MAX_ROUNDS:]
        self._save(data, wsid)
        rsid = e.get("session")
        if rsid and project.session_meta(rsid) and not await self._follow(wsid, rsid, cwd):
            rsid = None  # it couldn't follow the agent: a new reviewer, where the agent works
        if not rsid or not project.session_meta(rsid):
            rsid = await self.sessions.create(project, name=f"Review · {name}", prompt=None, role="review", cwd=cwd,
                                              harness_id=config.get("review_agent.harness") or None,
                                              model=config.get("review_agent.model") or None, effort=config.get("review_agent.effort") or None)
            project.set_session_meta(rsid, reviews=wsid)
            data = self._load()
            self._entry(data, wsid)["session"] = rsid
            self._save(data, wsid)
        await self.sessions.send(project, rsid, text, [att])
        return r

    def _drop(self, wsid: str, r: dict[str, Any]) -> None:
        data = self._load()
        e = self._entry(data, wsid)
        e["rounds"] = [x for x in e["rounds"] if x["n"] != r["n"]]
        self._save(data, wsid)
        self._pump()

    def _round_ended(self, h: "LiveHandle") -> None:
        """The reviewer's turn ended: so did the round it was working on, if any."""
        wsid = self.worker_of(h.sid)
        if not wsid:
            return
        rounds = (self._load().get(wsid) or {}).get("rounds") or []
        if rounds and rounds[-1].get("state") == "running":
            res = _last_result(h)
            stopped = bool(res and (res.get("terminal_reason") in INTERRUPTED or res.get("is_error")))
            self._finish(wsid, rounds[-1]["n"], "stopped" if stopped else "done", error=(res or {}).get("text") if stopped else None)
        if wsid in self._again:
            self._again.discard(wsid)
            asyncio.get_running_loop().create_task(self._auto(wsid, "reported done again during the review"))
        self._pump()

    def _finish(self, wsid: str, n: int, state: str, error: str | None = None) -> None:
        data = self._load()
        for r in self._entry(data, wsid)["rounds"]:
            if r["n"] == n:
                r.update(state=state, ended_at=time.time())
                if error:
                    r["error"] = _short(error, 500)
        self._save(data, wsid)

    def _pump(self) -> None:
        """Start the queued rounds that can start now, oldest first."""
        queued = sorted(((r["created_at"], wsid, r) for wsid, e in self._load().items() for r in (e.get("rounds") or [])[-1:]
                         if r.get("state") == "queued"), key=lambda x: x[0])
        starting = 0
        limit = int(config.get("review_agent.max_concurrent") or 0)
        for _ts, wsid, r in queued:
            if wsid in self._starting or not self._can_start(wsid, r):
                continue
            if r["trigger"] == "auto" and limit and self._running() + starting >= limit:
                continue
            starting += 1
            asyncio.get_running_loop().create_task(self._launch_safe(wsid, r))

    async def _launch_safe(self, wsid: str, r: dict[str, Any]) -> None:
        try:
            await self._launch(wsid, r)
        except Exception:
            log.exception("review round %s of %s failed to start", r.get("n"), wsid)

    async def follow(self, wsid: str) -> None:
        """The agent moved to another directory (``SessionManager.move``): its reviewer goes along."""
        rsid = (self._load().get(wsid) or {}).get("session")
        if rsid and self.project.session_meta(rsid):
            await self._follow(wsid, rsid)

    async def _follow(self, wsid: str, rsid: str, cwd: str | None = None) -> bool:
        """Move the reviewer to where its agent works now, if it isn't there. False if it couldn't be
        (e.g. it is in the middle of a round: the next round tries again)."""
        if cwd is None:
            cwd, _ = await self.sessions.session_info(self.project, wsid)
        rcwd, _ = await self.sessions.session_info(self.project, rsid)
        if os.path.realpath(rcwd) == os.path.realpath(cwd):
            return True
        root = gitops.repo_root(cwd)
        linked = root and os.path.realpath(root) != os.path.realpath(self.project.path)
        try:
            res = await self.sessions.move(self.project, rsid, {"kind": "worktree", "path": root} if linked else {"kind": "root"})
        except Exception as ex:
            log.info("the reviewer %s couldn't follow its agent to %s: %s", rsid, cwd, ex)
            return False
        return os.path.realpath(res["cwd"]) == os.path.realpath(cwd)

    async def _name(self, wsid: str) -> str:
        meta = self.project.session_meta(wsid)
        if meta.get("name"):
            return meta["name"]
        a = self.project.board.agent(wsid)
        if a and a.get("name") not in (None, "", "Agent"):
            return a["name"]
        s = await self.sessions.find_summary(self.project, wsid)
        return _short(s.title, 60) if s and s.title else "Agent"

    async def _intent(self, wsid: str, cwd: str, hid: str) -> dict[str, Any]:
        """What the agent was asked (its last few prompts) and what it answered last."""
        h = self.sessions.handle(wsid)
        try:
            items: list[Item] = h.items if h else await get_harness(hid).load_history(wsid, cwd)
        except Exception:
            log.debug("history of %s unavailable", wsid, exc_info=True)
            items = []
        asks = [it.get("text") or "" for it in items if it.get("kind") == "user" and not it.get("command") and (it.get("text") or "").strip()]
        reply = next((it.get("text") or "" for it in reversed(items) if it.get("kind") == "text" and not it.get("parent")), "")
        board = self.project.board.agent(wsid) or {}
        return {"asks": asks[-3:], "reply": reply, "doing": board.get("doing") or "", "summary": board.get("summary") or "",
                "cards": [c.get("title") for c in (board.get("cards") or {}).values() if c.get("title")]}

    # ---- the human's decisions -------------------------------------------
    def set_status(self, wsid: str, n: int, ids: list[str], status: str) -> dict[str, Any]:
        if status not in ("open", "adopted", "dismissed"):
            raise ValueError(f"unknown status {status}")
        data = self._load()
        r = next((x for x in self._entry(data, wsid)["rounds"] if x["n"] == n), None)
        if not r:
            raise KeyError(f"no round {n}")
        hit = [f for f in r["findings"] if str(f["id"]) in {str(i) for i in ids}]
        for f in hit:
            f["status"] = status
        self._save(data, wsid)
        return {"updated": [f["id"] for f in hit]}

    def cancel_draft(self, wsid: str, did: int) -> None:
        data = self._load()
        for d in self._entry(data, wsid)["drafts"]:
            if d["id"] == did and d["status"] == "pending":
                d["status"] = "cancelled"
        self._save(data, wsid)

    async def send(self, wsid: str, text: str, *, n: int | None = None, draft: int | None = None) -> dict[str, Any]:
        """The human sends review feedback to the agent: now, or once its turn ends if it is busy. The
        round's adopted findings count as sent from then on."""
        text = (text or "").strip()
        if not text:
            raise ValueError("empty message")
        rounds = (self._load().get(wsid) or {}).get("rounds") or []
        r = next((x for x in rounds if x["n"] == n), None) if n else (rounds[-1] if rounds else None)
        label = f"Code review · round {r['n']}" if r else "Code review"
        body = f"[{label} · sent by the human]\n\n{text}"
        h = self.sessions.handle(wsid)
        queued = bool(h and h.state in ("starting", "running", "waiting"))
        if not queued:
            await self.sessions.send(self.project, wsid, body)
        data = self._load()
        e = self._entry(data, wsid)
        if queued:
            prev = (e.get("outbox") or {}).get("text")
            e["outbox"] = {"text": f"{prev}\n\n{body}" if prev else body, "at": time.time()}
        for x in e["rounds"]:
            if r and x["n"] == r["n"]:
                for f in x["findings"]:
                    if f.get("status") == "adopted":
                        f["status"] = "sent"
        for d in e["drafts"]:
            if d["id"] == draft:
                d.update(status="sent", sent_at=time.time(), sent_text=text)
        self._save(data, wsid)
        return {"queued": queued}

    async def deliver_now(self, wsid: str) -> None:
        await self._deliver(wsid)

    def cancel_outbox(self, wsid: str) -> None:
        data = self._load()
        self._entry(data, wsid).pop("outbox", None)
        self._save(data, wsid)

    async def _deliver(self, wsid: str) -> None:
        data = self._load()
        box = self._entry(data, wsid).pop("outbox", None)
        if not box:
            return
        self._save(data, wsid)
        try:
            await self.sessions.send(self.project, wsid, box["text"])
        except Exception as ex:
            if isinstance(ex, FileNotFoundError):  # its worktree was removed: it waits until the agent is moved
                log.info("review feedback for %s waits: %s", wsid, ex)
            else:
                log.exception("delivering review feedback to %s failed", wsid)
            data = self._load()
            self._entry(data, wsid)["outbox"] = box  # still waiting: the human can retry or cancel
            self._save(data, wsid)

    async def forget(self, wsid: str) -> None:
        """The agent was deleted: so are its review session and rounds."""
        data = self._load()
        e = data.pop(wsid, None)
        timer = self._timers.pop(wsid, None)
        if timer:
            timer.cancel()
        if e is None:
            return
        self._save(data, wsid)
        rsid = e.get("session")
        if rsid and self.project.session_meta(rsid):
            try:
                await self.sessions.delete(self.project, rsid)
            except Exception:
                log.exception("deleting review session %s failed", rsid)

    # ---- the reviewer's tools -----------------------------------------------
    def _round_for(self, rsid: str, n: Any) -> tuple[dict[str, Any], str, dict[str, Any]]:
        wsid = self.worker_of(rsid)
        if not wsid:
            raise ValueError("this session reviews no agent")
        data = self._load()
        rounds = self._entry(data, wsid)["rounds"]
        if not rounds:  # asked to review in the chat before any round: open one
            rounds.append({"n": 1, "state": "done", "trigger": "chat", "reason": "", "created_at": time.time(), "findings": [], "summary": ""})
        try:
            r = next(x for x in rounds if x["n"] == int(n))
        except (StopIteration, TypeError, ValueError):
            raise ValueError(f"no round {n}: the current round is {rounds[-1]['n']}") from None
        return data, wsid, r

    def tool_report(self, rsid: str, args: dict[str, Any]) -> str:
        data, wsid, r = self._round_for(rsid, args.get("round"))
        old = {str(f["id"]): f for f in r["findings"]}
        used = {int(i) for i in old if str(i).isdigit()}
        out = []
        for f in (args.get("findings") or [])[:MAX_FINDINGS]:
            fid = str(f.get("id") or "").lstrip("#")
            if fid not in old or fid in {x["id"] for x in out}:
                fid = str(max(used | {0}) + 1)
            used.add(int(fid))
            sev = str(f.get("severity") or "P2").upper()
            line = _int(f.get("line"))
            out.append({
                "id": fid,
                "severity": sev if sev in SEVERITIES else "P2",
                "title": _short(str(f.get("title") or ""), 200),
                "file": str(f.get("file") or ""),
                "line": line,
                "end_line": _int(f.get("end_line")) if line else None,
                "problem": str(f.get("problem") or ""),
                "suggestion": str(f.get("suggestion") or ""),
                "status": old[fid]["status"] if fid in old else "open",
            })
        r["findings"] = out
        r["summary"] = str(args.get("summary") or "")
        r["reported_at"] = time.time()
        self._save(data, wsid)
        ids = ", ".join(f"#{f['id']}" for f in out) or "none"
        return f"Recorded {len(out)} finding(s) for round {r['n']} ({ids}). The human sees them as cards and decides what to send."

    def tool_mark(self, rsid: str, args: dict[str, Any]) -> str:
        _data, wsid, r = self._round_for(rsid, args.get("round"))
        ids = [str(i).lstrip("#") for i in args.get("ids") or []]
        done = self.set_status(wsid, r["n"], ids, str(args.get("status") or ""))["updated"]
        missing = [i for i in ids if i not in done]
        return f"Marked {', '.join('#' + i for i in done) or 'nothing'} {args.get('status')}." + (f" Unknown: {', '.join(missing)}." if missing else "")

    def tool_draft(self, rsid: str, args: dict[str, Any]) -> str:
        wsid = self.worker_of(rsid)
        if not wsid:
            raise ValueError("this session reviews no agent")
        text = str(args.get("message") or "").strip()
        if not text:
            raise ValueError("message must not be empty")
        data = self._load()
        e = self._entry(data, wsid)
        for d in e["drafts"]:
            if d["status"] == "pending":
                d["status"] = "replaced"
        did = (e["drafts"][-1]["id"] if e["drafts"] else 0) + 1
        e["drafts"] = (e["drafts"] + [{"id": did, "text": text, "at": time.time(), "status": "pending",
                                       "round": e["rounds"][-1]["n"] if e["rounds"] else None}])[-MAX_DRAFTS:]
        self._save(data, wsid)
        return "The draft is shown to the human with a Send button. They may edit it; nothing is sent until they press Send."


def review_toolset(project: "Project", session_id: Callable[[], str | None]) -> list[ToolSpec]:
    """The reviewer's tools, bound to its session (``session_id`` resolves once the harness assigned it)."""

    def call(fn: Callable[["ReviewManager", str, dict[str, Any]], str]):
        async def handler(args: dict[str, Any]) -> str | dict[str, Any]:
            if project.review is None:
                return {"text": "Code review is not running for this project", "is_error": True}
            try:
                return fn(project.review, session_id() or "", args)
            except ValueError as e:
                return {"text": f"Error: {e}", "is_error": True}

        return handler

    return [
        ToolSpec(
            name="report",
            description=(
                "Record a review round's findings for the human, who sees them as cards and decides what goes back to the agent. "
                "Pass the round's complete list; to revise a finding keep its id (statuses are kept)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "round": {"type": "integer", "description": "The round number from the review request"},
                    "summary": {"type": "string", "description": "One or two sentences: the verdict, and whether last round's findings were addressed"},
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "The id of a finding you revise; omit for a new one"},
                                "severity": {"type": "string", "enum": list(SEVERITIES)},
                                "title": {"type": "string", "description": "One line"},
                                "file": {"type": "string", "description": "Path relative to your working directory"},
                                "line": {"type": "integer", "description": "Line in the file as it is now"},
                                "end_line": {"type": "integer"},
                                "problem": {"type": "string", "description": "What is wrong and why it matters, with a concrete scenario"},
                                "suggestion": {"type": "string", "description": "How to fix it"},
                            },
                            "required": ["severity", "title", "file", "problem", "suggestion"],
                        },
                    },
                },
                "required": ["round", "summary", "findings"],
            },
            handler=call(ReviewManager.tool_report),
        ),
        ToolSpec(
            name="mark",
            description="Record the human's decision on findings of a round: adopted (to be sent to the agent), dismissed, or open again.",
            input_schema={
                "type": "object",
                "properties": {
                    "round": {"type": "integer"},
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": ["adopted", "dismissed", "open"]},
                },
                "required": ["round", "ids", "status"],
            },
            handler=call(ReviewManager.tool_mark),
        ),
        ToolSpec(
            name="draft_message",
            description=(
                "Draft the message for the reviewed agent once the human wants to send: the adopted findings (file:line, problem, fix) "
                "plus what they added. It is shown to the human with a Send button; you cannot send anything yourself."
            ),
            input_schema={"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
            handler=call(ReviewManager.tool_draft),
        ),
    ]


# ---------------------------------------------------------------------------
# materials
# ---------------------------------------------------------------------------
def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") and int(v) > 0 else None
    except (TypeError, ValueError):
        return None


def _interrupted(h: "LiveHandle") -> bool:
    res = _last_result(h)
    return bool(res and res.get("terminal_reason") in INTERRUPTED)


def _last_result(h: "LiveHandle") -> Item | None:
    return next((it for it in reversed(h.items) if it.get("kind") == "result"), None)


def _materials(project: "Project", wsid: str, cwd: str, hid: str, prev: dict[str, Any] | None) -> dict[str, Any]:
    """The files to review and their before / after content: what changed since the previous round's
    snapshot (``scope`` "new"), else everything the session changed ("all")."""
    files = [f for f in changes.session_changes(project, wsid, cwd, hid)["files"] if not f["unchanged"]]
    head = snapshots.take(cwd, "review")
    base = (prev or {}).get("head")
    if head and base and base.get("root") == head["root"] and snapshots.tree_exists(head["root"], base["tree"]):
        moved = set(snapshots.changed_paths(head["root"], base["tree"], head["tree"]))
        root = head["root"]
        picked = []
        for f in files:
            if f["path"] not in moved:
                continue
            rel = os.path.relpath(f["path"], root)
            had, before = snapshots.read_blob(root, base["tree"], rel)
            has, after = snapshots.read_blob(root, head["tree"], rel)
            add, rem = _count(before if had else None, after if has else None)
            status = "created" if not had else "deleted" if not has else "modified"  # since the previous round
            picked.append({**f, "status": status, "before": before if had else None, "after": after if has else None,
                           "added": add, "removed": rem, "known": True})
        return {"scope": "new", "files": picked, "head": head, "base": base}
    contents = changes.contents(project, wsid, cwd, hid, [f["path"] for f in files])
    picked = [{**f, **contents.get(f["path"], {"before": None, "after": None, "known": False})} for f in files]
    return {"scope": "all", "files": picked, "head": head, "base": None}


def _count(a: str | None, b: str | None) -> tuple[int, int]:
    add = rem = 0
    for line in difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            rem += 1
    return add, rem


def _patch(f: dict[str, Any]) -> str:
    rel = f["rel"]
    a = (f.get("before") or "").splitlines(keepends=True)
    b = (f.get("after") or "").splitlines(keepends=True)
    lines = difflib.unified_diff(a, b, fromfile="/dev/null" if f.get("before") is None else f"a/{rel}",
                                 tofile="/dev/null" if f.get("after") is None else f"b/{rel}", n=3)
    return "".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in lines)


def _document(r: dict[str, Any], name: str, cwd: str, hid: str, intent: dict[str, Any], mat: dict[str, Any],
              sent: list[dict[str, Any]], prev: dict[str, Any] | None) -> str:
    files = mat["files"]
    out = [f"# Review round {r['n']} — {name}", ""]
    out.append(f"Agent: {name} ({hid}), working directory `{cwd}`")
    if mat["scope"] == "new" and prev:
        out.append(f"Scope: the changes since round {prev['n']} (git snapshots `{mat['base']['tree']}` → `{mat['head']['tree']}`; "
                   f"`git diff {mat['base']['tree'][:12]} {mat['head']['tree'][:12]} -- <path>` from `{mat['head']['root']}`)")
    else:
        out.append("Scope: everything this agent changed in its session (against each file's content before the agent first touched it)")
    out += ["", "## What the agent was asked (its latest prompts, oldest first)", ""]
    out += [f"> {_short(a, 2000)}".replace("\n", "\n> ") + "\n" for a in intent["asks"]] or ["(none found)", ""]
    out += ["## What it says it did", ""]
    if intent["doing"] or intent["summary"]:
        out.append(f"Board status: {intent['doing'] or '—'}" + (f" — {intent['summary']}" if intent["summary"] else ""))
    if intent["cards"]:
        out.append("Its board cards: " + ", ".join(intent["cards"]))
    out += ["", "Its last reply:", "", _short(intent["reply"], 4000) or "(none)", ""]
    if sent:
        out += [f"## Sent to the agent after round {prev['n'] if prev else '?'} — check these first", ""]
        out += [f"- #{f['id']} [{f['severity']}] {f['file']}{':' + str(f['line']) if f.get('line') else ''} — {f['title']}" for f in sent]
        out.append("")
    out += [f"## Changed files ({len(files)}, +{sum(f['added'] for f in files)} −{sum(f['removed'] for f in files)})", ""]
    out += [f"- {f['status']}: {f['rel']} (+{f['added']} −{f['removed']})" for f in files] or ["(none)"]
    out += ["", "## Diff", ""]
    budget = MAX_DIFF
    for f in files:
        if GENERATED.search(f["rel"]):
            out.append(f"### {f['rel']}\n(lockfile / build output: diff left out)\n")
            continue
        if not f.get("known"):
            out.append(f"### {f['rel']}\n(its content before the agent is unknown: read the file as it is now)\n")
            continue
        patch = _patch(f)
        if len(patch) > min(MAX_FILE_DIFF, budget):
            out.append(f"### {f['rel']}\n(diff too long for this file — {len(patch)} characters: read the file, or use git diff)\n")
            continue
        budget -= len(patch)
        out.append(f"### {f['rel']}\n```diff\n{patch}```\n")
    return "\n".join(out)


def _write_materials(doc: str, label: str) -> Attachment:
    """Into the uploads folder, like a composer attachment: readable by the reviewer without asking,
    and openable from the chat."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    data = doc.encode()
    safe = re.sub(r"[^\w.\-]+", "_", label).strip("._") or "review.md"
    path = UPLOADS_DIR / f"{hashlib.sha1(data).hexdigest()[:12]}__{safe[-80:]}"
    if not path.exists():
        path.write_bytes(data)
    return Attachment(path=str(path), name=safe, mime="text/markdown")
