# Atrium

A project-centric workbench for coding agents — Claude Code, Codex and Antigravity side by side, harness-agnostic by design.

> *Atrium* — the central hall of a house where everyone meets. Here it is the shared space of one
> project: every agent session, the human, the files, the diffs and the git history, around one board.

```
┌ Workspace ─────────────────────────────────┬ Chat (selected agent) ─────┬ Rail ┐
│ Board · Files · Changes · Git              │  user ▸ task               │  AD  │ project
│                                            │  ◆ tool calls, diffs       │  +   │ new agent
│  board.html (live, shared)                 │  ◆ board.status / publish  │  ●○  │ agents (hover
│  Monaco editor + language server           │  ⚠ permission card         │  ●○  │  for details)
│  diff review · commit graph + Git Agent    │                            │  ≡   │ all agents
│                                            │  📎 mode model · effort ↑  │  ⚙   │ tracker, git,
└────────────────────────────────────────────┴────────────────────────────┴──────┘ settings
```

## Design principles

1. **Directory = project.** Pick a directory; you see every session that ever ran there — from every
   harness, including its git worktrees and sessions started from the `claude` / `codex` CLIs — and
   you can start new ones with either harness.
2. **One fixed HTML board per project** — `<project>/.atrium/board.html`. It is self-contained
   (open it straight from disk), embedded live in the UI, and written by two kinds of agents:
   * **Session agents** get a fixed system-prompt section plus the `board` tools
     (`status` / `publish` / `note` / `read`). Instead of ending
     with a long text answer they publish an **HTML result card** and keep a status line
     (`working 60% — writing tests for token refresh`).
   * **The Tracker Agent** (one per project, in the background) is proactive: on an interval,
     shortly after any turn ends, or on demand, it diffs *every* session transcript against its
     previous run (byte offsets), compresses the new activity, and updates status / progress /
     "doing" / summary for each agent, a project headline + overview, and the timeline. No model
     call when nothing changed. It also names unnamed sessions as **"<Topic> Agent"**.
3. **You stay in control of the craft.**
   * **Files** — tree + Monaco editor for every root (project + worktrees); agent edits to open
     files show up live, conflicting saves are detected. Language servers run behind the editor
     (Python: basedpyright, installed with Atrium; clangd / gopls / rust-analyzer when on `PATH`):
     semantic highlighting in VS Code's Light+ / Dark+ colours, hover types, completion,
     diagnostics, ⌘-click / F12 go to definition (library files open read-only), ⇧F12 references.
   * **Changes** — per-agent diffs. The baseline is a **git snapshot** Atrium takes at the start and
     end of every turn (a tree object written through a temporary index — no refs, your index and
     stash are untouched). Files changed by shell commands are detected by diffing those snapshots.
     For sessions without snapshots (CLI sessions, older history) the baseline comes from the
     transcript (Claude's `originalFile`, Codex patches applied in reverse), then `HEAD`. Each file
     shows where its baseline came from. Edit in the diff, mark reviewed, revert a file to its
     pre-agent state (refused when the baseline is unknown). Plus a classic *working tree* view: stage / unstage /
     discard / commit.
     Changed files are listed as a **directory tree** (single-child folders compacted, folder-wide
     "mark reviewed" / stage / unstage / discard); diffs are **side by side** (or inline) with
     before / after headers, previous / next change (⌥F5) and file (↑ / ↓ in the tree), and toggles
     for collapsing unchanged lines, ignoring whitespace and wrapping — remembered across reloads.
     The review can go full screen.
     Under the diffs, the **Code Review Agent** (a chat drawer) reviews each agent's work in a
     read-only session of its own, in the agent's directory: automatically after a turn in which the
     agent reports its task `done` or `waiting` for you (`board.status`), or with *Review*. Each
     round gets the task, the agent's last reply and the diff since the previous round (between turn
     snapshots), and records findings — severity, `file:line`, problem, fix — as cards: adopt or
     dismiss them, jump to the line in the diff, discuss them in the chat. Nothing reaches the agent
     until you press *Send* on a message (put together from the adopted findings, or drafted by the
     reviewer); a busy agent gets it when its turn ends. The next round checks those first.
   * **Git** — commit graph with lanes (pick a commit: its files as a tree, each one diffed side by
     side against the first parent, renames followed); *Smart* / *All* / *Custom* branch sets, and an
     **Agent view**: the **Git Agent** (a chat drawer) investigates with read-only git and drives
     the graph through a `graph.set_view` tool — "simplify the graph to what matters". Worktrees:
     create / remove, start an agent directly in a new worktree. A repository created later
     (`git init` in a terminal, or the button in the Git tab) is picked up live.
4. **MCP.** `web.search` (Serper) and `web.fetch` (Jina Reader) are mounted into every agent —
   Claude sees them as `mcp__web__search` / `mcp__web__fetch` (optionally replacing the built-in
   WebSearch/WebFetch); Codex reserves the `web` namespace, so there they are `internet.search` /
   `internet.fetch`; Antigravity names Atrium's tools `<namespace>_<tool>` (`web_search`,
   `board_status`…). Extra MCP servers for Claude and Antigravity sessions go under `mcp_servers` in
   Atrium's settings; Codex uses the `[mcp_servers]` of its own `config.toml`. The web tools are also
   available as a stdio MCP server: `atrium-web-mcp`.
5. **Several harnesses, one workbench.** Claude Code runs through `claude-agent-sdk`; Codex through
   `codex app-server` (JSON-RPC over stdio, one process shared by all Codex sessions); Google
   Antigravity through its SDK, `google-antigravity`, whose platform wheels bundle the agent runtime
   (one runtime process per live session). Sessions don't convert between harnesses — each continues
   in its own — but they collaborate through the board, the files and git: a Codex agent sees what a
   Claude agent published, and vice versa.
6. **The composer.** Paste, drop or pick images and files (stored in `~/.atrium/uploads`): images go
   to the model as images (base64 for Claude, `localImage` for Codex, inline media for Antigravity),
   other files are passed by path. Next to send there is a **reasoning-effort** picker (`Auto` = whatever the harness config
   says; Claude low…max, applied mid-session; Codex per turn, from the model's supported levels;
   Antigravity: Gemini's thinking level, minimal…high, applied from the next turn).
   Sending while a Codex turn runs steers that turn (Claude and Antigravity queue it). The new-agent page keeps a **draft** per project
   (name, task, attachments, workspace / harness / mode / model / effort) in the browser's
   localStorage: open another agent, hide the chat or reload, and it's still there until the agent
   starts; a dot on the rail's new-agent button says one is waiting. Every agent's reply box (the Git
   Agent's too) keeps its own draft the same way, text and attachments, until it's sent; while you're
   elsewhere, its hover card and the agent list show a ✎.
7. **Fork a conversation** to try several ideas in parallel — from the latest point (⑂ in the chat
   header) or from before any of your messages (hover it; the message is offered again, to edit).
   The fork is a new agent that remembers the conversation up to there and works in a **new
   worktree** (a branch from the original's commit, with the files of that moment — taken from the
   turn snapshots, uncommitted changes included), an existing worktree, or the same directory.
   This is also how a conversation changes worktree while it has one: an agent stays bound to its
   directory (its process, its transcript, the paths in its history, the uncommitted edits it made
   there), so it is copied rather than moved — tick *Archive the original* to move it. Forks are
   native to Claude Code (`fork_session`, the transcript moved under the new directory) and Codex
   (`thread/fork`); the fork's Changes and the Tracker only count what happened after the fork.
   Antigravity sessions can't be forked: its runtime keeps a conversation's state under its id.
8. **Agents outlive their worktrees.** Remove a worktree — from the Git panel, or the agent does it
   after merging its branch — and its agents stay in the list with the worktree struck through
   (⎇ ~~feat/x~~). Their process stops, and their Changes show the files as they left them, read-only.
   The next message to one first asks where it continues — the project directory (on its current
   branch), another worktree or a new one (a branch that still exists is checked out again), the
   new-agent page's menu — then moves the same conversation there and tells the agent (Claude: the
   transcript moves under the new directory; Codex: the thread resumes there). A worktree can't be
   removed while an agent is mid-turn in it. Archiving the last agent of a worktree whose work is
   committed and merged (into the project's HEAD or the remote's default branch) removes the
   worktree and its branch, after a snapshot; otherwise the toast says why they were kept. New
   worktrees get the gitignored files listed in `.worktreeinclude` (gitignore syntax — the file
   Claude Code and the Codex app read too), e.g. `.env`; an agent in a worktree is told to set up
   the rest (dependencies, builds) itself.

## Quick start

Requirements: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), Node ≥ 18 (only to build the UI), and
a working Claude Code login (the Agent SDK bundles the CLI and uses your existing auth/env).
The [Codex CLI](https://developers.openai.com/codex) is bundled too (OpenAI's `openai-codex-cli-bin`
wheel, pinned in `pyproject.toml`) and uses your Codex login in `~/.codex`. To run your own Codex
instead, set `harnesses.codex.binary` to its path, e.g. `/opt/homebrew/bin/codex` (`""` and `"codex"`,
the default before it was bundled, both mean the bundled one); `/api/meta` shows which one runs
(`cli`). No Codex login yet? After `make install`:
`$(.venv/bin/python -c 'import codex_cli_bin as c; print(c.bundled_codex_path())') login`.
Antigravity comes with `make install` too (the `google-antigravity` SDK bundles its runtime). It needs a
Gemini API key — from [Google AI Studio](https://aistudio.google.com/app/api-keys), or a relay's — in the
Antigravity CLI's own settings file, `~/.gemini/antigravity-cli/settings.json`:
`{"env": {"GEMINI_API_KEY": "..."}}`. Relays, Vertex AI and which key goes where are in
[Antigravity](docs/configuration.md#antigravity). Antigravity's terms keep the Google account sign-in to
Google's own apps, so Atrium doesn't use it.

How to configure Atrium and each harness — which file sets what, sign-in and relays, the defaults new
agents get, MCP servers, instructions: [docs/configuration.md](docs/configuration.md)
([中文](docs/configuration.zh-CN.md)).

```bash
make install          # python venv + npm deps
make build            # build the UI into atrium/static
export SERPER_API_KEY=...   # optional: web.search
export JINA_API_KEY=...     # optional: higher rate limits for web.fetch
make run              # → http://127.0.0.1:8787   (or: .venv/bin/atrium ~/code/myproject)
```

Port 8787 taken? `make run PORT=8790`. To check that the server is up,
`curl -s http://127.0.0.1:8787/api/meta` lists each harness and whether it is available.

Development: `make dev-backend` (auto-reload, port 8787) and `make dev-web` (Vite on 5173,
proxying `/api` and `/ws`).

Atrium binds to `127.0.0.1` and has **no authentication** — don't expose it.

## Where things live

| Path | What |
|---|---|
| `~/.atrium/settings.json` | Atrium settings: defaults per harness (mode / model / effort), tracker, Git Agent, Code Review Agent (`review_agent`: auto, harness / model / effort, language), desktop notifications, web keys (`chmod 600`; env vars take precedence), extra MCP servers |
| `~/.atrium/config.json` | the project list |
| `~/.atrium/uploads/` | composer attachments |
| `~/.atrium/backups/` | the previous versions of config files saved from Settings (last 20 each) |
| `~/.gemini/antigravity-cli/settings.json` | Antigravity's model, custom models (relays) and Gemini API key (`env`) — the Antigravity CLI's own file |
| `~/.atrium/antigravity/` | Antigravity conversations: the runtime's state (`save/`), its data and JSONL transcripts (`data/brain/<id>/`), Atrium's index (`sessions/`) |
| `<project>/.atrium/board.html` | the board (generated from `board.json`) |
| `<project>/.atrium/agents.json` | session → agent name / role / harness / mode / model / effort / turn snapshots |
| `<project>/.atrium/tracker.json` | tracker transcript offsets + run log |
| `<project>/.atrium/code_review.json` | per agent: its review session, rounds with their findings, drafts, a message waiting to be delivered |
| `<project>/.atrium/worktrees/` | worktrees created from Atrium |
| `<project>/.atrium/worktrees.json` | every worktree seen, removed ones too, so their agents stay listed |
| `<project>/.atrium/trash/` | files deleted or reverted from the UI |

`.atrium/` contains a `.gitignore` with `*`, so it never shows up in your repo.

Agent names are also written back to the harness (Claude Code session title, Codex thread name),
so `claude --resume` and `codex resume` show them. Antigravity sessions are Atrium's own: no CLI lists them.

**Settings** edits the real files, raw, in Monaco: Atrium's `settings.json`, Claude Code's
`~/.claude/settings.json` plus the project's `.claude/settings.json` / `settings.local.json`, and
Codex's `~/.codex/config.toml` plus the project's `.codex/config.toml`, and Antigravity's
`~/.gemini/antigravity-cli/settings.json` (Antigravity sessions also get the `AGENTS.md` / `GEMINI.md`
rules Antigravity's own apps load). Saves are validated
(JSON / TOML), refused if the file changed on disk meanwhile, and back up the previous version.
The editor's language servers are configured under `lsp` there (`enabled`, `commands` to override
the server per language, `python.typeCheckingMode`). What each file sets, and what Atrium decides over
it: [docs/configuration.md](docs/configuration.md).

**Notifications.** Agents keep running when you close the page; Atrium posts a desktop notification
when one asks for approval (or asks a question) and when its turn ends — except for a session whose
chat is open in a visible, focused tab. On macOS they go through `osascript` (they show up as
*Script Editor*; allow it under System Settings → Notifications), or through `terminal-notifier` if
it is installed (`brew install terminal-notifier`), which groups them per session and opens the
session when clicked. Linux uses `notify-send`. Toggle them under `notifications` in `settings.json`
(`enabled`, `permission`, `turn_end`, `sound`).

## Architecture

```
atrium/
  harness/base.py        Harness / LiveSession interfaces, normalized chat items, ToolSpec
  harness/claude_code.py Claude Code via claude-agent-sdk: live sessions, permissions, history,
                         transcript digests (tracker) and file edits (diffs)
  harness/codex.py       Codex via `codex app-server`: threads, approvals, dynamic tools, history,
                         rollout digests and patches
  harness/codex_rpc.py   the shared JSON-RPC client for `codex app-server`
  harness/antigravity.py Antigravity via its SDK (bundled runtime): live sessions, approvals through a
                         pre-tool hook, history / digests / edits from the runtime's JSONL transcript
  sessions.py            live session manager (start/resume, forks, streaming, permissions, idle
                         reaping, turn snapshots)
  notify.py              desktop notifications (approval requests, finished turns) unless watched
  config.py, configfiles.py  Atrium settings; the raw config-file editor (validate, conflict, backup)
  snapshots.py           turn snapshots as git tree objects
  board/                 board store + HTML template, board tools, system prompts
  tracker.py             the per-project Tracker Agent
  gitagent.py            graph.set_view tool for the Git Agent
  review.py              the Code Review Agent: when to review, review rounds and findings, the review.* tools
  lsp.py                 language servers for the editor, shared by all tabs over /ws/lsp
  gitops.py, fs.py       git plumbing, sandboxed file access
  changes.py             per-session change sets (diff review)
  mcp/web.py             Serper + Jina tools (in-process and stdio MCP)
  watch.py               file + transcript watchers → websocket events
  server.py              FastAPI REST + one multiplexed websocket; serves the built UI
web/                     React + TypeScript + Vite UI (Monaco for editor/diffs)
```

### Adding another harness (pi, …)

Implement `atrium.harness.base.Harness` and register it in `atrium/harness/__init__.py`
(`claude_code.py`, `codex.py` and `antigravity.py` are the references):

* `available()`, `catalog()` (models + supported efforts), `permission_modes`, `efforts`;
* `list_sessions(project_dir, roots)` (async), `transcript_path`, `watch_roots`, `transcript_cwd` — discovery;
* `load_history` (async) → normalized items (`user`, `text`, `thinking`, `tool`, `tool_result`, `permission`, …);
* `digest_lines(lines)` — compress raw transcript lines for the tracker;
* `file_edits(session_id, cwd)` — `FileEdit(path, before, after, kind, patch)` records, the
  transcript fallback for diff review;
* `create_live(spec, emit)` → a `LiveSession` (send with attachments / interrupt / set mode, model,
  effort / resolve permission);
* `fork_point` / `fork_session` (optional; set `can_fork = False` without them) — cut a conversation
  before a user message and copy it into a new session working in another directory;
* `config_files(project_dir)` — the harness's own config files for the Settings editor;
* `complete_json` — one-shot structured completion (used by the tracker when it is set to this harness).

Atrium's own tools are harness-neutral `ToolSpec`s grouped by server (`board.*`, `web.*`,
`graph.set_view`); expose them however the harness supports tools — in-process MCP for Claude,
dynamic tools for Codex, Python tools for Antigravity. Everything else (UI, board, tracker, diffs,
git) works unchanged.
