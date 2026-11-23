// Auto-save for the editable side of a diff (Changes: an agent's file as it is now, and the working tree), as the Files
// editor saves (ui.auto_save): a moment after the typing stops, and at once when the file is left (another one picked,
// the view gone, the page hidden or closed). A save moves the base along without reloading the diff, so the editor keeps
// its cursor, its scroll and whatever was typed while the save was on its way. A change on disk since the file was read
// stops it (the server's 409), for the human to choose: their text over it, or the file as it is now.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { useApp } from "./store";
import { basename } from "./util";

const AUTO_SAVE_MS = 1000; // as the Files editor
const RETRY_MS = 4000;

export interface DiffFile {
  key: string; // which file this is: a new key is another file
  pid: string;
  root?: string;
  path: string;
  text: string; // as read
  sha: string | null; // of the file as read: the base a save goes over
}

export interface DiffSave {
  /** from the editor: its text now */
  change: (text: string) => void;
  /** ⌘S, the Save button; `force`: over a change on disk */
  save: (opts?: { force?: boolean }) => Promise<boolean>;
  /** whether there is typing not on disk yet (or on its way): a reload of the file would undo it */
  busy: () => boolean;
  unsaved: boolean;
  conflict: boolean; // the file changed on disk since it was read: nothing saves until the human chooses
  error: string | null;
  auto: boolean;
}

export function useDiffAutoSave(file: DiffFile | null): DiffSave {
  const auto = useApp((s) => s.meta?.settings.ui.auto_save !== false);
  const toast = useApp((s) => s.toast);
  const autoRef = useRef(auto);
  autoRef.current = auto;
  // the file shown, with what is known of it on disk: its text and sha as last read or saved
  const cur = useRef<{ file: DiffFile; saved: string; sha: string | null; text: string } | null>(null);
  const timer = useRef(0);
  const inflight = useRef<Promise<boolean> | null>(null);
  const fails = useRef(0);
  const [state, setState] = useState<{ unsaved: boolean; conflict: boolean; error: string | null }>({ unsaved: false, conflict: false, error: null });
  const conflictRef = useRef(state.conflict);
  conflictRef.current = state.conflict;
  const errorRef = useRef(state.error);
  errorRef.current = state.error;

  // another file, or this one read again (its sha moved): what is on disk is what was just read. Here, not in an effect:
  // the editor reports the text it is handed before the effects of this render run, and must find it already known.
  // The same file read again keeps its record (what was typed stays; a save on its way, or leaving the file, holds it).
  if (file && cur.current?.file.key === file.key) {
    if (cur.current.file.sha !== file.sha) Object.assign(cur.current, { file, saved: file.text, sha: file.sha });
  } else cur.current = file ? { file, saved: file.text, sha: file.sha, text: file.text } : null;

  const write = useCallback(
    async (c: NonNullable<typeof cur.current>, opts: { force?: boolean; keepalive?: boolean; quiet?: boolean }): Promise<boolean> => {
      window.clearTimeout(timer.current);
      while (inflight.current) await inflight.current; // one at a time: the next goes over the sha the last one left
      const text = c.text;
      if (text === c.saved && !opts.force) return true;
      const run = (async () => {
        try {
          const f = c.file;
          const res = await api.writeFile(f.pid, { root: f.root, path: f.path, content: text, base_sha: c.sha, force: !!opts.force }, !!opts.keepalive);
          c.saved = text;
          c.sha = res.sha;
          fails.current = 0;
          if (cur.current === c) setState({ unsaved: c.text !== text, conflict: false, error: null });
          if (!opts.quiet) toast(`Saved ${basename(f.path)}`, "ok");
          return true;
        } catch (e: any) {
          if (e instanceof ApiError && e.status === 409) {
            if (cur.current === c) setState((s) => ({ ...s, conflict: true }));
            return false;
          }
          fails.current += 1;
          if (!opts.quiet || fails.current === 1) toast(`${basename(c.file.path)} not saved: ${e.message}`, "error");
          if (cur.current === c) setState((s) => ({ ...s, error: e.message }));
          // an auto-save tries again, later and later (the Save button shows meanwhile), one as the page was hidden too
          if (opts.quiet && autoRef.current && cur.current === c)
            timer.current = window.setTimeout(() => void write(c, { quiet: true }), Math.min(RETRY_MS * 2 ** (fails.current - 1), 60_000));
          return false;
        }
      })();
      inflight.current = run;
      const ok = await run;
      inflight.current = null;
      // typed on while it was on its way: that goes next, after the usual pause
      if (ok && autoRef.current && cur.current === c && c.text !== c.saved) timer.current = window.setTimeout(() => void write(c, { quiet: true }), AUTO_SAVE_MS);
      return ok;
    },
    [toast],
  );

  // leaving the file (another one, or the view gone): what is typed goes now
  const key = file?.key;
  useEffect(() => {
    const c = cur.current;
    return () => {
      if (c && c.text !== c.saved && autoRef.current && !conflictRef.current) void write(c, { quiet: true });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, write]);
  // closing or reloading the page with typing that won't be saved on the way out (a change on disk, a failed save,
  // auto-save off): the browser asks, as the Files editor has it
  useEffect(() => {
    const ask = (e: BeforeUnloadEvent) => {
      const c = cur.current;
      if (!c || c.text === c.saved || (autoRef.current && !conflictRef.current && !errorRef.current)) return;
      e.preventDefault();
      e.returnValue = ""; // what older WebKit asks by
    };
    window.addEventListener("beforeunload", ask);
    return () => window.removeEventListener("beforeunload", ask);
  }, []);
  // the page hidden or closed: at once, sent so that it survives the page
  useEffect(() => {
    const away = () => {
      const c = cur.current;
      if (document.visibilityState === "hidden" && c && c.text !== c.saved && autoRef.current) void write(c, { quiet: true, keepalive: true });
    };
    document.addEventListener("visibilitychange", away);
    window.addEventListener("pagehide", away);
    return () => {
      document.removeEventListener("visibilitychange", away);
      window.removeEventListener("pagehide", away);
    };
  }, [write]);

  const change = useCallback(
    (text: string) => {
      const c = cur.current;
      if (!c) return;
      c.text = text;
      const unsaved = text !== c.saved;
      setState((s) => (s.unsaved === unsaved ? s : { ...s, unsaved }));
      window.clearTimeout(timer.current);
      if (unsaved && autoRef.current && !conflictRef.current) timer.current = window.setTimeout(() => void write(c, { quiet: true }), AUTO_SAVE_MS);
    },
    [write],
  );
  const save = useCallback(async (opts: { force?: boolean } = {}) => (cur.current ? write(cur.current, { force: opts.force }) : false), [write]);
  const busy = useCallback(() => !!inflight.current || (!!cur.current && cur.current.text !== cur.current.saved), []);
  // another file, or this one read again (a reload, a change on disk applied): clean
  const sha = file?.sha;
  useEffect(() => setState({ unsaved: false, conflict: false, error: null }), [key, sha]);
  return { change, save, busy, auto, ...state };
}
