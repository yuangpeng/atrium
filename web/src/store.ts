import { create } from "zustand";
import { api, Meta, Project, ProjectInfo, SessionRow } from "./api";
import { socket } from "./ws";
import { throttle } from "./util";

export type Panel = "board" | "files" | "changes" | "git";
export type Theme = "light" | "dark" | "system";

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
  panel: Panel | null;
  panelMax: boolean;
  panelWidth: number;
  theme: Theme;
  showArchived: boolean;
  pickerOpen: boolean;
  settingsOpen: boolean;
  sidebarOpen: boolean;
  // cross-panel requests / change counters
  fileRequest: { root?: string; path: string; nonce: number } | null;
  changesSession: string | null;
  boardVersion: number;
  trackerVersion: number;
  gitVersion: number;
  fsVersion: number;
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
  setPanel: (p: Panel | null) => void;
  togglePanel: (p: Panel) => void;
  setPanelMax: (v: boolean) => void;
  setPanelWidth: (w: number) => void;
  setTheme: (t: Theme) => void;
  setShowArchived: (v: boolean) => void;
  setPickerOpen: (v: boolean) => void;
  setSettingsOpen: (v: boolean) => void;
  setSidebarOpen: (v: boolean) => void;
  openFile: (path: string, root?: string) => void;
  reviewChanges: (sid: string | null) => void;
  toast: (text: string, kind?: Toast["kind"]) => void;
}

let unsubProject: (() => void) | null = null;
let toastId = 0;

function syncUrl(pid: string | null, sid: string | null) {
  const u = new URL(location.href);
  pid ? u.searchParams.set("project", pid) : u.searchParams.delete("project");
  sid ? u.searchParams.set("session", sid) : u.searchParams.delete("session");
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

  return {
    meta: null,
    projects: [],
    project: null,
    sessions: [],
    sessionId: null,
    panel: (localStorage.getItem("atrium.panel") as Panel | null) ?? "board",
    panelMax: false,
    panelWidth: Number(localStorage.getItem("atrium.panelWidth")) || Math.round(Math.max(560, window.innerWidth * 0.46)),
    theme: (localStorage.getItem("atrium.theme") as Theme) || "system",
    showArchived: false,
    pickerOpen: false,
    settingsOpen: false,
    sidebarOpen: true,
    fileRequest: null,
    changesSession: null,
    boardVersion: 0,
    trackerVersion: 0,
    gitVersion: 0,
    fsVersion: 0,
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
        if (sid) get().selectSession(sid);
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
        set({ project: null, sessions: [], sessionId: null });
        syncUrl(null, null);
        return;
      }
      const project = await api.project(pid);
      localStorage.setItem("atrium.project", pid);
      set({ project, sessions: [], sessionId: null, changesSession: null, fileRequest: null });
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
          case "git_changed":
          case "git_view":
            set((s) => ({ gitVersion: s.gitVersion + 1 }));
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
      if (p) set({ project: await api.project(p.id) });
    },

    refreshSessions: async () => {
      const p = get().project;
      if (!p) return;
      try {
        const sessions = await api.sessions(p.id, get().showArchived);
        if (get().project?.id === p.id) set({ sessions });
      } catch {
        /* transient */
      }
    },

    selectSession: (sid) => {
      set({ sessionId: sid, panelMax: false });
      syncUrl(get().project?.id ?? null, sid);
    },

    setPanel: (p) => {
      if (p) localStorage.setItem("atrium.panel", p);
      set({ panel: p, panelMax: p ? get().panelMax : false });
    },
    togglePanel: (p) => {
      const cur = get().panel;
      get().setPanel(cur === p ? null : p);
    },
    setPanelMax: (v) => set({ panelMax: v }),
    setPanelWidth: (w) => {
      localStorage.setItem("atrium.panelWidth", String(Math.round(w)));
      set({ panelWidth: w });
    },
    setTheme: (t) => {
      localStorage.setItem("atrium.theme", t);
      applyTheme(t);
      set({ theme: t, boardVersion: get().boardVersion + 1 });
    },
    setShowArchived: (v) => {
      set({ showArchived: v });
      void get().refreshSessions();
    },
    setPickerOpen: (v) => set({ pickerOpen: v }),
    setSettingsOpen: (v) => set({ settingsOpen: v }),
    setSidebarOpen: (v) => set({ sidebarOpen: v }),
    openFile: (path, root) => {
      set({ fileRequest: { path, root, nonce: Date.now() } });
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
