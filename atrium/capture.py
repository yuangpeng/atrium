"""What the harnesses send to their model APIs, and what comes back.

Every live agent's model calls go through a reverse proxy that Atrium runs on a loopback port of its own.
Atrium points the agent's base URL (Claude Code's ``ANTHROPIC_BASE_URL``, the ``base_url`` of the Codex
model provider, the Gemini endpoint of Antigravity) at ``http://127.0.0.1:<port>/<token>``. The proxy sends
each request on, unchanged, to where the harness would have sent it, and the answer back as it arrives:
a stream stays a stream. On the way, both are recorded in ``$ATRIUM_HOME/captures/captures.db`` (SQLite,
bodies zlib-compressed) with credentials blanked; past ``capture.max_mb`` the oldest go first. One thing
is changed on the way out: an ``Accept-Encoding`` offering what can't be read back here (br, zstd) is
narrowed to gzip and deflate.

A token stands for one agent and one upstream (``Route``), so what comes in on it is that agent's: its
subagents' calls too, and those of model CLIs its tools start (they inherit its environment).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import time
import urllib.parse
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from .config import ATRIUM_HOME, config
from .events import hub

log = logging.getLogger("atrium.capture")

DB_PATH = ATRIUM_HOME / "captures" / "captures.db"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# not sent on: hop-by-hop, or set again by the client on the way out
HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "content-length", "expect"}
SECRET_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "x-goog-api-key", "api-key", "cookie", "set-cookie"}
SECRET_PARAMS = {"key", "api_key", "apikey", "access_token", "token"}
READABLE = {"gzip", "x-gzip", "deflate", "identity"}  # content codings decoded here
MAX_MARKS = 4000  # when the response's bytes came, to place each event of a stream in time
ROW_KEYS = ("harness", "session", "method", "path", "host", "started", "ttfb", "duration", "status", "req_bytes", "resp_bytes",
            "turn", "error", "summary", "narrowed")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


class CaptureContext:
    """A live agent's handle on the capture (``LiveSpec.capture``): its harness asks it which base URL to give
    the CLI. ``session`` and ``turn`` come from Atrium: the agent's id (a new Codex thread has one only once
    it started), and the user message the turn in progress answers."""

    def __init__(self, project: str, session: Callable[[], str | None], turn: Callable[[str], dict[str, Any] | None]):
        self.project = project
        self.session = session
        self.turn = turn
        self.tokens: dict[tuple[str, str, str | None, str | None], str] = {}

    def base_url(self, harness: str, upstream: str, *, env: Mapping[str, str] | None = None, session: str | None = None) -> str | None:
        """The proxy's URL for the calls the harness would send to ``upstream``; None leaves them alone (capture
        off or not running). ``env``: the environment the harness makes its calls from (None: Atrium's), for
        the proxy they would have gone through."""
        return capture.route(self, harness, upstream, env=env, session=session)


@dataclass
class Route:
    token: str
    project: str
    harness: str
    upstream: str  # the base URL the harness would have used, without a trailing slash
    proxy: str | None  # the proxy it would have reached it through
    context: CaptureContext
    session: str | None = None  # the agent's id when the harness knows it up front


def _bypassed(host: str, no_proxy: str) -> bool:
    for rule in (r.strip().lower() for r in no_proxy.split(",")):
        if rule == "*":
            return True
        if rule.count(":") == 1:
            rule = rule.split(":")[0]  # host:port
        rule = rule.lstrip("*").lstrip(".")
        if rule and (host == rule or host.endswith("." + rule)):
            return True
    return False


def outbound_proxy(url: str, env: Mapping[str, str]) -> str | None:
    """The proxy a client following the usual variables takes to ``url`` (lower-case names first, as curl)."""
    parts = urllib.parse.urlsplit(url)

    def var(name: str) -> str:
        return env.get(name) or env.get(name.upper()) or ""

    if _bypassed((parts.hostname or "").lower(), var("no_proxy")):
        return None
    return var(f"{parts.scheme}_proxy") or var("all_proxy") or None


def with_loopback(no_proxy: str | None) -> str:
    """``no_proxy`` plus the loopback addresses: the capture proxy is reached directly, never through another one."""
    have = [r.strip() for r in (no_proxy or "").split(",") if r.strip()]
    return ",".join(have + [h for h in LOOPBACK if h not in have])


def loopback_env(env: Mapping[str, str]) -> dict[str, str]:
    """NO_PROXY and no_proxy, both with the loopback addresses (clients differ in which one they read first)."""
    value = with_loopback(env.get("no_proxy") or env.get("NO_PROXY"))
    return {"NO_PROXY": value, "no_proxy": value}


# --------------------------------------------------------------------------
# What a request and its answer were
# --------------------------------------------------------------------------


def _redact_headers(headers: list[tuple[str, str]]) -> list[list[str]]:
    out = []
    for k, v in headers:
        if k.lower() in SECRET_HEADERS:
            scheme = v.strip().split(" ", 1)[0] if " " in v.strip() else ""
            v = f"{scheme} •••" if scheme else "•••"
        out.append([k, v])
    return out


def _redact_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    query = parts.query
    if query:
        q = urllib.parse.parse_qsl(query, keep_blank_values=True)
        query = urllib.parse.urlencode([(k, "***" if k.lower() in SECRET_PARAMS else v) for k, v in q], safe="*:/")
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=query))


def _header(headers: list[list[str]] | None, name: str) -> str | None:
    return next((v for k, v in headers or [] if k.lower() == name), None)


def _readable(headers: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str | None]:
    """An Accept-Encoding offering codings that can't be read back here (br, zstd, *) offers just the ones that can.
    Returns the headers, and the original value when it was changed."""
    out, narrowed = [], None
    for k, v in headers:
        if k.lower() == "accept-encoding":
            codings = [c.split(";")[0].strip().lower() for c in v.split(",") if c.strip()]
            if any(c not in READABLE for c in codings):
                narrowed = v
                v = ", ".join(c for c in codings if c in READABLE) or "identity"
        out.append((k, v))
    return out, narrowed


def decode_body(data: bytes, encoding: str | None) -> tuple[bytes, str | None]:
    """A body as its Content-Encoding sent it, decoded when that is gzip or deflate. Returns the bytes, and the
    coding left on them (None when they are plain)."""
    enc = (encoding or "").strip().lower()
    if not data or enc in ("", "identity"):
        return data, None
    if enc in ("gzip", "x-gzip", "deflate"):
        for wbits in (47, -zlib.MAX_WBITS):  # gzip or zlib header; raw deflate
            try:
                return zlib.decompress(data, wbits), None
            except zlib.error:
                pass
    return data, enc


def sse_events(text: str) -> list[tuple[str, Any]]:
    """(event name, data, parsed as JSON where it is) for each event of a ``text/event-stream`` body."""
    out: list[tuple[str, Any]] = []
    name, data = "", []

    def flush() -> None:
        if data:
            raw = "\n".join(data)
            try:
                out.append((name, json.loads(raw)))
            except ValueError:
                out.append((name, raw))

    for line in re.split(r"\r\n|\r|\n", text):
        if not line:
            flush()
            name, data = "", []
        elif line.startswith("data:"):
            data.append(line[6:] if line.startswith("data: ") else line[5:])
        elif line.startswith("event:"):
            name = line[6:].strip()
    flush()
    return out


def api_of(path: str) -> str:
    """Which model API a request path belongs to: anthropic, openai (Responses, Chat Completions), gemini or other."""
    if re.search(r"/messages(/count_tokens)?$", path):
        return "anthropic"
    if re.search(r"/responses(/[^/]+)?$|/chat/completions$", path):
        return "openai"
    if re.search(r":(streamGenerateContent|generateContent|countTokens)$", path):
        return "gemini"
    return "other"


def _texts(v: Any) -> list[str]:
    """The text in a system prompt: a string, content blocks, or a Gemini content with parts."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return _texts(v.get("parts") or v.get("text") or "")
    if isinstance(v, list):
        return [t for b in v for t in _texts(b)]
    return []


def _first_line(texts: list[str]) -> str | None:
    """The line that tells what a call is for: the system prompt's first, past a billing header, a lone tag
    (``<identity>``), and the one-line identity a harness puts in a block of its own ahead of the rest."""
    blocks = [t for t in texts if t.strip() and not t.lstrip().lower().startswith("x-anthropic-billing-header")]
    if len(blocks) > 1 and "\n" not in blocks[0].strip() and len(blocks[0]) < 200:
        blocks = blocks[1:]
    for t in blocks:
        for line in t.splitlines():
            line = line.strip()
            if line and not re.fullmatch(r"<[^>]+>", line):
                return line[:160]
    return None


def request_summary(path: str, body: Any) -> dict[str, Any]:
    """What the list shows of a request: its API, model, how much it carries (messages, tools, system prompt) and
    the kind of call: ``agent`` (the agent's own loop), ``sub`` (a subagent's: Claude Code's have no Agent tool, a
    Codex sub-agent's is for a thread of its own), ``aux`` (no tools: titles, summaries…) or ``count`` (token counting)."""
    api = api_of(path)
    s: dict[str, Any] = {"api": api}
    if not isinstance(body, dict):
        return s
    if api == "anthropic":
        tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
        system = _texts(body.get("system"))
        s.update(model=body.get("model"), messages=len(body.get("messages") or []), tools=len(tools),
                 system=sum(map(len, system)), first=_first_line(system), stream=bool(body.get("stream")))
        s["kind"] = "count" if path.endswith("/count_tokens") else ("agent" if {"Agent", "Task"} & set(tools) else "sub") if tools else "aux"
    elif api == "openai":
        inp = body.get("input") if "input" in body else body.get("messages")
        system = _texts(body.get("instructions"))
        s.update(model=body.get("model"), messages=len(inp) if isinstance(inp, list) else 1, tools=len(body.get("tools") or []),
                 system=sum(map(len, system)), first=_first_line(system), stream=bool(body.get("stream")))
        s["kind"] = "agent" if body.get("tools") else "aux"
        if isinstance(body.get("prompt_cache_key"), str):
            s["thread"] = body["prompt_cache_key"]  # Codex: the thread it is for (a sub-agent's is its own)
    elif api == "gemini":
        m = re.search(r"/models/([^/]+):[A-Za-z]+$", path)
        decls = sum(len(t.get("functionDeclarations") or t.get("function_declarations") or []) for t in body.get("tools") or [] if isinstance(t, dict))
        system = _texts(body.get("systemInstruction") or body.get("system_instruction"))
        s.update(model=urllib.parse.unquote(m.group(1)) if m else None, messages=len(body.get("contents") or []), tools=decls,
                 system=sum(map(len, system)), first=_first_line(system), stream=":streamGenerateContent" in path)
        s["kind"] = "count" if path.endswith(":countTokens") else "agent" if decls else "aux"
    return s


def response_summary(api: str, body: bytes, content_type: str) -> dict[str, Any]:
    """What came back, in short: token usage (``input`` counts every prompt token, cached ones included), why it
    stopped, the start of the text and the tools it called."""
    text = body.decode("utf-8", "replace")
    if "event-stream" in content_type:
        events = [v for _, v in sse_events(text) if isinstance(v, dict)]
    else:
        try:
            d = json.loads(text)
        except ValueError:
            return {}
        events = [d] if isinstance(d, dict) else [e for e in d if isinstance(e, dict)] if isinstance(d, list) else []
    out: list[str] = []
    calls: list[str] = []
    usage: dict[str, Any] = {}
    stop: Any = None
    error: Any = None
    if api == "anthropic":
        u: dict[str, Any] = {}
        for e in events:
            t = e.get("type")
            if t == "message_start":
                u.update((e.get("message") or {}).get("usage") or {})
            elif t == "message_delta":
                u.update({k: v for k, v in (e.get("usage") or {}).items() if v is not None})
                stop = (e.get("delta") or {}).get("stop_reason") or stop
            elif t == "content_block_start" and (e.get("content_block") or {}).get("type") in ("tool_use", "server_tool_use"):
                calls.append(e["content_block"].get("name"))
            elif t == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                out.append(e["delta"].get("text") or "")
            elif t == "message":  # not streamed
                u.update(e.get("usage") or {})
                stop = e.get("stop_reason")
                for b in e.get("content") or []:
                    if b.get("type") == "text":
                        out.append(b.get("text") or "")
                    elif b.get("type") in ("tool_use", "server_tool_use"):
                        calls.append(b.get("name"))
            elif t == "error":
                error = e.get("error")
            elif "input_tokens" in e and "type" not in e:  # count_tokens
                u["input_tokens"] = e["input_tokens"]
        cached, written = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
        if u.get("input_tokens") is not None:
            usage = {"input": u["input_tokens"] + cached + written, "cached": cached, "cache_write": written, "output": u.get("output_tokens")}
    elif api == "openai":
        for e in events:
            t = e.get("type") or ""
            r = e.get("response") if t.startswith("response.") else e if e.get("object") == "response" else None
            if isinstance(r, dict) and r.get("status") not in (None, "in_progress", "queued"):
                u = r.get("usage") or {}
                if u:
                    usage = {"input": u.get("input_tokens"), "cached": (u.get("input_tokens_details") or {}).get("cached_tokens"),
                             "output": u.get("output_tokens"), "reasoning": (u.get("output_tokens_details") or {}).get("reasoning_tokens")}
                stop = r.get("status")
                error = r.get("error") or error
                if not out:  # not streamed: the text is only here
                    for item in r.get("output") or []:
                        out += [c.get("text") or "" for c in item.get("content") or [] if isinstance(c, dict) and c.get("type") == "output_text"]
            if t == "response.output_text.delta":
                out.append(e.get("delta") or "")
            elif t == "response.output_item.done" and (e.get("item") or {}).get("type") not in ("message", "reasoning"):
                calls.append(e["item"].get("name") or e["item"].get("type"))
            elif t == "error":
                error = e.get("error") or e.get("message") or e
            elif "choices" in e:  # chat completions
                for c in e["choices"]:
                    out.append(((c.get("delta") or c.get("message") or {}).get("content")) or "")
                    stop = c.get("finish_reason") or stop
                if e.get("usage"):
                    u = e["usage"]
                    usage = {"input": u.get("prompt_tokens"), "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                             "output": u.get("completion_tokens")}
    elif api == "gemini":
        for e in events:
            if e.get("usageMetadata"):
                m = e["usageMetadata"]
                usage = {"input": m.get("promptTokenCount"), "cached": m.get("cachedContentTokenCount"),
                         "output": m.get("candidatesTokenCount"), "reasoning": m.get("thoughtsTokenCount")}
            if "totalTokens" in e:  # countTokens
                usage = {"input": e["totalTokens"]}
            for c in e.get("candidates") or []:
                stop = c.get("finishReason") or stop
                for p in (c.get("content") or {}).get("parts") or []:
                    if p.get("functionCall"):
                        calls.append(p["functionCall"].get("name"))
                    elif isinstance(p.get("text"), str) and not p.get("thought"):
                        out.append(p["text"])
            if e.get("error"):
                error = e["error"]
    s: dict[str, Any] = {"usage": {k: v for k, v in usage.items() if v is not None} or None, "stop": stop,
                         "output": " ".join("".join(out).split())[:240] or None, "calls": [c for c in calls if c][:12] or None}
    if error:
        s["api_error"] = str((error.get("message") if isinstance(error, dict) else None) or error)[:500]
    return {k: v for k, v in s.items() if v is not None}


def _body(data: bytes, encoding: str | None) -> dict[str, Any]:
    """A body for the browser: text when it is UTF-8 once decoded, else base64."""
    plain, left = decode_body(data, encoding)
    out: dict[str, Any] = {"bytes": len(data)}
    if left is None:
        try:
            out["text"] = plain.decode("utf-8")
            return out
        except UnicodeDecodeError:
            pass
    import base64

    out.update(base64=base64.b64encode(plain).decode(), encoding=left)
    return out


def _row(rid: str, state: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"id": rid, "state": state, **{k: meta.get(k) for k in ROW_KEYS if meta.get(k) is not None}}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


class _Store:
    """The captures database. Used from a single worker thread only (``Capture._pool``)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS exchanges (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        session TEXT NOT NULL,
        started REAL NOT NULL,
        state TEXT NOT NULL,
        meta TEXT NOT NULL,  -- what the list shows (ROW_KEYS)
        extra TEXT,  -- what only the detail does: URL, headers, timing marks
        req BLOB,
        resp BLOB,
        size INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS exchanges_session ON exchanges (project, session, started);
    CREATE INDEX IF NOT EXISTS exchanges_started ON exchanges (started);
    """

    def __init__(self, path: Path):
        self.path = path
        self.db: sqlite3.Connection | None = None
        self.total = 0  # bytes stored (compressed bodies + metadata)
        self.sizes: dict[str, int] = {}  # rows written but not finished yet: their size so far

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)  # whole conversations, files included
        new = not self.path.exists()
        db = sqlite3.connect(self.path, isolation_level=None)
        if new:
            db.execute("PRAGMA auto_vacuum = INCREMENTAL")  # what pruning frees goes back to the disk
            os.chmod(self.path, 0o600)
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA journal_size_limit = 8388608")  # the WAL shrinks back to 8 MB after a checkpoint
        db.executescript(self.SCHEMA)
        db.execute("UPDATE exchanges SET state = 'interrupted' WHERE state IN ('pending', 'streaming')")  # Atrium stopped mid-way
        self.total = db.execute("SELECT COALESCE(SUM(size), 0) FROM exchanges").fetchone()[0]
        self.db = db

    def close(self) -> None:
        if self.db:
            self.db.close()
            self.db = None

    def begin(self, rid: str, project: str, session: str, started: float, meta: dict[str, Any], extra: dict[str, Any], req: bytes) -> None:
        assert self.db
        blob = zlib.compress(req, 6)
        m, x = json.dumps(meta, ensure_ascii=False), json.dumps(extra, ensure_ascii=False)
        size = len(blob) + len(m) + len(x)
        self.db.execute("INSERT OR REPLACE INTO exchanges VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, NULL, ?)",
                        (rid, project, session, started, m, x, blob, size))
        self.sizes[rid] = len(blob)
        self.total += size

    def finish(self, rid: str, session: str, state: str, meta: dict[str, Any], extra: dict[str, Any], resp: bytes) -> None:
        assert self.db
        req_size = self.sizes.pop(rid, 0)
        old = self.db.execute("SELECT size FROM exchanges WHERE id = ?", (rid,)).fetchone()
        if not old:  # pruned meanwhile
            return
        blob = zlib.compress(resp, 6)
        m, x = json.dumps(meta, ensure_ascii=False), json.dumps(extra, ensure_ascii=False)
        size = req_size + len(blob) + len(m) + len(x)
        self.db.execute("UPDATE exchanges SET session = ?, state = ?, meta = ?, extra = ?, resp = ?, size = ? WHERE id = ?",
                        (session, state, m, x, blob, size, rid))
        self.total += size - old[0]

    def rows(self, project: str, session: str) -> list[dict[str, Any]]:
        assert self.db
        cur = self.db.execute("SELECT id, state, meta FROM exchanges WHERE project = ? AND session = ? ORDER BY started", (project, session))
        return [_row(rid, state, json.loads(meta)) for rid, state, meta in cur]

    def get(self, project: str, rid: str) -> tuple[dict[str, Any], dict[str, Any], str, bytes, bytes] | None:
        assert self.db
        r = self.db.execute("SELECT meta, extra, state, req, resp FROM exchanges WHERE id = ? AND project = ?", (rid, project)).fetchone()
        if not r:
            return None
        meta, extra, state, req, resp = r
        return (json.loads(meta), json.loads(extra or "{}"), state, zlib.decompress(req) if req else b"",
                zlib.decompress(resp) if resp else b"")

    def delete_session(self, project: str, session: str) -> None:
        assert self.db
        freed = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM exchanges WHERE project = ? AND session = ?", (project, session)).fetchone()[0]
        self.db.execute("DELETE FROM exchanges WHERE project = ? AND session = ?", (project, session))
        self.total -= freed
        self.db.execute("PRAGMA incremental_vacuum")

    def prune(self, limit: int) -> int:
        """Delete the oldest until what is stored is under 90% of ``limit`` bytes. Returns how many went."""
        assert self.db
        if self.total <= limit:
            return 0
        goal, n = int(limit * 0.9), 0
        while self.total > goal:
            batch = self.db.execute("SELECT id, size FROM exchanges WHERE state NOT IN ('pending', 'streaming') ORDER BY started LIMIT 200").fetchall()
            if not batch:
                break
            for rid, size in batch:
                if self.total <= goal:
                    break
                self.db.execute("DELETE FROM exchanges WHERE id = ?", (rid,))
                self.total -= size
                n += 1
        self.db.execute("PRAGMA incremental_vacuum")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return n


# --------------------------------------------------------------------------
# The proxy
# --------------------------------------------------------------------------


@dataclass
class _Exchange:
    id: str
    route: Route
    meta: dict[str, Any]  # the row's fields (ROW_KEYS), and its state
    extra: dict[str, Any]  # the URL and the headers
    request: bytes
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    marks: list[list[int]] = field(default_factory=list)  # [ms since the request, bytes received by then]


def _uvicorn_server(app: Any) -> Any:
    import uvicorn

    class Server(uvicorn.Server):
        """uvicorn without signal handlers of its own: Atrium's server has them, and stops this one."""

        @contextlib.contextmanager
        def capture_signals(self):  # type: ignore[override]
            yield

        def install_signal_handlers(self) -> None:  # uvicorn before 0.29
            pass

    return Server(uvicorn.Config(
        app, interface="asgi3", lifespan="off", ws="none", log_config=None, log_level=None, access_log=False,
        proxy_headers=False, server_header=False, date_header=False,  # the answer carries the upstream's own
        timeout_keep_alive=120,  # longer than clients keep idle connections (reqwest, Go: 90 s), so they never reuse a closed one
        timeout_graceful_shutdown=3,
    ))


async def _reply(send: Callable[..., Any], status: int, text: str) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class Capture:
    def __init__(self) -> None:
        self.port: int | None = None
        self._routes: dict[str, Route] = {}
        self._active: dict[str, _Exchange] = {}
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atrium-capture")
        self._store = _Store(DB_PATH)
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._pruned = 0.0

    @property
    def enabled(self) -> bool:
        return bool(config.get("capture.enabled", True))

    @property
    def running(self) -> bool:
        return self.port is not None

    def _db(self, fn: Callable[..., Any], *args: Any) -> asyncio.Future:
        return asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        try:
            await self._db(self._store.open)
        except Exception:
            log.exception("can't open %s: model calls go out without being recorded", DB_PATH)
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        server = _uvicorn_server(self)
        self._task = asyncio.create_task(server.serve(sockets=[sock]), name="capture-proxy")
        for _ in range(200):
            if server.started or self._task.done():
                break
            await asyncio.sleep(0.025)
        if not server.started:
            log.error("the capture proxy didn't start: model calls go out without being recorded")
            return
        self._server = server
        self.port = sock.getsockname()[1]
        log.info("model calls are captured through 127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        self.port = None
        if self._server and self._task:
            self._server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 8)
        for c in self._clients.values():
            with contextlib.suppress(Exception):
                await c.aclose()
        with contextlib.suppress(Exception):
            await self._db(self._store.close)
        self._pool.shutdown(wait=False)

    def route(self, context: CaptureContext, harness: str, upstream: str, *, env: Mapping[str, str] | None = None,
              session: str | None = None) -> str | None:
        if not self.port or not self.enabled:
            return None
        upstream = upstream.strip().rstrip("/")
        parts = urllib.parse.urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.hostname in LOOPBACK and parts.port == self.port:  # ours already
            return None
        proxy = outbound_proxy(upstream, os.environ if env is None else env)
        if proxy and proxy.lower().startswith("socks") and importlib.util.find_spec("socksio") is None:
            log.warning("not capturing %s's calls to %s: they go through a SOCKS proxy, and httpx can't without socksio", harness, parts.netloc)
            return None
        key = (harness, upstream, proxy, session)
        token = context.tokens.get(key)
        if token is None:
            token = context.tokens[key] = secrets.token_urlsafe(16)
            self._routes[token] = Route(token, context.project, harness, upstream, proxy, context, session)
        return f"http://127.0.0.1:{self.port}/{token}"

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        c = self._clients.get(proxy)
        if c is None:
            c = httpx.AsyncClient(
                proxy=proxy, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(connect=30, read=None, write=120, pool=None),  # the harness keeps its own time
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=32),
            )
            for k in list(c.headers):  # nothing of httpx's own (User-Agent, Accept…): only what the harness sent
                del c.headers[k]
            self._clients[proxy] = c
        return c

    # ---- the proxy ---------------------------------------------------------------
    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            return
        raw = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        token, _, rest = raw.lstrip("/").partition("/")
        route = self._routes.get(token)
        if route is None:
            return await _reply(send, 404, "Unknown capture route: Atrium restarted since this agent's process started. Start it again.")
        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        await self._exchange(route, scope, "/" + rest, bytes(body), receive, send)

    async def _exchange(self, route: Route, scope: dict[str, Any], path: str, body: bytes, receive: Callable[..., Any],
                        send: Callable[..., Any]) -> None:
        qs = scope.get("query_string", b"").decode("latin-1")
        url = route.upstream + path + (f"?{qs}" if qs else "")
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        out, narrowed = _readable([(k, v) for k, v in headers if k.lower() not in HOP])
        started = time.time()
        sid = route.session or route.context.session() or ""
        try:
            turn = route.context.turn(sid) if sid else None
        except Exception:
            turn = None
        rec = _Exchange(id=uuid.uuid4().hex[:16], route=route, request=body, meta={
            "harness": route.harness, "session": sid, "method": scope["method"],
            "path": urllib.parse.unquote(urllib.parse.urlsplit(route.upstream).path.rstrip("/") + path),  # as the upstream sees it
            "host": urllib.parse.urlsplit(route.upstream).netloc.rsplit("@", 1)[-1], "started": started, "turn": turn,
            "req_bytes": len(body), "narrowed": narrowed, "state": "pending",
        }, extra={"url": _redact_url(url), "req_headers": _redact_headers(headers)})
        record = self.enabled
        if record:
            self._begin(rec)
        client = self._client(route.proxy)
        try:
            resp = await client.send(client.build_request(scope["method"], url, headers=out, content=body), stream=True)
        except Exception as e:  # unreachable, refused, timed out: what the harness would have met, as a 502
            text = f"Atrium's capture proxy couldn't reach {rec.meta['host']}: {type(e).__name__}: {e}"
            with contextlib.suppress(Exception):
                await _reply(send, 502, text)
            if record:
                await self._end(rec, "error", error=text)
            return
        rec.meta.update(status=resp.status_code, ttfb=round(time.time() - started, 3), state="streaming")
        rec.extra["resp_headers"] = _redact_headers([(k.decode("latin-1"), v.decode("latin-1")) for k, v in resp.headers.raw])
        state, error = "done", None
        try:
            await send({"type": "http.response.start", "status": resp.status_code,
                        "headers": [(k, v) for k, v in resp.headers.raw if k.decode("latin-1").lower() not in HOP - {"content-length"}]})

            async def pump() -> None:
                async for chunk in resp.aiter_raw():
                    rec.chunks.append(chunk)
                    rec.size += len(chunk)
                    if len(rec.marks) >= MAX_MARKS:
                        del rec.marks[1::2]
                    rec.marks.append([int((time.time() - started) * 1000), rec.size])
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})

            async def gone() -> None:  # the harness hung up (an interrupt, its own timeout)
                while (await receive())["type"] != "http.disconnect":
                    pass

            pumping, watching = asyncio.create_task(pump()), asyncio.create_task(gone())
            done: set[asyncio.Task] = set()
            try:
                done, _ = await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (pumping, watching):
                    t.cancel()
                await asyncio.gather(pumping, watching, return_exceptions=True)
            if pumping not in done:
                state = "cancelled"
            elif pumping.exception():
                e = pumping.exception()
                state, error = "error", f"{type(e).__name__}: {e}"
        except Exception as e:
            state, error = "error", f"{type(e).__name__}: {e}"
        finally:
            with contextlib.suppress(Exception):
                await resp.aclose()
            if record:
                await asyncio.shield(self._end(rec, state, error=error))

    # ---- recording ---------------------------------------------------------------
    def _publish(self, rec: _Exchange) -> None:
        hub.publish(f"project:{rec.route.project}", {"type": "capture", "session_id": rec.meta.get("session"),
                                                     "row": _row(rec.id, rec.meta.get("state") or "pending", rec.meta)})

    def _begin(self, rec: _Exchange) -> None:
        self._active[rec.id] = rec

        def job() -> None:  # worker thread. A dict the event loop may be reading (a published row) is replaced, never changed
            try:
                plain, _ = decode_body(rec.request, _header(rec.extra["req_headers"], "content-encoding"))
                rec.meta["summary"] = request_summary(rec.meta["path"], json.loads(plain) if plain else None)
            except Exception:
                rec.meta["summary"] = {"api": api_of(rec.meta["path"])}
            if self._store.db:
                self._store.begin(rec.id, rec.route.project, rec.meta["session"], rec.meta["started"], dict(rec.meta), dict(rec.extra), rec.request)

        def done(f: asyncio.Future) -> None:
            if f.exception():
                log.warning("capture: recording a request failed: %s", f.exception())
            elif rec.id in self._active:  # its summary, while the answer is still coming
                self._publish(rec)

        self._db(job).add_done_callback(done)

    async def _end(self, rec: _Exchange, state: str, error: str | None = None) -> None:
        now = time.time()
        sid = rec.route.session or rec.route.context.session() or rec.meta["session"]
        rec.meta.update(state=state, session=sid, duration=round(now - rec.meta["started"], 3), resp_bytes=rec.size)
        rec.extra["marks"] = rec.marks
        if error:
            rec.meta["error"] = error[:2000]

        def job() -> None:  # worker thread, after _begin's
            summary = dict(rec.meta.get("summary") or {"api": api_of(rec.meta["path"])})
            if summary.get("thread") == sid:
                summary.pop("thread")  # the agent's own thread: only a sub-agent's is worth showing
            elif summary.get("thread") and summary.get("kind") == "agent":
                summary["kind"] = "sub"
            resp = b"".join(rec.chunks)
            try:
                plain, left = decode_body(resp, _header(rec.extra.get("resp_headers"), "content-encoding"))
                if left is None:
                    summary.update(response_summary(summary.get("api") or "other", plain, _header(rec.extra.get("resp_headers"), "content-type") or ""))
            except Exception:
                log.debug("capture: no summary for %s", rec.id, exc_info=True)
            rec.meta["summary"] = summary
            if self._store.db:
                self._store.finish(rec.id, sid, state, {k: v for k, v in rec.meta.items() if k != "state"}, dict(rec.extra), resp)

        try:
            await self._db(job)
        except Exception:
            log.warning("capture: recording a response failed", exc_info=True)
        self._active.pop(rec.id, None)
        self._publish(rec)
        limit = int(float(config.get("capture.max_mb", 1024) or 0) * 1024 * 1024)
        if limit > 0 and self._store.total > limit and now - self._pruned > 10:
            self._pruned = now
            n = await self._db(self._store.prune, limit)
            log.info("capture: pruned the %d oldest exchanges (over capture.max_mb)", n)

    # ---- reading -------------------------------------------------------------------
    async def rows(self, project: str, session: str) -> list[dict[str, Any]]:
        stored = await self._db(self._store.rows, project, session) if self._store.db else []
        live = {r.id: _row(r.id, r.meta.get("state") or "pending", r.meta) for r in list(self._active.values())
                if r.route.project == project and r.meta.get("session") == session}
        rows = [live.pop(r["id"], r) for r in stored] + list(live.values())
        return sorted(rows, key=lambda r: r.get("started") or 0)

    async def detail(self, project: str, rid: str) -> dict[str, Any] | None:
        """One exchange in full: its row, URL, headers, the times its bytes came, and both bodies (one still coming
        is what came so far)."""
        rec = self._active.get(rid)
        extra: dict[str, Any]
        if rec and rec.route.project == project:
            meta, extra, state = dict(rec.meta), {**rec.extra, "marks": list(rec.marks)}, rec.meta.get("state") or "pending"
            req, resp = rec.request, b"".join(rec.chunks)
        elif self._store.db:
            got = await self._db(self._store.get, project, rid)
            if not got:
                return None
            meta, extra, state, req, resp = got
        else:
            return None

        def bodies() -> tuple[dict[str, Any], dict[str, Any]]:
            return (_body(req, _header(extra.get("req_headers"), "content-encoding")),
                    _body(resp, _header(extra.get("resp_headers"), "content-encoding")))

        request, response = await asyncio.to_thread(bodies)
        return {**_row(rid, state, meta), "url": extra.get("url"), "req_headers": extra.get("req_headers") or [],
                "resp_headers": extra.get("resp_headers") or [], "marks": extra.get("marks") or [], "request": request, "response": response}

    async def forget(self, project: str, session: str) -> None:
        """Delete what was recorded of a session (it was deleted, or the human cleared it)."""
        if self._store.db:
            await self._db(self._store.delete_session, project, session)


capture = Capture()
