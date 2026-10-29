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

import difflib
import os
import re
from pathlib import Path
from typing import Any

from . import gitops, snapshots
from .fs import read_text, sha_of
from .harness import get_harness
from .harness.base import FileEdit
from .harness.util import iso_to_epoch
from .project import Project

_edit_cache: dict[tuple[str, str], tuple[int, list[FileEdit]]] = {}
_UNKNOWN = object()  # baseline / agent-final content could not be determined
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _edits(sid: str, cwd: str, harness_id: str) -> list[FileEdit]:
    harness = get_harness(harness_id)
    path = harness.transcript_path(sid, cwd)
    if not path:
        return []
    size = path.stat().st_size
    key = (str(path), sid)
    hit = _edit_cache.get(key)
    if hit and hit[0] == size:
        return hit[1]
    edits = harness.file_edits(sid, cwd)
    _edit_cache[key] = (size, edits)
    return edits


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
    inherited = project.session_meta(sid).get("forked_at")  # a fork's transcript opens with its source's edits
    for e in _edits(sid, cwd, harness_id):
        ts = iso_to_epoch(e.ts)
        if inherited and (ts is None or ts <= inherited):
            continue
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


def _baseline(f: dict[str, Any], snaps: list[dict[str, Any]], current: str | None) -> tuple[Any, str]:
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
    root = gitops.repo_root(os.path.dirname(path)) if os.path.isdir(os.path.dirname(path)) else None
    if root:
        present, content = snapshots.head_blob(root, os.path.relpath(path, root))
        return (content if present else None), "head"
    return _UNKNOWN, "unknown"


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
    baseline, source = _baseline(f, snaps, current)
    return {"exists": exists, "current": current, "baseline": baseline, "source": source, "final": final, "gone": gone}


def session_changes(project: Project, sid: str, cwd: str, harness_id: str) -> dict[str, Any]:
    files, snaps = _collect(project, sid, cwd, harness_id)
    gone_roots = _gone_roots(project, sid, cwd, snaps)
    reviews = project.reviews(sid)
    out = []
    for path, f in files.items():
        r = _resolve(f, snaps, gone_roots)
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
    return {"session_id": sid, "cwd": cwd, "files": out, "snapshots": len(snaps)}


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
