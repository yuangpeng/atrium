"""Harness registry. Add new runtimes (pi, …) by registering them here."""

from __future__ import annotations

from .base import Harness, LiveSession, LiveSpec, SessionSummary, ToolSpec
from .claude_code import ClaudeCodeHarness
from .codex import CodexHarness

_HARNESSES: dict[str, Harness] = {}


def register(h: Harness) -> None:
    _HARNESSES[h.id] = h


def get_harness(hid: str | None) -> Harness:
    if hid and hid in _HARNESSES:
        return _HARNESSES[hid]
    return _HARNESSES["claude-code"]


def all_harnesses() -> list[Harness]:
    return list(_HARNESSES.values())


register(ClaudeCodeHarness())
register(CodexHarness())

__all__ = ["Harness", "LiveSession", "LiveSpec", "SessionSummary", "ToolSpec", "get_harness", "all_harnesses", "register"]
