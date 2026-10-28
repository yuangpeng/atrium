"""Git plumbing: graph data, branches, worktrees, status, staging, commits."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

SEP = "\x1f"


class GitError(RuntimeError):
    pass


async def run_git(cwd: str, *args: str, check: bool = True, max_bytes: int = 20_000_000) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise GitError(err.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
    return out[:max_bytes].decode(errors="replace")


def _run_sync(cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def is_repo(path: str) -> bool:
    return (_run_sync(path, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def repo_root(path: str) -> str | None:
    out = _run_sync(path, "rev-parse", "--show-toplevel")
    return out.strip() if out else None


def _parse_worktrees(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in text.splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": os.path.realpath(val), "branch": None, "head": None, "detached": False, "locked": False, "prunable": False, "bare": False}
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key in ("detached", "bare"):
            cur[key] = True
        elif key in ("locked", "prunable"):
            cur[key] = val or True
    for i, w in enumerate(out):
        w["main"] = i == 0
    return out


def worktree_paths_sync(path: str) -> list[str]:
    text = _run_sync(path, "worktree", "list", "--porcelain")
    if not text:
        return []
    return [w["path"] for w in _parse_worktrees(text) if not w.get("bare")]


async def worktrees(path: str) -> list[dict[str, Any]]:
    try:
        return _parse_worktrees(await run_git(path, "worktree", "list", "--porcelain"))
    except GitError:
        return []


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "wt"


async def add_worktree(path: str, branch: str, base: str | None = None, create_branch: bool = True, dest: str | None = None) -> dict[str, Any]:
    root = repo_root(path) or path
    dest = dest or os.path.join(root, ".atrium", "worktrees", _slug(branch))
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    if create_branch:
        args = ["worktree", "add", "-b", branch, dest] + ([base] if base else [])
    else:
        args = ["worktree", "add", dest, branch]
    await run_git(path, *args)
    return {"path": os.path.realpath(dest), "branch": branch}


async def remove_worktree(path: str, wt_path: str, force: bool = False) -> None:
    args = ["worktree", "remove"] + (["--force"] if force else []) + [wt_path]
    await run_git(path, *args)


async def branches(path: str) -> list[dict[str, Any]]:
    fmt = SEP.join(
        ["%(refname)", "%(refname:short)", "%(objectname)", "%(upstream:short)", "%(upstream:track)", "%(committerdate:unix)", "%(subject)", "%(HEAD)"]
    )
    text = await run_git(path, "for-each-ref", "--sort=-committerdate", f"--format={fmt}", "refs/heads", "refs/remotes")
    out = []
    for line in text.splitlines():
        parts = line.split(SEP)
        if len(parts) < 8 or parts[0].endswith("/HEAD"):
            continue
        ref, short, sha, upstream, track, date, subject, head = parts[:8]
        out.append(
            {
                "ref": ref,
                "name": short,
                "remote": ref.startswith("refs/remotes/"),
                "sha": sha,
                "upstream": upstream or None,
                "track": track or None,
                "date": int(date or 0),
                "subject": subject,
                "head": head == "*",
            }
        )
    return out


async def current_branch(path: str) -> str | None:
    try:
        name = (await run_git(path, "rev-parse", "--abbrev-ref", "HEAD")).strip()
        return None if name == "HEAD" else name
    except GitError:
        return None


async def smart_refs(path: str, limit: int = 8) -> list[str]:
    """Heuristic default for the graph: HEAD, worktree branches, trunk, recently active branches."""
    bs = await branches(path)
    wts = await worktrees(path)
    names = {b["name"] for b in bs}
    picked: list[str] = []

    def add(n: str | None) -> None:
        if n and n in names and n not in picked:
            picked.append(n)

    for b in bs:
        if b["head"]:
            add(b["name"])
            add(b["upstream"])
    for w in wts:
        add(w.get("branch"))
    for trunk in ("main", "master", "develop", "origin/main", "origin/master"):
        add(trunk)
    cutoff = time.time() - 14 * 86400
    for b in bs:
        if len(picked) >= limit:
            break
        if not b["remote"] and b["date"] >= cutoff:
            add(b["name"])
    return picked[: max(limit, len(picked))]


async def graph(path: str, refs: list[str] | None = None, limit: int = 400) -> dict[str, Any]:
    fmt = SEP.join(["%H", "%P", "%D", "%an", "%at", "%s"])
    rev_args = refs if refs else ["--all"]
    try:
        text = await run_git(path, "log", "--topo-order", f"--format={fmt}", "-n", str(limit), *rev_args, "--")
    except GitError as e:
        if "does not have any commits" in str(e) or "unknown revision" in str(e):
            return {"commits": [], "head": None}
        raise
    commits = []
    for line in text.splitlines():
        parts = line.split(SEP)
        if len(parts) < 6:
            continue
        sha, parents, deco, author, at, subject = parts[:6]
        refs_list = [r.strip() for r in deco.split(",") if r.strip()] if deco else []
        commits.append(
            {"sha": sha, "parents": parents.split() if parents else [], "refs": refs_list, "author": author, "time": int(at or 0), "subject": subject}
        )
    head = None
    try:
        head = (await run_git(path, "rev-parse", "HEAD")).strip()
    except GitError:
        pass
    return {"commits": commits, "head": head}


async def status(path: str) -> dict[str, Any]:
    text = await run_git(path, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
    info: dict[str, Any] = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "files": []}
    fields = text.split("\0")
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if not f:
            continue
        if f.startswith("# branch.head "):
            info["branch"] = f[len("# branch.head ") :]
        elif f.startswith("# branch.upstream "):
            info["upstream"] = f[len("# branch.upstream ") :]
        elif f.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", f)
            if m:
                info["ahead"], info["behind"] = int(m.group(1)), int(m.group(2))
        elif f[0] == "1":
            p = f.split(" ", 8)
            xy = p[1]
            info["files"].append({"path": p[8], "index": xy[0], "worktree": xy[1]})
        elif f[0] == "2":
            p = f.split(" ", 9)
            xy = p[1]
            orig = fields[i] if i < len(fields) else ""
            i += 1
            info["files"].append({"path": p[9], "orig": orig, "index": xy[0], "worktree": xy[1]})
        elif f[0] == "u":
            p = f.split(" ", 10)
            info["files"].append({"path": p[10], "index": "U", "worktree": "U"})
        elif f[0] == "?":
            info["files"].append({"path": f[2:], "index": "?", "worktree": "?"})
    return info


def status_map_sync(path: str) -> dict[str, str]:
    """rel path -> single-letter decoration for the file tree."""
    text = _run_sync(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    out: dict[str, str] = {}
    if not text:
        return out
    fields = text.split("\0")
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        xy, p = f[:2], f[3:]
        if xy[0] == "R":
            i += 1
        code = "?" if xy == "??" else ("A" if "A" in xy else "D" if "D" in xy else "U" if "U" in xy else "M")
        out[p] = code
    return out


async def show_file_at(path: str, rev: str, rel: str) -> str | None:
    try:
        return await run_git(path, "show", f"{rev}:{rel}")
    except GitError:
        return None


async def stage(path: str, files: list[str]) -> None:
    await run_git(path, "add", "--", *files)


async def unstage(path: str, files: list[str]) -> None:
    try:
        await run_git(path, "restore", "--staged", "--", *files)
    except GitError:
        await run_git(path, "rm", "--cached", "--quiet", "--", *files)  # repo without commits


async def discard(path: str, files: list[str]) -> None:
    await run_git(path, "restore", "--worktree", "--", *files)


async def init(path: str) -> None:
    await run_git(path, "init")


async def commit(path: str, message: str, amend: bool = False) -> str:
    args = ["commit", "-m", message] + (["--amend"] if amend else [])
    return await run_git(path, *args)


async def checkout(path: str, branch: str) -> None:
    await run_git(path, "switch", branch)


async def create_branch(path: str, name: str, base: str | None = None) -> None:
    await run_git(path, "branch", name, *([base] if base else []))


async def commit_detail(path: str, sha: str) -> dict[str, Any]:
    fmt = SEP.join(["%H", "%P", "%an", "%ae", "%at", "%D", "%B"])
    head = await run_git(path, "show", "-s", f"--format={fmt}", sha)
    parts = head.split(SEP)
    numstat = await run_git(path, "show", "--format=", "--numstat", "-M", sha)
    files = []
    for line in numstat.splitlines():
        cols = line.split("\t")
        if len(cols) == 3:
            files.append({"path": cols[2], "added": cols[0], "removed": cols[1]})
    patch = await run_git(path, "show", "--format=", "--patch", "-M", "--no-color", sha, max_bytes=400_000)
    return {
        "sha": parts[0],
        "parents": parts[1].split() if len(parts) > 1 and parts[1] else [],
        "author": parts[2] if len(parts) > 2 else "",
        "email": parts[3] if len(parts) > 3 else "",
        "time": int(parts[4]) if len(parts) > 4 and parts[4] else 0,
        "refs": parts[5] if len(parts) > 5 else "",
        "message": parts[6].strip() if len(parts) > 6 else "",
        "files": files,
        "patch": patch,
    }
