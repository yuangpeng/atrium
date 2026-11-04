# Configuring Atrium

[中文](configuration.zh-CN.md)

Atrium has one settings file of its own, `~/.atrium/settings.json`. Everything else is each harness's
own configuration — the files the `claude`, `codex` and `grok` CLIs and the Antigravity CLI read — so a
setup that works in those CLIs works in Atrium too.

**⚙ Settings** (at the bottom of the rail) opens all of these files in an editor, the open project's
too. A save is checked first (valid JSON / TOML, not changed on disk since you opened it), and the
previous version goes to `~/.atrium/backups/`.

What an agent gets is decided in three places:

1. **The agent's pickers** (new-agent page, chat): permission mode, model, effort.
2. **Atrium's `settings.json`**: what those pickers start with (`harnesses.<harness>`), and everything
   about Atrium itself — the Tracker, Git and Code Review agents, notifications, web tools, extra MCP servers,
   voice input.
3. **The harness's own files**: sign-in, endpoints and relays, MCP servers, hooks, instructions — and the
   model and effort when 1 and 2 leave them on *Default* (`""`).

The permission mode always comes from 1 or 2; the harness's own default mode isn't used.

- [Which file sets what](#which-file-sets-what)
- [Atrium's settings.json](#atriums-settingsjson)
- [Claude Code](#claude-code)
- [Codex](#codex)
- [Antigravity](#antigravity)
- [Grok Build](#grok-build)
- [MCP servers](#mcp-servers)
- [Instructions for agents](#instructions-for-agents)
- [Files in a project](#files-in-a-project)
- [Environment variables](#environment-variables)
- [Checking your setup](#checking-your-setup)

## Which file sets what

| To set… | Claude Code | Codex | Antigravity | Grok Build |
|---|---|---|---|---|
| Sign-in or API key | `claude auth login`, or `ANTHROPIC_API_KEY` | `codex login` | `env.GEMINI_API_KEY` in `~/.gemini/antigravity-cli/settings.json` | `grok login`, or `XAI_API_KEY`; a custom model's `api_key` / `env_key` |
| A relay or another endpoint | `env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json` | `openai_base_url` or `[model_providers]` in `~/.codex/config.toml` | a custom model in `~/.gemini/antigravity-cli/settings.json` | a custom model (`[model.<id>]`) in `~/.grok/config.toml` |
| Model and effort of new agents | Atrium's `harnesses.claude-code`; `""` = `model` / `effortLevel` in Claude Code's settings | Atrium's `harnesses.codex`; `""` = `model` / `model_reasoning_effort` in `config.toml` | Atrium's `harnesses.antigravity`; `""` = `model` in its settings file | Atrium's `harnesses.grok`; `""` = `[models] default` in `config.toml`, and the model's own effort |
| Permission mode of new agents | Atrium's `harnesses.claude-code.permission_mode` | Atrium's `harnesses.codex.permission_mode` | Atrium's `harnesses.antigravity.permission_mode` | Atrium's `harnesses.grok.permission_mode` |
| What runs without asking | the mode, and the `permissions` rules in Claude Code's settings | the mode (approval policy + sandbox) | the mode | the mode, and the `[permission]` rules in `config.toml` |
| MCP servers | Atrium's `mcp_servers`, and Claude Code's own | `[mcp_servers]` in `config.toml` | Atrium's `mcp_servers` | Atrium's `mcp_servers`, and `[mcp_servers]` in `config.toml` |
| Instructions | `CLAUDE.md` (or `AGENTS.md`) | `AGENTS.md` | `AGENTS.md`, `GEMINI.md`, `.agents/rules/` | `AGENTS.md`, `CLAUDE.md`, `.grok/rules/` (and Claude's and Cursor's rules) |
| Which CLI runs | the one bundled with `claude-agent-sdk` | the bundled one, or `harnesses.codex.binary` | the runtime bundled with `google-antigravity` | yours (`~/.grok/bin/grok`, `PATH`), else the bundled one; or `harnesses.grok.binary` |

## Atrium's settings.json

`~/.atrium/settings.json` (`$ATRIUM_HOME/settings.json`): ⚙ Settings → Atrium, or any editor.

- **Changes apply without a restart.** Atrium re-reads the file when it changes. What a setting is for
  decides when you see it: new agents get the `harnesses` defaults, the Tracker reads its settings at its
  next run, and so on. An open page reads the settings when it loads and after a save in ⚙ Settings:
  after editing the file elsewhere, reload the page, or its new-agent page keeps offering the old defaults.
- **Your file is merged over the built-in defaults**, key by key: a key in your file wins, a missing key
  has its default.
- **The first start writes every default into the file.** Those copies keep the values of that day: when
  a later version of Atrium changes a default, your file still says the old one. Delete a key to get the
  current default. A section added after your first start (such as `review_agent`) isn't in your file;
  it runs with its defaults until you add it.
- **A file that isn't valid JSON is ignored**: the last good settings stay in use, and `/api/meta` shows
  the error as `settings._error`. ⚙ Settings doesn't save invalid JSON.
- It holds secrets (the web keys, the voice key), so it is created with mode 600. `SERPER_API_KEY` and
  `JINA_API_KEY` in Atrium's environment win over the file.

The defaults:

```json
{
  "defaults": {"harness": "claude-code"},
  "harnesses": {
    "claude-code": {"permission_mode": "bypassPermissions", "model": "", "effort": ""},
    "codex": {"enabled": true, "binary": "", "permission_mode": "full-access", "model": "", "effort": ""},
    "antigravity": {"enabled": true, "permission_mode": "always-proceed", "model": "", "effort": "high"},
    "grok": {"enabled": true, "binary": "", "permission_mode": "always-approve", "model": "", "effort": ""}
  },
  "sessions": {"idle_close_minutes": 30},
  "notifications": {"enabled": true, "permission": true, "turn_end": true, "sound": true},
  "tracker": {"enabled": true, "harness": "claude-code", "model": "", "effort": "low", "interval_seconds": 300, "after_turn_seconds": 45},
  "git_agent": {"harness": "claude-code", "model": ""},
  "review_agent": {"auto": true, "harness": "claude-code", "model": "", "effort": "", "language": "", "delay_seconds": 15, "max_concurrent": 2},
  "web": {"serper_api_key": "", "jina_api_key": "", "replace_builtin_web_tools": true},
  "mcp_servers": {},
  "voice": {"base_url": "", "api_key": "", "asr_model": "", "refine_model": "", "refine_effort": "low"},
  "lsp": {"enabled": true, "commands": {}, "python": {"typeCheckingMode": "standard"}},
  "terminal": {"enabled": true, "shell": ""},
  "capture": {"enabled": true, "max_mb": 1024},
  "ui": {"theme": "system"}
}
```

### New agents

What the new-agent page starts with. The pickers there and in the chat change it per agent.

| Key | Default | |
|---|---|---|
| `defaults.harness` | `"claude-code"` | The harness the new-agent page starts on: `claude-code`, `codex`, `antigravity` or `grok`. |
| `harnesses.<harness>.permission_mode` | see [below](#permission-modes) | The mode new agents start in. |
| `harnesses.<harness>.model` | `""` | `""` = the harness's own default, from its files. Claude Code: `opus`, `sonnet`, `haiku` or a model id; Codex: a model from its list; Antigravity: a model id, or a custom model's key or label; Grok Build: a model from `grok models`, a custom model's key among them. |
| `harnesses.<harness>.effort` | `""`; Antigravity `"high"` | `""` = the harness's own. Claude Code: `low`, `medium`, `high`, `xhigh`, `max`; Codex: what the model supports (`low` … `xhigh`); Antigravity: Gemini's thinking level, `minimal` … `high` — with `""` no thinking config is sent, so no thoughts come back for the chat to show or the Tracker to read ([below](#antigravity)); Grok Build: the model's levels, for a model that takes one ([below](#grok-build)). |
| `harnesses.codex.enabled`, `harnesses.antigravity.enabled`, `harnesses.grok.enabled` | `true` | `false` turns the harness off. |
| `harnesses.codex.binary` | `""` | Which Codex CLI runs. `""` (or `"codex"`) = the one bundled with Atrium, else `codex` on `PATH`; a path such as `/opt/homebrew/bin/codex` = that one. Takes effect when Codex's app-server starts again: restart Atrium. |
| `harnesses.grok.binary` | `""` | Which Grok CLI runs. `""` (or `"grok"`) = yours when there is one — `~/.grok/bin/grok`, where xAI's installer puts it, else `grok` on `PATH` — else the one bundled with Atrium; a path = that one. Applies to agents started afterwards. |

### Permission modes

| Harness | `permission_mode` | In the picker | |
|---|---|---|---|
| Claude Code | `default` | Ask permissions | Asks before edits and commands your `permissions` rules don't allow. |
| | `acceptEdits` | Accept edits | Edits files without asking; asks before commands. |
| | `plan` | Plan mode | Reads and plans; changes nothing. |
| | `auto` | Auto (classifier) | A classifier approves or blocks each action. |
| | `bypassPermissions` | Bypass permissions | Never asks. The default. |
| Codex | `auto` | Ask for approval | Works in a sandbox that can write only to the workspace; asks for more. |
| | `auto-review` | Approve for me | The same; Codex's auto-reviewer answers instead of you. |
| | `full-access` | Full access | No sandbox, never asks. The default. |
| Antigravity | `request-review` | Request review | Reads freely; asks before edits, commands and MCP tools. |
| | `accept-edits` | Accept edits | Edits inside the agent's directory without asking; asks before the rest. |
| | `always-proceed` | Always proceed | Never asks. The default. |
| Grok Build | `ask` | Ask | Reads and commands Grok knows to be safe run; asks before edits and the rest. |
| | `auto` | Auto | Grok's classifier approves safe actions and asks before risky ones. |
| | `always-approve` | Always approve | Asks only for a command an `ask` rule covers, or what a hook wants asked; deny rules still apply. The default. |

The Git Agent and the Code Review Agent have modes of their own, whatever these say: they read, and the
Git Agent asks before anything else.

### Sessions and notifications

| Key | Default | |
|---|---|---|
| `sessions.idle_close_minutes` | `30` | An idle agent's process stops after this long. The conversation stays; the next message starts it again, with the configuration of that moment. |
| `notifications.enabled` | `true` | Desktop notifications, except for an agent whose chat is open in a visible, focused tab. |
| `notifications.permission` | `true` | When an agent asks for approval or asks you a question. |
| `notifications.turn_end` | `true` | When an agent's turn ends. |
| `notifications.sound` | `true` | With a sound. |

How notifications reach the desktop (osascript, terminal-notifier, notify-send) is in the
[README](../README.md#where-things-live).

### Tracker, Git Agent, Code Review Agent

The **Tracker Agent** (one per project) keeps the board's status lines, headline and timeline current and
names new agents.

| Key | Default | |
|---|---|---|
| `tracker.enabled` | `true` | `false`: it runs only when you ask it to. |
| `tracker.harness` | `"claude-code"` | Which harness makes its model calls; Claude Code when that one is unavailable. |
| `tracker.model` | `""` | `""` = the harness's default model. A small, fast one is plenty (`haiku`). |
| `tracker.effort` | `"low"` | |
| `tracker.interval_seconds` | `300` | How often it looks for new activity (at least 30). No model call when nothing changed. |
| `tracker.after_turn_seconds` | `45` | How soon after a turn ends it runs. |

The **Git Agent** is the Git panel's chat. These are read when a project's Git Agent is created; after
that it is one conversation, resumed each time.

| Key | Default | |
|---|---|---|
| `git_agent.harness` | `"claude-code"` | `""` = `defaults.harness`. |
| `git_agent.model` | `""` | `""` = `harnesses.<harness>.model`. |

The **Code Review Agent** reviews each agent's work in a read-only session of its own. Its harness, model
and effort are read when an agent gets its reviewer, at the first review; later rounds go to the same one.

| Key | Default | |
|---|---|---|
| `review_agent.auto` | `true` | Review an agent's new changes when it reports its task `done` or `waiting` for you. The drawer's *Auto* box overrides it per agent. |
| `review_agent.harness` | `"claude-code"` | `""` = `defaults.harness`. |
| `review_agent.model`, `review_agent.effort` | `""` | `""` = `harnesses.<harness>.model` / `effort`. |
| `review_agent.language` | `""` | The language the reviewer writes in; `""` = yours. |
| `review_agent.delay_seconds` | `15` | How long after the turn a review starts; skipped when the agent is busy again by then. |
| `review_agent.max_concurrent` | `2` | Automatic reviews running at once. *Review* doesn't wait. |

### Web tools and MCP servers

These reach agents started afterwards (or woken up after `sessions.idle_close_minutes`).

| Key | Default | |
|---|---|---|
| `web.serper_api_key` | `""` | [Serper](https://serper.dev) key for `web.search`. `SERPER_API_KEY` wins. |
| `web.jina_api_key` | `""` | Optional [Jina Reader](https://jina.ai/reader) key: higher rate limits for `web.fetch`. `JINA_API_KEY` wins. |
| `web.replace_builtin_web_tools` | `true` | Agents use Atrium's web tools instead of their own: Claude Code goes without WebFetch, and without WebSearch when there is a Serper key; Antigravity likewise without `read_url_content` and `search_web`. Codex's own web search is set in its `config.toml`; Grok Build keeps its `web_search`. |
| `mcp_servers` | `{}` | Your MCP servers for Claude Code, Antigravity and Grok Build agents: see [MCP servers](#mcp-servers). |

### Voice input

The mic next to send — in an agent's chat, the Git and Code Review drawers, and on the new-agent page — turns
what you say into the message you meant to type, with two model calls to one OpenAI-compatible server:

1. **Speech to text.** The recording (16 kHz mono WAV) goes to `<base_url>/v1/audio/transcriptions`. A model the
   server doesn't serve there (it answers 400, 404, 405, 415 or 422) — a chat model that takes audio, such as
   Doubao Seed — gets the recording through `/v1/chat/completions` instead, as `input_audio`, with
   `reasoning_effort: "minimal"`: a reasoning model would otherwise think for a minute first (a model that refuses
   the parameter is asked again without it). Which route a model takes is found out on its first recording and
   remembered until Atrium restarts.
2. **Refine.** The transcript goes to `<base_url>/v1/responses` together with what the agent sees: its conversation
   since the last compaction, starting with the compaction's summary — at most its latest 60,000 characters or
   so (about 15k tokens): where there is more, the oldest part is left out, the summary first —, thinking left
   out, tool calls and results shortened, its replies since your last message kept longer; its cards on the board, as text, and the other agents the board shows
   (not the archived ones), by name and what each is doing; the files of its directory (`git ls-files`); and the
   draft in the composer. The model writes what you meant rather than what you said word for word: filler and
   false starts dropped, a correction ("no wait, …") in place of what it corrects, "this one" and "the second
   one" named (a file by its path, code by its name, a listed item by its number and gist) where the context makes
   it clear. Where it fits two things, the model is asked to leave it as said, but often picks one: read the
   reference before sending, and switch back to *As heard* if it is the wrong one. Paths, identifiers and
   commands spelled as the project and the conversation have them, misheard words fixed, English made fluent, the
   language you spoke kept. Nothing you didn't say comes in from the context: "ok, go ahead" stays that short. On
   the new-agent page there is no conversation yet: the project's files, the board and the draft are the context.

The text goes into the draft at the caret, to be read and edited before you send it. Until then the strip over
the composer's bar swaps it for what was heard (*As heard* / *Refined*); when refining fails, the transcript goes
in as heard. A recording stops by itself after 5 minutes.

| Key | Default | |
|---|---|---|
| `voice.base_url` | `""` | The server, with or without `/v1`: `https://api.openai.com` and `https://api.openai.com/v1` are the same. |
| `voice.api_key` | `""` | Sent as `Authorization: Bearer …`; not sent when empty. `/api/meta` only says whether it is set; ⚙ Settings shows the file as it is, like the web keys. |
| `voice.asr_model` | `""` | The speech-to-text model, e.g. `gpt-4o-transcribe`, or a chat model that takes audio. `""` = no mic. |
| `voice.refine_model` | `""` | The model that refines the transcript, through the Responses API. `""` = the transcript goes in as heard. |
| `voice.refine_effort` | `"low"` | Its reasoning effort, one the model takes (`minimal`, `low`, `medium`, `high`…); low keeps dictation quick. A model without reasoning that refuses it is asked again without it, and sent none until Atrium restarts; `""` sends none at all. |

Recommended: Doubao Seed 2.1 Lite to hear (it takes the chat route), gpt-6-luna to refine, at `low`:

```json
"voice": {
  "base_url": "https://<your server>",
  "api_key": "…",
  "asr_model": "doubao-seed-2-1-lite-260915",
  "refine_model": "gpt-6-luna",
  "refine_effort": "low"
}
```

On one such server a 13 s recording took 3–5 s to transcribe and 2–3 s to refine. A server that routes models by
a suffix (`<model>:<route>`) wants its own names for them here.

Browsers give the microphone only to secure pages: Atrium opened over HTTPS, or on `localhost`. Opened as
`http://<address>` from a phone, the mic says so instead of recording. The first recording asks for the
microphone; if that was denied, allow it in the browser's settings for the site.

### Editor and UI

| Key | Default | |
|---|---|---|
| `lsp.enabled` | `true` | Language servers behind the editor. |
| `lsp.commands` | `{}` | A server per language instead of the one found automatically: a program (name or path, no arguments) or an argument list, e.g. `{"c": "clangd", "python": ["pyright-langserver", "--stdio"]}`. Languages: `python`, `c`, `cpp`, `go`, `rust`. |
| `lsp.python.typeCheckingMode` | `"standard"` | `off`, `basic`, `standard`, `strict`, `recommended` or `all`. |
| `ui.theme` | `"system"` | Not read: the browser remembers the rail's theme switch. |

`lsp` changes reach language servers started afterwards.

### Terminal

| Key | Default | |
|---|---|---|
| `terminal.enabled` | `true` | The workspace's Terminal tab: shells on the machine Atrium runs on, as you, in the project directory or a worktree. Off: the tab goes, and terminals can't be opened or attached to; the ones already open run until Atrium restarts. |
| `terminal.shell` | `""` | `""` is your shell (`$SHELL`), started as a login shell: it reads your profile, so `PATH` is the one your terminal app has. A shell's name or path, e.g. `"/opt/homebrew/bin/fish"`, starts that one the same way; an argument list, e.g. `["/bin/bash", "--noprofile"]`, runs as given. |

A terminal lives on the server: a reload, a phone's dropped connection or another device attaches to the
same shell, and gets its recent output (the last 1 MiB, with the modes its programs set) first. The page used
last (it attached, typed, resized or was clicked into) gives the terminal its size and answers what programs ask
the terminal, as in tmux. It ends when its tab is closed, when its shell exits (with `exit`; any other ending
keeps the tab, to be read), or when Atrium stops. The shell starts fresh, as in a new terminal window, and your
profile sets it up: from Atrium's own environment it only gets who you are (`HOME`, `USER`…), the locale,
`TMPDIR`, `SSH_AUTH_SOCK`, the proxy variables and `ATRIUM_HOME`, not what Atrium was started from (an agent's
session, tmux, a virtualenv). It runs with `TERM=xterm-256color`, `TERM_PROGRAM=Atrium`, and a UTF-8 `LANG`
when no locale is set.

### Request capture

The workspace's **Requests** tab shows what each agent sends to its model API and what comes back, call by
call. Each call is laid out: its settings, system prompt, tools and messages (with the ones the call before
didn't have marked), and the answer. Beside that are the answer's stream event by event, and both bodies as
they were.

| Key | Default | |
|---|---|---|
| `capture.enabled` | `true` | Send agents' model calls through Atrium's capture proxy, and record them. Reaches agents whose process starts afterwards: new ones, and ones woken up after `sessions.idle_close_minutes` or an Atrium restart. Off, nothing more is recorded, and new agents call their API directly. |
| `capture.max_mb` | `1024` | How much the recorded calls may take, compressed, in `~/.atrium/captures/captures.db`; past it, the oldest go first, down to 90%. Compactions, and the agent's last call before each, are kept apart: they don't count against it and aren't deleted ([below](#request-capture)). `0` = no limit. |

Atrium runs the proxy on a loopback port of its own (`127.0.0.1` only, whatever address Atrium serves on) and
points each agent's base URL at it. The proxy sends every request on, unchanged, to where the harness would
have sent it (through the same `HTTPS_PROXY`), and the answer back as it arrives: a stream stays a stream.

- It trusts what the harness trusts, and more: the system's CAs (the macOS keychain, Windows' store, OpenSSL's
  paths), certifi's, and the ones the harness is given (`NODE_EXTRA_CA_CERTS`, `CODEX_CA_CERTIFICATE`,
  `GROK_EXTRA_CA_BUNDLE`, `SSL_CERT_FILE`, `SSL_CERT_DIR`). It presents Claude Code's client certificate
  (`CLAUDE_CODE_CLIENT_CERT`, `CLAUDE_CODE_CLIENT_KEY`). A setup it can't reproduce (a CA file it can't read) isn't
  captured: that agent calls its API directly.
- Credentials are blanked before anything is written: `Authorization`, `x-api-key`, `x-goog-api-key`, cookies, any
  request header whose name says it holds one (`key`, `token`, `secret`, `auth`…), the headers
  `ANTHROPIC_CUSTOM_HEADERS`, a Codex provider's `http_headers` / `env_http_headers` or a Grok model's
  `extra_headers` / `env_http_headers` add, and a `key=` in the URL. The bodies are kept whole. They hold the
  conversation, including the files the agent read. The database is readable by you alone.
- One thing changes on the way out. An `Accept-Encoding` offering br or zstd offers gzip and deflate
  instead, so the answer can be read back (the Request view shows what the harness offered).
- Deleting an agent deletes its calls; the tab's trash button deletes them too.
- **Compactions are kept.** Every call carries the conversation so far, so most of an old call is in the calls
  after it, until a compaction: from then on the calls carry a summary instead. So a compaction (the harness
  asking the model for that summary, the whole conversation with it; *Compact* in the tab) and the agent's last
  call before it are kept (📌): they don't count against `capture.max_mb`, and pruning leaves them. When the
  compaction rewrote the conversation's start (Claude Code's after a prompt too long: images left out, the oldest
  messages dropped), the agent's last call of the same thread is kept instead.
  Deleting the agent, or its calls, deletes them. Removing the project from Atrium makes them calls like the
  others; so does a session Atrium no longer finds (its transcript deleted elsewhere), a week after its last call.
  Atrium recognizes a compaction by how the instruction each harness adds starts (a message quoting it after other
  words isn't one): Claude Code's "CRITICAL: Respond with TEXT ONLY… Your task is to create a detailed summary of the
  conversation so far" (or its summarizing system prompt), Codex's "You are performing a CONTEXT CHECKPOINT
  COMPACTION" (or a call to `/responses/compact`), Antigravity's "You have been working on the task described
  above… Write a continuation summary", Grok Build's "Your task is to produce a faithful, concise summary of the
  conversation so far… a successor assistant". A harness version that words it differently isn't recognized until
  Atrium learns the new words.
- **Processes an agent starts.** A Claude Code agent's environment has the proxy as `ANTHROPIC_BASE_URL`, so a
  process it starts (a script running `claude -p`) inherits it, and its calls are recorded under that agent. They
  also get `ATRIUM_CAPTURE_URL` (the proxy's address) and `ATRIUM_CAPTURE_UPSTREAM` (the one it stands for, `""` =
  Anthropic's API). Such a process can outlive Atrium: the proxy takes the same port again when it is free, and
  keeps its routes for 90 days, so its calls go on working after a restart. An Atrium started from an agent (a test
  instance) first takes its environment back to `ATRIUM_CAPTURE_UPSTREAM`: its own agents call the API through
  their own proxy, not through two. A Grok Build agent's environment has the campaign below in
  `GROK_CAMPAIGNS_OVERRIDE`, which a `grok` it starts inherits likewise; an Atrium started from it drops Atrium's
  campaign from that variable.

What goes through it:

- **Claude Code**: everything sent to `ANTHROPIC_BASE_URL`, or to Anthropic's API. That covers the agent's
  loop, its subagents, and side calls such as the session's title. Not with Bedrock, Vertex or Foundry
  (`CLAUDE_CODE_USE_*`).
- **Codex**: what a thread sends to its model provider, when that is a provider of your own
  (`[model_providers]`). The built-in OpenAI provider (ChatGPT sign-in, `openai_base_url`), and providers
  with `supports_websockets`, call out directly.
- **Antigravity**: what goes to the Gemini API endpoint, a custom model's relay or Google's. Not on Vertex AI.
- **Grok Build**: what goes to a custom model's endpoint (a `[model.<id>]` with a `base_url`, or `api_base_url`),
  its title and summary calls included. Its `config.toml` is left alone: the agent's process gets a campaign of
  Atrium's (`atrium-capture`) in `GROK_CAMPAIGNS_OVERRIDE`, Grok's config patches that apply over its config
  files and may set a model's `base_url` (`GROK_CONFIG`, its overlay, may not). Campaigns already in that variable
  still apply; without it, Grok's own (xAI's, `[[campaigns]]` in its files) don't in Atrium's agents. xAI's
  endpoints (`grok login`, `XAI_API_KEY`), whose address depends on the sign-in, are called directly, and so is a
  model with `mtls_cert_dir`, or one whose `base_url` `requirements.toml` pins. A model added to `config.toml` is
  captured in agents started afterwards.

Anything else the harnesses send isn't recorded: telemetry, update checks, MCP servers over HTTP, the
plugins Codex fetches, web pages.

## Claude Code

Atrium runs Claude Code through `claude-agent-sdk`, which bundles the CLI: nothing to install.

Atrium's Claude Code agents load all of Claude Code's settings files, as the `claude` CLI does (the more
specific file wins), plus managed settings if your organization has them:

| File | Applies to |
|---|---|
| `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR/settings.json`) | you, in every project |
| `<project>/.claude/settings.json` | everyone in the project (commit it) |
| `<project>/.claude/settings.local.json` | you, in this project (keep it out of git) |

**Sign-in.** The bundled CLI uses your Claude Code login. Not signed in yet? From the Atrium checkout:

```bash
CLAUDE=$(.venv/bin/python -c 'import claude_agent_sdk, os; print(os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude"))')
$CLAUDE auth login
$CLAUDE auth status
```

Or an API key: `ANTHROPIC_API_KEY` in Atrium's environment, or under `env` in `~/.claude/settings.json`.

**A relay or gateway** goes under `env` in `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://relay.example.com",
    "ANTHROPIC_AUTH_TOKEN": "...",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "relay-opus"
  },
  "model": "opus"
}
```

`ANTHROPIC_AUTH_TOKEN` is sent as `Authorization: Bearer …`, `ANTHROPIC_API_KEY` as `x-api-key`.
`ANTHROPIC_DEFAULT_OPUS_MODEL` (`…_SONNET_…`, `…_HAIKU_…`) maps an alias to the relay's model name. The
model picker's *Default* shows what `ANTHROPIC_MODEL`, else `model`, resolves to.

**What Atrium sets for each agent**, over your settings:

- the **permission mode**, always: `permissions.defaultMode` isn't used. Your `allow` / `ask` / `deny`
  rules still are.
- the **model** and **effort** when the agent has them picked. *Default* is your settings': the model
  above, and `env.CLAUDE_CODE_EFFORT_LEVEL`, else `effortLevel`. A picked effort wins over both.
- **additions**: the board protocol, appended to Claude Code's system prompt; Atrium's tools
  (`mcp__board__*`, `mcp__web__*`); the servers in Atrium's `mcp_servers`; read access to
  `~/.atrium/uploads` (attachments).
- **removals**, when `web.replace_builtin_web_tools` is on: WebFetch, and WebSearch when there is a
  Serper key.
- the **API's address**, while `capture.enabled` is on: `ANTHROPIC_BASE_URL` points at Atrium's capture
  proxy, which sends each call on to the address your settings give ([Request capture](#request-capture)).
  `NO_PROXY` gets the loopback addresses added, so that the CLI reaches the proxy directly, and
  `ATRIUM_CAPTURE_URL` / `ATRIUM_CAPTURE_UPSTREAM` say what was replaced. When the calls go to Anthropic's own API,
  also `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1`: the CLI takes any other base URL for a gateway, and leaves out
  what only Anthropic's API gets (billing fields such as `cch=`, a `diagnostics` field).

Everything else — the environment, hooks, plugins, skills, Claude Code's own MCP servers — works as in
the CLI. The Tracker's model calls on Claude Code read only `~/.claude/settings.json` (a relay there
applies) and use no tools.

Changes apply to agents started afterwards. Reference: [Claude Code settings](https://code.claude.com/docs/en/settings).

## Codex

Atrium runs the Codex CLI bundled with it (`openai-codex-cli-bin`, pinned in `pyproject.toml`) as
`codex app-server`: one process for every Codex agent.

| File | Applies to |
|---|---|
| `~/.codex/config.toml` (`$CODEX_HOME/config.toml`) | you, in every project |
| `<project>/.codex/config.toml` | the project, once it is trusted |

Codex reads a project's file only once you trust the project. Running `codex` there asks, and records the
answer in your `config.toml`; or add it yourself:

```toml
[projects."/Users/me/code/app"]
trust_level = "trusted"
```

A project's file can't change providers or credentials (`model_provider`, `model_providers`,
`openai_base_url`, profiles…).

**Sign-in**, with the bundled CLI, from the Atrium checkout:

```bash
CODEX=$(.venv/bin/python -c 'import codex_cli_bin as c; print(c.bundled_codex_path())')
$CODEX login                                          # ChatGPT account, in the browser (--device-auth: with a code)
printenv OPENAI_API_KEY | $CODEX login --with-api-key
$CODEX login status
```

**A relay** that speaks OpenAI's Responses API: point the built-in provider at it,

```toml
openai_base_url = "https://relay.example.com/v1"
```

or define a provider of its own:

```toml
model = "relay-model"
model_provider = "relay"

[model_providers.relay]
name = "My relay"
base_url = "https://relay.example.com/v1"
env_key = "RELAY_API_KEY"
wire_api = "responses"
```

`env_key` names the environment variable that holds the key. Codex reads it from its own environment,
which is Atrium's: export it before you start Atrium.

**What Atrium sets for each agent:**

- the **permission mode**, as Codex's approval policy and sandbox: `auto` = `on-request` +
  `workspace-write`; `auto-review` = the same, with Codex's auto-reviewer answering; `full-access` =
  `never` + `danger-full-access`. `approval_policy` and `sandbox_mode` in `config.toml` aren't used.
- the **model** and **effort** when the agent has them picked. *Default* is `model` /
  `model_reasoning_effort` in `config.toml`.
- **additions**: Atrium's instructions (as developer instructions), and its tools as Codex dynamic
  tools: `board.*`, and `internet.search` / `internet.fetch` (Codex reserves `web`).
- Atrium's `mcp_servers` are **not** passed to Codex: its MCP servers come from `[mcp_servers]` in
  `config.toml`.
- the **provider's address**, while `capture.enabled` is on: the thread's `model_providers.<id>.base_url`
  points at Atrium's capture proxy, which sends each call on to yours ([Request capture](#request-capture)).
  The app-server runs with the loopback addresses added to `NO_PROXY`.

Changes apply to agents started afterwards.

**Your own Codex CLI**: `"harnesses": {"codex": {"binary": "/opt/homebrew/bin/codex"}}`, then restart
Atrium. Atrium speaks the app-server protocol of the pinned version, which changes often: stay close to
it. `/api/meta` shows which CLI runs (`cli.path`, `cli.version`, `cli.bundled`).

Reference: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[providers and project files](https://learn.chatgpt.com/docs/config-file/config-advanced).

## Antigravity

Atrium runs Google Antigravity through its SDK, `google-antigravity`, whose platform wheels bundle the
agent runtime: nothing to install. Where its model calls go — the model, a relay, the key — comes from
the Antigravity CLI's own settings file, `~/.gemini/antigravity-cli/settings.json` (⚙ Settings →
Antigravity), which Atrium reads.

**A Gemini API key** — from [Google AI Studio](https://aistudio.google.com/app/api-keys), or a relay's —
goes under `env`:

```json
{
  "modelProvider": "gemini",
  "model": "gemini-3.8-flash",
  "env": {"GEMINI_API_KEY": "..."}
}
```

For Google's own API, `GEMINI_API_KEY` in Atrium's environment works too. The CLI itself reads the key
only from the environment; the `env` block is for Atrium, which takes only the key, endpoint and Vertex AI
variables described here from it. Anything else, such as a proxy, has to be in Atrium's environment.
Antigravity's terms keep the Google account sign-in to Google's own apps, so Atrium doesn't use it.

**A relay** that speaks the Gemini API is a custom model, as the CLI writes it:

```json
{
  "modelProvider": "gemini",
  "customModelsConfig": {"customModels": {"my-model": {
    "modelName": "gemini-api://relay.example.com/gemini/models/<model>", "label": "My model"}}},
  "model": "My model",
  "env": {"GEMINI_API_KEY": "..."}
}
```

Requests go to `<base_url>/v1beta/models/<model>:streamGenerateContent`, with the key as
`x-goog-api-key`. After `gemini-api://`, a host without a scheme means `https://`; a local one is
`gemini-api://http://127.0.0.1:<port>/models/<model>`. Without the `gemini-api://` prefix, `modelName`
counts as a model id on Google's API, which then gets this file's key. A custom model can also have:

- `maxTokens`: its context window in tokens, for the context gauge (default 1,048,576).
- `modelFeatures.supportsThinking: false`: no thinking level is sent; the effort picker does nothing.
- `modelFeatures.supportsImages: false`: attached images go by path only, not as images.

**Which model**: the agent's pick (`harnesses.antigravity.model` for new agents), else the file's
`model`, else `gemini-3.8-flash`. Each can name a custom model (key or label), a model id, or one of the
CLI's labels (`"Gemini 3.8 Flash (High)"`); the level in parentheses isn't used: set the effort with
Atrium's picker or `harnesses.antigravity.effort`. The model picker lists *Default* (the file's `model`)
and the custom models.

**Which key goes where:**

- A relay set in this file (a custom model, or `GOOGLE_GEMINI_BASE_URL` under `env`) gets only this file's
  `env.GEMINI_API_KEY`, never a `GEMINI_API_KEY` from Atrium's environment. Without one, Atrium says so
  instead of starting. A relay that takes no key needs a placeholder: the runtime won't start without a key.
- Google's Gemini API (a model id such as `gemini-3.8-flash`), or a `GOOGLE_GEMINI_BASE_URL` set in
  Atrium's environment, gets this file's `env.GEMINI_API_KEY`, else the environment's. So with a relay's
  key in this file, picking one of Google's models sends that key to Google.

**Gemini Enterprise (Vertex AI)**: `"GOOGLE_GENAI_USE_VERTEXAI": "true"` under `env`, then either a
project — `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` (default `global`) under `env`, or
`"gcp": {"project": …, "location": …}` at the top level — with Application Default Credentials
(`gcloud auth application-default login`), or `GOOGLE_API_KEY` alone, for express mode.
`GOOGLE_VERTEX_BASE_URL` changes the endpoint. These variables can also come from Atrium's environment.
Custom models still go to their own endpoint.

**What Atrium sets for each agent:**

- the **permission mode**, through a gate of Atrium's: the CLI's `permissions`, `toolPermission` and
  `agentMode` aren't read.
- the **model** and **thinking level**: the agent's effort, sent as Gemini's `thinkingLevel` (with
  `includeThoughts`), from the next turn when it changes. With no effort (`""`, picked nowhere) no thinking
  config is sent: the model thinks at its own default level and, without `includeThoughts`, the Gemini API
  returns no thoughts — none for the chat to show, none for the Tracker to read, and Gemini seldom writes
  anything else between tool calls. Hence the `high` default of `harnesses.antigravity.effort`.
- **additions**: the instruction files Antigravity's own apps load ([below](#instructions-for-agents)),
  since the SDK's runtime doesn't; Atrium's tools, named `<namespace>_<tool>` (`board_status`,
  `web_search`…); the servers in Atrium's `mcp_servers` — Antigravity's `mcp_config.json` files aren't read.
- **removals**, when `web.replace_builtin_web_tools` is on: `read_url_content`, and `search_web` when
  there is a Serper key.
- the **endpoint's address**, while `capture.enabled` is on: the Gemini endpoint's base URL points at
  Atrium's capture proxy, which sends each call on to yours ([Request capture](#request-capture)).

Changes apply to agents started afterwards. Whether Antigravity is available, and its model list, update
right away, in an open page after a save in ⚙ Settings or a reload. Its conversations live in
`~/.atrium/antigravity/`, and no CLI lists them. A fork copies one there under a new id (the runtime's
state database and the transcript, cut at the fork point); changing a message cuts it in place.

## Grok Build

Atrium runs xAI's Grok Build through its CLI's agent mode, `grok agent stdio` (the
[Agent Client Protocol](https://agentclientprotocol.com) on stdio), one process per live agent. The CLI
comes with `make install`: xAI's `grok-cli-bin` wheel bundles it, pinned in `pyproject.toml` (0.2.111). When
you have your own — `~/.grok/bin/grok`, where xAI's installer (`curl -fsSL https://x.ai/cli/install.sh | bash`)
puts it, or `grok` on `PATH` — Atrium runs that one instead: it is newer, and the one your terminal writes
sessions with. `harnesses.grok.binary` names another; `/api/meta` says which runs (`cli`). Everything else is
Grok's own `~/.grok/config.toml` (`$GROK_HOME/config.toml`; ⚙ Settings → Grok Build), plus a project's
`.grok/config.toml` for its MCP servers, plugins and permission rules.

**Signing in** is the CLI's: `grok login` (your Grok subscription), or an API key from
[console.x.ai](https://console.x.ai) as `XAI_API_KEY` in Atrium's environment. A model with a key of its own
needs neither.

**A relay**, or any endpoint that speaks OpenAI's Chat Completions or Responses API or Anthropic's
Messages API, is a custom model:

```toml
[models]
default = "my-grok"

[model.my-grok]
model = "grok-4.7"                    # the id the endpoint takes
base_url = "https://relay.example.com/v1"
name = "Grok 4.7 (relay)"
api_key = "..."                       # or env_key = "RELAY_API_KEY": a variable in Atrium's environment
api_backend = "chat_completions"      # or "responses", "messages"
supports_reasoning_effort = true      # the endpoint takes reasoning_effort: Atrium's effort picker works
reasoning_effort = "high"             # what it gets when none is picked (else none is sent)
context_window = 500000               # for the context gauge and auto-compaction (default 200000)
```

An endpoint that answers `Invalid schema for function 'use_tool': schema must have type 'object' and not have
'oneOf'…` (Azure's Responses API does) takes the same model through `api_backend = "chat_completions"`.

**Which model**: the agent's pick (`harnesses.grok.model` for new agents), else `[models] default`, else
Grok's. The model picker lists *Default* and what `grok models` lists.

**Effort** is the model's own, and Atrium's picker offers the levels the model has. xAI's models list theirs and
a default (Grok 4.5: `low`, `medium`, `high`; `high`). A custom model has none unless its block says
`supports_reasoning_effort = true`: without it Grok sends no `reasoning_effort` (the endpoint decides), and the
picker offers none. With it, the levels are Grok's (`minimal` … `xhigh`, or what
`reasoning_efforts = [{value = "low"}, …]` lists), and *Default* is the block's `reasoning_effort`, else
`[models] default_reasoning_effort` (for the default model), else nothing is sent. Which levels an endpoint
takes is up to it. `harnesses.grok.effort` (`""`: the model's default) and the picker set an agent's, applied to a
running agent at once.

**What Atrium sets for each agent:**

- the **permission mode** (`ask`, `auto`, `always-approve`), for the agent alone and changed live: `[ui]
  permission_mode` in `config.toml` isn't used, its `[permission]` rules are: in `always-approve`, a command
  an `ask` rule covers (and what a hook wants asked) is still yours to approve, once (*Always allow* has
  Grok remember it for the project). Turning an action down ends Grok's turn;
  what you write when you turn it down reaches the agent as your next message, in the same turn. *Deny &
  stop* ends the turn there, and what you write reaches the agent with your next message.
- **its own section of the system prompt**, when the agent starts: Grok keeps a session's prompt, so a
  change reaches agents started afterwards, not resumed ones.
- **Atrium's tools**, as in-process MCP servers named after their namespace (`board`, `web`, `graph`,
  `review`) that the agent reaches over its connection to Atrium. Grok defers MCP tools: the model finds
  them with `search_tool` and calls them through `use_tool` (`board__status`…). They run without asking.
- the servers in Atrium's `mcp_servers`, beside the `[mcp_servers]` of Grok's own files.
- no update checks (`GROK_DISABLE_AUTOUPDATER`).
- with `capture.enabled`, its custom models' `base_url` set to Atrium's capture proxy, through
  `GROK_CAMPAIGNS_OVERRIDE` ([Request capture](#request-capture)).

Its sessions are the CLI's own, in `~/.grok/sessions/<directory>/<id>/`: `grok --resume` lists the agents
Atrium started, and Atrium lists the sessions you start in a terminal. Deleting an agent deletes its session.
An agent whose worktree is gone carries on where you pick: Atrium files its session under that directory,
and tells the agent with your next message (Grok keeps the system prompt it started with).

Grok also carries on by itself: when a command it moved to the background ends, a monitor reports, a
subagent or a `/loop` task finishes. The chat shows that as a turn like any other — the agent works, then
answers — and a message you send meanwhile waits for it. Grok's reminders to itself that start such a turn
aren't shown.
The composer offers the CLI's commands (`/compact`, `/session-info`, `/deep-research`, `/goal`…). By default
Grok also reads Claude Code's and Cursor's rules and skills, and runs Claude Code's hooks; `[compat.claude]`
and `[compat.cursor]` in `config.toml` turn that off. Not yet: forking an agent, editing a message already
sent, its subagents in the rail. The bundled 0.2.111 takes no images (an attached one
reaches it by its path).

## MCP servers

Every agent gets Atrium's own tools — the board (`board.status`, `board.publish`, `board.note`,
`board.read`, `board.todo_add`, `board.todo_done`) and the web (`web.search`, `web.fetch`) — with nothing to set up but the
[web keys](#web-tools-and-mcp-servers).

**Claude Code, Antigravity and Grok Build** take your servers from `mcp_servers` in Atrium's settings, in
Claude Code's format:

```json
"mcp_servers": {
  "notes": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/notes"]},
  "docs": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
}
```

- Claude Code agents also get the servers set up for Claude Code itself (`claude mcp add`, a project's
  `.mcp.json`).
- Antigravity takes `command` servers (stdio) and `url` servers (streamable HTTP), with names of
  letters, digits, `_` and `-`.
- Grok Build takes `command` servers and `url` servers (`"type": "sse"` for SSE, else streamable HTTP),
  besides the `[mcp_servers]` of its own `config.toml` (`grok mcp add`).

**Codex** takes them from `[mcp_servers]` in `~/.codex/config.toml`; `/mcp` in a Codex agent's chat lists
them:

```toml
[mcp_servers.notes]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/notes"]

[mcp_servers.docs]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "DOCS_TOKEN"
```

**Atrium's web tools elsewhere**: `atrium-web-mcp` serves them over stdio, with the keys from Atrium's
settings or environment, e.g. `claude mcp add atrium-web -- /path/to/atrium/.venv/bin/atrium-web-mcp`.

## Instructions for agents

Each harness reads its own instruction files. Atrium adds a section of its own to every agent's system
prompt (the board, the agent's worktree); there is nothing to set up for it.

| Harness | Reads |
|---|---|
| Claude Code | `CLAUDE.md`: `~/.claude/CLAUDE.md`, and the project's and its parent directories' (`CLAUDE.md`, `CLAUDE.local.md`, `.claude/CLAUDE.md`), `.claude/rules/`. Where none of those three is in the agent's directory or above it, its `AGENTS.md` files instead. |
| Codex | `AGENTS.md`: `~/.codex/AGENTS.md`, then each directory's from the repository root down to the agent's. |
| Grok Build | Its own: `AGENTS.md`, `AGENT.md`, `CLAUDE.md` and `CLAUDE.local.md` in each directory from the repository root down to the agent's, `.grok/rules/*.md` and `~/.grok/rules/`; and, unless `[compat]` in its `config.toml` turns them off, Claude Code's (`~/.claude/`, `.claude/rules/`) and Cursor's. |
| Antigravity | Atrium adds them, since the SDK's runtime doesn't load them: `~/.gemini/AGENTS.md` and `~/.gemini/GEMINI.md`, then for each directory from the repository root down to the agent's, `AGENTS.md`, `GEMINI.md`, `.agents/AGENTS.md`, `.agents/GEMINI.md` and the rules in `.agents/rules/*.md` (`trigger: always_on` in full, `trigger: model_decision` with a `description` as a pointer the agent follows when it applies). Up to 24 KB per file. |

One `AGENTS.md` serves all four. A project that also has a `CLAUDE.md` can pull it in there with a line
`@AGENTS.md`, as this repository does. Or have Claude Code read both, in `~/.claude/settings.json`:

```json
{"pluginConfigs": {"cc-plugin-agents-md@builtin": {"options": {"instructionFiles": "claude-md-and-agents-md"}}}}
```

([Claude Code's AGENTS.md support](https://code.claude.com/docs/en/memory#agents-md); a project's own
settings can't set this.)

## Files in a project

| File | |
|---|---|
| `.worktreeinclude` | Gitignored files to copy into each new worktree (gitignore syntax), e.g. `.env`. The file Claude Code and the Codex app read too. |
| `.claude/settings.json`, `.claude/settings.local.json` | Claude Code's project settings ([above](#claude-code)). |
| `.codex/config.toml` | Codex's project settings, once the project is trusted ([above](#codex)). |
| `.grok/config.toml` | Grok Build's project settings: MCP servers, plugins, permission rules ([above](#grok-build)). |
| `.mcp.json` | Claude Code's project MCP servers. |
| `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.agents/rules/` | [Instructions](#instructions-for-agents). |
| `.atrium/` | Atrium's state for the project: the board, agents, tracker, reviews, worktrees, trash. Not settings: Atrium manages it, and its `.gitignore` (`*`) keeps it out of git. |

## Environment variables

Every agent's process inherits Atrium's environment: start Atrium from a shell that has the keys and
proxy the agents need. Claude Code's settings can add variables of their own under `env`; Antigravity's
`env` only takes its key, endpoint and Vertex AI variables ([Antigravity](#antigravity)).

| Variable | |
|---|---|
| `ATRIUM_HOME` | Where Atrium keeps its settings and state (default `~/.atrium`). |
| `ATRIUM_HOST`, `ATRIUM_PORT` | Defaults for `--host` (`127.0.0.1`) and `--port` (`8787`); `make run PORT=8790` sets the port. Every page, API call and WebSocket needs a login ([README → Login](../README.md#login)); across a network, put HTTPS in front. |
| `ATRIUM_URL` | The address notification links open (default: the one Atrium serves on). |
| `SERPER_API_KEY`, `JINA_API_KEY` | The web tools' keys; they win over `settings.json`. |
| `CLAUDE_CONFIG_DIR` | Claude Code's directory (default `~/.claude`): its settings and transcripts. |
| `CODEX_HOME` | Codex's (default `~/.codex`): `config.toml`, the login, threads. |
| `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL`, Vertex AI's variables | Antigravity's, where its settings file's `env` doesn't set them ([Antigravity](#antigravity)). |
| `ANTIGRAVITY_HARNESS_PATH` | Another Antigravity runtime binary instead of the one in the SDK's wheel. |
| `GROK_HOME` | Grok Build's directory (default `~/.grok`): `config.toml`, the login, sessions, its installed CLI (`bin/`). |
| `XAI_API_KEY` | Grok Build's key for xAI's API, when it isn't signed in with `grok login` ([Grok Build](#grok-build)). |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | Passed on to every agent. With `capture.enabled`, Claude Code, Codex and Grok Build get the loopback addresses added to `NO_PROXY`, and the capture proxy sends their calls out through the proxy they would have used. |
| `ATRIUM_CAPTURE_URL`, `ATRIUM_CAPTURE_UPSTREAM` | Set by Atrium for a captured Claude Code agent: the proxy's address and the one it replaced. An Atrium started with them sends its agents to `ATRIUM_CAPTURE_UPSTREAM` ([Request capture](#request-capture)). |
| `GROK_CAMPAIGNS_OVERRIDE` | Grok Build's config patches (a JSON list). Atrium adds a campaign of its own (`atrium-capture`) for a captured Grok Build agent, and keeps the ones you set; an Atrium started with it drops that campaign ([Request capture](#request-capture)). |

## Checking your setup

- `curl -s --noproxy '*' -H "Authorization: Bearer $(.venv/bin/atrium --token)" http://127.0.0.1:8787/api/meta` lists each harness: `available`, and
  `unavailable_reason` when it isn't; `config_model` / `config_effort`, what *Default* resolves to; for
  Codex and Grok Build, `cli` (`path`, `version`, `bundled`). `settings._error` says why `settings.json` couldn't be read.
  `capture` says whether model calls are recorded (`enabled`), whether the capture proxy started (`running`), and how
  many megabytes the recorded calls take (`mb`), of them kept apart from `capture.max_mb` (`kept_mb`).
- ⚙ Settings keeps the last 20 versions of each file in `~/.atrium/backups/` (`<file>-<date>-<time>`).
  To go back, copy one over the file.

| Symptom | Why |
|---|---|
| A default from the code or this guide doesn't apply | Your `settings.json` still holds the value of its first start: change or delete the key. |
| `model` or effort in a harness's file is ignored | The agent has one picked, or `harnesses.<harness>.model` / `effort` sets one; `""` there leaves it to the harness's file. In Claude Code's settings, `env.CLAUDE_CODE_EFFORT_LEVEL` wins over `effortLevel`. |
| The harness's own default permission mode is ignored | Atrium always sets the mode: the agent's picker, or `harnesses.<harness>.permission_mode`. |
| An MCP server is missing in Codex | Atrium's `mcp_servers` are for Claude Code, Antigravity and Grok Build; Codex reads `[mcp_servers]` in `config.toml`. |
| A Grok Build agent fails with `Invalid schema for function 'use_tool'` | Its endpoint's Responses API rejects the schema of Grok's `use_tool`: `api_backend = "chat_completions"` for that model ([Grok Build](#grok-build)). |
| Antigravity is unavailable: "Needs the API key of …" | A relay set in its settings file only gets the key in that file's `env`. |
| A running agent doesn't see a change | Harness settings apply when an agent's process starts: new agents, and agents woken up after `sessions.idle_close_minutes`. |
| An agent's model calls fail with a 502 from "Atrium's capture proxy" | The proxy couldn't reach the API; the message says why (a certificate it doesn't trust, the proxy on the way). `"capture": {"enabled": false}` sends agents started afterwards straight to their API. |
| An agent's calls aren't in Requests | Its process started before `capture.enabled` was on, or its harness's endpoint isn't one that goes through the proxy ([Request capture](#request-capture)). |
| No mic next to send | `voice.base_url` or `voice.asr_model` is empty, or the page was open before they were set: save them in ⚙ Settings, or reload the page. |
| The mic says it needs a secure page | Browsers give the microphone only to HTTPS pages and `localhost`. |
