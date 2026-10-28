"""Board state store: ``board.json`` is the source of truth, ``board.html`` is rendered from it."""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import TYPE_CHECKING, Any

from ..events import hub
from .template import render_board

if TYPE_CHECKING:
    from ..project import Project

STATUSES = ("working", "waiting", "blocked", "done", "idle")
TIMELINE_MAX = 200


def _empty() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 0,
        "overview": {"headline": "", "html": "", "updated_at": 0},
        "agents": {},
        "timeline": [],
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
        if not self.html_path.exists():
            self._write()

    # ---- read --------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state)

    def agent(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            a = self.state["agents"].get(sid)
            return copy.deepcopy(a) if a else None

    # ---- write -------------------------------------------------------------
    def _write(self) -> None:
        self.json_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))
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

    def rename_agent(self, sid: str, name: str) -> None:
        with self._lock:
            if sid in self.state["agents"]:
                self.state["agents"][sid]["name"] = name
                self._commit("agent", sid)

    def remove_agent(self, sid: str) -> None:
        with self._lock:
            if self.state["agents"].pop(sid, None) is not None:
                self._commit("agent", sid)

    def rerender(self) -> None:
        with self._lock:
            self._commit("render")
