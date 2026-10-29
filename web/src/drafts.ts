// Composer drafts: the new-agent page has one per project, every agent's reply box one per session.
// They live here rather than in the components, so opening another agent and coming back (or hiding
// the chat, or switching projects) keeps them, and they're mirrored to localStorage, so a reload keeps
// them too.
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

/** What's typed in an agent's reply box. */
export interface ReplyDraft {
  text: string;
  attachments: PendingUpload[]; // only finished uploads are saved
}

export const EMPTY_DRAFT: NewAgentDraft = { name: "", prompt: "", ws: { kind: "root" }, attachments: [] };
export const EMPTY_REPLY: ReplyDraft = { text: "", attachments: [] };
const PREFIX = "atrium.draft."; // + project id
const REPLY_PREFIX = "atrium.reply."; // + session id

/** Something typed or attached, i.e. worth coming back for. */
export const hasContent = (d?: NewAgentDraft) => !!d && (!!d.name.trim() || !!d.prompt.trim() || d.attachments.length > 0);
export const hasReply = (d?: ReplyDraft) => !!d && (!!d.text.trim() || d.attachments.length > 0);

// restored images preview from the upload's URL
const restore = (uploads: Upload[] = []): PendingUpload[] =>
  uploads.map((u, i) => ({ key: `${i}-${u.id}`, name: u.name, mime: u.mime, preview: u.mime.startsWith("image/") ? u.url : undefined, upload: u }));
const finished = (xs: PendingUpload[]) => xs.flatMap((a) => (a.upload ? [a.upload] : []));

function load(key: string): NewAgentDraft {
  try {
    const { uploads, ...d } = JSON.parse(localStorage.getItem(key) || "{}");
    return { ...EMPTY_DRAFT, ...d, attachments: restore(uploads) };
  } catch {
    return EMPTY_DRAFT;
  }
}

function loadReply(key: string): ReplyDraft {
  try {
    const { text = "", uploads } = JSON.parse(localStorage.getItem(key) || "{}");
    return { text, attachments: restore(uploads) };
  } catch {
    return EMPTY_REPLY;
  }
}

/** `null` = nothing worth keeping: the key goes. */
function write(key: string, value: object | null) {
  try {
    if (value) localStorage.setItem(key, JSON.stringify(value));
    else localStorage.removeItem(key);
  } catch {
    /* storage full: the draft still lives in memory */
  }
}

function save(pid: string, d: NewAgentDraft) {
  const { attachments, ...rest } = d;
  const uploads = finished(attachments);
  const blank = !d.name && !d.prompt && !uploads.length && d.ws.kind === "root" && [d.harness, d.mode, d.model, d.effort].every((x) => x === undefined);
  write(PREFIX + pid, blank ? null : { ...rest, uploads });
}

interface Drafts {
  byProject: Record<string, NewAgentDraft>;
  bySession: Record<string, ReplyDraft>;
  update: (pid: string, fn: (d: NewAgentDraft) => NewAgentDraft) => void;
  updateReply: (sid: string, fn: (d: ReplyDraft) => ReplyDraft) => void;
}

export const useDrafts = create<Drafts>((set, get) => {
  const byProject: Record<string, NewAgentDraft> = {};
  const bySession: Record<string, ReplyDraft> = {};
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith(PREFIX)) byProject[key.slice(PREFIX.length)] = load(key);
    else if (key?.startsWith(REPLY_PREFIX)) bySession[key.slice(REPLY_PREFIX.length)] = loadReply(key);
  }
  return {
    byProject,
    bySession,
    // both also from upload callbacks that finish after the composer is gone
    update: (pid, fn) => {
      const d = fn(get().byProject[pid] ?? EMPTY_DRAFT);
      set((s) => ({ byProject: { ...s.byProject, [pid]: d } }));
      save(pid, d);
    },
    updateReply: (sid, fn) => {
      const d = fn(get().bySession[sid] ?? EMPTY_REPLY);
      set((s) => ({ bySession: { ...s.bySession, [sid]: d } }));
      const uploads = finished(d.attachments);
      write(REPLY_PREFIX + sid, d.text || uploads.length ? { text: d.text, uploads } : null);
    },
  };
});
