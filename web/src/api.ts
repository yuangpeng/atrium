// REST client + shared types for the Atrium backend.

export interface HarnessModel {
  id: string;
  label: string;
  description?: string | null;
  efforts?: string[];
  default_effort?: string | null;
}

export interface Harness {
  id: string;
  label: string;
  available: boolean;
  unavailable_reason?: string | null;
  catalog_error?: string;
  permission_modes: { id: string; label: string }[];
  models: HarnessModel[];
  efforts: string[];
}

export interface HarnessDefaults {
  permission_mode?: string;
  model?: string;
  effort?: string;
  enabled?: boolean;
  binary?: string;
}

/** ~/.atrium/settings.json (secrets reduced to booleans). Edited as a raw file in Settings. */
export interface Settings {
  defaults: { harness: string };
  harnesses: Record<string, HarnessDefaults>;
  sessions: { idle_close_minutes: number };
  tracker: { enabled: boolean; harness: string; model: string; effort: string; interval_seconds: number; after_turn_seconds: number };
  git_agent: { harness: string; model: string };
  web: { serper_api_key: boolean; jina_api_key: boolean; replace_builtin_web_tools: boolean };
  ui: { theme: "system" | "light" | "dark" };
  _error?: string | null;
}

export interface ConfigFileInfo {
  id: string;
  label: string;
  path: string;
  language: "json" | "toml";
  scope: "user" | "project" | "local";
  harness: string;
  hint: string;
  exists: boolean;
}

export interface ConfigFileData extends ConfigFileInfo {
  content: string;
  sha: string | null;
  backup?: string | null;
}

export interface Attachment {
  name: string;
  mime: string;
  path?: string;
  url?: string | null;
}

export interface Upload {
  id: string;
  name: string;
  path: string;
  mime: string;
  size: number;
  url: string;
}

export interface Meta {
  version: string;
  harnesses: Harness[];
  settings: Settings;
  lsp: Record<string, { server: string }>; // Monaco language id -> language server on this machine
}

export interface Project {
  id: string;
  path: string;
  name: string;
  exists?: boolean;
  last_opened?: number;
}

export interface Worktree {
  path: string;
  branch: string | null;
  head: string | null;
  detached: boolean;
  locked: boolean | string;
  prunable: boolean | string;
  bare: boolean;
  main: boolean;
}

export interface ProjectInfo extends Project {
  is_git: boolean;
  worktrees: Worktree[];
}

export type LiveState = "starting" | "running" | "idle" | "waiting" | "closed" | "error";
export type AgentStatus = "working" | "waiting" | "blocked" | "done" | "idle";
export type SubagentStatus = "running" | "done" | "failed" | "stopped";

/** An agent spawned by a session (Claude's Agent tool, a Codex sub-agent thread); read-only. */
export interface SubagentRow {
  id: string;
  agent_type: string | null;
  description: string;
  status: SubagentStatus;
  updated_at: number;
  created_at: number | null;
  name: string | null; // Codex nickname
  tool_use_id: string | null; // the parent's tool call that spawned it (Claude)
  model: string | null;
  depth: number;
}

export interface SessionRow {
  id: string;
  harness: string;
  name: string | null;
  title: string;
  role: "worker" | "git";
  cwd: string;
  worktree: boolean;
  branch: string | null;
  created_at: number | null;
  updated_at: number;
  live: LiveState | null;
  pending: number;
  status: AgentStatus | null;
  progress: number | null;
  doing: string | null;
  first_prompt: string;
  archived: boolean;
  created_by: "atrium" | "external";
  effort: string;
  subagents: SubagentRow[]; // oldest first, nested ones included
}

export type Item =
  | { kind: "user"; id: string; text: string; ts?: number; attachments?: Attachment[] }
  | { kind: "text"; id: string; text: string }
  | { kind: "thinking"; id: string; text: string; parent?: string }
  | { kind: "tool"; id: string; name: string; input: Record<string, any>; parent?: string }
  | { kind: "tool_result"; id: string; content: string; is_error: boolean; parent?: string }
  | {
      kind: "permission";
      id: string;
      tool: string;
      input: Record<string, any>;
      title?: string | null;
      display_name?: string | null;
      description?: string | null;
      reason?: string | null;
      blocked_path?: string | null;
      can_remember: boolean;
    }
  | { kind: "permission_resolved"; id: string; decision: string }
  | {
      kind: "result";
      id: string;
      subtype: string;
      is_error: boolean;
      duration_ms: number;
      cost_usd: number | null;
      turns: number;
      text?: string | null;
      terminal_reason?: string | null;
    }
  | { kind: "system"; id: string; subtype: string; text: string; model?: string; permission_mode?: string; tool_use_id?: string | null }
  | { kind: "error"; id: string; text: string }
  | { kind: "delta"; text: string }
  | { kind: "state"; state: LiveState };

export interface BoardAgent {
  session_id: string;
  name: string;
  status: AgentStatus;
  progress: number;
  doing: string;
  summary: string;
  updated_at: number;
  source: string;
  cards: Record<string, { title: string; html: string; updated_at: number }>;
  stats: Record<string, any>;
}

export interface Snapshot {
  live: boolean;
  state: LiveState | null;
  items: Item[];
  pending: string[];
  settings: SessionSettings;
  session: SessionRow | null;
  board: BoardAgent | null;
}

export interface SessionSettings {
  model: string;
  effort: string;
  mode: string;
}

export interface TrackerRun {
  ts: number;
  reason: string;
  sessions?: number;
  events?: number;
  skipped?: string;
  error?: string;
  cost_usd?: number;
  duration_ms?: number;
  updated_agents?: number;
  harness?: string;
}

export interface TrackerStatus {
  enabled: boolean;
  interval: number;
  harness: string;
  running: boolean;
  next_run: number;
  runs: TrackerRun[];
}

export interface DirEntry {
  name: string;
  path: string;
  type: "dir" | "file";
  size?: number;
  git?: string;
}

export interface FileData {
  root: string;
  path: string;
  abs: string;
  size: number;
  mtime: number;
  binary: boolean;
  too_large?: boolean;
  content: string | null;
  sha: string | null;
}

export interface ChangeFile {
  path: string;
  rel: string;
  status: "created" | "modified" | "deleted" | "created+deleted";
  edits: number;
  tools: string[];
  added: number;
  removed: number;
  unchanged: boolean;
  drift: boolean;
  reviewed: boolean;
  last_ts: number | null;
  baseline_source: "snapshot" | "transcript" | "head" | "unknown";
  detected: boolean;
}

export interface ChangeDetail {
  path: string;
  rel: string;
  baseline: string | null;
  baseline_source: "snapshot" | "transcript" | "head" | "unknown";
  agent_final: string | null;
  current: string | null;
  exists: boolean;
  sha: string | null;
}

export interface AgentChanges {
  session: SessionRow;
  files: number;
  unreviewed: number;
  added: number;
  removed: number;
}

export interface Commit {
  sha: string;
  parents: string[];
  refs: string[];
  author: string;
  time: number;
  subject: string;
}

export interface GitView {
  branches: string[];
  highlight: string[];
  note: string;
  source: string;
  updated_at: number;
}

export interface Branch {
  ref: string;
  name: string;
  remote: boolean;
  sha: string;
  upstream: string | null;
  track: string | null;
  date: number;
  subject: string;
  head: boolean;
}

export interface GitStatusFile {
  path: string;
  orig?: string;
  index: string;
  worktree: string;
}

export interface GitSummary {
  is_repo: boolean;
  root?: string;
  status?: { branch: string | null; upstream: string | null; ahead: number; behind: number; files: GitStatusFile[] };
  branches?: Branch[];
  worktrees?: Worktree[];
  view?: GitView | null;
}

export interface CommitFile {
  path: string;
  orig: string | null; // renames / copies: the path in the parent
  status: "A" | "M" | "D" | "R" | "C" | "T";
  added: number;
  removed: number;
  binary: boolean;
}

export interface CommitDetail {
  sha: string;
  parents: string[];
  author: string;
  email: string;
  time: number;
  refs: string;
  message: string;
  files: CommitFile[]; // against the first parent
}

export class ApiError extends Error {
  status: number;
  body: any;
  constructor(status: number, body: any) {
    super(body?.error || body?.detail || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

const q = (params: Record<string, string | number | boolean | null | undefined>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") s.set(k, String(v));
  const str = s.toString();
  return str ? `?${str}` : "";
};

export const api = {
  meta: () => req<Meta>("GET", "/api/meta"),
  settings: () => req<Settings>("GET", "/api/settings"),
  patchSetting: (path: string, value: unknown) => req<Settings>("PATCH", "/api/settings", { path, value }),
  configFiles: (pid?: string) => req<ConfigFileInfo[]>("GET", `/api/config-files${q({ pid })}`),
  configFile: (id: string, pid?: string) => req<ConfigFileData>("GET", `/api/config-files/${id}${q({ pid })}`),
  saveConfigFile: (id: string, body: { content: string; base_sha: string | null; pid?: string }) =>
    req<ConfigFileData>("PUT", `/api/config-files/${id}`, body),
  upload: async (file: File | Blob, name: string): Promise<Upload> => {
    const res = await fetch(`/api/uploads${q({ name })}`, {
      method: "PUT",
      headers: { "content-type": file.type || "application/octet-stream" },
      body: file,
    });
    const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new ApiError(res.status, data);
    return data as Upload;
  },
  projects: () => req<Project[]>("GET", "/api/projects"),
  addProject: (path: string) => req<Project>("POST", "/api/projects", { path }),
  removeProject: (pid: string) => req("DELETE", `/api/projects/${pid}`),
  browse: (path?: string) =>
    req<{ path: string; parent: string | null; git: boolean; dirs: { name: string; path: string; git: boolean }[] }>(
      "GET",
      `/api/browse${q({ path })}`,
    ),
  project: (pid: string) => req<ProjectInfo>("GET", `/api/p/${pid}`),

  sessions: (pid: string, archived = false) => req<SessionRow[]>("GET", `/api/p/${pid}/sessions${q({ archived })}`),
  createSession: (
    pid: string,
    body: {
      name: string;
      prompt?: string;
      harness?: string;
      cwd?: string;
      model?: string;
      effort?: string;
      mode?: string;
      attachments?: string[];
      worktree?: { branch: string; base?: string; create?: boolean };
    },
  ) => req<{ id: string; cwd: string }>("POST", `/api/p/${pid}/sessions`, body),
  session: (pid: string, sid: string) => req<Snapshot>("GET", `/api/p/${pid}/sessions/${sid}`),
  subagent: (pid: string, sid: string, aid: string) =>
    req<{ subagent: SubagentRow; items: Item[] }>("GET", `/api/p/${pid}/sessions/${sid}/subagents/${encodeURIComponent(aid)}`),
  patchSession: (pid: string, sid: string, body: { name?: string; archived?: boolean }) =>
    req("PATCH", `/api/p/${pid}/sessions/${sid}`, body),
  deleteSession: (pid: string, sid: string) => req<{ ok: boolean; backup: string | null }>("DELETE", `/api/p/${pid}/sessions/${sid}`),
  send: (pid: string, sid: string, text: string, attachments: string[] = []) =>
    req("POST", `/api/p/${pid}/sessions/${sid}/send`, { text, attachments }),
  attach: (pid: string, sid: string) => req("POST", `/api/p/${pid}/sessions/${sid}/attach`, {}),
  interrupt: (pid: string, sid: string) => req("POST", `/api/p/${pid}/sessions/${sid}/interrupt`),
  setMode: (pid: string, sid: string, mode: string) => req("POST", `/api/p/${pid}/sessions/${sid}/mode`, { mode }),
  setModel: (pid: string, sid: string, model: string) => req("POST", `/api/p/${pid}/sessions/${sid}/model`, { model }),
  setEffort: (pid: string, sid: string, effort: string) => req("POST", `/api/p/${pid}/sessions/${sid}/effort`, { effort }),
  permission: (pid: string, sid: string, body: Record<string, unknown>) =>
    req("POST", `/api/p/${pid}/sessions/${sid}/permission`, body),
  closeSession: (pid: string, sid: string) => req("POST", `/api/p/${pid}/sessions/${sid}/close`),

  changes: (pid: string, sid: string) =>
    req<{ session_id: string; cwd: string; files: ChangeFile[]; snapshots: number }>("GET", `/api/p/${pid}/sessions/${sid}/changes`),
  changeFile: (pid: string, sid: string, path: string) =>
    req<ChangeDetail>("GET", `/api/p/${pid}/sessions/${sid}/changes/file${q({ path })}`),
  revertChange: (pid: string, sid: string, path: string) => req("POST", `/api/p/${pid}/sessions/${sid}/changes/revert`, { path }),
  reviewChange: (pid: string, sid: string, path: string, reviewed: boolean) =>
    req("POST", `/api/p/${pid}/sessions/${sid}/changes/review`, { path, reviewed }),
  projectChanges: (pid: string) => req<AgentChanges[]>("GET", `/api/p/${pid}/changes`),

  board: (pid: string) => req<{ agents: Record<string, BoardAgent> }>("GET", `/api/p/${pid}/board`),
  boardHtmlUrl: (pid: string) => `/api/p/${pid}/board.html`,
  deleteCard: (pid: string, sid: string, key: string) => req("DELETE", `/api/p/${pid}/board/agents/${sid}/cards/${key}`),
  tracker: (pid: string) => req<TrackerStatus>("GET", `/api/p/${pid}/tracker`),
  runTracker: (pid: string) => req("POST", `/api/p/${pid}/tracker/run`),

  files: (pid: string, path: string, root?: string, hidden = false) =>
    req<{ root: string; path: string; entries: DirEntry[] }>("GET", `/api/p/${pid}/files${q({ path, root, hidden })}`),
  file: (pid: string, path: string, root?: string) => req<FileData>("GET", `/api/p/${pid}/file${q({ path, root })}`),
  writeFile: (pid: string, body: { root?: string; path: string; content: string; base_sha?: string | null; force?: boolean }) =>
    req<{ path: string; sha: string; mtime: number }>("PUT", `/api/p/${pid}/file`, body),
  createFile: (pid: string, body: { root?: string; path: string; kind: "file" | "dir" }) => req("POST", `/api/p/${pid}/file/create`, body),
  renameFile: (pid: string, body: { root?: string; src: string; dst: string }) => req("POST", `/api/p/${pid}/file/rename`, body),
  deleteFile: (pid: string, body: { root?: string; path: string }) => req("POST", `/api/p/${pid}/file/delete`, body),

  gitSummary: (pid: string, root?: string) => req<GitSummary>("GET", `/api/p/${pid}/git/summary${q({ root })}`),
  gitGraph: (pid: string, mode: string, refs?: string[]) =>
    req<{ commits: Commit[]; head: string | null; refs: string[]; mode: string; view: GitView | null }>(
      "GET",
      `/api/p/${pid}/git/graph${q({ mode, refs: refs?.join(",") })}`,
    ),
  gitCommit: (pid: string, sha: string) => req<CommitDetail>("GET", `/api/p/${pid}/git/commit/${sha}`),
  gitCommitFile: (pid: string, sha: string, path: string, orig?: string | null) =>
    req<{ path: string; parent: string | null; original: string; modified: string }>(
      "GET",
      `/api/p/${pid}/git/commit/${sha}/file${q({ path, orig })}`,
    ),
  gitDiff: (pid: string, path: string, root?: string, staged = false) =>
    req<{ path: string; original: string; current: string; editable: boolean }>(
      "GET",
      `/api/p/${pid}/git/diff${q({ path, root, staged })}`,
    ),
  gitInit: (pid: string) => req("POST", `/api/p/${pid}/git/init`),
  gitStage: (pid: string, paths: string[], root?: string) => req("POST", `/api/p/${pid}/git/stage`, { paths, root }),
  gitUnstage: (pid: string, paths: string[], root?: string) => req("POST", `/api/p/${pid}/git/unstage`, { paths, root }),
  gitDiscard: (pid: string, paths: string[], root?: string) => req("POST", `/api/p/${pid}/git/discard`, { paths, root }),
  gitCommitCreate: (pid: string, message: string, root?: string, amend = false) =>
    req<{ ok: boolean; output: string }>("POST", `/api/p/${pid}/git/commit`, { message, root, amend }),
  gitCheckout: (pid: string, branch: string, root?: string) => req("POST", `/api/p/${pid}/git/checkout`, { branch, root }),
  gitAddWorktree: (pid: string, body: { branch: string; base?: string; create?: boolean; path?: string }) =>
    req<{ path: string; branch: string }>("POST", `/api/p/${pid}/git/worktrees`, body),
  gitRemoveWorktree: (pid: string, path: string, force = false) =>
    req("POST", `/api/p/${pid}/git/worktrees/remove`, { path, force }),
  gitSetView: (pid: string, body: { branches: string[]; highlight?: string[]; note?: string }) =>
    req<GitView>("POST", `/api/p/${pid}/git/view`, body),
  gitResetView: (pid: string) => req("DELETE", `/api/p/${pid}/git/view`),
  gitAgent: (pid: string) => req<{ id: string }>("POST", `/api/p/${pid}/git/agent`),
};
