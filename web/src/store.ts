import { create } from "zustand";
import { api, ForkAt, Meta, Project, ProjectInfo, SessionRow } from "./api";
import { socket } from "./ws";
import { throttle } from "./util";

export type Panel = "board" | "files" | "changes" | "git";
const PANELS: Panel[] = ["board", "files", "changes", "git"];
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
  chatWidth: number;
  theme: Theme;
  showArchived: boolean;
  pickerOpen: boolean;
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
  fsPaths: string[];
  toasts: Toast[];

  init: () => Promise<void>;
  refreshMeta: () => Promise<void>;
  loadProjects: () => Promise<void>;
  openProject: (pid: string | null) => Promise<void>;
  addProject: (path: string) => Promise<void>;
  refreshProject: () => Promise<void>;
  refreshSessions: () => Promise<void>;
  selectSession: (sid: string | null) => void;
  selectSubagent: (sid: string, aid: string) => void;
  selectCompaction: (sid: string, cid: string) => void;
  setPanel: (p: Panel) => void;
  setChatOpen: (v: boolean) => void;
  setChatWidth: (w: number) => void;
  setTheme: (t: Theme) => void;
  setShowArchived: (v: boolean) => void;
  setPickerOpen: (v: boolean) => void;
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
    chatWidth: Number(localStorage.getItem("atrium.chatWidth")) || Math.round(Math.min(520, Math.max(400, window.innerWidth * 0.3))),
    theme: (localStorage.getItem("atrium.theme") as Theme) || "system",
    showArchived: false,
    pickerOpen: false,
    settingsOpen: false,
    forkRequest: null,
    fileRequest: null,
    changesSession: null,
    boardVersion: 0,
    trackerVersion: 0,
    gitVersion: 0,
    fsVersion: 0,
    reviewVersion: 0,
    fsPaths: [],
    toasts: [],

    init: async () => {
      applyTheme(get().theme);
      matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(get().theme));
      const [meta] = await Promise.all([api.meta(), get().loadProjects()]);
      set({ meta });
      const params = new URLSearchParams(location.search);
      const pid = params.get("project") || localStorage.getItem("atrium.project") || get().projects[0]?.id || null;
      if (pid && get().projects.some((p) => p.id === pid)) {
        await get().openProject(pid);
        const sid = params.get("session");
        const aid = params.get("agent");
        const cid = params.get("compaction");
        if (sid && aid) get().selectSubagent(sid, aid);
        else if (sid && cid) get().selectCompaction(sid, cid);
        else if (sid) get().selectSession(sid);
      } else {
        set({ pickerOpen: true });
      }
      socket.onReconnect(() => {
        void get().refreshSessions();
        set((s) => ({ boardVersion: s.boardVersion + 1, gitVersion: s.gitVersion + 1, trackerVersion: s.trackerVersion + 1 }));
      });
    },

    refreshMeta: async () => {
      set({ meta: await api.meta() });
    },

    loadProjects: async () => {
      set({ projects: await api.projects() });
    },

    openProject: async (pid) => {
      unsubProject?.();
      unsubProject = null;
      if (!pid) {
        set({ project: null, sessions: [], sessionId: null, subagentId: null, compactionId: null });
        syncUrl(null, null);
        return;
      }
      const project = await api.project(pid);
      localStorage.setItem("atrium.project", pid);
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
    },

    addProject: async (path) => {
      const p = await api.addProject(path);
      await get().loadProjects();
      await get().openProject(p.id);
      set({ pickerOpen: false });
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
      set({ sessionId: sid, subagentId: null, compactionId: null });
      syncUrl(get().project?.id ?? null, sid);
    },
    selectSubagent: (sid, aid) => {
      get().setChatOpen(true);
      set({ sessionId: sid, subagentId: aid, compactionId: null });
      syncUrl(get().project?.id ?? null, sid, aid);
    },
    selectCompaction: (sid, cid) => {
      get().setChatOpen(true);
      set({ sessionId: sid, subagentId: null, compactionId: cid });
      syncUrl(get().project?.id ?? null, sid, null, cid);
    },

    setPanel: (p) => {
      localStorage.setItem("atrium.panel", p);
      set({ panel: p });
    },
    setChatOpen: (v) => {
      localStorage.setItem("atrium.chatOpen", v ? "1" : "0");
      set({ chatOpen: v });
    },
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
    setSettingsOpen: (v) => set({ settingsOpen: v }),
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
