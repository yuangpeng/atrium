"""Quick open (⌘P) and Find in Files (⌘⇧F) for the Files panel.

``list_files``: a checkout's files for quick open, as git lists them (tracked and new, not ignored); a folder that
isn't a repository is walked, less ``node_modules`` and the like.

``search``: the lines that match a query, through ripgrep when it is installed, else ``git grep``, else Python. It
honours ``.gitignore`` unless asked not to, never looks in ``.git`` or ``.atrium``, and stops at ``limit`` matches or
after ``TIMEOUT`` seconds, saying so. The panel searches as the query is typed: a page's new search stops its last one.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from .project import Project

LIST_MAX = 100_000  # files offered to quick open
TIMEOUT = 20.0  # seconds a search may take
MAX_LIMIT = 5000  # matches a search returns at most
MAX_FILE_BYTES = 4 * 1024 * 1024  # a larger file isn't searched (Python's search; ripgrep's --max-filesize)
PREVIEW = 240  # characters of a matching line sent; a longer one is cut round its first match
PER_FILE = 200  # matching lines sent per file: one huge file (a log) doesn't take the whole limit; the rest are counted
RG_PER_FILE = 1000  # where ripgrep stops reading one file (counted that far)
# walked past where git can't say what is ignored (a folder that isn't a repository), as by the file watch
_SKIP_DIRS = frozenset(("node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".git", ".atrium", ".next", "dist", "build", "target"))
_GLOB = re.compile(r"[*?\[{]")

_running: dict[str, asyncio.subprocess.Process] = {}  # page -> its search on its way
_pcre: bool | None = None  # git grep -P works (git built with PCRE)
_git_ver: tuple[int, int] | None = None


def _checkout(project: Project, root: str | None) -> str:
    root = os.path.realpath(root or project.path)
    project.resolve(".", root=root)  # one of the project's roots, or inside one; else PermissionError
    return root


def _git_lines(root: str, *args: str, timeout: float = 30) -> list[str] | None:
    """NUL-separated output of a git command as a list (None: not a repository, or git failed)."""
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=timeout, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _walk(root: str, cap: int) -> tuple[list[str], bool]:
    out: list[str] = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in _SKIP_DIRS)
        rel = os.path.relpath(d, root)
        for f in sorted(files):
            out.append(f if rel == "." else f"{rel}/{f}")
            if len(out) >= cap:
                return out, True
    return out, False


def list_files(project: Project, root: str | None) -> dict[str, Any]:
    """The files of a checkout (one of the project's roots), relative to it, for quick open."""
    root = _checkout(project, root)
    listed = _git_lines(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is None:
        files, truncated = _walk(root, LIST_MAX)
        return {"root": root, "files": files, "truncated": truncated}
    deleted = set(_git_lines(root, "ls-files", "-z", "--deleted") or [])  # tracked, but gone from the checkout
    # once each: a file in a merge conflict is in the index once per side (ls-files --deduplicate, git 2.31, does this)
    files = [p for p in dict.fromkeys(listed) if p not in deleted]
    return {"root": root, "files": files[:LIST_MAX], "truncated": len(files) > LIST_MAX}


# ---- search --------------------------------------------------------------------------------------------------------
def _patterns(items: str) -> list[str]:
    """Comma-separated globs as typed (``src``, ``*.py``, ``tests/**``): a bare name means the folder or file and all
    that is under it, as in VS Code."""
    out = []
    for raw in items.split(","):
        g = raw.strip().removeprefix("./").rstrip("/")
        if not g:
            continue
        out.append(g)
        if not _GLOB.search(g):
            out.append(f"{g}/**")
    return out


def _span_finder(query: str, regex: bool, case: bool, word: bool) -> Callable[[str], list[tuple[int, int]]] | None:
    """Where a line matches, in characters, for engines that don't say (git grep tells the first column only)."""
    body = query if regex else re.escape(query)
    if word:
        body = rf"\b(?:{body})\b"
    try:
        pat = re.compile(body, 0 if case else re.IGNORECASE)
    except re.error:
        return None
    return lambda line: [(m.start(), m.end()) for m in pat.finditer(line) if m.end() > m.start()]


def _preview(text: str, spans: list[tuple[int, int]]) -> tuple[str, list[list[int]]]:
    """The line as shown: no line break, its indent dropped, and cut round its first match when long."""
    text = text.rstrip("\r\n")
    lead = len(text) - len(text.lstrip())
    if spans and spans[0][0] < lead:
        lead = spans[0][0]
    text = text[lead:]
    spans = [(s - lead, e - lead) for s, e in spans]
    start = 0
    if len(text) > PREVIEW:
        start = max(0, (spans[0][0] if spans else 0) - 60)
    cut = text[start:start + PREVIEW]
    out = [[max(0, s - start), min(len(cut), e - start)] for s, e in spans if e > start and s < start + PREVIEW]
    if start > 0:
        cut = "…" + cut
        out = [[s + 1, e + 1] for s, e in out]
    if start + PREVIEW < len(text):
        cut += "…"
    return cut, out


class _Results:
    def __init__(self, limit: int):
        self.limit = limit
        self.files: dict[str, list[dict[str, Any]]] = {}
        self.more: dict[str, int] = {}  # matching lines past PER_FILE, by file: counted, not sent
        self.total = 0  # lines sent
        self.truncated = False

    def add(self, path: str, line: int, text: str, spans: list[tuple[int, int]]) -> bool:
        """Adds a matching line; False once the limit is reached."""
        lines = self.files.setdefault(path, [])
        if len(lines) >= PER_FILE:
            self.more[path] = self.more.get(path, 0) + 1
            return True
        if self.total >= self.limit:
            self.truncated = True
            return False
        col = spans[0][0] + 1 if spans else 1
        shown, ranges = _preview(text, spans)
        lines.append({"line": line, "col": col, "text": shown, "ranges": ranges})
        self.total += 1
        return True

    def payload(self, engine: str, started: float, error: str | None = None) -> dict[str, Any]:
        files = [{"path": p, "matches": m, "more": self.more.get(p, 0)} for p, m in sorted(self.files.items()) if m]
        return {"files": files, "total": self.total, "more": sum(self.more.values()), "truncated": self.truncated, "engine": engine,
                "elapsed": round(time.monotonic() - started, 3), "error": error}


async def _lines(proc: asyncio.subprocess.Process, deadline: float) -> AsyncIterator[bytes]:
    assert proc.stdout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        line = await asyncio.wait_for(proc.stdout.readline(), left)
        if not line:
            return
        yield line


def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _run(argv: list[str], root: str, page: str | None, res: _Results, parse: Callable[[bytes], bool | None], engine: str) -> dict[str, Any]:
    """Runs a search program, handing each line of its output to ``parse`` (False: enough); stops it at the limit,
    the deadline, or a newer search from the same page."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*argv, cwd=root, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                                                limit=8 * 1024 * 1024)
    if page:
        if (old := _running.get(page)) is not None:
            _stop(old)
        _running[page] = proc
    error = None
    try:
        async for line in _lines(proc, started + TIMEOUT):
            if parse(line) is False:
                break
    except (TimeoutError, asyncio.TimeoutError):
        res.truncated = True
        error = f"stopped after {TIMEOUT:.0f} s"
    except ValueError:  # a line longer than the reader's limit: what came before it stands
        res.truncated = True
    finally:
        _stop(proc)
        if page and _running.get(page) is proc:
            del _running[page]
    stderr = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
    await proc.wait()
    if error is None and not res.total and proc.returncode not in (0, 1, -9) and stderr:
        error = stderr.splitlines()[-1][:500]  # a bad regex, mostly
    return res.payload(engine, started, error)


def _rg_text(v: dict[str, Any] | None) -> bytes:
    if not v:
        return b""
    return v["text"].encode() if "text" in v else base64.b64decode(v.get("bytes", ""))


async def _search_rg(rg: str, root: str, query: str, regex: bool, case: bool, word: bool, include: list[str], exclude: list[str],
                     ignored: bool, res: _Results, page: str | None) -> dict[str, Any]:
    argv = [rg, "--json", "--hidden", "--no-require-git", "--max-filesize", str(MAX_FILE_BYTES), "--max-count", str(RG_PER_FILE),
            "-g", "!.git", "-g", "!.atrium", "-s" if case else "-i"]
    if word:
        argv.append("-w")
    if not regex:
        argv.append("-F")
    if ignored:
        argv.append("--no-ignore")
    for g in include:
        argv += ["-g", g]
    for g in exclude:
        argv += ["-g", f"!{g}"]
    argv += ["-e", query]

    def parse(line: bytes) -> bool | None:
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if ev.get("type") != "match":
            return None
        d = ev["data"]
        path = _rg_text(d.get("path")).decode(errors="replace").removeprefix("./")
        raw = _rg_text(d.get("lines"))
        text = raw.decode(errors="replace")
        # byte offsets into the line → characters
        spans = [(len(raw[: m["start"]].decode(errors="replace")), len(raw[: m["end"]].decode(errors="replace"))) for m in d.get("submatches", [])]
        return res.add(path, d.get("line_number") or 0, text, spans)

    return await _run(argv, root, page, res, parse, "ripgrep")


def _git_version() -> tuple[int, int]:
    """git's version, (0, 0) when it can't be told. Machines keep old ones (Ubuntu 18.04 has 2.17, CentOS 7 1.8)."""
    global _git_ver
    if _git_ver is None:
        try:
            m = re.search(rb"(\d+)\.(\d+)", subprocess.run(["git", "--version"], capture_output=True, timeout=10).stdout)
            _git_ver = (int(m[1]), int(m[2])) if m else (0, 0)
        except (OSError, subprocess.TimeoutExpired):
            _git_ver = (0, 0)
    return _git_ver


def _git_pcre() -> bool:
    global _pcre
    if _pcre is None:
        try:
            r = subprocess.run(["git", "grep", "--no-index", "-q", "-P", "x", "--", os.devnull], capture_output=True, timeout=10)
            _pcre = r.returncode in (0, 1)  # found or not: either way -P works
        except (OSError, subprocess.TimeoutExpired):
            _pcre = False
    return _pcre


async def _search_git(root: str, query: str, regex: bool, case: bool, word: bool, include: list[str], exclude: list[str], ignored: bool,
                      res: _Results, page: str | None) -> dict[str, Any]:
    argv = ["git", "grep", "-n", "--column", "-I", "-z", "--no-color", "--untracked"]
    if ignored:
        argv.append("--no-exclude-standard")
    if not case:
        argv.append("-i")
    if word:
        argv.append("-w")
    argv.append(("-P" if _git_pcre() else "-E") if regex else "-F")
    argv += ["-e", query, "--"]
    # without a slash a glob matches at any depth, as in .gitignore (and ripgrep); git's own doesn't cross a slash
    deep = lambda g: g if "/" in g else f"**/{g}"  # noqa: E731
    argv += [f":(glob){deep(g)}" for g in include] or ["."]
    argv += [f":(glob,exclude){deep(g)}" for g in exclude] + [":(glob,exclude)**/.atrium/**"]
    spans_of = _span_finder(query, regex, case, word)

    def parse(line: bytes) -> bool | None:
        parts = line.rstrip(b"\n").split(b"\0", 3)
        if len(parts) != 4:
            return None
        path, num, col, raw = parts
        text = raw.decode(errors="replace")
        spans = spans_of(text) if spans_of else []
        if not spans:  # Python reads the pattern otherwise: at least where git said it starts
            c = max(0, int(col or 1) - 1)
            spans = [(c, c + 1)]
        return res.add(path.decode(errors="replace"), int(num or 0), text, spans)

    return await _run(argv, root, page, res, parse, "git grep")


def _search_py(root: str, query: str, regex: bool, case: bool, word: bool, include: list[str], exclude: list[str], res: _Results) -> dict[str, Any]:
    """For a folder that isn't a repository, on a machine without ripgrep."""
    import fnmatch

    started = time.monotonic()
    spans_of = _span_finder(query, regex, case, word)
    if spans_of is None:
        return res.payload("python", started, "not a valid regular expression")
    files, _ = _walk(root, LIST_MAX)
    hit = lambda rel, g: fnmatch.fnmatch(rel, g) or ("/" not in g and fnmatch.fnmatch(rel, f"*/{g}"))  # noqa: E731
    for rel in files:
        if include and not any(hit(rel, g) for g in include):
            continue
        if any(hit(rel, g) for g in exclude):
            continue
        if time.monotonic() - started > TIMEOUT:
            res.truncated = True
            return res.payload("python", started, f"stopped after {TIMEOUT:.0f} s")
        try:
            p = os.path.join(root, rel)
            if os.path.getsize(p) > MAX_FILE_BYTES:
                continue
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            spans = spans_of(line)
            if spans and not res.add(rel, n, line, spans):
                return res.payload("python", started)
    return res.payload("python", started)


async def search(project: Project, root: str | None, query: str, *, regex: bool = False, case: bool = False, word: bool = False,
                 include: str = "", exclude: str = "", ignored: bool = False, limit: int = 1000, page: str | None = None) -> dict[str, Any]:
    """Lines of a checkout matching ``query``: {files: [{path, matches: [{line, col, text, ranges}], more}], total, more,
    truncated, engine, elapsed, error}; ``more``: matching lines past PER_FILE in a file, counted only. ``ignored``: search
    what ``.gitignore`` leaves out too (``node_modules``, build output…)."""
    root = await asyncio.to_thread(_checkout, project, root)
    res = _Results(max(1, min(int(limit), MAX_LIMIT)))
    if not query:
        return res.payload("", time.monotonic())
    inc, exc = _patterns(include), _patterns(exclude)
    rg = shutil.which("rg")
    if rg:
        return await _search_rg(rg, root, query, regex, case, word, inc, exc, ignored, res, page)
    # git grep --column is git 2.19's
    if await asyncio.to_thread(_git_version) >= (2, 19) and await asyncio.to_thread(_git_lines, root, "rev-parse", "-z", "--show-toplevel") is not None:
        return await _search_git(root, query, regex, case, word, inc, exc, ignored, res, page)
    return await asyncio.to_thread(_search_py, root, query, regex, case, word, inc, exc, res)
