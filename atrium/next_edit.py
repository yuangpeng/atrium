"""Next edit, as Cursor's Tab after Tab: after an edit, where the next one goes and what it is (the same rename further
down, an argument added at the other calls, what the change needs elsewhere). The page (editor/nextEdit.ts) marks the
place; Tab goes there, Tab again makes the edit.

The model gets the human's recent edits in the file (the page keeps them, as changed lines), the code around the
cursor (the whole file, if it is short) and the errors the language server reports in it, and answers with SEARCH/REPLACE blocks: what to find in that code and what it becomes. A block is short: the
whole region written out again would be many times the tokens, and the time. Each must be found in the code it was
shown, once (exactly, or but for whitespace at the lines' ends); one that isn't is dropped. They come back as line
ranges of the file, the nearest to the cursor first, below it before above.

It is asked for only with room to spare under ``completion.max_per_minute`` (complete.py's throttle): the suggestion
at the cursor comes first.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import complete
from .complete import CURSOR, CompletionError

MAX_EDITS = 3  # blocks taken from an answer
MAX_TOKENS = 700
REGION_CHARS = 6000  # of code around the cursor (the page sends whole lines)
MAX_PROBLEMS = 8

SYSTEM = f"""You predict the next edit a programmer will make in a code editor. You see their recent edits in a file, \
oldest first, and the code around the cursor ({CURSOR} marks the cursor; it is not part of the code).

Reply with the edits that follow from what they just did, in the code shown: the same change where else it applies (a \
rename, an argument added or removed, a type or a call changed), or what the change needs elsewhere (an import, a \
matching update). The language server's errors in that code, when given, often show where. Only what clearly follows \
from the recent edits. Don't continue the code at the cursor itself: that is suggested separately. If nothing clearly \
follows, reply NONE.

Write each edit as a block:
<<<<<<< SEARCH
lines of the code shown, copied exactly (only as many as it takes to find them once)
=======
those lines after the edit
>>>>>>> REPLACE

At most {MAX_EDITS} blocks, the nearest to the cursor first. No explanations, nothing else."""

_BLOCK = re.compile(r"<{5,}\s*SEARCH[^\n]*\n(.*?)\n?={5,}[^\n]*\n(.*?)\n?>{5,}\s*REPLACE", re.S)


def blocks(out: str) -> list[tuple[str, str]]:
    """The SEARCH/REPLACE blocks of the model's answer, without the cursor's marker (it may copy it)."""
    return [(s.replace(CURSOR, ""), r.replace(CURSOR, "")) for s, r in _BLOCK.findall(out)]


def _find(lines: list[str], wanted: list[str]) -> tuple[int, bool] | None:
    """Where the lines ``wanted`` are in ``lines``, if they are there once: exactly, else but for trailing whitespace,
    else but for indentation (a model that re-indents: the second value says so)."""
    n = len(wanted)
    if not n or n > len(lines):
        return None
    for level, norm in enumerate((lambda s: s, str.rstrip, str.strip)):
        w = [norm(x) for x in wanted]
        if not any(w):
            return None  # blank lines alone: anywhere
        hits = [i for i in range(len(lines) - n + 1) if [norm(x) for x in lines[i:i + n]] == w]
        if len(hits) == 1:
            return hits[0], level == 2
        if hits:
            return None  # more than once: which one is meant isn't known
    return None


def _reindent(new: list[str], old: list[str], lines: list[str], at: int) -> list[str]:
    """A block the model re-indented: its replacement moved by as much as its first line is off."""
    pairs = [(a, b) for a, b in zip(lines[at:at + len(old)], old) if a.strip()]
    if not pairs:
        return new
    have, gave = pairs[0]
    delta = (len(have) - len(have.lstrip())) - (len(gave) - len(gave.lstrip()))
    if delta > 0:
        return [" " * delta + x if x.strip() else x for x in new]
    return [x[min(-delta, len(x) - len(x.lstrip())):] for x in new]


def edits(region: str, start: int, out: str, cursor_line: int) -> list[dict[str, Any]]:
    """The edits in the model's answer ``out``, as line ranges of the file: ``region`` is the code it was shown (with the
    cursor's marker), from line ``start`` (1-based). Each: ``start`` and ``end`` (the lines it replaces, end excluded,
    start == end: inserted before ``start``), ``old`` (those lines) and ``text`` (what replaces them)."""
    lines = region.replace(CURSOR, "").split("\n")
    found: list[dict[str, Any]] = []
    taken: list[tuple[int, int]] = []
    for search, replace in blocks(out):
        old, new = search.split("\n"), replace.split("\n") if replace else []
        hit = _find(lines, old)
        if hit is None or old == new:
            continue
        at, moved = hit
        if moved:
            new = _reindent(new, old, lines, at)
        s, e = at, at + len(old)
        # the lines the block keeps at its ends needn't be replaced: what changes is between them
        while s < e and new and lines[s] == new[0]:
            s, new = s + 1, new[1:]
        while e > s and new and lines[e - 1] == new[-1]:
            e, new = e - 1, new[:-1]
        if s == e and not new:
            continue
        if any(s < b and a < e or s == e == a for a, b in taken):  # overlapping another
            continue
        taken.append((s, e))
        found.append({"start": start + s, "end": start + e, "old": "\n".join(lines[s:e]), "text": "\n".join(new)})
    # nearest to the cursor first, those below it before those above
    found.sort(key=lambda x: (x["start"] < cursor_line, abs(x["start"] - cursor_line)))
    return found[:MAX_EDITS]


async def next_edit(pid: str, path: str, label: str, language: str, region: str, start: int, cursor_line: int, events: list[Any],
                    problems: list[str] | None = None) -> dict[str, Any]:
    """The edits that follow the recent ones (``events``), in ``region`` (the code around the cursor, from line
    ``start``, the cursor marked): {edits: [...]}, none when there is nothing to go on or no room under the limit.
    ``problems``: the language server's errors in the region, each after the code of its line."""
    s = complete.settings()
    if not s.get("model") or not s["base_url"]:
        raise CompletionError("Inline completion has no model or server: set completion.model and completion.base_url", 400)
    history = complete.events_text(events)
    if not history or language in complete.SKIP_LANGUAGES or complete.SECRET_FILES.search(path.rsplit("/", 1)[-1]):
        return {"edits": []}
    if not complete._throttle(s).spare(complete._limit(s)):  # the suggestions at the cursor first
        return {"edits": [], "skipped": "rate"}
    payload: dict[str, Any] = {
        "model": s["model"],
        "stream": True,
        "temperature": 0,
        "max_completion_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"File: {label}{f' ({language})' if language else ''}\n\nRecent edits, oldest first:\n{history}\n\n"
                                        f"The code around the cursor (lines {start}–{start + region.count(chr(10))}):\n{region[:REGION_CHARS]}"
                                        + (f"\n\nErrors the language server reports in it, each after the code of its line:\n" + "\n".join(p[:400] for p in problems[:MAX_PROBLEMS]) if problems else "")},
        ],
    }
    if s.get("effort"):
        payload["reasoning_effort"] = s["effort"]
    for k in complete._omit.get((s["base_url"], s["model"]), ()):
        payload = complete._without(payload, k)
    started = time.monotonic()
    out = ""
    async for piece in complete._stream(s, payload, complete._url(pid, s)):
        out += piece
    return {"edits": edits(region, start, out, cursor_line), "ms": round((time.monotonic() - started) * 1000)}
