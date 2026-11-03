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
subagents' calls too, and those of model CLIs its tools start (they inherit its environment). Such a process may
outlive Atrium, so the port is the same after a restart when it is free, and the routes are kept in the database.
An Atrium started from an agent first takes its environment back to the real address (``unwrap_inherited``).
The proxy trusts what the system and the harness trust (``Tls``): a call the harness could make must not fail
because it is captured.

Every call carries the conversation so far, so most of an old call is in the calls after it, until a
compaction: from then on the calls carry a summary instead. So the compaction itself (the harness asking the
model for that summary, the conversation with it) and the agent's last call before it are kept: they don't
count against ``capture.max_mb``, and pruning leaves them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import ssl
import time
import urllib.parse
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping

import certifi
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
# How a harness's request to have the model summarize the conversation (compaction) starts its last user text, and
# what it says further on: Claude Code appends it to the last user message, Codex and Antigravity send it as a user
# message of its own. Claude Code without the agent's prompt (when there is no prompt cache to share) has its own system
# prompt instead; Codex on OpenAI's own provider posts to /responses/compact.
COMPACTION_PROMPTS = (
    ("CRITICAL: Respond with TEXT ONLY", "create a detailed summary of the conversation so far"),  # Claude Code
    ("Your task is to create a detailed summary of the conversation so far", ""),  # Claude Code, without that preamble
    ("You are performing a CONTEXT CHECKPOINT COMPACTION", ""),  # Codex
    ("You have been working on the task described above but have not yet completed it", "continuation summary"),  # Antigravity
)
SUMMARIZER_SYSTEM = "You are a helpful AI assistant tasked with summarizing conversations"  # Claude Code's
# request headers blanked by their name too: a relay's own auth header (response headers: SECRET_HEADERS only, their
# rate limits say "tokens")
SECRET_NAME = re.compile(r"auth|key|token|secret|passw|signature|credential|cookie", re.I)
# What a Claude Code process whose base URL is a capture proxy's also gets: that URL, the one it stands for ("" =
# Anthropic's own API), and the CLI's switch for Anthropic's API behind a proxy (it takes any other base URL for a
# gateway, and leaves out what only Anthropic's API has). A process started from the agent inherits them.
CAPTURE_URL_ENV, UPSTREAM_ENV, FIRST_PARTY_ENV = "ATRIUM_CAPTURE_URL", "ATRIUM_CAPTURE_UPSTREAM", "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"
# What the harnesses add to the CAs they trust: Claude Code (Node), Codex, OpenSSL's
CA_ENV = ("NODE_EXTRA_CA_CERTS", "CODEX_CA_CERTIFICATE", "SSL_CERT_FILE", "SSL_CERT_DIR")
ROUTE_DAYS = 90  # a route outlives the agent it was made for: a process started from it may still use it
RELEASE_DAYS = 7  # a session Atrium no longer finds: its kept calls are pruned like the others this long after its last


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
        self.tokens: dict[tuple[Any, ...], str] = {}  # a route's (harness, upstream, proxy, session, tls, secret) → its token

    def base_url(self, harness: str, upstream: str, *, env: Mapping[str, str] | None = None, session: str | None = None,
                 secret: Iterable[str] = ()) -> str | None:
        """The proxy's URL for the calls the harness would send to ``upstream``; None leaves them alone (capture
        off or not running, or a TLS setup the proxy can't reproduce). ``env``: the environment the harness makes
        its calls from (None: Atrium's), for the proxy and the CAs they would have used. ``secret``: headers the
        harness was configured to send, blanked like credentials."""
        return capture.route(self, harness, upstream, env=env, session=session, secret=secret)


@dataclass(frozen=True)
class Tls:
    """What a harness adds to its TLS connections: CA files or directories, and a client certificate."""

    cas: tuple[str, ...] = ()
    cert: str | None = None
    key: str | None = None
    password: str | None = field(default=None, repr=False)  # the key's: not kept in the database


def tls_of(env: Mapping[str, str]) -> Tls:
    return Tls(tuple(env[v] for v in CA_ENV if env.get(v)), env.get("CLAUDE_CODE_CLIENT_CERT") or None,
               env.get("CLAUDE_CODE_CLIENT_KEY") or None, env.get("CLAUDE_CODE_CLIENT_KEY_PASSPHRASE") or None)


def ssl_context(tls: Tls) -> ssl.SSLContext:
    """The system's trust (the macOS keychain, Windows' store, OpenSSL's paths: truststore) and certifi's, plus what the
    harness adds: a superset of what the harness trusted itself."""
    try:
        import truststore

        ctx: ssl.SSLContext = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        ctx = ssl.create_default_context()
    ctx.load_verify_locations(certifi.where())
    for ca in tls.cas:
        if os.path.isdir(ca):
            ctx.load_verify_locations(capath=ca)
        else:
            ctx.load_verify_locations(cafile=ca)
    if tls.cert:
        ctx.load_cert_chain(tls.cert, tls.key, tls.password)
    return ctx


@dataclass
class Route:
    token: str
    project: str
    harness: str
    upstream: str  # the base URL the harness would have used, without a trailing slash
    proxy: str | None  # the proxy it would have reached it through
    context: CaptureContext
    session: str | None = None  # the agent's id when the harness knows it up front
    tls: Tls = Tls()
    secret: frozenset[str] = frozenset()  # header names (lower case) blanked like credentials

    def spec(self) -> dict[str, Any]:
        """What the database keeps of it, for a later run of Atrium: no passphrase, no proxy password."""
        proxy = self.proxy and urllib.parse.urlsplit(self.proxy)
        return {"harness": self.harness, "upstream": self.upstream, "session": self.session,
                "proxy": proxy and urllib.parse.urlunsplit(proxy._replace(netloc=proxy.netloc.rsplit("@", 1)[-1])),
                "cas": list(self.tls.cas), "cert": self.tls.cert, "key": self.tls.key, "secret": sorted(self.secret)}


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


def unwrap_inherited(env: MutableMapping[str, str]) -> None:
    """The environment of a process started from a captured Claude Code agent (an Atrium, say), back to the address its
    base URL stands for: this process's own agents and calls then don't depend on that Atrium, nor go through two
    proxies."""
    url, upstream = env.pop(CAPTURE_URL_ENV, None), env.pop(UPSTREAM_ENV, None)
    if url and env.get("ANTHROPIC_BASE_URL") == url:
        env.pop(FIRST_PARTY_ENV, None)
        if upstream:
            env["ANTHROPIC_BASE_URL"] = upstream
        else:
            env.pop("ANTHROPIC_BASE_URL", None)


# --------------------------------------------------------------------------
# What a request and its answer were
# --------------------------------------------------------------------------


def _redact_headers(headers: list[tuple[str, str]], secret: frozenset[str] = frozenset(), by_name: bool = True) -> list[list[str]]:
    """Headers with credentials blanked: the usual ones, the ones the harness was configured to send (``secret``), and,
    ``by_name``, any whose name says it is one."""
    out = []
    for k, v in headers:
        name = k.lower()
        if name in SECRET_HEADERS or name in secret or (by_name and SECRET_NAME.search(name)):
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
    """The text in a system prompt or a message: a string, content blocks, or a Gemini content with parts."""
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


def _conversation(api: str, body: dict[str, Any]) -> Any:
    """The messages of a request: Anthropic's ``messages``, the Responses API's ``input``, Gemini's ``contents``."""
    return body.get({"anthropic": "messages", "openai": "input", "gemini": "contents"}.get(api, ""))


def _last_user_text(api: str, body: dict[str, Any]) -> str:
    """The last piece of text in the last user message."""
    conv = _conversation(api, body)
    if isinstance(conv, str):  # the Responses API's input as one string
        return conv
    user = next((m for m in reversed(conv) if isinstance(m, dict) and m.get("role") == "user"), None) if isinstance(conv, list) else None
    if not user:
        return ""
    texts = _texts(user.get("parts") if api == "gemini" else user.get("content"))
    return texts[-1] if texts else ""


def _is_compaction(api: str, path: str, body: dict[str, Any]) -> bool:
    """Whether the request is a compaction: its last user text starts as a harness's instruction does (one quoting it in
    the middle of a message isn't)."""
    if path.endswith("/responses/compact"):
        return True
    if api == "anthropic" and SUMMARIZER_SYSTEM in " ".join(_texts(body.get("system"))):
        return True
    text = " ".join(_last_user_text(api, body)[:3000].split())
    return any(text.startswith(start) and said in text for start, said in COMPACTION_PROMPTS)


def _without_cache_marks(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _without_cache_marks(x) for k, x in v.items() if k != "cache_control"}
    if isinstance(v, list):
        return [_without_cache_marks(x) for x in v]
    return v


def conversation_head(api: str, body: Any) -> str | None:
    """A fingerprint of the conversation a request belongs to: its first message, which every call of the conversation
    repeats (the compaction too) until a compaction replaces the history. Cache breakpoints, which move, aside."""
    conv = _conversation(api, body) if isinstance(body, dict) else None
    first = conv[0] if isinstance(conv, list) and conv else conv if isinstance(conv, str) else None
    if first is None:
        return None
    return hashlib.sha1(json.dumps(_without_cache_marks(first), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def request_summary(path: str, body: Any) -> dict[str, Any]:
    """What the list shows of a request: its API, model, how much it carries (messages, tools, system prompt) and
    the kind of call: ``agent`` (the agent's own loop), ``sub`` (a subagent's: Claude Code's have no Agent tool, a
    Codex sub-agent's is for a thread of its own), ``aux`` (no tools: titles, summaries…), ``count`` (token counting)
    or ``compact`` (the harness having the model summarize the conversation, to go on with the summary instead)."""
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
    if api != "other" and s.get("kind") != "count" and _is_compaction(api, path, body):
        s["kind"], s["loop"] = "compact", s.get("kind")  # loop: what its tools make it, the agent's loop or a subagent's
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


def _row(rid: str, state: str, meta: dict[str, Any], keep: str | None = None, conv: str | None = None) -> dict[str, Any]:
    row = {"id": rid, "state": state, **{k: meta.get(k) for k in ROW_KEYS if meta.get(k) is not None}}
    if keep:
        row["keep"] = keep
    if conv:
        row["conv"] = conv
    return row


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
        size INTEGER NOT NULL DEFAULT 0,
        kind TEXT,  -- the summary's: agent, sub, aux, count, compact
        conv TEXT,  -- the conversation it is a call of: "<another thread>:<its first message's fingerprint>"
        keep TEXT  -- left by pruning: 'compact' (a compaction), 'before-compact' (the agent's last call before one)
    );
    CREATE INDEX IF NOT EXISTS exchanges_session ON exchanges (project, session, started);
    CREATE INDEX IF NOT EXISTS exchanges_started ON exchanges (started);
    CREATE TABLE IF NOT EXISTS routes (
        token TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        created REAL NOT NULL,
        spec TEXT NOT NULL  -- Route.spec()
    );
    """

    def __init__(self, path: Path):
        self.path = path
        self.db: sqlite3.Connection | None = None
        self.total = 0  # bytes stored (compressed bodies + metadata)
        self.kept = 0  # of those, in kept rows: they don't count against the limit
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
        db.execute("DELETE FROM routes WHERE created < ?", (time.time() - ROUTE_DAYS * 86400,))
        self.total, self.kept = db.execute(
            "SELECT COALESCE(SUM(size), 0), COALESCE(SUM(CASE WHEN keep IS NULL THEN 0 ELSE size END), 0) FROM exchanges").fetchone()
        self.db = db

    def close(self) -> None:
        if self.db:
            self.db.close()
            self.db = None

    def begin(self, rid: str, project: str, session: str, started: float, meta: dict[str, Any], extra: dict[str, Any], req: bytes,
              conv: str | None, keep: str | None) -> None:
        assert self.db
        blob = zlib.compress(req, 6)
        m, x = json.dumps(meta, ensure_ascii=False), json.dumps(extra, ensure_ascii=False)
        size = len(blob) + len(m) + len(x)
        self.db.execute("INSERT OR REPLACE INTO exchanges (id, project, session, started, state, meta, extra, req, size, kind, conv, keep) "
                        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                        (rid, project, session, started, m, x, blob, size, (meta.get("summary") or {}).get("kind"), conv, keep))
        self.sizes[rid] = len(blob)
        self.total += size
        if keep:
            self.kept += size

    def finish(self, rid: str, session: str, state: str, meta: dict[str, Any], extra: dict[str, Any], resp: bytes) -> None:
        assert self.db
        req_size = self.sizes.pop(rid, 0)
        old = self.db.execute("SELECT size, keep FROM exchanges WHERE id = ?", (rid,)).fetchone()
        if not old:  # pruned meanwhile
            return
        blob = zlib.compress(resp, 6)
        m, x = json.dumps(meta, ensure_ascii=False), json.dumps(extra, ensure_ascii=False)
        size = req_size + len(blob) + len(m) + len(x)
        self.db.execute("UPDATE exchanges SET session = ?, state = ?, meta = ?, extra = ?, resp = ?, size = ?, kind = ? WHERE id = ?",
                        (session, state, m, x, blob, size, (meta.get("summary") or {}).get("kind"), rid))
        self.total += size - old[0]
        if old[1]:
            self.kept += size - old[0]

    def keep_before(self, project: str, session: str, conv: str, loop: str | None, before: float) -> dict[str, Any] | None:
        """Keep the agent's last call of conversation ``conv`` before ``before`` (a compaction's start): the fullest
        history of the conversation the compaction replaces. When the compaction rewrote the conversation's start
        (Claude Code's after a prompt too long: images left out, the oldest messages dropped), the last call of the same
        thread and ``loop`` kind (agent, sub) instead. Returns its row, or None when there is none."""
        assert self.db
        query = ("SELECT id, state, meta, size, keep FROM exchanges WHERE project = ? AND session = ? AND started < ? AND {} "
                 "AND kind IN ({}) ORDER BY started DESC LIMIT 1")
        r = self.db.execute(query.format("conv = ?", "'agent', 'sub'"), (project, session, before, conv)).fetchone()
        if not r:
            thread = conv.split(":", 1)[0] + ":"
            kinds = (loop,) if loop in ("agent", "sub") else ("agent", "sub")
            r = self.db.execute(query.format("substr(conv, 1, ?) = ?", ", ".join("?" * len(kinds))),
                                (project, session, before, len(thread), thread, *kinds)).fetchone()
        if not r:
            return None
        rid, state, meta, size, keep = r
        if not keep:
            keep = "before-compact"
            self.db.execute("UPDATE exchanges SET keep = ? WHERE id = ?", (keep, rid))
            self.kept += size
        return _row(rid, state, json.loads(meta), keep, conv)

    def release_project(self, project: str) -> None:
        """Its kept calls become calls like the others (the project was removed from Atrium)."""
        assert self.db
        self.kept -= self.db.execute("SELECT COALESCE(SUM(size), 0) FROM exchanges WHERE project = ? AND keep IS NOT NULL", (project,)).fetchone()[0]
        self.db.execute("UPDATE exchanges SET keep = NULL WHERE project = ? AND keep IS NOT NULL", (project,))

    def release_gone(self, project: str, known: set[str], before: float) -> int:
        """The same for the project's sessions not in ``known`` whose last call is older than ``before``. Returns how
        many sessions."""
        assert self.db
        n = 0
        for session, last in self.db.execute("SELECT session, MAX(started) FROM exchanges WHERE project = ? GROUP BY session "
                                             "HAVING SUM(keep IS NOT NULL) > 0", (project,)).fetchall():
            if session not in known and last < before:
                self.kept -= self.db.execute("SELECT COALESCE(SUM(size), 0) FROM exchanges WHERE project = ? AND session = ? AND keep IS NOT NULL",
                                             (project, session)).fetchone()[0]
                self.db.execute("UPDATE exchanges SET keep = NULL WHERE project = ? AND session = ?", (project, session))
                n += 1
        return n

    def add_route(self, token: str, project: str, spec: dict[str, Any]) -> None:
        if self.db:
            self.db.execute("INSERT OR REPLACE INTO routes VALUES (?, ?, ?, ?)", (token, project, time.time(), json.dumps(spec)))

    def route(self, token: str) -> tuple[str, dict[str, Any]] | None:
        assert self.db
        r = self.db.execute("SELECT project, spec FROM routes WHERE token = ?", (token,)).fetchone()
        return (r[0], json.loads(r[1])) if r else None

    def rows(self, project: str, session: str) -> list[dict[str, Any]]:
        assert self.db
        cur = self.db.execute("SELECT id, state, meta, keep, conv FROM exchanges WHERE project = ? AND session = ? ORDER BY started",
                              (project, session))
        return [_row(rid, state, json.loads(meta), keep, conv) for rid, state, meta, keep, conv in cur]

    def get(self, project: str, rid: str, request: bool = True) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes] | None:
        """One row (with ``state``, ``keep`` and ``conv`` in its meta), its extra, and both bodies (without ``request``,
        an empty request body)."""
        assert self.db
        r = self.db.execute(f"SELECT meta, extra, state, keep, conv, {'req' if request else 'NULL'}, resp FROM exchanges WHERE id = ? AND project = ?",
                            (rid, project)).fetchone()
        if not r:
            return None
        meta, extra, state, keep, conv, req, resp = r
        return ({**json.loads(meta), "state": state, "keep": keep, "conv": conv}, json.loads(extra or "{}"),
                zlib.decompress(req) if req else b"", zlib.decompress(resp) if resp else b"")

    def delete_session(self, project: str, session: str) -> None:
        assert self.db
        freed, kept = self.db.execute("SELECT COALESCE(SUM(size), 0), COALESCE(SUM(CASE WHEN keep IS NULL THEN 0 ELSE size END), 0) "
                                      "FROM exchanges WHERE project = ? AND session = ?", (project, session)).fetchone()
        self.db.execute("DELETE FROM exchanges WHERE project = ? AND session = ?", (project, session))
        self.total -= freed
        self.kept -= kept
        self.db.execute("PRAGMA incremental_vacuum")

    def prune(self, limit: int) -> int:
        """Delete the oldest calls that aren't kept until the rest take under 90% of ``limit`` bytes (kept ones don't
        count). Returns how many went."""
        assert self.db
        if self.total - self.kept <= limit:
            return 0
        goal, n = int(limit * 0.9), 0
        while self.total - self.kept > goal:
            batch = self.db.execute("SELECT id, size FROM exchanges WHERE keep IS NULL AND state NOT IN ('pending', 'streaming') "
                                    "ORDER BY started LIMIT 200").fetchall()
            if not batch:
                break
            for rid, size in batch:
                if self.total - self.kept <= goal:
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
    conv: str | None = None  # the conversation it is a call of (_Store's conv)
    keep: str | None = None  # left by pruning (_Store's keep)
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
        self._clients: dict[tuple[str | None, Tls], httpx.AsyncClient] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atrium-capture")
        self._store = _Store(DB_PATH)
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._pruned = 0.0
        self._released: dict[str, float] = {}  # project → when release_gone last looked
        self.turn_of: Callable[[str], dict[str, Any] | None] = lambda _sid: None  # a session's turn (SessionManager's), for restored routes

    @property
    def enabled(self) -> bool:
        return bool(config.get("capture.enabled", True))

    @property
    def running(self) -> bool:
        return self.port is not None

    def usage(self) -> dict[str, float]:
        """Megabytes stored, and of them kept apart from ``capture.max_mb`` (compactions and the calls before them)."""
        return {"mb": round(self._store.total / 2**20, 1), "kept_mb": round(self._store.kept / 2**20, 1)}

    def _db(self, fn: Callable[..., Any], *args: Any) -> asyncio.Future:
        return asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        try:
            await self._db(self._store.open)
        except Exception:
            log.exception("can't open %s: model calls go out without being recorded", DB_PATH)
            return
        port_file = DB_PATH.parent / "port"
        try:
            last = int(port_file.read_text())
        except (OSError, ValueError):
            last = 0
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # the last run's port, which processes started from its agents still have in their environment. On a restart that
        # run may still be closing its agents, port held: it gets a while (another program holding it delays one start only)
        for _ in range(40 if last else 0):
            try:
                sock.bind(("127.0.0.1", last))
                break
            except OSError:
                await asyncio.sleep(0.25)
        else:
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
        with contextlib.suppress(OSError):
            port_file.write_text(str(self.port))
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
              session: str | None = None, secret: Iterable[str] = ()) -> str | None:
        if not self.port or not self.enabled:
            return None
        upstream = upstream.strip().rstrip("/")
        parts = urllib.parse.urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.hostname in LOOPBACK and parts.port == self.port:  # ours already
            return None
        env = os.environ if env is None else env
        proxy = outbound_proxy(upstream, env)
        if proxy and proxy.lower().startswith("socks") and importlib.util.find_spec("socksio") is None:
            log.warning("not capturing %s's calls to %s: they go through a SOCKS proxy, and httpx can't without socksio", harness, parts.netloc)
            return None
        tls = tls_of(env) if parts.scheme == "https" else Tls()
        try:
            self._client(proxy, tls)
        except (OSError, ValueError, ssl.SSLError) as e:  # a call the harness could make must not fail for being captured
            log.warning("not capturing %s's calls to %s: their TLS setup can't be reproduced (%s)", harness, parts.netloc, e)
            return None
        key = (harness, upstream, proxy, session, tls, frozenset(h.lower() for h in secret))
        token = context.tokens.get(key)
        if token is None:
            token = context.tokens[key] = secrets.token_urlsafe(16)
            route = self._routes[token] = Route(token, context.project, harness, upstream, proxy, context, session, tls, key[-1])
            self._pool.submit(self._store.add_route, token, route.project, route.spec())
        return f"http://127.0.0.1:{self.port}/{token}"

    def _client(self, proxy: str | None, tls: Tls) -> httpx.AsyncClient:
        """A client for the proxy and TLS setup (made at the first call: a bad CA or certificate raises here)."""
        c = self._clients.get((proxy, tls))
        if c is None:
            c = httpx.AsyncClient(
                proxy=proxy, verify=ssl_context(tls), trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(connect=30, read=None, write=120, pool=None),  # the harness keeps its own time
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=32),
            )
            for k in list(c.headers):  # nothing of httpx's own (User-Agent, Accept…): only what the harness sent
                del c.headers[k]
            self._clients[(proxy, tls)] = c
        return c

    async def _restore(self, token: str) -> Route | None:
        """A route of an earlier run of Atrium, which a process started from one of its agents still uses."""
        got = await self._db(self._store.route, token) if self._store.db and re.fullmatch(r"[\w-]{16,64}", token) else None
        if not got:
            return None
        project, spec = got
        session = spec.get("session")
        tls = Tls(tuple(spec.get("cas") or ()), spec.get("cert"), spec.get("key"))
        route = Route(token, project, spec["harness"], spec["upstream"], spec.get("proxy"), CaptureContext(project, lambda: session, self.turn_of),
                      session, tls, frozenset(spec.get("secret") or ()))
        try:
            self._client(route.proxy, tls)
        except (OSError, ValueError, ssl.SSLError) as e:
            log.warning("capture route of an earlier run can't be restored: %s", e)
            return None
        self._routes[token] = route
        return route

    # ---- the proxy ---------------------------------------------------------------
    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            return
        raw = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        token, _, rest = raw.lstrip("/").partition("/")
        route = self._routes.get(token) or await self._restore(token)
        if route is None:
            return await _reply(send, 404, "Unknown capture route: it was made for a process of an Atrium that has forgotten it (or another "
                                           "ATRIUM_HOME's). Start the agent again.")
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
        }, extra={"url": _redact_url(url), "req_headers": _redact_headers(headers, route.secret)})
        record = self.enabled
        if record:
            self._begin(rec)
        client = self._client(route.proxy, route.tls)
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
        rec.extra["resp_headers"] = _redact_headers([(k.decode("latin-1"), v.decode("latin-1")) for k, v in resp.headers.raw], route.secret, by_name=False)
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
    @staticmethod
    def _live_row(rec: _Exchange) -> dict[str, Any]:
        return _row(rec.id, rec.meta.get("state") or "pending", rec.meta, rec.keep, rec.conv)

    @staticmethod
    def _publish(project: str, row: dict[str, Any]) -> None:
        hub.publish(f"project:{project}", {"type": "capture", "session_id": row.get("session"), "row": row})

    def _begin(self, rec: _Exchange) -> None:
        self._active[rec.id] = rec

        def job() -> list[dict[str, Any]]:  # worker thread. A dict the event loop may be reading (a published row) is replaced, never changed
            body: Any = None
            try:
                plain, _ = decode_body(rec.request, _header(rec.extra["req_headers"], "content-encoding"))
                body = json.loads(plain) if plain else None
                summary = request_summary(rec.meta["path"], body)
            except Exception:
                summary = {"api": api_of(rec.meta["path"])}
            sid, thread, head = rec.meta["session"], summary.get("thread"), conversation_head(summary["api"], body)
            rec.conv = f"{thread if thread and thread != sid else ''}:{head}" if head else None
            if thread and thread == sid:  # Codex: the agent's own thread; another one is a sub-agent's
                summary.pop("thread")
            elif thread and summary.get("kind") == "agent":
                summary["kind"] = "sub"
            rec.keep = "compact" if summary.get("kind") == "compact" else None
            rec.meta["summary"] = summary
            kept: list[dict[str, Any]] = []
            if self._store.db:
                self._store.begin(rec.id, rec.route.project, sid, rec.meta["started"], dict(rec.meta), dict(rec.extra), rec.request,
                                  rec.conv, rec.keep)
                before = (self._store.keep_before(rec.route.project, sid, rec.conv, summary.get("loop"), rec.meta["started"])
                          if rec.keep and rec.conv else None)
                if before:
                    kept.append(before)
            return kept

        def done(f: asyncio.Future) -> None:
            if f.exception():
                log.warning("capture: recording a request failed: %s", f.exception())
                return
            if rec.id in self._active:  # its summary, while the answer is still coming
                self._publish(rec.route.project, self._live_row(rec))
            for row in f.result():  # the agent's last call before this compaction, kept from now on
                live = self._active.get(row["id"])
                if live:
                    live.keep = row["keep"]
                self._publish(rec.route.project, self._live_row(live) if live else row)

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
            if summary.get("thread") == sid:  # as in _begin, for a session known only by now
                summary.pop("thread")
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
        self._publish(rec.route.project, self._live_row(rec))
        limit = int(float(config.get("capture.max_mb", 1024) or 0) * 1024 * 1024)
        if limit > 0 and self._store.total - self._store.kept > limit and now - self._pruned > 10:
            self._pruned = now
            n = await self._db(self._store.prune, limit)
            log.info("capture: pruned the %d oldest exchanges (over capture.max_mb; %d MB kept apart)", n, self._store.kept >> 20)

    # ---- reading -------------------------------------------------------------------
    async def rows(self, project: str, session: str) -> list[dict[str, Any]]:
        stored = await self._db(self._store.rows, project, session) if self._store.db else []
        live = {r.id: self._live_row(r) for r in list(self._active.values()) if r.route.project == project and r.meta.get("session") == session}
        rows = [live.pop(r["id"], r) for r in stored] + list(live.values())
        return sorted(rows, key=lambda r: r.get("started") or 0)

    async def detail(self, project: str, rid: str, request: bool = True) -> dict[str, Any] | None:
        """One exchange in full: its row, URL, headers, the times its bytes came, and both bodies (one still coming
        is what came so far). Without ``request``, no request body."""
        rec = self._active.get(rid)
        meta: dict[str, Any]
        extra: dict[str, Any]
        if rec and rec.route.project == project:
            meta = {**rec.meta, "state": rec.meta.get("state") or "pending", "keep": rec.keep, "conv": rec.conv}
            extra, req, resp = {**rec.extra, "marks": list(rec.marks)}, rec.request, b"".join(rec.chunks)
        elif self._store.db:
            got = await self._db(self._store.get, project, rid, request)
            if not got:
                return None
            meta, extra, req, resp = got
        else:
            return None

        def bodies() -> dict[str, Any]:
            out = {"response": _body(resp, _header(extra.get("resp_headers"), "content-encoding"))}
            if request:
                out["request"] = _body(req, _header(extra.get("req_headers"), "content-encoding"))
            return out

        return {**_row(rid, meta["state"], meta, meta.get("keep"), meta.get("conv")), "url": extra.get("url"),
                "req_headers": extra.get("req_headers") or [], "resp_headers": extra.get("resp_headers") or [], "marks": extra.get("marks") or [],
                **await asyncio.to_thread(bodies)}

    async def release_project(self, project: str) -> None:
        """A project removed from Atrium: its kept calls are pruned like the others from now on."""
        if self._store.db:
            await self._db(self._store.release_project, project)

    async def release_gone(self, project: str, known: Iterable[str]) -> None:
        """Sessions of the project Atrium no longer finds (``known``: the ones it does; their transcript was deleted
        elsewhere): from RELEASE_DAYS after their last call, their kept calls are pruned like the others. Looks at most
        once an hour per project, and not when it finds no session at all."""
        known, now = set(known), time.time()
        if not self._store.db or not known or now - self._released.get(project, 0) < 3600:
            return
        self._released[project] = now
        n = await self._db(self._store.release_gone, project, known, now - RELEASE_DAYS * 86400)
        if n:
            log.info("capture: %d sessions of %s are gone; their kept calls are pruned like the others from now on", n, project)

    async def forget(self, project: str, session: str) -> None:
        """Delete what was recorded of a session (it was deleted, or the human cleared it)."""
        if self._store.db:
            await self._db(self._store.delete_session, project, session)


capture = Capture()
