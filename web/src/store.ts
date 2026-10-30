import { create } from "zustand";
import { api, ForkAt, Meta, onLoggedOut, Project, ProjectInfo, SessionRow } from "./api";
import { socket } from "./ws";
import { throttle } from "./util";

export type Panel = "board" | "files" | "changes" | "git" | "terminal";
/** What a single-screen layout shows (layout.ts): the chat column or the workspace; wider ones show both. */
export type Pane = "chat" | "workspace";
const PANELS: Panel[] = ["board", "files", "changes", "git", "terminal"];
export type Theme = "light" | "dark" | "system";

/** Where to put the cursor when a file opens; `external` = read-only, outside the project. */
export interface FileTarget {
  line?: number;
  column?: number;
  external?: boolean;
  content?: string;
}

/** The fork dialog: fork session `sid` from the latest point (`at` null) or before a user message. */
export interface ForkRequest {
  sid: string;
  at: ForkAt | null;
  text?: string; // that message, offered as the fork's first message
}

interface Toast {
  id: number;
  text: string;
  kind: "info" | "error" | "ok";
}

interface AppState {
  meta: Meta | null;
  projects: Project[];
  project: ProjectInfo | null;
  sessions: SessionRow[];
  sessionId: string | null;
  subagentId: string | null; // a subagent of the selected session, shown read-only in the chat column
  compactionId: string | null; // ... or the part of its conversation a compaction closed, also read-only
  panel: Panel; // the workspace tab (left, always shown)
  chatOpen: boolean; // the chat column (right)
  pane: Pane; // the one on screen when they take turns (phones); picking an agent shows the chat, a panel the workspace
  railOpen: boolean; // the rail as a drawer over the panes (single layout); going anywhere closes it
  chatWidth: number;
  theme: Theme;
  showArchived: boolean;
  pickerOpen: false | "open" | "create"; // the folder dialog: open one, or make a new one
  homeFocus: number; // ⌘K: the home page focuses its search when this changes (0: don't)
  unsaved: string[]; // files with unsaved edits in the editor (FilesPanel): leaving the project asks first
  settingsOpen: boolean;
  forkRequest: ForkRequest | null;
  // cross-panel requests / change counters
  fileRequest: ({ root?: string; path: string; nonce: number } & FileTarget) | null;
  changesSession: string | null;
  boardVersion: number;
  trackerVersion: number;
  gitVersion: number;
  fsVersion: number;
  reviewVersion: number; // the Code Review Agent's rounds / findings changed
  terminalVersion: number; // a terminal was opened, closed or ended
  fsPaths: string[];
  toasts: Toast[];
  loggedOut: boolean; // a request found the login gone: the page offers to log in again

  init: () => Promise<void>;
  refreshMeta: () => Promise<void>;
  loadProjects: () => Promise<void>;
  openProject: (pid: string | null, opened?: boolean) => Promise<boolean>; // false: stayed (unsaved edits kept)
  addProject: (path: string, create?: { git: boolean }) => Promise<void>;
  goHome: (search?: boolean) => Promise<void>;
  refreshProject: () => Promise<void>;
  refreshSessions: () => Promise<void>;
  selectSession: (sid: string | null) => void;
  selectSubagent: (sid: string, aid: string) => void;
  selectCompaction: (sid: string, cid: string) => void;
  setPanel: (p: Panel) => void;
  setChatOpen: (v: boolean) => void;
  setPane: (p: Pane) => void;
  setRailOpen: (v: boolean) => void;
  setChatWidth: (w: number) => void;
  setTheme: (t: Theme) => void;
  setShowArchived: (v: boolean) => void;
  setPickerOpen: (v: false | "open" | "create") => void;
  setUnsaved: (names: string[]) => void;
  setSettingsOpen: (v: boolean) => void;
  setForkRequest: (r: ForkRequest | null) => void;
  openFile: (path: string, root?: string, target?: FileTarget) => void;
  reviewChanges: (sid: string | null) => void;
  toast: (text: string, kind?: Toast["kind"]) => void;
}

let unsubProject: (() => void) | null = null;
let toastId = 0;
let sessionsAsked = 0; // session-list requests sent
let sessionsShown = 0; // the one whose answer is in the store

function syncUrl(pid: string | null, sid: string | null, aid: string | null = null, cid: string | null = null) {
  const u = new URL(location.href);
  pid ? u.searchParams.set("project", pid) : u.searchParams.delete("project");
  sid ? u.searchParams.set("session", sid) : u.searchParams.delete("session");
  aid ? u.searchParams.set("agent", aid) : u.searchParams.delete("agent");
  cid ? u.searchParams.set("compaction", cid) : u.searchParams.delete("compaction");
  history.replaceState(null, "", u.toString());
}

function applyTheme(t: Theme) {
  const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  // the browser's bars (and a Home Screen app's status bar) in the page's own background
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#262624" : "#faf9f5");
}

export function resolvedTheme(): "light" | "dark" {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

export const useApp = create<AppState>((set, get) => {
  const refreshSessionsThrottled = throttle(() => void get().refreshSessions(), 800);
  const refreshProjectThrottled = throttle(() => void get().refreshProject(), 800);

  return {
    meta: null,
    projects: [],
    project: null,
    sessions: [],
    sessionId: null,
    subagentId: null,
    compactionId: null,
    panel: PANELS.find((p) => p === localStorage.getItem("atrium.panel")) ?? "board",
    chatOpen: localStorage.getItem("atrium.chatOpen") !== "0",
    pane: "chat",
    railOpen: false,
    chatWidth: Number(localStorage.getItem("atrium.chatWidth")) || Math.round(Math.min(520, Math.max(400, window.innerWidth * 0.3))),
    theme: (localStorage.getItem("atrium.theme") as Theme) || "system",
    showArchived: false,
    pickerOpen: false,
    homeFocus: 0,
    unsaved: [],
    settingsOpen: false,
    forkRequest: null,
    fileRequest: null,
    changesSession: null,
    boardVersion: 0,
    trackerVersion: 0,
    gitVersion: 0,
    fsVersion: 0,
    reviewVersion: 0,
    terminalVersion: 0,
    fsPaths: [],
    toasts: [],
    loggedOut: false,

    init: async () => {
      applyTheme(get().theme);
      matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(get().theme));
      const [meta] = await Promise.all([api.meta(), get().loadProjects()]);
      set({ meta });
      onLoggedOut(() => set({ loggedOut: true }));
      const params = new URLSearchParams(location.search);
      const pid = params.get("project"); // none: the home page
      if (pid && get().projects.some((p) => p.id === pid)) {
        await get().openProject(pid);
        const sid = params.get("session");
        const aid = params.get("agent");
        const cid = params.get("compaction");
        if (sid && aid) get().selectSubagent(sid, aid);
        else if (sid && cid) get().selectCompaction(sid, cid);
        else if (sid) get().selectSession(sid);
      } else if (pid) {
        syncUrl(null, null); // a project that isn't in the list (any more)
      }
      socket.onReconnect(() => {
        void get().refreshSessions();
        set((s) => ({ boardVersion: s.boardVersion + 1, gitVersion: s.gitVersion + 1, trackerVersion: s.trackerVersion + 1, terminalVersion: s.terminalVersion + 1 }));
      });
    },

    refreshMeta: async () => {
      set({ meta: await api.meta() });
    },

    loadProjects: async () => {
      set({ projects: await api.projects() });
    },

    openProject: async (pid, opened = false) => {
      const { project: current, unsaved } = get();
      // the editor's open files live in FilesPanel, which goes with the project: same question as closing a tab
      if (pid !== (current?.id ?? null) && unsaved.length && !confirm(`Discard unsaved changes to ${unsaved.length === 1 ? unsaved[0] : `${unsaved.length} files`}?`))
        return false;
      unsubProject?.();
      unsubProject = null;
      set({ railOpen: false });
      if (!pid) {
        set({ project: null, sessions: [], sessionId: null, subagentId: null, compactionId: null, homeFocus: 0 });
        syncUrl(null, null);
        return true;
      }
      const project = await api.project(pid, opened);
      set({ project, sessions: [], sessionId: null, subagentId: null, compactionId: null, changesSession: null, fileRequest: null, forkRequest: null });
      syncUrl(pid, null);
      unsubProject = socket.subscribe(`project:${pid}`, (msg) => {
        switch (msg.type) {
          case "sessions_changed":
          case "session_state":
            refreshSessionsThrottled();
            break;
          case "board":
            set((s) => ({ boardVersion: s.boardVersion + 1 }));
            break;
          case "tracker":
            set((s) => ({ trackerVersion: s.trackerVersion + 1 }));
            if (msg.run?.updated_agents) refreshSessionsThrottled();
            break;
          case "fs":
            set((s) => ({ fsVersion: s.fsVersion + 1, fsPaths: msg.paths || [] }));
            break;
          case "review":
            set((s) => ({ reviewVersion: s.reviewVersion + 1 }));
            break;
          case "terminals_changed":
            set((s) => ({ terminalVersion: s.terminalVersion + 1 }));
            break;
          case "git_changed":
          case "git_view":
            set((s) => ({ gitVersion: s.gitVersion + 1 }));
            // `git init` in a plain directory: is_git / worktrees were captured when the project opened
            if (msg.type === "git_changed" && !get().project?.is_git) refreshProjectThrottled();
            break;
          case "worktrees_changed": // the worktree pickers, and agents whose worktree went away
            refreshProjectThrottled();
            refreshSessionsThrottled();
            break;
        }
      });
      await get().refreshSessions();
      void get().loadProjects();
      return true;
    },

    addProject: async (path, create) => {
      const p = await api.addProject(path, create);
      await get().loadProjects();
      await get().openProject(p.id, true);
      set({ pickerOpen: false });
    },

    goHome: async (search = false) => {
      if (await get().openProject(null)) set({ pickerOpen: false, homeFocus: search ? Date.now() : 0 });
    },

    refreshProject: async () => {
      const p = get().project;
      if (!p) return;
      const fresh = await api.project(p.id);
      if (get().project?.id === p.id) set({ project: fresh });
    },

    refreshSessions: async () => {
      const p = get().project;
      if (!p) return;
      const seq = ++sessionsAsked;
      const archived = get().showArchived;
      try {
        const sessions = await api.sessions(p.id, archived);
        // answers can overtake each other: never go back to an older list, nor show one from before Archived was toggled
        if (seq < sessionsShown || archived !== get().showArchived || get().project?.id !== p.id) return;
        sessionsShown = seq;
        set({ sessions });
      } catch {
        /* transient */
      }
    },

    selectSession: (sid) => {
      get().setChatOpen(true);
      set({ sessionId: sid, subagentId: null, compactionId: null, pane: "chat", railOpen: false });
      syncUrl(get().project?.id ?? null, sid);
    },
    selectSubagent: (sid, aid) => {
      get().setChatOpen(true);
      set({ sessionId: sid, subagentId: aid, compactionId: null, pane: "chat", railOpen: false });
      syncUrl(get().project?.id ?? null, sid, aid);
    },
    selectCompaction: (sid, cid) => {
      get().setChatOpen(true);
      set({ sessionId: sid, subagentId: null, compactionId: cid, pane: "chat", railOpen: false });
      syncUrl(get().project?.id ?? null, sid, null, cid);
    },

    setPanel: (p) => {
      localStorage.setItem("atrium.panel", p);
      set({ panel: p, pane: "workspace", railOpen: false });
    },
    setChatOpen: (v) => {
      localStorage.setItem("atrium.chatOpen", v ? "1" : "0");
      set({ chatOpen: v });
    },
    setPane: (p) => set({ pane: p }),
    setRailOpen: (v) => set({ railOpen: v }),
    setChatWidth: (w) => {
      localStorage.setItem("atrium.chatWidth", String(Math.round(w)));
      set({ chatWidth: w });
    },
    setTheme: (t) => {
      localStorage.setItem("atrium.theme", t);
      applyTheme(t);
      set({ theme: t, boardVersion: get().boardVersion + 1 });
    },
    setShowArchived: (v) => {
      set((s) => ({ showArchived: v, sessions: v ? s.sessions : s.sessions.filter((r) => !r.archived) })); // hiding them needs no request
      void get().refreshSessions();
    },
    setPickerOpen: (v) => set({ pickerOpen: v }),
    setUnsaved: (names) => {
      if (names.join("\n") !== get().unsaved.join("\n")) set({ unsaved: names }); // FilesPanel reports on every keystroke
    },
    setSettingsOpen: (v) => set({ settingsOpen: v, railOpen: false }),
    setForkRequest: (r) => set({ forkRequest: r }),
    openFile: (path, root, target) => {
      set({ fileRequest: { path, root, ...target, nonce: Date.now() } });
      get().setPanel("files");
    },
    reviewChanges: (sid) => {
      set({ changesSession: sid });
      get().setPanel("changes");
    },
    toast: (text, kind = "info") => {
      const id = ++toastId;
      set((s) => ({ toasts: [...s.toasts, { id, text, kind }] }));
      setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), kind === "error" ? 7000 : 3500);
    },
  };
});
