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

/** Filled in by the LSP client once the editor bundle has loaded. */
export const editorBridge: { closeFile?: (path: string) => void } = {};
