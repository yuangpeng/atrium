"""Inline completion in the Files editor, as Cursor's Tab: what comes next at the cursor, shown in grey while one types,
Tab taking it.

A model behind an OpenAI-compatible ``/v1/chat/completions`` (``completion`` in settings.json; the server and key are
``voice``'s unless given) gets the file around the cursor, the cursor marked, and writes the cursor's line from its
start, completed, with the lines that should follow it. What it adds there is the suggestion (``suggestion``), cut
where it has enough: at the end of the line when there is code after the cursor, else at the end of the statement or
block the cursor is in (``MAX_LINES`` at most), and without what it repeats of the code after the cursor.

A model answers in a second or two, slow for this. What makes up for it here (the page does its part, completion.ts):
- the connection to the server stays open between requests: a new one (TLS, a proxy) costs up to a second more;
- a request the page gave up on runs on, its answer kept (stopping it would close the connection): the human may
  type on just as the model predicted;
- a request looks through the answers of those before it, done or on their way (``_Job.typed``): when what was typed
  since is how one of them starts, the rest of it is the answer, at once or as soon as it comes;
- ``speculative``: what comes after the suggestion shown is asked for while it shows, so that Tab after Tab finds the
  next one waiting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from .config import config
from .voice import endpoint

log = logging.getLogger("atrium.complete")

BEFORE_CHARS = 6000  # of the file before the cursor, sent (the page cuts it at a line's start)
AFTER_CHARS = 2000  # after it
MAX_LINES = 12  # of a suggestion
MAX_TOKENS = 400  # the model's reply: the cursor's line repeated, and the suggestion
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
KEEP_SECONDS = 90  # an answer is kept this long for the requests after it
MAX_JOBS = 16  # answers kept per file
MAX_RUNNING = 3  # requests to the model at once per file: beyond, the oldest is stopped
FIRST_GRACE = 0.15  # s: a suggestion's first line is sent alone when the rest isn't there this long after it
CURSOR = "<|cursor|>"
SKIP_LANGUAGES = {"plaintext", "markdown"}  # prose: no suggestions
# files that hold secrets, never sent to the model (editor/completion.ts has the same)
SECRET_FILES = re.compile(
    r"(?:^\.env(?:rc|\..*)?|\.(?:pem|key|p12|pfx|keystore|jks)|^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?"
    r"|^\.(?:npmrc|pypirc|netrc|pgpass)|^credentials(?:\..*)?|secrets?\.(?:json|ya?ml|toml))$",
    re.I,
)

SYSTEM = f"""You are the autocomplete of a code editor. You get a file with the marker {CURSOR} where the cursor is, \
and write the code that should come next at the cursor.

Reply with the line the cursor is on, from its first character, completed; then the lines that should follow it if \
the code at the cursor goes on there: the rest of the statement, or of the block, the cursor is in. Stop there.

- Repeat the cursor's line up to {CURSOR} exactly, indentation included, then go on from the cursor.
- Never write again what already comes after {CURSOR}: it stays in the file after what you add.
- Follow the file's style and indentation, and use the names it defines.
- No explanations, no Markdown, no code fences. If nothing should be added, reply with the cursor's line up to \
{CURSOR} alone."""


class CompletionError(Exception):
    """A completion that can't be had: not set up (400), or the model's server said no or didn't answer (502)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def settings() -> dict[str, Any]:
    s = dict(config.get("completion") or {})
    voice = config.get("voice") or {}
    s["base_url"] = str(s.get("base_url") or voice.get("base_url") or "")
    s["api_key"] = str(s.get("api_key") or voice.get("api_key") or "")
    return s


# ---- what the model's reply makes of the suggestion ---------------------------------------------------------------
# a line that ends by opening a block: what follows is indented under it
_OPENS = re.compile(r"(?:[{(\[:]|=>|->|\bdo|\bthen|\belse|\\)\s*(?://.*|#.*)?$")
# a line that goes on in the next: a comma, an operator, a dot
_GOES_ON = re.compile(r"(?:[,=+\-*/%&|^<>?.]|\band|\bor|\bnot)\s*$")
# a line that starts by closing one
_CLOSES = re.compile(r"\s*(?:[)\]}]|end\b|fi\b|done\b|esac\b|</)")
# what may follow the cursor on its line for a suggestion to go in before it: closing brackets, quotes, punctuation
_CLOSERS_ONLY = re.compile(r"[\s)\]}>'\"`;,.:]*")


def _indent(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def wanted(line_after: str, language: str = "") -> bool:
    """Whether to suggest at all: not in prose, nor in the middle of a word or of a line's code (closing brackets and
    the like may follow the cursor)."""
    return language not in SKIP_LANGUAGES and _CLOSERS_ONLY.fullmatch(line_after) is not None


def _unfenced(out: str) -> tuple[str, bool] | None:
    """The reply without a code fence around it, and whether it was closed (nothing after it counts); None while the
    fence's first line is still coming."""
    if out.lstrip().startswith("```"):
        start = out.lstrip()
        if "\n" not in start:
            return None
        out = start.split("\n", 1)[1]
    end = re.search(r"^[ \t]*```", out, re.M)
    return (out[: end.start()], True) if end else (out, False)


def _added(out: str, line_before: str, final: bool) -> str | None:
    """What the reply adds at the cursor: the reply less the cursor's line up to the cursor, which it repeats (its
    indentation may differ); None while it may still be repeating it."""
    if out.startswith(line_before):
        return out[len(line_before):]
    if line_before.startswith(out) and not final:
        return None
    head, lb = out.lstrip(" \t"), line_before.lstrip(" \t")
    if lb and head.startswith(lb):
        return head[len(lb):]
    if lb and lb.startswith(head) and not final:
        return None
    if not lb:  # the cursor after indentation: the reply's own, less what is there already
        lead = len(out) - len(head)
        return out[min(lead, len(line_before)):]
    # it didn't repeat the line: all of it is new. Indented, it is the next line's (a model that wrote only what follows
    # a finished line): on a line of its own
    return "\n" + out if re.match(r"(?: {2,}|\t)\S", out) else out


def suggestion(out: str, line_before: str, after: str, final: bool) -> str | None:
    """The suggestion in the model's reply ``out`` so far, for a cursor with ``line_before`` before it on its line and
    ``after`` after it; None while more of the reply is needed. ``final``: the reply is complete."""
    unfenced = _unfenced(out)
    if unfenced is None:
        return None if not final else ""
    body, closed = unfenced
    done = final or closed
    added = _added(body, line_before, done)
    if added is None:
        return None
    line_after, _, rest = after.partition("\n")
    if line_after.strip():
        # code after the cursor: the rest of this line, without the brackets and the like it ends with already
        if "\n" not in added and not done:
            return None
        text = added.split("\n", 1)[0].rstrip()
        closers = line_after.strip()
        for k in range(len(closers), 0, -1):
            if text.endswith(closers[:k]):
                return text[: len(text) - k].rstrip()
        return text
    return _block(added, line_before, rest, done)


def first_line(out: str, line_before: str, after: str) -> str | None:
    """The suggestion's first line, once the reply has it whole, as long as more may follow: the reply's first line of
    code at the end of a line (the line's rest when it has one, else the next line). None: not yet, or none."""
    line_after, _, rest = after.partition("\n")
    unfenced = _unfenced(out)
    if line_after.strip() or unfenced is None:
        return None
    added = _added(unfenced[0], line_before, unfenced[1])
    if not added or "\n" not in added:
        return None
    lines = added.split("\n")[:-1]  # the complete ones
    k = next((i for i, line in enumerate(lines) if line.strip()), None)
    return None if k is None else _block("\n".join(lines[: k + 1]), line_before, rest, True) or None


def _block(added: str, line_before: str, rest: str, done: bool) -> str | None:
    """A suggestion at a line's end: the line's rest and the lines that follow it, to the end of the statement or block
    (MAX_LINES at most), less the lines the file has already after the cursor."""
    lines = added.split("\n")
    if not done:
        lines = lines[:-1]  # the last one may not be complete yet
        if not lines:
            return None
    base = _indent(line_before) if line_before.strip() else len(line_before.expandtabs(4))
    head = line_before + lines[0]
    if not line_before.strip():
        mode = "statements"  # a new line: whole statements at its indentation
    elif _OPENS.search(head.rstrip()):
        mode = "body"  # it opens a block: the lines under it, and its closing line
    elif not lines[0].strip() and not _GOES_ON.search(line_before):
        mode = "statements"  # the line complete: the statements after it
    else:
        mode = "continued"  # a statement in the middle: its lines go deeper, a closing line at its own depth ends it
    out = [lines[0]]
    stopped = False
    for line in lines[1:]:
        if len(out) >= MAX_LINES:
            stopped = True
            break
        if not line.strip():
            if mode == "statements" and any(x.strip() for x in out):
                stopped = True  # a paragraph's end ends the suggestion
                break
            out.append(line)
            continue
        depth = _indent(line)
        if mode == "statements":
            if depth < base:
                stopped = True
                break
        elif depth <= base:
            stopped = True
            if _CLOSES.match(line):
                out.append(line)
            break
        out.append(line)
    if not stopped and not done:
        return None
    # what the file has already after the cursor: the reply's last lines, when they are the next ones there
    nxt = [x.strip() for x in rest.split("\n") if x.strip()][:4]
    for k in range(min(len(out) - 1, len(nxt)), 0, -1):
        if [x.strip() for x in out[-k:]] == nxt[:k]:
            out = out[:-k]
            break
    text = "\n".join(out).rstrip()
    return text if text.strip() else ""


# ---- the requests ------------------------------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None
_omit: dict[tuple[str, str], set[str]] = {}  # (server, model) -> what it refused, sent no more: reasoning_effort, …


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT, limits=httpx.Limits(max_connections=32, max_keepalive_connections=8, keepalive_expiry=120))
    return _client


def _payload(s: dict[str, Any], label: str, language: str, before: str, after: str, line_rest: str) -> dict[str, Any]:
    tokens = 120 if line_rest.strip() else MAX_TOKENS
    p: dict[str, Any] = {
        "model": s["model"],
        "stream": True,
        "temperature": 0,
        "max_completion_tokens": tokens,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"File: {label}{f' ({language})' if language else ''}\n\n{before}{CURSOR}{after}"},
        ],
    }
    if s.get("effort"):
        p["reasoning_effort"] = s["effort"]
    for k in _omit.get((s["base_url"], s["model"]), ()):
        p = _without(p, k)
    return p


def _without(p: dict[str, Any], key: str) -> dict[str, Any]:
    p = dict(p)
    v = p.pop(key, None)
    if key == "max_completion_tokens" and v is not None:
        p["max_tokens"] = v  # what an older server takes instead
    return p


async def _stream(s: dict[str, Any], payload: dict[str, Any]):
    """The reply's text, piece by piece. A parameter the model refuses (a 400 naming it) is dropped, for good."""
    url = endpoint(s["base_url"], "/chat/completions")
    headers = {"Authorization": f"Bearer {s['api_key']}"} if s.get("api_key") else {}
    for _ in range(3):
        try:
            async with _http().stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode(errors="replace")
                    try:
                        err = json.loads(text).get("error")
                        msg = str((err.get("message") if isinstance(err, dict) else err) or text)
                    except (ValueError, AttributeError):
                        msg = text
                    refused = next((k for k in ("reasoning_effort", "temperature", "max_completion_tokens") if k in msg and k in payload), None)
                    if r.status_code == 400 and refused:
                        _omit.setdefault((s["base_url"], s["model"]), set()).add(refused)
                        log.info("%s refused %s: asked again without it", s["model"], refused)
                        payload = _without(payload, refused)
                        continue
                    raise CompletionError(f"{s['model']} ({r.status_code}): {msg[:300]}")
                # read to the end of the body, past [DONE]: a response left unread closes the connection
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    for choice in chunk.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            yield piece
                return
        except httpx.TimeoutException as e:
            raise CompletionError(f"{s['model']}: no answer from {url} in time") from e
        except httpx.TransportError as e:
            raise CompletionError(f"{s['model']}: can't reach {url}: {e or type(e).__name__}") from e


class _Job:
    """One request to the model, for the cursor where it was: ``answer`` is the suggestion, as soon as the reply has it
    (the reply is read to its end all the same, which keeps the connection for the next request); ``first`` its first
    line, as soon as that is there ("": none came before the answer)."""

    def __init__(self, s: dict[str, Any], label: str, language: str, before: str, after: str):
        self.model = s["model"]
        self.before, self.after = before, after
        self.tail = before[-400:]  # what finds where it was, after typing
        self.line_before = before.rsplit("\n", 1)[-1]
        self.answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.first: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.started = time.monotonic()
        self.ended = 0.0
        line_rest = after.split("\n", 1)[0]
        self.task = asyncio.create_task(self._run(s, _payload(s, label, language, before, after, line_rest)))

    async def _run(self, s: dict[str, Any], payload: dict[str, Any]) -> None:
        out = ""
        try:
            async for piece in _stream(s, payload):
                out += piece
                if not self.answer.done() and (text := suggestion(out, self.line_before, self.after, False)) is not None:
                    self.answer.set_result(text)
                if not self.first.done() and (line := first_line(out, self.line_before, self.after)) is not None:
                    self.first.set_result(line)
            if not self.answer.done():
                self.answer.set_result(suggestion(out, self.line_before, self.after, True) or "")
        except asyncio.CancelledError:
            if not self.answer.done():
                self.answer.set_result("")
            raise
        except Exception as e:
            if not self.answer.done():
                self.answer.set_exception(e if isinstance(e, CompletionError) else CompletionError(f"{self.model}: {e}"))
                self.answer.exception()  # retrieved: no "never retrieved" warning when no one waits for it
        finally:
            if not self.first.done():
                self.first.set_result("")
            self.ended = time.monotonic()

    def typed(self, before: str, after: str) -> str | None:
        """What was typed at the cursor since this request, if the request was for the same place: the file after the
        cursor as it was, and before it the same, but for what was typed."""
        if after != self.after:
            return None
        if before.endswith(self.tail):
            return ""
        at = before.rfind(self.tail, max(0, len(before) - len(self.tail) - 600))
        return before[at + len(self.tail):] if at >= 0 else None

    def answered(self) -> bool:
        return self.answer.done() and not self.answer.cancelled() and self.answer.exception() is None

    def rest(self, typed: str, first: bool = False) -> str:
        """The suggestion (``first``: its first line) less what was typed of it: "" once it is all typed, or when the
        typing went elsewhere."""
        fut = self.first if first else self.answer
        if not (fut.done() and not fut.cancelled() and fut.exception() is None):
            return ""
        text = fut.result()
        return text[len(typed):] if text.startswith(typed) and len(text) > len(typed) else ""


_jobs: dict[str, list[_Job]] = {}  # file -> its requests, the latest last


def _tidy(jobs: list[_Job]) -> list[_Job]:
    now = time.monotonic()
    jobs = [j for j in jobs if not j.ended or now - j.ended < KEEP_SECONDS][-MAX_JOBS:]
    running = [j for j in jobs if not j.task.done()]
    for j in running[: max(0, len(running) - MAX_RUNNING)]:
        j.task.cancel()  # the oldest
    return jobs


async def complete(pid: str, path: str, label: str, language: str, before: str, after: str, speculative: bool = False,
                   whole: bool = False) -> dict[str, Any]:
    """The suggestion for a cursor with ``before`` and ``after`` around it in the file ``path`` (absolute; ``label``:
    its name for the model). ``speculative``: asked ahead (what comes after the suggestion shown): answered at once,
    the request left to run for the one that comes after Tab. A suggestion of several lines may come first as its
    first line alone (``partial``: the model is still writing the rest), to be asked for again with ``whole``."""
    s = settings()
    if not s.get("model"):
        raise CompletionError("Inline completion has no model: set completion.model in Atrium's settings", 400)
    if not s["base_url"]:
        raise CompletionError("Inline completion has no server: set completion.base_url (or voice.base_url)", 400)
    if not wanted(after.split("\n", 1)[0], language) or SECRET_FILES.search(path.rsplit("/", 1)[-1]):
        return {"text": ""}
    key = f"{pid}\0{path}"
    jobs = _jobs[key] = _tidy(_jobs.get(key, []))
    # what the latest requests hold already, or will
    fits: list[tuple[_Job, str]] = []
    for j in reversed(jobs):
        if j.model != s["model"] or (typed := j.typed(before, after)) is None:
            continue
        if j.answer.done():
            if (text := j.rest(typed)) or (typed == "" and j.answered()):
                return {"text": text, "cached": True}  # "" for this very place: the model had nothing to add
        elif len(fits) < 2:
            fits.append((j, typed))
    exact = any(typed == "" for _, typed in fits)
    if speculative:
        if not exact:
            jobs.append(_Job(s, label, language, before, after))
            _jobs[key] = _tidy(jobs)
        return {"text": "", "queued": True}
    own = None
    if not exact:
        own = _Job(s, label, language, before, after)
        jobs.append(own)
        _jobs[key] = _tidy(jobs)
    # the first that fits: one on its way since before the typing, or this one's own; its first line, if the rest is slow
    candidates = [*fits, *([(own, "")] if own else [])]
    waiting: dict[asyncio.Future[str], tuple[_Job, str, bool]] = {}
    for j, typed in candidates:
        waiting[asyncio.ensure_future(asyncio.shield(j.answer))] = (j, typed, False)
        if not whole:
            waiting[asyncio.ensure_future(asyncio.shield(j.first))] = (j, typed, True)
    try:
        while waiting:
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                j, typed, first = waiting.pop(fut)
                if not first and j is own and (err := fut.exception()) is not None:
                    raise err
                if text := j.rest(typed):
                    return {"text": text, "ms": round((time.monotonic() - j.started) * 1000)}
                if first and (line := j.rest(typed, first=True)):
                    try:  # a short answer ends right after its first line
                        await asyncio.wait_for(asyncio.shield(j.answer), FIRST_GRACE)
                    except (TimeoutError, CompletionError):
                        pass
                    if text := j.rest(typed):
                        return {"text": text, "ms": round((time.monotonic() - j.started) * 1000)}
                    if not j.answer.done():
                        return {"text": line, "partial": True, "ms": round((time.monotonic() - j.started) * 1000)}
        return {"text": ""}
    finally:
        for fut in waiting:
            fut.cancel()
