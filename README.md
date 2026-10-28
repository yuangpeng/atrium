# Atrium

A project-centric workbench for coding agents — Claude Code and Codex side by side, harness-agnostic by design.

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
   * **Git** — commit graph with lanes (pick a commit: its files as a tree, each one diffed side by
     side against the first parent, renames followed); *Smart* / *All* / *Custom* branch sets, and an
     **Agent view**: the **Git Agent** (a chat drawer) investigates with read-only git and drives
     the graph through a `graph.set_view` tool — "simplify the graph to what matters". Worktrees:
     create / remove, start an agent directly in a new worktree. A repository created later
     (`git init` in a terminal, or the button in the Git tab) is picked up live.
4. **MCP.** `web.search` (Serper) and `web.fetch` (Jina Reader) are mounted into every agent —
   Claude sees them as `mcp__web__search` / `mcp__web__fetch` (optionally replacing the built-in
   WebSearch/WebFetch); Codex reserves the `web` namespace, so there they are `internet.search` /
   `internet.fetch`. Extra MCP servers for Claude sessions go under `mcp_servers` in Atrium's
   settings; Codex uses the `[mcp_servers]` of its own `config.toml`. The web tools are also
   available as a stdio MCP server: `atrium-web-mcp`.
5. **Several harnesses, one workbench.** Claude Code runs through `claude-agent-sdk`; Codex through
   `codex app-server` (JSON-RPC over stdio, one process shared by all Codex sessions). Sessions
   don't convert between harnesses — each continues in its own — but they collaborate through the
   board, the files and git: a Codex agent sees what a Claude agent published, and vice versa.
6. **The composer.** Paste, drop or pick images and files (stored in `~/.atrium/uploads`): images go
   to the model as images (base64 for Claude, `localImage` for Codex), other files are passed by
   path. Next to send there is a **reasoning-effort** picker (`Auto` = whatever the harness config
   says; Claude low…max, applied mid-session; Codex per turn, from the model's supported levels).
   Sending while a Codex turn runs steers that turn.

## Quick start

Requirements: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), Node ≥ 18 (only to build the UI), and
a working Claude Code login (the Agent SDK bundles the CLI and uses your existing auth/env).
Optional: the [Codex CLI](https://developers.openai.com/codex) on `PATH` (or set
`harnesses.codex.binary`) — the Codex harness shows as unavailable without it.

```bash
make install          # python venv + npm deps
make build            # build the UI into atrium/static
export SERPER_API_KEY=...   # optional: web.search
export JINA_API_KEY=...     # optional: higher rate limits for web.fetch
make run              # → http://127.0.0.1:8787   (or: .venv/bin/atrium ~/code/myproject)
```

Development: `make dev-backend` (auto-reload, port 8787) and `make dev-web` (Vite on 5173,
proxying `/api` and `/ws`).

Atrium binds to `127.0.0.1` and has **no authentication** — don't expose it.

## Where things live

| Path | What |
|---|---|
| `~/.atrium/settings.json` | Atrium settings: defaults per harness (mode / model / effort), tracker, Git Agent, web keys (`chmod 600`; env vars take precedence), extra MCP servers |
| `~/.atrium/config.json` | the project list |
| `~/.atrium/uploads/` | composer attachments |
| `~/.atrium/backups/` | the previous versions of config files saved from Settings (last 20 each) |
| `<project>/.atrium/board.html` | the board (generated from `board.json`) |
| `<project>/.atrium/agents.json` | session → agent name / role / harness / mode / model / effort / turn snapshots |
| `<project>/.atrium/tracker.json` | tracker transcript offsets + run log |
| `<project>/.atrium/worktrees/` | worktrees created from Atrium |
| `<project>/.atrium/trash/` | files deleted or reverted from the UI |

`.atrium/` contains a `.gitignore` with `*`, so it never shows up in your repo.

Agent names are also written back to the harness (Claude Code session title, Codex thread name),
so `claude --resume` and `codex resume` show them.

**Settings** edits the real files, raw, in Monaco: Atrium's `settings.json`, Claude Code's
`~/.claude/settings.json` plus the project's `.claude/settings.json` / `settings.local.json`, and
Codex's `~/.codex/config.toml` plus the project's `.codex/config.toml`. Saves are validated
(JSON / TOML), refused if the file changed on disk meanwhile, and back up the previous version.
The editor's language servers are configured under `lsp` there (`enabled`, `commands` to override
the server per language, `python.typeCheckingMode`).

## Architecture

```
atrium/
  harness/base.py        Harness / LiveSession interfaces, normalized chat items, ToolSpec
  harness/claude_code.py Claude Code via claude-agent-sdk: live sessions, permissions, history,
                         transcript digests (tracker) and file edits (diffs)
  harness/codex.py       Codex via `codex app-server`: threads, approvals, dynamic tools, history,
                         rollout digests and patches
  harness/codex_rpc.py   the shared JSON-RPC client for `codex app-server`
  sessions.py            live session manager (start/resume, streaming, permissions, idle reaping,
                         turn snapshots)
  config.py, configfiles.py  Atrium settings; the raw config-file editor (validate, conflict, backup)
  snapshots.py           turn snapshots as git tree objects
  board/                 board store + HTML template, board tools, system prompts
  tracker.py             the per-project Tracker Agent
  gitagent.py            graph.set_view tool for the Git Agent
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
(`claude_code.py` and `codex.py` are the two references):

* `available()`, `catalog()` (models + supported efforts), `permission_modes`, `efforts`;
* `list_sessions(project_dir, roots)` (async), `transcript_path`, `watch_roots`, `transcript_cwd` — discovery;
* `load_history` (async) → normalized items (`user`, `text`, `thinking`, `tool`, `tool_result`, `permission`, …);
* `digest_lines(lines)` — compress raw transcript lines for the tracker;
* `file_edits(session_id, cwd)` — `FileEdit(path, before, after, kind, patch)` records, the
  transcript fallback for diff review;
* `create_live(spec, emit)` → a `LiveSession` (send with attachments / interrupt / set mode, model,
  effort / resolve permission);
* `config_files(project_dir)` — the harness's own config files for the Settings editor;
* `complete_json` — one-shot structured completion (used by the tracker when it is set to this harness).

Atrium's own tools are harness-neutral `ToolSpec`s grouped by server (`board.*`, `web.*`,
`graph.set_view`); expose them however the harness supports tools — in-process MCP for Claude,
dynamic tools for Codex. Everything else (UI, board, tracker, diffs, git) works unchanged.
