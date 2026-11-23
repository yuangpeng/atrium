import type { JSX } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown, ChevronRight, CircleAlert, Code, Ellipsis, ExternalLink, Eye, EyeOff, File, FilePlus, FileSearch, Folder, FolderOpen, FolderPlus, FolderTree, Lock,
  Maximize2, Minimize2, PanelLeftClose, PanelLeftOpen, PanelTopClose, PanelTopOpen, Pencil, RefreshCw, Save, SaveAll, SaveOff, Search, Trash2, TriangleAlert, X,
} from "lucide-react";
import { api, ApiError, DirEntry, FileData } from "../api";
import { FileTarget, useApp, useAppState } from "../store";
import { basename, clsx, isMac, languageFor, MEDIA_KINDS, previewKind, shortPath, throttle } from "../util";
import { CodeEditor, FilePreview } from "../components/LazyEditors";
import type { Reveal } from "../components/Editors";
import { LockToggle, useEditLock } from "../components/EditLock";
import { Popover, useFullscreen, usePanelShown } from "../components/ui";
import { QuickOpen } from "../components/QuickOpen";
import { OpenInEditor } from "../components/OpenInEditor";
import { NO_SEARCH, SearchOptions, SearchView } from "../components/SearchView";
import { editorBridge, useEditorStatus } from "../editorStatus";
import { applyTextEdits } from "../textEdits";

interface OpenFile {
  key: string;
  pid: string; // the project it was opened in: a save still on its way when the project is left goes there
  root: string;
  path: string; // relative to root
  abs: string;
  content: string;
  saved: string;
  sha: string | null;
  mtime?: number; // the disk's version it holds, by size and modification time: a change there is read
  size?: number;
  binary: boolean;
  diskChanged: boolean;
  gone?: boolean; // deleted on disk: not saved back by itself
  error?: string; // why the last save failed (a conflict is diskChanged instead)
  external?: boolean; // outside the project (a library the language server pointed at, an agent's report in /tmp): read-only
  reveal?: Reveal;
  view?: "preview" | "source"; // a file that renders (Markdown, HTML, a CSV…): shown as itself, or as its text
}

const toReveal = (t?: FileTarget): Reveal | undefined => (t?.line ? { line: t.line, column: t.column, endColumn: t.endColumn, nonce: Date.now() } : undefined);

// auto-save: this long after the last keystroke, and at once when the file is left (another tab, panel or page)
const AUTO_SAVE_MS = 1000;
// a failed auto-save tries again after this, twice as long each time, up to a minute
const RETRY_MS = 4000;
// how often the open files are held against the disk while the panel shows, for what the file watch doesn't report: a
// directory git ignores (left unwatched on Linux, watch.py), node_modules, another machine writing to a network disk
const POLL_MS = 2000;
// a save sent as the page goes away (keepalive): browsers refuse a larger body there
const KEEPALIVE_MAX = 60_000;

// shown as itself unless the text was asked for last time, for that kind of file (someone writing Markdown keeps its
// source), or the file is opened at a line
const VIEWS_KEY = "atrium.fileViews";
const views = (): Record<string, OpenFile["view"]> => {
  try {
    return JSON.parse(localStorage.getItem(VIEWS_KEY) || "{}");
  } catch {
    return {};
  }
};
// the tree beside (or over) the open files, or put away to give them the room: in full screen too, where it stays to
// switch files from
const TREE_KEY = "atrium.filesTree";
// what the side shows: the tree, or Find in Files
const SIDE_KEY = "atrium.filesSide";
// the files opened lately, per project ({root, path}), the latest first: quick open lists them before anything is typed
const RECENT_MAX = 50;
const recentKey = (pid: string) => `atrium.recentFiles.${pid}`;
function loadRecent(pid: string): { root: string; path: string }[] {
  try {
    const v = JSON.parse(localStorage.getItem(recentKey(pid)) || "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}
/** ⌘P / ⌘⇧F (Ctrl off the Mac): Ctrl+P and Ctrl+Shift+F in a terminal are the shell's. */
function shortcut(e: KeyboardEvent, key: string, shift: boolean): boolean {
  if (e.key.toLowerCase() !== key || e.shiftKey !== shift || e.altKey || !(isMac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey)) return false;
  return isMac || !(e.target as HTMLElement | null)?.closest?.(".xterm");
}

function viewFor(path: string, target?: FileTarget): OpenFile["view"] {
  const kind = previewKind(path);
  if (!kind || MEDIA_KINDS.has(kind)) return undefined;
  return target?.line ? "source" : views()[kind] === "source" ? "source" : "preview";
}

/** Where an HTML page is served for its preview (fs.Sites), so what it loads by a relative URL comes from beside it.
 *  undefined while that is asked; null when it can't be (a folder the server won't serve, such as /tmp or home, a
 *  library from the language server, or a server from before previews). */
function useSiteUrl(pid: string, f: OpenFile | null): string | null | undefined {
  const want = !!f && f.view === "preview" && previewKind(f.path) === "html";
  const [site, setSite] = useState<{ key: string; url: string | null } | null>(null);
  const key = want ? `${f.root}::${f.path}` : "";
  useEffect(() => {
    if (!key || !f) return;
    let alive = true;
    api.fileSite(pid, f.path, f.root).then(
      (r) => alive && setSite({ key, url: r.url }),
      () => alive && setSite({ key, url: null }),
    );
    return () => void (alive = false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, key]);
  return !want ? null : site?.key === key ? site.url : undefined;
}

/** Language server state + problem counts for the open file. */
function EditorStatusLine({ path, language }: { path: string; language: string }) {
  const st = useEditorStatus((s) => s.files[path]);
  const server = useApp((s) => s.meta?.lsp?.[language]?.server);
  if (!server) return null;
  const name = (st?.server ?? server).replace(/-langserver$/, "");
  return (
    <div className="editor-status">
      <span className={clsx("lsp-dot", st?.state ?? "connecting")} />
      <span title={st?.detail}>{st?.state === "unavailable" ? `${name} unavailable` : st?.state === "ready" ? name : `starting ${name}…`}</span>
      {st?.state === "ready" && (
        <>
          <span className={clsx("count", st.errors > 0 && "err")}>
            <CircleAlert size={12} /> {st.errors}
          </span>
          <span className={clsx("count", st.warnings > 0 && "warn")}>
            <TriangleAlert size={12} /> {st.warnings}
          </span>
        </>
      )}
      <span className="grow" />
      {st?.state === "ready" && <span className="faint hide-narrow">⌘-click definition · ⇧F12 references · F2 rename · ⌘. fixes · ⇧⌘O symbols</span>}
    </div>
  );
}

export function FilesPanel() {
  const { project, fileRequest, fsVersion, fsPaths, toast } = useAppState("project", "fileRequest", "fsVersion", "fsPaths", "toast");
  const roots = useMemo(() => project?.worktrees.length ? project.worktrees.filter((w) => !w.prunable).map((w) => ({ path: w.path, label: w.main ? `${project.name} (${w.branch ?? "detached"})` : `⎇ ${w.branch ?? "detached"}` })) : project ? [{ path: project.path, label: project.name }] : [], [project]);
  const [root, setRoot] = useState<string>(project?.path ?? "");
  const [hidden, setHidden] = useState(false);
  const [children, setChildren] = useState<Record<string, DirEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["."]));
  const [open, setOpen] = useState<OpenFile[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const locked = useEditLock((s) => s.locked);
  const autoSave = useApp((s) => s.meta?.settings.ui.auto_save !== false);
  const setAutoSave = useApp((s) => s.setAutoSave);
  // full screen takes the tree along, to switch files from
  const panelRef = useRef<HTMLDivElement>(null);
  const [full, toggleFull] = useFullscreen(panelRef);
  const [treeShown, setTreeShown] = useState(() => localStorage.getItem(TREE_KEY) !== "0");
  const toggleTree = () =>
    setTreeShown((on) => {
      localStorage.setItem(TREE_KEY, on ? "0" : "1");
      return !on;
    });
  const pid = project?.id ?? "";
  const [side, setSideState] = useState<"tree" | "search">(() => (localStorage.getItem(SIDE_KEY) === "search" ? "search" : "tree"));
  const setSide = (v: "tree" | "search") => {
    localStorage.setItem(SIDE_KEY, v);
    setSideState(v);
  };
  const [search, setSearch] = useState<SearchOptions>(NO_SEARCH);
  const [searchFocus, setSearchFocus] = useState(0);
  const [quick, setQuick] = useState(false);
  const [recentAll, setRecentAll] = useState(() => loadRecent(pid));
  useEffect(() => setRecentAll(loadRecent(pid)), [pid]);

  // What saving and the disk checks go by, per open file, changed at once with every change (the state that renders
  // follows a moment later, and a save just landed, or a key just typed, must not be missed): the text in the editor,
  // the text last on disk, and the sha of that version.
  const texts = useRef(new Map<string, string>());
  const saveds = useRef(new Map<string, string>());
  const shas = useRef(new Map<string, string | null>());
  const timers = useRef(new Map<string, number>()); // auto-saves waiting for typing to stop, and retries
  const fails = useRef(new Map<string, number>()); // auto-saves failed in a row
  const inflight = useRef(new Map<string, Promise<boolean>>()); // a save on its way, one per file
  const openRef = useRef(open);
  openRef.current = open;
  const autoRef = useRef(autoSave);
  autoRef.current = autoSave;
  const textOf = (f: OpenFile) => texts.current.get(f.key) ?? f.content;
  const dirty = (f: OpenFile) => !f.external && !f.binary && textOf(f) !== (saveds.current.get(f.key) ?? f.saved);
  const track = (f: OpenFile) => {
    texts.current.set(f.key, f.content);
    saveds.current.set(f.key, f.saved);
    shas.current.set(f.key, f.sha);
  };
  const untrack = (key: string) => {
    window.clearTimeout(timers.current.get(key));
    for (const m of [texts, saveds, shas, timers, fails]) m.current.delete(key);
  };
  /** Changes an open file; what saving goes by changes with it, at once. */
  const patch = useCallback((key: string, p: Partial<OpenFile>) => {
    if (!openRef.current.some((x) => x.key === key)) return; // closed meanwhile (a save landing late)
    if (p.content !== undefined) texts.current.set(key, p.content);
    if (p.saved !== undefined) saveds.current.set(key, p.saved);
    if (p.sha !== undefined) shas.current.set(key, p.sha);
    setOpen((all) => all.map((x) => (x.key === key ? { ...x, ...p } : x)));
  }, []);

  useEffect(() => {
    setRoot(project?.path ?? "");
    setChildren({});
    setExpanded(new Set(["."]));
    setOpen([]);
    setActive(null);
    setSearch(NO_SEARCH);
    setQuick(false);
    for (const key of [...texts.current.keys()]) untrack(key);
  }, [project?.id]);
  // the worktree shown is gone (removed, or its folder deleted): back to the project's own tree, which the picker can
  // show (it has no entry for a root that isn't listed)
  useEffect(() => {
    if (project && root !== project.path && !roots.some((r) => r.path === root)) setRoot(project.path);
  }, [project, root, roots]);
  // what can't save itself goes with this panel when the project is left (the store asks first): with auto-save, a
  // conflict, a deleted file or a failed save; without it, every unsaved edit. The rest is saved on the way out.
  const setUnsaved = useApp((s) => s.setUnsaved);
  const stuck = useMemo(() => open.filter((f) => f.content !== f.saved && (!autoSave || f.diskChanged || f.gone || !!f.error)), [open, autoSave]);
  useEffect(() => setUnsaved(stuck.map((f) => basename(f.path))), [stuck, setUnsaved]);
  useEffect(() => () => setUnsaved([]), [setUnsaved]);
  // and closing or reloading the page asks too (where the browser lets a page ask)
  const stuckRef = useRef(stuck);
  stuckRef.current = stuck;
  useEffect(() => {
    const ask = (e: BeforeUnloadEvent) => {
      if (!stuckRef.current.length) return;
      e.preventDefault();
      e.returnValue = ""; // what older WebKit asks by
    };
    window.addEventListener("beforeunload", ask);
    return () => window.removeEventListener("beforeunload", ask);
  }, []);

  const loadDir = useCallback(
    async (dir: string) => {
      if (!pid || !root) return;
      try {
        const res = await api.files(pid, dir === "." ? "" : dir, root, hidden);
        setChildren((c) => ({ ...c, [dir]: res.entries }));
      } catch (e: any) {
        if (e instanceof ApiError && e.status === 404) setChildren((c) => ({ ...c, [dir]: [] }));
      }
    },
    [pid, root, hidden],
  );

  useEffect(() => {
    setChildren({});
    void loadDir(".");
    expanded.forEach((d) => d !== "." && void loadDir(d));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root, hidden, pid]);

  // ---- saving
  /** Writes what the editor holds. quiet: an auto-save (no toast, and a failure is told once). Resolves whether the
   *  file on disk now has it. */
  const save = useCallback(
    async (key: string, opts: { force?: boolean; quiet?: boolean; keepalive?: boolean } = {}): Promise<boolean> => {
      window.clearTimeout(timers.current.get(key));
      timers.current.delete(key);
      // one at a time: the next goes from the sha the last one left
      while (inflight.current.has(key)) await inflight.current.get(key);
      const f = openRef.current.find((x) => x.key === key);
      if (!f || f.external || f.binary) return false;
      if (!opts.force && (f.diskChanged || f.gone)) return false; // the banner asks first
      const text = textOf(f);
      if (!opts.force && text === (saveds.current.get(key) ?? f.saved)) return true;
      const run = (async () => {
        try {
          const body = { root: f.root, path: f.path, content: text, base_sha: shas.current.get(key) ?? f.sha, force: !!opts.force };
          const res = await api.writeFile(f.pid, body, !!opts.keepalive && text.length < KEEPALIVE_MAX);
          fails.current.delete(key);
          patch(key, { saved: text, sha: res.sha, mtime: res.mtime, size: res.size, diskChanged: false, gone: false, error: undefined });
          if (!opts.quiet) toast(`Saved ${basename(f.path)}`, "ok");
          return true;
        } catch (e: any) {
          if (e instanceof ApiError && e.status === 409) {
            patch(key, { diskChanged: true });
            return false;
          }
          const n = (fails.current.get(key) ?? 0) + 1;
          fails.current.set(key, n);
          if (!opts.quiet || n === 1) toast(`${basename(f.path)} not saved: ${e.message}`, "error");
          patch(key, { error: e.message });
          // an auto-save keeps trying (the Save button shows meanwhile)
          if (opts.quiet && autoRef.current && !opts.keepalive)
            timers.current.set(key, window.setTimeout(() => void saveRef.current(key, { quiet: true }), Math.min(RETRY_MS * 2 ** (n - 1), 60_000)));
          return false;
        }
      })();
      inflight.current.set(key, run);
      const ok = await run;
      inflight.current.delete(key);
      // typed on while it was on its way: that goes next, after the usual pause
      if (ok && autoRef.current && openRef.current.some((x) => x.key === key) && texts.current.get(key) !== text) schedule(key);
      return ok;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [patch, toast],
  );
  const saveRef = useRef(save);
  saveRef.current = save;
  /** An auto-save once typing has stopped for a moment. */
  const schedule = (key: string) => {
    window.clearTimeout(timers.current.get(key));
    timers.current.set(key, window.setTimeout(() => void saveRef.current(key, { quiet: true }), AUTO_SAVE_MS));
  };
  /** Saves now what is waiting to be auto-saved: the file is left, or the page goes away. */
  const flush = useCallback((keepalive = false) => {
    if (!autoRef.current) return;
    for (const f of openRef.current) {
      if (f.diskChanged || f.gone || !dirty(f)) continue;
      void saveRef.current(f.key, { quiet: true, keepalive });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // another file shown: the one left is saved (as is anything else waiting)
  useEffect(() => flush(), [active, flush]);
  // the project left: its files are saved before they go (cleanups run before the reset above)
  useEffect(() => () => flush(), [project?.id, flush]);
  useEffect(() => {
    const away = () => document.visibilityState === "hidden" && flush(true);
    const leave = () => flush(true);
    document.addEventListener("visibilitychange", away);
    window.addEventListener("pagehide", leave);
    return () => {
      document.removeEventListener("visibilitychange", away);
      window.removeEventListener("pagehide", leave);
    };
  }, [flush]);
  useEffect(() => () => timers.current.forEach((t) => window.clearTimeout(t)), []);

  // ---- the disk
  /** The disk's version of an open file, just read: taken in where the editor holds no edits of its own, else the
   *  banner asks. */
  const reconcile = useCallback(
    (key: string, d: FileData) => {
      if (inflight.current.has(key)) return; // a save of ours landing: its answer brings the sha
      const f = openRef.current.find((x) => x.key === key);
      if (!f) return;
      const disk = { mtime: d.mtime, size: d.size, gone: false };
      if (d.sha === shas.current.get(key)) return patch(key, disk); // nothing new, or what we wrote
      const content = d.content ?? "";
      if (f.binary || d.binary) return patch(key, { ...disk, binary: d.binary, content, saved: content, sha: d.sha }); // the preview follows
      const text = textOf(f);
      if (content === text) return patch(key, { ...disk, saved: content, sha: d.sha, diskChanged: false });
      if (text === (saveds.current.get(key) ?? f.saved)) return patch(key, { ...disk, content, saved: content, sha: d.sha, diskChanged: false });
      patch(key, { ...disk, diskChanged: true });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [patch],
  );
  const refetch = useCallback(
    (f: OpenFile) =>
      void api.file(f.pid, f.path, f.root).then(
        (d) => reconcile(f.key, d),
        (e) => e instanceof ApiError && e.status === 404 && openRef.current.some((x) => x.key === f.key) && patch(f.key, { gone: true }),
      ),
    [reconcile, patch],
  );

  // A language server's edits in a file the editor doesn't show (a rename, a quick fix: lsp/client.ts). An open tab
  // takes them in its text, and auto-save writes it (without, it stays unsaved, to look at first); a file that isn't
  // open is read, changed and written back, refused if it changed on disk meanwhile.
  useEffect(() => {
    editorBridge.editFile = async (abs, edits) => {
      const f = openRef.current.find((x) => x.abs === abs && !x.external);
      if (f) {
        if (f.binary) throw new Error("not a text file");
        const next = applyTextEdits(textOf(f), edits);
        texts.current.set(f.key, next);
        setOpen((all) => all.map((x) => (x.key === f.key ? { ...x, content: next } : x)));
        if (autoRef.current && !f.diskChanged && !f.gone) schedule(f.key);
        return next;
      }
      const owner = [...roots].sort((a, b) => b.path.length - a.path.length).find((r) => abs === r.path || abs.startsWith(r.path + "/"));
      if (!owner) throw new Error("outside the project");
      const rel = abs.slice(owner.path.length + 1);
      // a file that isn't there yet is made (a refactor that moves code to a new file writes into it)
      const d = await api.file(pid, rel, owner.path).catch((e) => {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      });
      if (d && (d.binary || d.content == null)) throw new Error("not a text file");
      const next = applyTextEdits(d?.content ?? "", edits);
      await api.writeFile(pid, { root: owner.path, path: rel, content: next, base_sha: d?.sha ?? null });
      return next;
    };
    return () => void (editorBridge.editFile = undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, roots]);

  // keep the tree and open buffers in sync with the disk
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const refreshTree = useMemo(() => throttle(() => expandedRef.current.forEach((d) => void loadDir(d)), 1200), [loadDir]);
  // behind another panel, the tree is listed again once it is shown
  const shown = usePanelShown("files");
  const stale = useRef(false);
  useEffect(() => {
    if (!shown || !stale.current) return;
    stale.current = false;
    refreshTree();
  }, [shown, refreshTree]);
  // put away, the panel saves what it has (as when another file is shown)
  useEffect(() => void (!shown && flush()), [shown, flush]);
  useEffect(() => {
    if (!fsVersion) return;
    if (shown) refreshTree();
    else stale.current = true;
    const changed = new Set(fsPaths);
    for (const f of openRef.current) if (!f.external && changed.has(f.abs)) refetch(f);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsVersion]);
  // The watch doesn't see everything (above): while the open files can be seen, they are held against the disk every
  // few seconds, by size and modification time, and read again when either moved; at once when the page comes back.
  useEffect(() => {
    if (!shown || !pid) return;
    let busy = false;
    const tick = async () => {
      const files = openRef.current.filter((f) => f.pid === pid);
      if (busy || !files.length || document.visibilityState !== "visible") return;
      busy = true;
      try {
        const { stats } = await api.fileStats(pid, files.map((f) => ({ root: f.root, path: f.path })));
        files.forEach((was, i) => {
          const f = openRef.current.find((x) => x.key === was.key);
          const s = stats[i];
          if (!f || inflight.current.has(f.key)) return;
          if (!s) {
            if (!f.gone) patch(f.key, { gone: true });
          } else if (f.gone || s.mtime !== f.mtime || s.size !== f.size) refetch(f);
        });
      } catch {
        /* the next tick asks again */
      } finally {
        busy = false;
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), POLL_MS);
    const back = () => document.visibilityState === "visible" && void tick();
    document.addEventListener("visibilitychange", back);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", back);
    };
  }, [shown, pid, patch, refetch]);

  const openFile = useCallback(
    async (rootPath: string, rel: string, target?: FileTarget) => {
      const key = `${rootPath}::${rel}`;
      const reveal = toReveal(target);
      const existing = open.find((f) => f.key === key);
      if (existing) {
        setActive(key);
        if (reveal) setOpen((cur) => cur.map((f) => (f.key === key ? { ...f, reveal, view: f.view && "source" } : f)));
        return;
      }
      try {
        const d = await api.file(pid, rel, rootPath);
        // outside the project (the server says so): read-only, by its own path. Opened again, it is read again: the
        // watcher doesn't look there, and an agent may have rewritten it since
        const disk = { pid, content: d.content ?? "", saved: d.content ?? "", sha: d.sha, mtime: d.mtime, size: d.size, binary: d.binary, diskChanged: false, reveal };
        const f: OpenFile = d.external
          ? { ...disk, key: `external::${d.abs}`, root: "", path: d.abs, abs: d.abs, external: true, view: d.binary ? undefined : viewFor(d.abs, target) }
          : { ...disk, key, root: rootPath, path: d.path, abs: d.abs, view: d.binary ? undefined : viewFor(d.path, target) };
        if (f.external || !openRef.current.some((x) => x.key === f.key)) track(f);
        setOpen((cur) => {
          const was = cur.find((x) => x.key === f.key);
          if (!was) return [...cur, f];
          // a file outside the project is read again (nothing typed in it to lose), in the view it was left in
          return f.external ? cur.map((x) => (x === was ? { ...f, view: reveal ? f.view : was.view } : x)) : cur;
        });
        setActive(f.key);
      } catch (e: any) {
        toast(e.message, "error");
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [open, pid, toast],
  );

  const openExternal = (abs: string, content: string, target?: FileTarget) => {
    const key = `external::${abs}`;
    const reveal = toReveal(target);
    setOpen((cur) =>
      cur.some((f) => f.key === key)
        ? cur.map((f) => (f.key === key ? { ...f, reveal: reveal ?? f.reveal } : f))
        : [...cur, { key, pid, root: "", path: abs, abs, content, saved: content, sha: null, binary: false, diskChanged: false, external: true, reveal, view: viewFor(abs, target) }],
    );
    setActive(key);
  };

  // external "open this file" requests (from chat, changes, git)
  useEffect(() => {
    if (!fileRequest || !project) return;
    if (fileRequest.external && fileRequest.content != null) return openExternal(fileRequest.path, fileRequest.content, fileRequest);
    let p = fileRequest.path;
    let r = fileRequest.root ?? root;
    if (p.startsWith("/")) {
      const owner = [...roots].sort((a, b) => b.path.length - a.path.length).find((x) => p === x.path || p.startsWith(x.path + "/"));
      if (owner) {
        r = owner.path;
        p = p.slice(owner.path.length + 1);
      } else {
        // outside the project (or in it by another name, /tmp for /private/tmp): the server finds it by its own path,
        // whatever the root (an agent's cwd may be a worktree removed since), and the tree stays where it is
        void openFile(project.path, p, fileRequest);
        return;
      }
    } else if (fileRequest.root && fileRequest.root !== root) {
      r = fileRequest.root;
    }
    if (r !== root && roots.some((x) => x.path === r)) setRoot(r);
    if (p) void openFile(r, p, fileRequest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileRequest?.nonce]);

  const cur = open.find((f) => f.key === active) ?? null;
  const kind = cur ? previewKind(cur.path) : null;
  const media = !!kind && MEDIA_KINDS.has(kind);
  const previewing = media || cur?.view === "preview";
  const site = useSiteUrl(pid, cur);
  // to see it in a tab of its own (as saved): a page at its served URL, a PDF or a picture as itself
  const newTab = !cur || !previewing ? null : kind === "html" ? site : kind === "svg" || media ? api.rawFileUrl(pid, cur.path, cur.root, cur.sha) : null;
  const setView = (f: OpenFile, view: OpenFile["view"]) => {
    setOpen((all) => all.map((x) => (x.key === f.key ? { ...x, view } : x)));
    const kind = previewKind(f.path);
    if (kind) localStorage.setItem(VIEWS_KEY, JSON.stringify({ ...views(), [kind]: view }));
  };

  // the file shown goes first in quick open's list
  useEffect(() => {
    if (!cur || cur.external || !pid) return;
    setRecentAll((all) => {
      const next = [{ root: cur.root, path: cur.path }, ...all.filter((r) => !(r.root === cur.root && r.path === cur.path))].slice(0, RECENT_MAX);
      localStorage.setItem(recentKey(pid), JSON.stringify(next));
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur?.key, pid]);
  const recent = useMemo(() => recentAll.filter((r) => r.root === root).map((r) => r.path), [recentAll, root]);

  /** Find in Files, with the field focused. seed (⌘⇧F): the text selected in the editor (on one line) to look for. */
  const findInFiles = (seed = false) => {
    const picked = seed ? editorBridge.selectedText?.() : undefined;
    if (picked && !picked.includes("\n") && picked.length <= 200) setSearch((o) => ({ ...o, query: picked }));
    setSide("search");
    if (!treeShown) toggleTree();
    setSearchFocus(Date.now());
    useApp.getState().setPanel("files");
  };
  const findRef = useRef(findInFiles);
  findRef.current = findInFiles;
  // ⌘P: quick open, ⌘⇧F: Find in Files — from anywhere in the project (the chat, a terminal on the Mac)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (shortcut(e, "p", false)) {
        e.preventDefault(); // not the browser's Print
        setQuick(true);
      } else if (shortcut(e, "f", true)) {
        e.preventDefault();
        findRef.current(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const quickPick = (path: string, line?: number, column?: number) => {
    setQuick(false);
    useApp.getState().setPanel("files");
    void openFile(root, path, line ? { line, column } : undefined);
  };

  const reload = async (f: OpenFile) => {
    const d = await api.file(f.pid, f.path, f.root);
    patch(f.key, { content: d.content ?? "", saved: d.content ?? "", sha: d.sha, mtime: d.mtime, size: d.size, diskChanged: false, gone: false, error: undefined });
  };

  const toggleAutoSave = () => {
    setAutoSave(!autoSave);
    // turned on: what is unsaved goes now
    if (!autoSave) for (const f of openRef.current) if (!f.diskChanged && !f.gone && dirty(f)) void save(f.key, { quiet: true });
  };

  const close = async (key: string) => {
    const f = openRef.current.find((x) => x.key === key);
    if (f && dirty(f)) {
      // with auto-save, closing saves it; what can't be saved is asked about, as without
      const kept = autoRef.current && !f.diskChanged && !f.gone && (await save(key, { quiet: true }));
      if (!kept && !confirm(`Discard unsaved changes to ${basename(f.path)}?`)) return;
    }
    untrack(key);
    const rest = openRef.current.filter((x) => x.key !== key);
    setOpen((all) => all.filter((x) => x.key !== key));
    setActive((a) => (a === key ? rest[rest.length - 1]?.key ?? null : a));
    if (f) editorBridge.closeFile?.(f.abs);
  };

  const create = async (dir: string, kind: "file" | "dir") => {
    const name = prompt(kind === "file" ? "New file name" : "New folder name");
    if (!name) return;
    const path = dir === "." ? name : `${dir}/${name}`;
    try {
      await api.createFile(pid, { root, path, kind });
      await loadDir(dir);
      setExpanded((e) => new Set(e).add(dir));
      if (kind === "file") void openFile(root, path);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const rename = async (entry: DirEntry) => {
    const dst = prompt("Rename to", entry.path);
    if (!dst || dst === entry.path) return;
    try {
      await api.renameFile(pid, { root, src: entry.path, dst });
      refreshTree();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const remove = async (entry: DirEntry) => {
    if (!confirm(`Move ${entry.path} to .atrium/trash?`)) return;
    try {
      await api.deleteFile(pid, { root, path: entry.path });
      refreshTree();
      const gone = (f: OpenFile) => f.root === root && (f.path === entry.path || f.path.startsWith(entry.path + "/"));
      openRef.current.filter(gone).forEach((f) => untrack(f.key));
      setOpen((all) => all.filter((f) => !gone(f)));
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const toggleDir = (path: string) => {
    setExpanded((e) => {
      const n = new Set(e);
      if (n.has(path)) n.delete(path);
      else {
        n.add(path);
        if (!children[path]) void loadDir(path);
      }
      return n;
    });
  };

  const renderDir = (dir: string, depth: number): JSX.Element[] =>
    (children[dir] ?? []).flatMap((e) => {
      const isOpen = expanded.has(e.path);
      const row = (
        <div
          key={e.path}
          className={clsx("tree-row", cur?.root === root && cur?.path === e.path && "on")}
          style={{ paddingLeft: 6 + depth * 14 }}
          onClick={() => (e.type === "dir" ? toggleDir(e.path) : void openFile(root, e.path))}
          title={e.path}
        >
          <span className="chev">{e.type === "dir" ? isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} /> : null}</span>
          <span className="fico">{e.type === "dir" ? isOpen ? <FolderOpen size={14} /> : <Folder size={14} /> : <File size={13} />}</span>
          <span className="nm">{e.name}</span>
          <span className="hover-actions" onClick={(ev) => ev.stopPropagation()}>
            {e.type === "dir" && (
              <button className="icon-btn sm" title="New file here" onClick={() => void create(e.path, "file")}>
                <FilePlus size={12} />
              </button>
            )}
            <button className="icon-btn sm" title="Rename / move" onClick={() => void rename(e)}>
              <Pencil size={12} />
            </button>
            <button className="icon-btn sm" title="Delete (to trash)" onClick={() => void remove(e)}>
              <Trash2 size={12} />
            </button>
          </span>
          {e.git && <span className={`g ${e.git}`}>{e.git}</span>}
        </div>
      );
      return e.type === "dir" && isOpen ? [row, ...renderDir(e.path, depth + 1)] : [row];
    });

  if (!project) return null;
  const editable = !!cur && !cur.external && !cur.binary;

  const rootLabel = roots.find((r) => r.path === root)?.label ?? project.name;
  return (
    <div ref={panelRef} className="panel-body fs-host">
      {quick && (
        <QuickOpen
          pid={pid}
          root={root}
          rootLabel={rootLabel}
          fsVersion={fsVersion}
          recent={recent}
          current={cur && !cur.external && cur.root === root ? cur.path : null}
          onPick={quickPick}
          onClose={() => setQuick(false)}
        />
      )}
      {/* with no file open there is nothing to make room for, and no bar to bring the tree back from */}
      <div className={clsx("split stack-files", open.length > 0 && !treeShown && "tree-off")}>
        <div className={clsx("side", side === "search" && "searching")}>
          <div className="toolbar files-bar" style={{ padding: "6px 8px", gap: 2, flexWrap: "nowrap" }}>
            <div className="seg icons">
              <button className={clsx(side === "tree" && "on")} aria-label="File tree" aria-pressed={side === "tree"} data-tip="File tree" onClick={() => setSide("tree")}>
                <FolderTree size={14} />
              </button>
              <button className={clsx(side === "search" && "on")} aria-label="Search in files" aria-pressed={side === "search"} data-tip="Search in files (⌘⇧F)" onClick={() => findInFiles()}>
                <Search size={14} />
              </button>
            </div>
            {roots.length > 1 ? (
              <select className="select" style={{ height: 28, fontSize: 12.5, flex: 1, minWidth: 0 }} value={root} onChange={(e) => setRoot(e.target.value)}>
                {roots.map((r) => (
                  <option key={r.path} value={r.path}>
                    {r.label}
                  </option>
                ))}
              </select>
            ) : (
              <span className="title grow ellipsis" style={{ paddingLeft: 4 }}>{project.name}</span>
            )}
            <button className="icon-btn sm" aria-label="Go to file" data-tip="Go to file (⌘P)" onClick={() => setQuick(true)}>
              <FileSearch size={14} />
            </button>
            {side === "tree" && (
              <>
                <button className="icon-btn sm" aria-label="New file" data-tip="New file" onClick={() => void create(".", "file")}>
                  <FilePlus size={14} />
                </button>
                <Popover
                  align="right"
                  within=".workspace"
                  trigger={(isOpen, toggle) => (
                    <button className={clsx("icon-btn sm", isOpen && "active")} aria-label="More" data-tip="New folder, hidden files, refresh" onClick={toggle}>
                      <Ellipsis size={14} />
                    </button>
                  )}
                >
                  {(closeMenu) => (
                    <>
                      <button className="menu-item" onClick={() => (closeMenu(), void create(".", "dir"))}>
                        <FolderPlus size={14} /> New folder
                      </button>
                      <button className="menu-item" onClick={() => (closeMenu(), setHidden((h) => !h))}>
                        {hidden ? <EyeOff size={14} /> : <Eye size={14} />} {hidden ? "Hide .git / .atrium" : "Show hidden"}
                      </button>
                      <button className="menu-item" onClick={() => (closeMenu(), refreshTree())}>
                        <RefreshCw size={13} /> Refresh
                      </button>
                    </>
                  )}
                </Popover>
              </>
            )}
          </div>
          {side === "tree" && <div className="tree">{renderDir(".", 0)}</div>}
          {/* kept while the tree shows: its query and results stay */}
          <div className="search-slot" hidden={side !== "search"}>
            <SearchView
              pid={pid}
              root={root}
              opts={search}
              setOpts={setSearch}
              focus={searchFocus}
              onOpen={(path, line, column, endColumn) => void openFile(root, path, { line, column, endColumn })}
            />
          </div>
        </div>
        <div className="content">
          {open.length > 0 && (
            <div className="editor-bar">
              {/* beside the open files the tree is on their left; over them, on a narrow panel, above (CSS) */}
              <div className="editor-lead">
                <button className="icon-btn sm tree-toggle" aria-label={treeShown ? "Hide the files" : "Show the files"} data-tip={treeShown ? "Hide the files" : "Show the files"} onClick={toggleTree}>
                  {treeShown ? <PanelLeftClose size={14} className="beside" /> : <PanelLeftOpen size={14} className="beside" />}
                  {treeShown ? <PanelTopClose size={14} className="above" /> : <PanelTopOpen size={14} className="above" />}
                </button>
              </div>
              <div className="editor-tabs">
                {open.map((f) => (
                  <div key={f.key} className={clsx("editor-tab", f.key === active && "on", f.external && "external", f.gone && "gone")} onClick={() => setActive(f.key)} title={f.abs}>
                    {f.external && <Lock size={11} className="faint" />}
                    {basename(f.path)}
                    {!f.external && f.root !== project.path && <span className="faint mono" style={{ fontSize: 10.5 }}>⎇</span>}
                    {f.content !== f.saved ? <span className={clsx("dirty", (f.diskChanged || f.gone || f.error) && "stuck")} /> : null}
                    <button className="icon-btn sm" aria-label="Close" onClick={(e) => (e.stopPropagation(), void close(f.key))}>
                      <X size={12} />
                    </button>
                  </div>
                ))}
              </div>
              <div className="editor-acts">
                {/* a phone has no ⌘S; with auto-save, it shows when a save failed */}
                {editable && cur.content !== cur.saved && (!autoSave || cur.error) && (
                  <button className="icon-btn sm" aria-label="Save" data-tip="Save (⌘S)" onClick={() => void save(cur.key)}>
                    <Save size={14} />
                  </button>
                )}
                {cur?.view && (
                  <button
                    className="icon-btn sm"
                    aria-label={previewing ? "Show the source" : "Show the preview"}
                    data-tip={previewing ? "Source" : "Preview"}
                    onClick={() => setView(cur, previewing ? "source" : "preview")}
                  >
                    {previewing ? <Code size={14} /> : <Eye size={14} />}
                  </button>
                )}
                {editable && !previewing && (
                  <button
                    className={clsx("icon-btn sm auto-save", autoSave && "active")}
                    aria-pressed={autoSave}
                    aria-label={autoSave ? "Auto-save on" : "Auto-save off"}
                    data-tip={autoSave ? "Auto-save on: files save as you type — tap for ⌘S instead" : "Auto-save off: ⌘S saves — tap to save as you type"}
                    onClick={toggleAutoSave}
                  >
                    {autoSave ? <SaveAll size={14} /> : <SaveOff size={14} />}
                  </button>
                )}
                {editable && !previewing && <LockToggle />}
                {cur && !cur.binary && <OpenInEditor abs={cur.abs} />}
                {newTab && (
                  <a className="icon-btn sm" href={newTab} target="_blank" rel="noopener noreferrer" aria-label="Open in a new tab" data-tip="Open in a new tab (as saved)">
                    <ExternalLink size={14} />
                  </a>
                )}
                <button className={clsx("icon-btn sm", full && "active")} aria-label={full ? "Exit full screen" : "Full screen"} data-tip={full ? "Exit full screen (Esc)" : "Full screen"} onClick={toggleFull}>
                  {full ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
                </button>
              </div>
            </div>
          )}
          {cur?.gone ? (
            <div className="banner">
              <span className="grow">This file was deleted on disk.</span>
              {!cur.external && (
                <button className="btn btn-sm" onClick={() => void save(cur.key, { force: true })}>
                  Save it again
                </button>
              )}
              <button className="btn btn-sm" onClick={() => void close(cur.key)}>
                Close
              </button>
            </div>
          ) : (
            cur?.diskChanged && (
              <div className="banner">
                <span className="grow">This file changed on disk (an agent may have edited it).</span>
                <button className="btn btn-sm" onClick={() => void reload(cur)}>
                  Reload from disk
                </button>
                <button className="btn btn-sm" onClick={() => void save(cur.key, { force: true })}>
                  Keep mine & overwrite
                </button>
              </div>
            )
          )}
          {cur?.external && (
            <div className="banner subtle">
              <Lock size={13} />
              <span className="grow ellipsis mono" title={cur.abs}>
                Read-only · outside the project · {shortPath(cur.abs)}
              </span>
            </div>
          )}
          <div className="editor-host">
            {cur && previewing && (
              <FilePreview kind={kind!} pid={pid} root={cur.root} path={cur.path} content={cur.content} version={cur.sha} site={site} />
            )}
            {cur ? (
              media ? null : cur.binary ? (
                <div className="empty-state">Binary file — not shown.</div>
              ) : (
                // kept while the preview shows: its undo history, cursor and scroll stay
                <div className="editor-slot" hidden={previewing}>
                  <CodeEditor
                    filePath={cur.abs}
                    value={cur.content}
                    language={languageFor(cur.path)}
                    readOnly={cur.external || locked}
                    reveal={cur.reveal}
                    onChange={(v) => {
                      texts.current.set(cur.key, v);
                      setOpen((all) => all.map((x) => (x.key === cur.key ? { ...x, content: v } : x)));
                      if (autoRef.current && !cur.external) schedule(cur.key);
                    }}
                    // with auto-save it just saves now, without a word
                    onSave={() => void save(cur.key, { quiet: autoRef.current })}
                  />
                </div>
              )
            ) : (
              <div className="empty-state">
                <File size={28} />
                <h3>Pick a file to edit</h3>
                <div>
                  {autoSave ? (
                    <>Edits save themselves as you type</>
                  ) : (
                    <>
                      Edits are yours — <span className="kbd">⌘S</span> or <Save size={11} /> saves
                    </>
                  )}
                  {locked && <>, once the lock is open</>}. Agent edits to open files show up live.
                </div>
              </div>
            )}
          </div>
          {cur && !cur.binary && !previewing && <EditorStatusLine path={cur.abs} language={languageFor(cur.path)} />}
        </div>
      </div>
    </div>
  );
}
