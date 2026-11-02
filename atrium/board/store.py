"""Board state store: ``board.json`` is the source of truth, ``board.html`` is rendered from it."""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import TYPE_CHECKING, Any

from ..events import hub
from .template import is_suggestion, render_board

if TYPE_CHECKING:
    from ..project import Project

STATUSES = ("working", "waiting", "blocked", "done", "idle")
TIMELINE_MAX = 200
TODO_DONE_MAX = 100  # done TODOs kept, the latest; open ones are all kept
TODO_SUGGESTED_MAX = 50  # open suggestions an agent can't add to: the human has a backlog to go through first


def _empty() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 0,
        "overview": {"headline": "", "html": "", "updated_at": 0},
        "agents": {},
        "timeline": [],
        # source human: the human's own (added on the board, or by an agent they asked to); agent: suggested by
        # an agent, until the human accepts it into their list
        "todos": [],
        "todo_seq": 0,
    }


class BoardStore:
    def __init__(self, project: "Project"):
        self.project = project
        self.json_path = project.state_dir / "board.json"
        self.html_path = project.state_dir / "board.html"
        self._lock = threading.RLock()
        try:
            self.state = json.loads(self.json_path.read_text())
        except (OSError, json.JSONDecodeError):
            self.state = _empty()
        for k, v in _empty().items():
            self.state.setdefault(k, v)
        if not self.json_path.exists():
            self._write()
        else:
            self._write_html()  # the template may have changed since it was last written (a new Atrium)

    # ---- read --------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state)

    def agent(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            a = self.state["agents"].get(sid)
            return copy.deepcopy(a) if a else None

    def agent_states(self) -> dict[str, dict[str, Any]]:
        """What session rows show of each agent (status, progress, doing, stats), without copying the cards."""
        with self._lock:
            return {sid: {"status": a.get("status"), "progress": a.get("progress"), "doing": a.get("doing"), "stats": dict(a["stats"]) if "stats" in a else None}
                    for sid, a in self.state["agents"].items()}

    def headline(self) -> str:
        with self._lock:
            return self.state["overview"].get("headline") or ""

    def todos(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.state["todos"])

    # ---- write -------------------------------------------------------------
    def _write(self) -> None:
        self.json_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))
        self._write_html()

    def _write_html(self) -> None:
        html = render_board(self.state, self.project.name, self.project.path)
        tmp = self.html_path.with_suffix(".html.tmp")
        tmp.write_text(html)
        tmp.replace(self.html_path)

    def _commit(self, change: str, sid: str | None = None) -> None:
        self.state["updated_at"] = time.time()
        self._write()
        hub.publish(f"project:{self.project.id}", {"type": "board", "change": change, "session_id": sid, "updated_at": self.state["updated_at"]})

    def _ensure(self, sid: str) -> dict[str, Any]:
        agents = self.state["agents"]
        if sid not in agents:
            agents[sid] = {
                "session_id": sid,
                "name": self.project.agent_name(sid) or "Agent",
                "status": "idle",
                "progress": 0,
                "doing": "",
                "summary": "",
                "updated_at": time.time(),
                "source": "",
                "cards": {},
                "stats": {},
            }
        return agents[sid]

    def update_agent(
        self,
        sid: str,
        *,
        name: str | None = None,
        status: str | None = None,
        progress: int | float | None = None,
        doing: str | None = None,
        summary: str | None = None,
        source: str = "self",
        stats: dict[str, Any] | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        if sid in self.project.deleted:  # e.g. a tracker run that started before the delete
            return {}
        with self._lock:
            a = self._ensure(sid)
            if name:
                a["name"] = name
            if status:
                a["status"] = status if status in STATUSES else "working"
            if progress is not None:
                a["progress"] = max(0, min(100, int(progress)))
            if doing is not None:
                a["doing"] = doing[:200]
            if summary is not None:
                a["summary"] = summary[:1200]
            if stats is not None:
                a["stats"] = {**a.get("stats", {}), **stats}
            if any(v is not None for v in (status, progress, doing, summary)):
                a["updated_at"] = time.time()
                a["source"] = source
            if not quiet:  # quiet: caller batches updates and calls rerender() once
                self._commit("agent", sid)
            return copy.deepcopy(a)

    def publish_card(self, sid: str, title: str, html: str, key: str = "main") -> None:
        with self._lock:
            a = self._ensure(sid)
            a["cards"][key or "main"] = {"title": title[:200], "html": html, "updated_at": time.time()}
            a["updated_at"] = time.time()
            self._commit("card", sid)

    def remove_card(self, sid: str, key: str) -> bool:
        with self._lock:
            a = self.state["agents"].get(sid)
            if not a or key not in a.get("cards", {}):
                return False
            del a["cards"][key]
            self._commit("card", sid)
            return True

    def log(self, sid: str | None, text: str, kind: str = "note", source: str = "self", quiet: bool = False) -> None:
        if sid and sid in self.project.deleted:
            return
        with self._lock:
            name = (self.state["agents"].get(sid) or {}).get("name") if sid else None
            self.state["timeline"].append({"ts": time.time(), "session_id": sid, "agent": name or "", "text": text[:400], "kind": kind, "source": source})
            self.state["timeline"] = self.state["timeline"][-TIMELINE_MAX:]
            if not quiet:
                self._commit("timeline", sid)

    def set_overview(self, headline: str | None, html: str | None, quiet: bool = False) -> None:
        with self._lock:
            ov = self.state["overview"]
            if headline:
                ov["headline"] = headline[:400]
            if html:
                ov["html"] = html
            ov["updated_at"] = time.time()
            if not quiet:
                self._commit("overview")

    # ---- TODOs -------------------------------------------------------------
    def add_todo(self, text: str, *, sid: str | None = None, why: str = "", source: str = "human") -> dict[str, Any]:
        """A new open TODO; the open one with the same text instead, if there is one. Raises ValueError when there is
        nothing to add, or an agent's suggestion would go past ``TODO_SUGGESTED_MAX``."""
        text = " ".join(text.split())[:300]
        if not text:
            raise ValueError("text must not be empty")
        with self._lock:
            todos = self.state["todos"]
            same = next((t for t in todos if not t.get("done") and t["text"].casefold() == text.casefold()), None)
            if same:
                return copy.deepcopy(same)
            if source == "agent" and sum(1 for t in todos if is_suggestion(t)) >= TODO_SUGGESTED_MAX:
                raise ValueError(f"{TODO_SUGGESTED_MAX} suggestions are waiting for the human already: suggest more once they have gone through them")
            self.state["todo_seq"] = self.state.get("todo_seq", 0) + 1
            name = (self.state["agents"].get(sid) or {}).get("name") or self.project.agent_name(sid) if sid else None
            now = time.time()
            t = {"id": f"t{self.state['todo_seq']}", "text": text, "why": " ".join(why.split())[:400], "source": "agent" if source == "agent" else "human",
                 "session_id": sid, "agent": name or "", "accepted": False, "done": False, "done_by": "", "created_at": now, "updated_at": now}
            todos.append(t)
            self._commit("todo", sid)
            return copy.deepcopy(t)

    def update_todo(self, tid: str, *, done: bool | None = None, accept: bool | None = None, text: str | None = None,
                    by: str = "") -> dict[str, Any] | None:
        """Check a TODO off (``by``: the agent who did it, "" for the human) or open it again, accept a suggestion
        into the human's list, or reword it. None if there is no such TODO."""
        with self._lock:
            t = next((t for t in self.state["todos"] if t["id"] == tid), None)
            if t is None:
                return None
            if text is not None and " ".join(text.split()):
                t["text"] = " ".join(text.split())[:300]
            if accept is not None and t["source"] == "agent":
                t["accepted"] = accept
            if done is not None and done != t["done"]:
                t["done"], t["done_by"], t["done_at"] = done, by if done else "", time.time()
                done_ = [x for x in self.state["todos"] if x["done"]]
                if len(done_) > TODO_DONE_MAX:  # the oldest done go first
                    drop = {x["id"] for x in sorted(done_, key=lambda x: x.get("done_at") or 0)[: len(done_) - TODO_DONE_MAX]}
                    self.state["todos"] = [x for x in self.state["todos"] if x["id"] not in drop]
            t["updated_at"] = time.time()
            self._commit("todo")
            return copy.deepcopy(t)

    def remove_todo(self, tid: str) -> bool:
        with self._lock:
            before = len(self.state["todos"])
            self.state["todos"] = [t for t in self.state["todos"] if t["id"] != tid]
            if len(self.state["todos"]) == before:
                return False
            self._commit("todo")
            return True

    def rename_agent(self, sid: str, name: str) -> None:
        with self._lock:
            if sid in self.state["agents"]:
                self.state["agents"][sid]["name"] = name
                self._commit("agent", sid)

    def remove_agent(self, sid: str, timeline: bool = False) -> None:
        """Remove an agent's tile (and with ``timeline``, its timeline entries too)."""
        with self._lock:
            removed = self.state["agents"].pop(sid, None) is not None
            if timeline:
                before = len(self.state["timeline"])
                self.state["timeline"] = [t for t in self.state["timeline"] if t.get("session_id") != sid]
                removed = removed or len(self.state["timeline"]) != before
            if removed:
                self._commit("agent", sid)

    def rerender(self) -> None:
        with self._lock:
            self._commit("render")
