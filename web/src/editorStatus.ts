// Editor ↔ app glue that must not pull in Monaco (FilesPanel lives in the main bundle):
// per-file language-server status for the status line, and hooks the lazy LSP client fills in.
import { create } from "zustand";

export interface LspFileStatus {
  state: "connecting" | "ready" | "unavailable";
  server?: string;
  detail?: string;
  errors: number;
  warnings: number;
}

const EMPTY: LspFileStatus = { state: "connecting", errors: 0, warnings: 0 };

interface EditorStatusState {
  files: Record<string, LspFileStatus>; // absolute path -> status
  patch: (path: string, p: Partial<LspFileStatus>) => void;
  drop: (path: string) => void;
}

export const useEditorStatus = create<EditorStatusState>((set) => ({
  files: {},
  patch: (path, p) =>
    set((s) => ({ files: { ...s.files, [path]: { ...(s.files[path] ?? EMPTY), ...p } } })),
  drop: (path) =>
    set((s) => {
      const { [path]: _gone, ...rest } = s.files;
      return { files: rest };
    }),
}));

/** The Files editor's inline completion (editor/completion.ts), for its switch: requests on their way, the last failure. */
export const useCompletionStatus = create<{ busy: number; error: string | null }>(() => ({ busy: 0, error: null }));

/** Filled in once the editor bundle has loaded: by the LSP client, and by the Files editor (its selection, for ⌘⇧F);
 *  and by the Files panel, for a language server's edits (a rename, a quick fix) in a file the editor doesn't show:
 *  `readFile` gives its text as the panel has it (an open tab's, else the file on disk with its sha; null: no such
 *  file yet), `writeFile` puts a new text where that was found (the tab, which auto-save writes; else the disk, over
 *  `sha`: refused if the file changed since) and resolves to the sha written (null for a tab). */
export const editorBridge: {
  closeFile?: (path: string) => void;
  selectedText?: () => string | undefined;
  cursor?: () => { line: number; column: number } | undefined; // where "Open in VS Code" goes
  trigger?: (action: string) => void; // runs one of the editor's actions in it (⌘⇧P: its command palette)
  readFile?: (path: string) => Promise<{ text: string; tab: boolean; sha: string | null } | null>;
  writeFile?: (path: string, text: string, sha: string | null) => Promise<string | null>;
} = {};
