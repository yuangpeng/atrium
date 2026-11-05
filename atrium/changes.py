"""Per-session change sets for human review.

For every file a session changed, the UI diffs a *baseline* (the file before
the session first touched it) against the file on disk now. Baselines come
from, in order of preference:

1. **git snapshots** taken at the start of each turn (``snapshots.py``) — the
   snapshot preceding the session's first change to the file;
2. **the transcript** — Claude Code records the original file with each edit;
   Codex records patches, which are reverse-applied to the current content;
3. **HEAD**, when neither is available.

Files changed during a session's turns by other means (shell commands,
code generators…) are found by diffing each turn's start/end snapshots. The
content the agent left behind (end-of-turn snapshot, else transcript) is used
to flag *drift*: later edits by the human or other agents.

Once the checkout a session worked in is removed (a worktree, e.g. after it was
merged), its files are shown as the agent left them, read-only.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import gitops, snapshots
from .fs import read_text, sha_of
from .harness import get_harness
from .harness.base import FileEdit, Replacement
from .harness.util import iso_to_epoch
from .project import Project

# (transcript, session) -> (what they were read from, the session's edits): the last few sessions', whose files are
# looked at one after another. Not every session's: an edit tool's result can hold the whole file, so the edits of a
# directory's sessions took as much memory as their transcripts (gigabytes), and kept it.
_edit_cache: dict[tuple[str, str], tuple[Any, list[FileEdit]]] = {}
_EDIT_CACHE_MAX = 8
# (project, session) -> (what its change set was worked out from, the change set): the review inbox and the Tracker ask
# for every agent's, again whenever a file changes, and one is tens of git calls and a read of its transcript
_changes_cache: dict[tuple[str, str], tuple[Any, ...]] = {}
_CHANGES_CACHE_MAX = 4096
_cache_lock = threading.Lock()
_roots: dict[str, tuple[float, str | None]] = {}  # directory -> (when, the checkout holding it)
_UNKNOWN = object()  # baseline / agent-final content could not be determined
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _edits(project: Project, sid: str, cwd: str, harness_id: str) -> list[FileEdit]:
    """The session's file edits, in order: a fork's own (not those its transcript copied from its source), holding only
    what its changes are worked out from (``_slim``)."""
    harness = get_harness(harness_id)
    path = harness.transcript_path(sid, cwd)
    if not path:
        return []
    inherited = project.session_meta(sid).get("forked_at")
    key, state = (str(path), sid), (_state(str(path)), inherited)
    with _cache_lock:
        hit = _edit_cache.pop(key, None)
    if hit is None or hit[0] != state:
        edits = [e for e in harness.file_edits(sid, cwd) if not inherited or ((ts := iso_to_epoch(e.ts)) is not None and ts > inherited)]
        hit = (state, _slim(edits))
    with _cache_lock:
        _edit_cache[key] = hit  # the most recently used last
        while len(_edit_cache) > _EDIT_CACHE_MAX:
            del _edit_cache[next(iter(_edit_cache))]
    return hit[1]


def _slim(edits: list[FileEdit]) -> list[FileEdit]:
    """``edits`` with only the contents a file's changes are worked out from: before its first edit (the baseline) and
    after its last (what the agent left). Patches, replayed backwards, all stay."""
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    for i, e in enumerate(edits):
        first.setdefault(e.path, i)
        last[e.path] = i
    out = []
    for i, e in enumerate(edits):
        before = e.before if first[e.path] == i else None
        after = e.after if last[e.path] == i else None
        out.append(e if (before, after) == (e.before, e.after) else dataclasses.replace(e, before=before, after=after))
    return out


def _stats(a: str | None, b: str | None) -> tuple[int, int]:
    add = rem = 0
    for line in difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            rem += 1
    return add, rem


def reverse_patch(after: str, patch: str) -> str | None:
    """Undo unified-diff hunks on ``after``; None if they do not apply."""
    hunks: list[tuple[int, list[str], list[str]]] = []
    cur: tuple[int, list[str], list[str]] | None = None
    for line in patch.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            cur = (int(m.group(3)), [], [])
            hunks.append(cur)
        elif cur is not None and line and line[0] in " +-":
            if line[0] in " +":
                cur[1].append(line[1:])
            if line[0] in " -":
                cur[2].append(line[1:])
    if not hunks:
        return None
    lines = after.split("\n")
    for start, new, old in reversed(hunks):
        idx = start if not new else start - 1
        found = None
        for delta in range(0, 60):
            for i in {idx - delta, idx + delta}:
                if 0 <= i <= len(lines) and lines[i : i + len(new)] == new:
                    found = i
                    break
            if found is not None:
                break
        if found is None:
            return None
        lines[found : found + len(new)] = old
    return "\n".join(lines)


def apply_patch(before: str, patch: str) -> str | None:
    """Apply unified-diff hunks to ``before``; None if they do not apply."""
    inverse, hunks = [], False
    for line in patch.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            hunks = True
            inverse.append(f"@@ -{m.group(3)},{m.group(4) or 1} +{m.group(1)},{m.group(2) or 1} @@")
        elif hunks and line[:1] in ("+", "-"):
            inverse.append({"+": "-", "-": "+"}[line[0]] + line[1:])
        else:
            inverse.append(line)
    return reverse_patch(before, "\n".join(inverse))


def apply_replacements(text: str | None, reps: list[Replacement]) -> str | None:
    """Make an edit's string replacements (``FileEdit.replacements``) on ``text`` (None: no such file), in order. None
    if one doesn't apply: its ``old`` isn't there (in the lines it was searched in), or more than one occurrence is
    where the edit changed only one."""
    for r in reps:
        if r.old == "":  # creates the file (Claude's Edit with no old_string)
            if text:
                return None
            text = r.new
            continue
        if text is None:
            return None
        lo, hi = 0, len(text)
        if r.lines:  # the text of those lines, without the newline that ends the last (as Antigravity searches them)
            starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
            first, last = max(r.lines[0], 1), max(r.lines[1], r.lines[0], 1)
            lo = starts[first - 1] if first - 1 < len(starts) else len(text)
            hi = starts[last] - 1 if last < len(starts) else len(text)
        found, i = [], text.find(r.old, lo)
        while i != -1 and i + len(r.old) <= hi:
            found.append(i)
            i = text.find(r.old, i + len(r.old))
        if not r.all and not found and r.lines:  # the lines moved (a command changed the file): the only occurrence anywhere
            found = [i for i in [text.find(r.old)] if i != -1 and text.count(r.old) == 1]
        if not found or not r.all and len(found) > 1:
            return None
        for i in reversed(found):
            text = text[:i] + r.new + text[i + len(r.old):]
    return text


def _snapshots(project: Project, sid: str) -> list[dict[str, Any]]:
    # "git": where to read them — every checkout of the repository has the objects, the project's
    # own one included, so a removed worktree's snapshots stay readable
    return [{**s, "git": s["root"] if os.path.isdir(s["root"]) else project.path}
            for s in project.session_meta(sid).get("snapshots") or [] if s.get("tree") and s.get("root")]


def _gone_roots(project: Project, sid: str, cwd: str, snaps: list[dict[str, Any]]) -> list[str]:
    """Directories the session worked in that no longer exist (removed worktrees)."""
    moves = project.session_meta(sid).get("moves") or []
    return [r for r in dict.fromkeys([cwd, *(s["root"] for s in snaps), *(m["from"] for m in moves)]) if r and not os.path.isdir(r)]


def _turns(snaps: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out, start = [], None
    for s in snaps:
        if s["phase"] == "start":
            start = s
        elif s["phase"] == "end" and start and start["root"] == s["root"]:
            out.append((start, s))
            start = None
    return out


def _inside(path: str, root: str) -> bool:
    return path.startswith(root.rstrip(os.sep) + os.sep)


def _collect(project: Project, sid: str, cwd: str, harness_id: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    files: dict[str, dict[str, Any]] = {}
    for e in _edits(project, sid, cwd, harness_id):
        ts = iso_to_epoch(e.ts)
        f = files.get(e.path)
        if f is None:
            files[e.path] = f = {"path": e.path, "edits": [], "tools": set(), "first": ts, "last": ts, "detected": False}
        f["edits"].append(e)
        f["tools"].add(e.tool)
        f["last"] = ts or f["last"]
    snaps = _snapshots(project, sid)
    for a, b in _turns(snaps):
        if a["tree"] == b["tree"]:
            continue
        for p in snapshots.changed_paths(a["root"], a["tree"], b["tree"], git=a["git"]):
            if "/.atrium/" in p and "/.atrium/worktrees/" not in p:
                continue
            f = files.get(p)
            if f is None:
                files[p] = {"path": p, "edits": [], "tools": {"shell"}, "first": a["ts"], "last": b["ts"], "detected": True}
            else:
                f["first"] = min(x for x in (f["first"], a["ts"]) if x is not None)
                f["last"] = max(x for x in (f["last"], b["ts"]) if x is not None)
    return files, snaps


def _baseline(f: dict[str, Any], snaps: list[dict[str, Any]], current: str | None) -> tuple[Any, ...]:
    """(content before the session, where it was found[, the checkout whose HEAD it is])"""
    path, first = f["path"], f["first"]
    starts = [s for s in snaps if s["phase"] == "start" and _inside(path, s["root"]) and (first is None or s["ts"] <= first + 1)]
    if starts:
        s = starts[-1]
        rel = os.path.relpath(path, s["root"])
        present, content = snapshots.read_blob(s["git"], s["tree"], rel)
        if present:
            return content, "snapshot"
        if snapshots.tree_exists(s["git"], s["tree"]) and not snapshots.is_ignored(s["git"], rel):
            return None, "snapshot"  # not there before → created by the session
    edits: list[FileEdit] = f["edits"]
    if edits:
        first_edit = edits[0]
        if first_edit.patch is None:
            if first_edit.before is not None or first_edit.kind == "create":
                return first_edit.before, "transcript"
        elif current is not None:  # replay patches backwards from the file on disk
            content: str | None = current
            for e in reversed(edits):
                if e.kind == "create":
                    return None, "transcript"
                if e.patch is None or content is None:
                    content = None
                    break
                content = reverse_patch(content, e.patch)
            if content is not None:
                return content, "transcript"
    root = _repo_root(os.path.dirname(path)) if os.path.isdir(os.path.dirname(path)) else None
    if root:
        present, content = snapshots.head_blob(root, os.path.relpath(path, root))
        return (content if present else None), "head", root
    return _UNKNOWN, "unknown"


def _repo_root(d: str) -> str | None:
    """The checkout holding directory ``d``, as git said in the last minute: an agent's files are in a few directories."""
    hit = _roots.get(d)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    root = gitops.repo_root(d)
    _roots[d] = (time.monotonic(), root)
    return root


def _agent_final(f: dict[str, Any], snaps: list[dict[str, Any]]) -> Any:
    last = f["last"]
    ends = [s for s in snaps if s["phase"] == "end" and _inside(f["path"], s["root"]) and last is not None and s["ts"] >= last - 1]
    if ends:
        s = ends[0]
        present, content = snapshots.read_blob(s["git"], s["tree"], os.path.relpath(f["path"], s["root"]))
        if present or snapshots.tree_exists(s["git"], s["tree"]):
            return content
    edits: list[FileEdit] = f["edits"]
    if edits:
        e = edits[-1]
        if e.after is not None or e.kind == "delete":
            return e.after
    return _UNKNOWN


def _resolve(f: dict[str, Any], snaps: list[dict[str, Any]], gone_roots: list[str]) -> dict[str, Any]:
    path = f["path"]
    exists = os.path.exists(path)
    current = read_text(Path(path)) if exists else None
    final = _agent_final(f, snaps)
    gone = not exists and any(_inside(path, r) for r in gone_roots)
    if gone and final is not _UNKNOWN:  # its checkout was removed: as the agent left it
        current, exists = final, final is not None
    baseline, source, *root = _baseline(f, snaps, current)
    return {"exists": exists, "current": current, "baseline": baseline, "source": source, "final": final, "gone": gone, "root": root[0] if root else None}


def _state(path: str) -> tuple[int, int, int] | None:
    """What says the file is as it was: its inode, size and mtime (None: not there)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def session_changes(project: Project, sid: str, cwd: str, harness_id: str, heads: dict[str, str | None] | None = None) -> dict[str, Any]:
    """The files the session changed, each with how it stands now. Worked out again only when something it is worked
    out from changed: its transcript, its snapshots, the files themselves, what was marked reviewed, and the HEAD of a
    checkout a file's baseline was read from; else neither the transcript nor git is read. ``heads``: those HEADs as
    already read (the same call for many sessions reads each once). The result is shared: don't change it."""
    meta = project.session_meta(sid)
    snaps = _snapshots(project, sid)
    gone_roots = _gone_roots(project, sid, cwd, snaps)
    reviews = project.reviews(sid)
    transcript = get_harness(harness_id).transcript_path(sid, cwd)
    base = (cwd, str(transcript), transcript and _state(str(transcript)), meta.get("forked_at"), tuple(gone_roots),
            tuple(sorted(reviews.items())), tuple((s["tree"], s["root"], s["phase"], s["ts"]) for s in snaps))
    key = (project.id, sid)
    heads = {} if heads is None else heads
    hit = _changes_cache.get(key)
    if hit and hit[0] == base and all(_state(p) == st for p, st in hit[1]) and all(_head(root, heads) == head for root, head in hit[2].items()):
        return hit[3]
    files, snaps = _collect(project, sid, cwd, harness_id)
    states = [(p, _state(p)) for p in files]  # before their contents are read: a change meanwhile is seen next time
    used: dict[str, str | None] = {}  # the checkouts whose HEAD a baseline was read from
    out = []
    with snapshots.batch():  # the git objects read through one process
        for path, f in files.items():
            r = _resolve(f, snaps, gone_roots)
            if r["source"] == "head":
                used[r["root"]] = _head(r["root"], heads)
            baseline, current, exists = r["baseline"], r["current"], r["exists"]
            if f["detected"] and baseline is not _UNKNOWN and baseline == current:
                continue  # a shell-detected change that is gone again
            known = baseline is not _UNKNOWN
            if known and baseline is None:
                status = "created" if exists else "created+deleted"
            else:
                status = "deleted" if not exists else "modified"
            add, rem = _stats(baseline if known else None, current) if known else (0, 0)
            cur_sha = sha_of(current) if exists else "deleted"
            out.append({
                "path": path,
                "rel": os.path.relpath(path, cwd) if _inside(path, cwd) else path,
                "status": status,
                "edits": len(f["edits"]),
                "tools": sorted(f["tools"]),
                "added": add,
                "removed": rem,
                "unchanged": known and baseline == current and (baseline is not None or not exists),
                "drift": not r["gone"] and r["final"] is not _UNKNOWN and r["final"] != current,
                "gone": r["gone"],
                "reviewed": reviews.get(path) == cur_sha,
                "last_ts": f["last"],
                "baseline_source": r["source"],
                "detected": f["detected"],
            })
    out.sort(key=lambda x: x["rel"])
    result = {"session_id": sid, "cwd": cwd, "files": out, "snapshots": len(snaps)}
    with _cache_lock:
        _changes_cache.pop(key, None)
        _changes_cache[key] = (base, states, used, result)
        while len(_changes_cache) > _CHANGES_CACHE_MAX:
            del _changes_cache[next(iter(_changes_cache))]
    return result


def _head(root: str, heads: dict[str, str | None]) -> str | None:
    if root not in heads:
        heads[root] = gitops.head_sha(root)
    return heads[root]


def _file(project: Project, sid: str, cwd: str, harness_id: str, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    files, snaps = _collect(project, sid, cwd, harness_id)
    f = files.get(path)
    if not f:
        raise FileNotFoundError(path)
    return f, _resolve(f, snaps, _gone_roots(project, sid, cwd, snaps))


def file_change(project: Project, sid: str, cwd: str, harness_id: str, path: str) -> dict[str, Any]:
    _f, r = _file(project, sid, cwd, harness_id, path)
    return {
        "path": path,
        "rel": os.path.relpath(path, cwd) if _inside(path, cwd) else path,
        "baseline": None if r["baseline"] is _UNKNOWN else r["baseline"],
        "baseline_source": r["source"],
        "agent_final": None if r["final"] is _UNKNOWN else r["final"],
        "current": r["current"],
        "exists": r["exists"],
        "sha": sha_of(r["current"]) if r["exists"] else None,
        "gone": r["gone"],
    }


def contents(project: Project, sid: str, cwd: str, harness_id: str, paths: list[str]) -> dict[str, dict[str, Any]]:
    """Before the session (None: it didn't exist) and now, for some of a session's files; ``known`` is
    False when the content before is unknown."""
    files, snaps = _collect(project, sid, cwd, harness_id)
    gone_roots = _gone_roots(project, sid, cwd, snaps)
    out = {}
    for path in paths:
        if path in files:
            r = _resolve(files[path], snaps, gone_roots)
            known = r["baseline"] is not _UNKNOWN
            out[path] = {"before": r["baseline"] if known else None, "after": r["current"], "known": known}
    return out


def revert(project: Project, sid: str, cwd: str, harness_id: str, path: str) -> dict[str, Any]:
    """Restore the file to its content before this session first touched it."""
    project.resolve(path)  # must be inside the project
    _f, r = _file(project, sid, cwd, harness_id, path)
    if r["gone"]:
        raise PermissionError("The worktree this file was in has been removed: there is nothing to revert")
    if r["baseline"] is _UNKNOWN:
        raise RuntimeError("The content before this session is unknown (no snapshot, transcript or git history)")
    p = Path(path)
    if r["baseline"] is None:
        if p.exists():
            from .fs import trash

            trash(project, None, path)
        return {"path": path, "action": "removed"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(r["baseline"])
    return {"path": path, "action": "restored"}


def mark_reviewed(project: Project, sid: str, path: str, reviewed: bool) -> None:
    if not reviewed:
        project.set_review(sid, path, None)
        return
    current = read_text(Path(path)) if os.path.exists(path) else None
    project.set_review(sid, path, sha_of(current) if current is not None else "deleted")
