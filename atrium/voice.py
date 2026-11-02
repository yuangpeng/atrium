"""Voice input for the composer: what the human said, as the message they meant to type.

Two model calls, both to an OpenAI-compatible server (``voice`` in settings.json):

1. **Speech to text**: the browser's recording (16 kHz WAV) goes to ``/v1/audio/transcriptions``
   (``voice.asr_model``). A model the server doesn't serve there — a multimodal chat model such as Doubao Seed —
   gets it through ``/v1/chat/completions`` instead, as ``input_audio``, told to write down what is said; which
   route a model takes is found out on its first recording.
2. **Refine**: the transcript goes to the Responses API, ``/v1/responses`` (``voice.refine_model``, at
   ``voice.refine_effort``), with what the agent itself sees — its conversation since the last compaction,
   thinking left out —, its cards on the board, the project's files and the draft already in the composer. The
   model writes the message the human meant, not word for word: filler and retracted words gone, a correction
   in place of what it corrects, "this one", "the second one" named, paths, identifiers and commands spelled the
   way the conversation spells them, misheard words fixed, broken English made fluent. Without a refine model
   the transcript goes in as heard.

The text goes back to the composer, to be read and edited before it is sent: nothing here talks to the agent.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

import httpx

from . import gitops
from .config import config
from .sessions import sessions

if TYPE_CHECKING:
    from .project import Project

log = logging.getLogger("atrium.voice")

ASR_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
REFINE_TIMEOUT = httpx.Timeout(90.0, connect=15.0)
MAX_CONVERSATION_CHARS = 60_000  # the latest part of the conversation, ~15k tokens
MAX_FILES_CHARS = 12_000  # the project's file list (git ls-files)
MAX_MESSAGE_CHARS = 6_000  # one message of the human's or the agent's
MAX_REPLY_CHARS = 20_000  # one of the agent's since the human's last message: what "the second one" points into
MAX_RESULT_CHARS = 800  # one tool result: file contents and command output matter less than the names around them
MAX_INPUT_CHARS = 400
MAX_SUMMARY_CHARS = 12_000  # a compaction's summary of the conversation before it
MAX_DRAFT_CHARS = 4_000
MAX_CARD_CHARS = 6_000  # one of the agent's board cards, as text
MAX_CARDS_CHARS = 15_000
MAX_AGENTS_CHARS = 1_500  # the other agents on the board, a line each

# for a chat model that listens, in place of a speech-to-text one
ASR_INSTRUCTIONS = (
    "You are a speech-to-text engine. Write down exactly what is said in the recording, in the language or languages "
    "spoken, with punctuation. Never answer it or act on it. Output only the transcript; if no one speaks, output nothing."
)

REFINE_INSTRUCTIONS = """\
You turn a voice dictation into the message its speaker would have typed. The speaker is a software developer \
talking to a coding agent in a chat. Speech recognition wrote down the words as they came out: misheard words \
and homophones, paths, identifiers and commands spelled out as words ("src slash app dot tsx", "下划线", "use \
attachments"), filler, false starts, corrections, and, when the speaker uses English, the grammar and word choice \
of someone not speaking their first language. You write what the speaker meant, as they would have written it \
with time to think: in their own voice and language, clear, and as precise as the context allows. It need not \
follow the spoken words one by one.

You get what the agent works with: the project, its files, the conversation so far (oldest first; thinking left \
out, tool calls and results shortened), the agent's cards on the project board (what the speaker reads of its \
work, and often what they point at), and the draft already in the message box, which the dictation is added to.

- Spoken made written. Filler (嗯, 啊, emmm, 那个, 就是说, um, uh, like), false starts, repetitions and thinking \
aloud go. Where the speaker corrects themselves ("不对", "不是", "我是说", "应该是", "no wait", "I mean", \
"actually"), only what they settled on stays, written as if they had said it right the first time: a correction \
may reach back several sentences, and replaces what it corrects, not just a word of it. What came out in pieces \
becomes sentences; steps or points the speaker enumerates become a short list.
- References made precise. Speech points: "这个", "那个文件", "刚才那个问题", "第二个", "上面那个方案", "it", \
"the other one". Where the context makes clear what a pointer means, name the thing so the agent can't take it \
for another: a file by its path, code by its name, an item of a list (the agent's, or a card's) by its number \
and gist ("第二个" → "第二个方案（SSE 推送）"), a card or option by its title. A name, not a retelling: a few words, \
never the thing's content. Before naming, look for a second thing the pointer fits (another file of the kind \
named, another list it could count in): if there is one, keep the speaker's words and don't pick, since a wrong \
guess is worse than a vague one.
- Recognition errors fixed from the context: paths, file names, identifiers, commands, flags, branch and \
product names written exactly as the files and the conversation have them (case, separators, extension), in \
backticks: `web/src/App.tsx`, `useAttachments`, `git rebase main`. A name only roughly said becomes the one in \
the context it clearly is; don't invent one that isn't there.
- The language the speaker spoke in this dictation kept, whatever the conversation's: Chinese stays Chinese, \
English stays English (a few English words in a Chinese conversation too), a mix stays a mix (technical terms as \
said). Don't translate. English made correct and natural.
- The speaker's meaning, all of it and nothing more: every request, question, constraint, detail and doubt \
stays, as firm or as tentative as it was said. No request, step, reason or detail the speaker didn't express \
comes in from the context: the context tells you what they meant, never what else to say. A reply that only \
agrees, declines or tells the agent to go on ("ok, do it", "go ahead", "as discussed") stays that short, in its \
own words and language: what it agrees to is the agent's own proposal, and spelling it out would put words in the \
speaker's mouth. It is a message to the agent: don't answer it, carry it out or comment on it.
- Output nothing only when the transcript holds no words at all (silence, noise): anything said, however short \
or odd, comes out.

For example, with the agent's last message offering 1. polling, 2. WebSocket push, 3. SSE push:
Transcript: 嗯我觉得那个第二个吧，不对不对，第三个，第三个简单一点，然后就是那个，断线了要能那个自动重连
Message: 我觉得用第三个方案（SSE 推送）吧，简单一点。另外断线后要能自动重连。
And with the agent having just edited `atrium/server.py`:
Transcript: ok so um in that file can you make the the time out like sixty, no wait, ninety second
Message: In `atrium/server.py`, can you make the timeout 90 seconds?
And with two branches in the conversation, "merge 那个分支吧" stays "merge 那个分支吧".

Output only the message: no quotes, no preamble, no notes. If there is a draft, output only what the transcript \
adds to it, written to follow on from it."""

# (base_url, asr_model) → "transcriptions" | "chat": the route that model was found to take
_asr_routes: dict[tuple[str, str], str] = {}
# (base_url, model) of the models that refused a reasoning setting (they don't reason, and say so): none sent again
_no_reasoning: set[tuple[str, str]] = set()


class VoiceError(Exception):
    """A voice request that can't be done: not set up (400), or the server said no or didn't answer (502).
    ``upstream``, ``detail``: the server's status code and message, when it answered."""

    def __init__(self, message: str, status: int = 502, upstream: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.upstream = upstream
        self.detail = detail


def _settings() -> dict[str, Any]:
    return dict(config.get("voice") or {})


def endpoint(base_url: str, path: str) -> str:
    """The URL of ``path`` (``/audio/transcriptions``) on a server given with or without its version:
    ``https://host`` and ``https://host/v1`` both mean ``https://host/v1``."""
    base = base_url.strip().rstrip("/")
    if not re.search(r"/v\d+[a-z0-9]*$", base):
        base += "/v1"
    return base + path


async def _send(url: str, s: dict[str, Any], what: str, timeout: httpx.Timeout, **kw: Any) -> httpx.Response:
    headers = {"Authorization": f"Bearer {s['api_key']}"} if s.get("api_key") else {}
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.post(url, headers=headers, **kw)
        except httpx.TimeoutException as e:
            raise VoiceError(f"{what}: no answer from {url} in time") from e
        except httpx.TransportError as e:  # a dropped connection: once more
            if attempt == 2:
                raise VoiceError(f"{what}: can't reach {url}: {e or type(e).__name__}") from e
    raise AssertionError("unreachable")


async def _post(url: str, s: dict[str, Any], what: str, timeout: httpx.Timeout, **kw: Any) -> dict[str, Any]:
    r = await _send(url, s, what, timeout, **kw)
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        if r.status_code < 400:
            return {"text": r.text}  # a plain-text reply (a transcription server's response_format=text)
        body = {}
    if r.status_code >= 400 or body.get("error"):
        err = body.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or body.get("detail") or body.get("message")
        msg = str(msg or r.text[:300] or r.reason_phrase)
        raise VoiceError(f"{what} failed ({r.status_code}): {msg}", upstream=r.status_code, detail=msg)
    return body


async def _post_reasoning(url: str, s: dict[str, Any], what: str, timeout: httpx.Timeout, payload: dict[str, Any], key: str) -> dict[str, Any]:
    """``_post`` of a JSON ``payload`` whose ``key`` sets the model's reasoning. A model that refuses it (a 400 that
    says so: it doesn't reason) is asked again without it, and sent none from then on."""
    model = (s["base_url"], payload["model"])
    if model in _no_reasoning:
        payload = {k: v for k, v in payload.items() if k != key}
    try:
        return await _post(url, s, what, timeout, json=payload)
    except VoiceError as e:
        if key not in payload or e.upstream != 400 or "reasoning" not in str(e).lower():
            raise
        _no_reasoning.add(model)
        log.info("%s refused %s: asked again without it", payload["model"], key)
        return await _post(url, s, what, timeout, json={k: v for k, v in payload.items() if k != key})


async def transcribe(audio: bytes, filename: str, mime: str | None) -> str:
    """The recording as text, as the speech-to-text model heard it."""
    s = _settings()
    if not (s.get("base_url") and s.get("asr_model")):
        raise VoiceError("Voice input isn't set up: give voice.base_url and voice.asr_model in Atrium's settings", 400)
    started = time.monotonic()
    key = (s["base_url"], s["asr_model"])
    route = _asr_routes.get(key)
    if route == "chat":
        text = await _transcribe_chat(audio, filename, s)
    else:
        try:
            text = await _transcribe(audio, filename, mime, s)
            route = "transcriptions"
        except VoiceError as e:
            if route or e.upstream not in (400, 404, 405, 415, 422):  # it works there, or failed for another reason
                raise
            try:  # not a speech-to-text model there: a chat model that listens?
                text = await _transcribe_chat(audio, filename, s)
            except VoiceError as e2:
                raise VoiceError(f"{e}; and through /chat/completions ({e2.upstream or 'no answer'}): {e2.detail or e2}") from e2
            route = "chat"
    _asr_routes[key] = route
    log.info("transcribed %d bytes in %.1fs (%s, %s): %d chars", len(audio), time.monotonic() - started, s["asr_model"], route, len(text))
    return text


async def _transcribe(audio: bytes, filename: str, mime: str | None, s: dict[str, Any]) -> str:
    body = await _post(
        endpoint(s["base_url"], "/audio/transcriptions"), s, "Speech recognition", ASR_TIMEOUT,
        files={"file": (filename, audio, mime or "application/octet-stream")}, data={"model": s["asr_model"]},
    )
    return str(body.get("text") or "").strip()


async def _transcribe_chat(audio: bytes, filename: str, s: dict[str, Any]) -> str:
    fmt = os.path.splitext(filename)[1].lstrip(".").lower() or "wav"
    payload: dict[str, Any] = {
        "model": s["asr_model"],
        "messages": [
            {"role": "system", "content": ASR_INSTRUCTIONS},
            {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}]},
        ],
        "reasoning_effort": "minimal",  # a reasoning model would think it over first: a minute instead of seconds
    }
    body = await _post_reasoning(endpoint(s["base_url"], "/chat/completions"), s, "Speech recognition", ASR_TIMEOUT, payload, "reasoning_effort")
    choices = body.get("choices") or [{}]
    return _text((choices[0] or {}).get("message", {}).get("content")).strip()


async def refine(project: "Project", transcript: str, *, sid: str | None = None, cwd: str | None = None, draft: str = "") -> dict[str, Any]:
    """The transcript rewritten as the human meant it, with the agent's context (``sid``), or the project's
    for a new agent (``cwd``: where it will work). ``refined`` is false when it comes back as heard."""
    s = _settings()
    transcript = transcript.strip()
    if not transcript or not (s.get("base_url") and s.get("refine_model")):
        return {"text": transcript, "refined": False}
    started = time.monotonic()
    prompt = await refine_input(project, transcript, sid=sid, cwd=cwd, draft=draft)
    payload: dict[str, Any] = {"model": s["refine_model"], "instructions": REFINE_INSTRUCTIONS, "input": prompt, "store": False}
    if s.get("refine_effort"):
        payload["reasoning"] = {"effort": s["refine_effort"]}
    body = await _post_reasoning(endpoint(s["base_url"], "/responses"), s, "Refining", REFINE_TIMEOUT, payload, "reasoning")
    if body.get("status") == "failed":
        raise VoiceError(f"Refining failed: {body.get('error') or 'the model gave up'}")
    text = output_text(body).strip()
    log.info("refined %d chars (%d of context) in %.1fs (%s): %d chars", len(transcript), len(prompt), time.monotonic() - started,
             s["refine_model"], len(text))
    return {"text": text, "refined": True}


def output_text(body: dict[str, Any]) -> str:
    """The text of a Responses API reply: its final answer, when the model labels its messages."""
    if isinstance(body.get("output_text"), str):  # what the SDKs compute; some servers send it too
        return body["output_text"]
    messages = [o for o in body.get("output") or [] if isinstance(o, dict) and o.get("type") == "message"]
    final = [m for m in messages if m.get("phase") == "final_answer"] or messages
    return "".join(c.get("text") or "" for m in final for c in m.get("content") or [] if c.get("type") == "output_text")


# ---- context -----------------------------------------------------------------
async def refine_input(project: "Project", transcript: str, *, sid: str | None, cwd: str | None, draft: str) -> str:
    """What the refine model reads: the project, its files, the agent's conversation and board cards, the draft,
    the transcript."""
    items: list[dict[str, Any]] = []
    if sid:
        items = list((await sessions.snapshot(project, sid))["items"])
        cwd, _harness = await sessions.session_info(project, sid)
    if not cwd or not os.path.isdir(cwd):
        cwd = project.path  # a new worktree, not made yet
    project.resolve(".", root=cwd)  # the project's own directory or one of its worktrees
    branch = await gitops.current_branch(cwd) if gitops.is_repo(cwd) else None
    parts = [f"<project>\nName: {project.name}\nDirectory: {cwd}" + (f" (branch {branch})" if branch else "") + "\n</project>"]
    files = await _files(cwd)
    if files:
        parts.append(f"<files>\n{files}\n</files>")
    conversation = conversation_text(items, cwd)
    if conversation:
        parts.append(f"<conversation>\n{conversation}\n</conversation>")
    board = board_text(project, sid)
    if board:
        parts.append(f"<board>\n{board}\n</board>")
    if draft.strip():
        parts.append(f"<draft>\n{_cap(draft.strip(), MAX_DRAFT_CHARS)}\n</draft>")
    parts.append(f"<transcript>\n{transcript}\n</transcript>")
    return "\n\n".join(parts)


async def _files(cwd: str) -> str:
    if not gitops.is_repo(cwd):
        return ""
    try:
        out = await gitops.run_git(cwd, "ls-files", "--cached", "--others", "--exclude-standard", max_bytes=MAX_FILES_CHARS * 4)
    except gitops.GitError:
        return ""
    paths = out.splitlines()
    text, size = [], 0
    for i, p in enumerate(paths):
        size += len(p) + 1
        if size > MAX_FILES_CHARS:
            text.append(f"… and {len(paths) - i} more")
            break
        text.append(p)
    return "\n".join(text)


def conversation_text(items: list[dict[str, Any]], cwd: str = "", budget: int = MAX_CONVERSATION_CHARS) -> str:
    """The conversation as the agent sees it now — from its last compaction on —, without thinking, tool
    calls and results shortened (paths inside ``cwd`` relative to it); its latest part, when all of it is
    more than ``budget`` characters."""
    start = max((i for i, it in enumerate(items) if it.get("kind") == "system" and it.get("subtype") == "compact"), default=0)
    items = [it for it in items[start:] if not it.get("parent")]  # a subagent's steps: only its result reaches the agent
    last = max((i for i, it in enumerate(items) if it.get("kind") == "user"), default=-1)
    lines = [line for i, it in enumerate(items) if (line := _line(it, cwd, MAX_REPLY_CHARS if i > last else MAX_MESSAGE_CHARS))]
    out, size = [], 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > budget:
            out.append("… (the conversation before this is left out)")
            break
        out.append(line)
    return "\n".join(reversed(out))


def _line(it: dict[str, Any], cwd: str, cap: int) -> str | None:
    kind = it.get("kind")
    if kind == "user":
        names = ", ".join(a.get("name") or "?" for a in it.get("attachments") or [])
        return f"User: {_cap(it.get('text') or '', MAX_MESSAGE_CHARS)}" + (f" [attached: {names}]" if names else "")
    if kind == "text":
        return f"Agent: {_cap(it.get('text') or '', cap)}"
    if kind == "command_output":
        return f"Command output: {_cap(it.get('text') or '', MAX_RESULT_CHARS)}"
    if kind == "tool":
        return f"Agent used {it.get('name')}: {_tool_input(it.get('input'), cwd)}"
    if kind == "tool_result":
        return f"  → {'error: ' if it.get('is_error') else ''}{_cap(_text(it.get('content')), MAX_RESULT_CHARS)}"
    if kind == "system" and it.get("subtype") == "compact_summary":
        return f"Summary of the conversation before: {_cap(it.get('detail') or it.get('text') or '', MAX_SUMMARY_CHARS)}"
    return None  # thinking, permissions, turn results, usage…


def board_text(project: "Project", sid: str | None) -> str:
    """The agent's cards on the board, as text — the human reads its results there rather than in the chat, and
    points into them —, and the other agents the board shows (not the archived ones), by name and what they are
    doing."""
    agents: dict[str, dict[str, Any]] = project.board.snapshot()["agents"]
    cards: dict[str, dict[str, Any]] = (agents.get(sid or "") or {}).get("cards") or {}
    parts: list[str] = []
    size = 0
    for key, card in sorted(cards.items(), key=lambda kv: -(kv[1].get("updated_at") or 0)):
        text = f"## {card.get('title') or key}\n{_cap(html_text(card.get('html') or ''), MAX_CARD_CHARS)}"
        size += len(text)
        if size > MAX_CARDS_CHARS:
            break
        parts.append(text)
    if parts:
        parts.insert(0, "This agent's cards (newest first):")
    others = sorted((a for s, a in agents.items() if s != sid and not (a.get("stats") or {}).get("archived")),
                    key=lambda a: -(a.get("updated_at") or 0))
    rows = [f"- {a.get('name') or 'Agent'}" + (f": {a['doing']}" if a.get("doing") else "") for a in others]
    if rows:
        parts.append("Other agents on the board (latest first):\n" + _cap("\n".join(rows), MAX_AGENTS_CHARS))
    return "\n\n".join(parts)


_BLOCKS = {"p", "div", "br", "li", "tr", "table", "ul", "ol", "pre", "section", "details", "summary", "blockquote",
           "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "hr", "figcaption"}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip: int = 0  # inside <style> or <script>
        self.pre: int = 0
        self.lists: list[int | None] = []  # the items so far of each <ol> it is in; None for a <ul>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self.skip += 1
        elif tag == "pre":
            self.pre += 1
        if tag in _BLOCKS:
            self.out.append("\n")
        if tag in ("ol", "ul"):
            self.lists.append(0 if tag == "ol" else None)
        elif tag == "li":
            n = self.lists[-1] if self.lists else None
            if n is None:
                self.out.append("- ")
            else:
                self.lists[-1] = n + 1
                self.out.append(f"{n + 1}. ")
        elif tag in ("td", "th"):
            self.out.append(" | " if self.out and not self.out[-1].endswith("\n") else "| ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
        elif tag in ("ol", "ul") and self.lists:
            self.lists.pop()
        if tag in _BLOCKS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_text(html: str) -> str:
    """A card's HTML as the text it shows: one block a line, list items numbered as shown (or "- "), table cells
    between " | "."""
    p = _HTMLText()
    p.feed(html)
    p.close()
    return "\n".join(line for raw in "".join(p.out).splitlines() if (line := raw.rstrip()).strip())


_INPUT_KEYS = ("command", "cmd", "pattern", "glob", "query", "url", "file_path", "path", "notebook_path", "description", "title")
_PATH_KEYS = ("file_path", "path", "notebook_path")


def _tool_input(inp: Any, cwd: str) -> str:
    """A tool call's arguments by what they name (commands, patterns, paths), not the contents it writes."""
    if not isinstance(inp, dict):
        return _cap(_text(inp), MAX_INPUT_CHARS)
    parts = [f"{k}={_cap(_rel(v, cwd) if k in _PATH_KEYS else v, MAX_INPUT_CHARS)}" for k in _INPUT_KEYS if isinstance(v := inp.get(k), str) and v]
    if isinstance(inp.get("changes"), list):  # a Codex patch
        parts.append("files=" + ", ".join(_rel(str(c.get("path")), cwd) for c in inp["changes"] if isinstance(c, dict)))
    return " ".join(parts) or _cap(json.dumps(inp, ensure_ascii=False), MAX_INPUT_CHARS)


def _rel(path: str, cwd: str) -> str:
    return path[len(cwd) + 1 :] if cwd and path.startswith(cwd + os.sep) else path


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):  # content blocks
        return "\n".join(b.get("text") or "" if isinstance(b, dict) else str(b) for b in v)
    return json.dumps(v, ensure_ascii=False) if v is not None else ""


def _cap(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + " …"
