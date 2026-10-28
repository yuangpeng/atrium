// The new-agent composer's draft, one per project. It lives here rather than in the component, so
// opening another agent and coming back (or hiding the chat, or switching projects) keeps it, and it's
// mirrored to localStorage, so a reload keeps it too.
import { create } from "zustand";
import type { Upload } from "./api";
import type { PendingUpload } from "./chat/Composer";

/** Where a new agent works: the project directory, an existing worktree, or a new one. */
export type Workspace = { kind: "root" } | { kind: "worktree"; path: string; branch: string | null } | { kind: "new"; branch: string; base: string };

export interface NewAgentDraft {
  name: string;
  prompt: string;
  ws: Workspace;
  // unset = the default harness, and that harness's defaults from ~/.atrium/settings.json
  harness?: string;
  mode?: string;
  model?: string;
  effort?: string;
  attachments: PendingUpload[]; // only finished uploads are saved
}

export const EMPTY_DRAFT: NewAgentDraft = { name: "", prompt: "", ws: { kind: "root" }, attachments: [] };
const PREFIX = "atrium.draft.";

/** Something typed or attached, i.e. worth coming back for. */
export const hasContent = (d?: NewAgentDraft) => !!d && (!!d.name.trim() || !!d.prompt.trim() || d.attachments.length > 0);

function load(key: string): NewAgentDraft {
  try {
    const { uploads = [], ...d } = JSON.parse(localStorage.getItem(key) || "{}");
    const attachments = (uploads as Upload[]).map((u, i) => ({
      key: `${i}-${u.id}`, name: u.name, mime: u.mime, preview: u.mime.startsWith("image/") ? u.url : undefined, upload: u,
    }));
    return { ...EMPTY_DRAFT, ...d, attachments };
  } catch {
    return EMPTY_DRAFT;
  }
}

function save(pid: string, d: NewAgentDraft) {
  const { attachments, ...rest } = d;
  const uploads = attachments.flatMap((a) => (a.upload ? [a.upload] : []));
  const blank = !d.name && !d.prompt && !uploads.length && d.ws.kind === "root" && [d.harness, d.mode, d.model, d.effort].every((x) => x === undefined);
  try {
    if (blank) localStorage.removeItem(PREFIX + pid);
    else localStorage.setItem(PREFIX + pid, JSON.stringify({ ...rest, uploads }));
  } catch {
    /* storage full: the draft still lives in memory */
  }
}

interface Drafts {
  byProject: Record<string, NewAgentDraft>;
  update: (pid: string, fn: (d: NewAgentDraft) => NewAgentDraft) => void;
}

export const useDrafts = create<Drafts>((set, get) => {
  const byProject: Record<string, NewAgentDraft> = {};
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith(PREFIX)) byProject[key.slice(PREFIX.length)] = load(key);
  }
  return {
    byProject,
    // also from upload callbacks that finish after the composer is gone
    update: (pid, fn) => {
      const d = fn(get().byProject[pid] ?? EMPTY_DRAFT);
      set((s) => ({ byProject: { ...s.byProject, [pid]: d } }));
      save(pid, d);
    },
  };
});
