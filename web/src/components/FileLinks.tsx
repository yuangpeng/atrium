// The files an agent names in what it writes, as links: a tap opens one in the Files panel, shown as itself when it
// renders (Markdown, a page, a picture…), at the line when one is given (`api.ts:42`, `api.ts#L42`). A path in
// backticks, a Markdown link's or one in the prose itself; only those the server finds a file for are linked
// (fs.resolve_paths: relative to the agent's directory, or the end of one file's path in its checkout).
import type { ReactNode } from "react";
import { createContext, useContext, useEffect, useMemo, useRef } from "react";
import type { Components } from "react-markdown";
import { create } from "zustand";
import { api } from "../api";
import { useApp } from "../store";
import { basename, clsx } from "../util";
import { socket } from "../ws";

/** The conversation's project and agent directory, which its paths are taken from; none: nothing is linked. */
const Scope = createContext<{ pid: string; cwd: string } | null>(null);

/** Round an agent's conversation: the files it names in Markdown (components/ui) become links. When its turn ends
 *  (`running` goes), its paths are asked again: it may have named a file before writing it, where the watcher doesn't
 *  look (build/, dist/…). */
export function FileLinks({ cwd, running, children }: { cwd: string; running?: boolean; children: ReactNode }) {
  const pid = useApp((s) => s.project?.id);
  const scope = useMemo(() => (pid ? { pid, cwd } : null), [pid, cwd]);
  const was = useRef(running);
  useEffect(() => {
    if (was.current && !running && pid) recheck(`${pid}\n${cwd}\n`);
    was.current = running;
  }, [running, pid, cwd]);
  return <Scope.Provider value={scope}>{children}</Scope.Provider>;
}

export const useFileLinks = () => useContext(Scope) !== null;

// ---------------------------------------------------------------------------- what reads as a path
/** where in the file: `:12`, `:12:5`, `:12-20`, `#L12`, `#L12C5`, `#L12-L20` */
const AT = /(?::(\d+)(?::(\d+))?(?:-\d+)?|#L(\d+)(?:C(\d+))?(?:-L?\d+(?:C\d+)?)?)$/;
const NAMES = new Set(["Makefile", "Dockerfile", "LICENSE", "README", "Procfile", "Gemfile", "Rakefile", "Justfile", "Vagrantfile", "CODEOWNERS"]);

function parseRef(spec: string): { path: string; line?: number; column?: number } {
  const m = AT.exec(spec);
  if (!m || m.index === 0) return { path: spec };
  return { path: spec.slice(0, m.index), line: Number(m[1] ?? m[3]) || undefined, column: Number(m[2] ?? m[4]) || undefined };
}

/** Could it name a file: a path with a name at its end, a name with an extension, or one of the usual ones without. */
function fileLike(path: string): boolean {
  if (path.length < 2 || path.length > 300 || !/[A-Za-z]/.test(path) || path.includes("://")) return false;
  const name = basename(path);
  return path.includes("/") ? !path.endsWith("/") : /\.[A-Za-z][\w-]*$/.test(name) || NAMES.has(name);
}

/** What is in backticks, if all of it is a path (a command with one in it is not) */
function codeRef(code: string): string | null {
  const s = code.trim();
  if (!s || s.length > 300 || /[\n\t`]/.test(s)) return null;
  const { path } = parseRef(s);
  return fileLike(path) && (!/\s/.test(path) || path.includes("/")) ? s : null;
}

/** A Markdown link's target, if it is a file rather than a page: no scheme (but file:; `api.ts:42` is a line, not
 *  one), and not a heading's #id */
function linkRef(href: string): string | null {
  const file = href.startsWith("file://");
  if (!file && /^([a-z][a-z\d+.-]*:(?!\d+(:\d+)?$)|\/\/|#|\?)/i.test(href)) return null;
  let s = (file ? href.slice(7) : href).replace(/\?[^#]*/, "").replace(/#(?!L\d).*$/, "");
  try {
    s = decodeURIComponent(s);
  } catch {
    /* as written */
  }
  return !/[\n\0]/.test(s) && fileLike(parseRef(s).path) ? s : null;
}

// a run of what paths are written with (ASCII: the words round one, in Chinese too, are not part of it), then where
const RUN = /[\w.~@+%/-]+/g;
const RUN_AT = /:\d+(?::\d+)?(?:-\d+)?|#L\d+(?:C\d+)?(?:-L\d+)?/y;

/** The paths in a run of prose, as [start, end) */
function bareRefs(text: string): [number, number][] {
  const out: [number, number][] = [];
  RUN.lastIndex = 0;
  for (let m; (m = RUN.exec(text)); ) {
    RUN_AT.lastIndex = m.index + m[0].length;
    const at = RUN_AT.exec(text);
    // on from past all of it, never back: what is cut off below (a full stop, a dash) starts no path, and a search
    // that went back to it would find it again, for ever
    RUN.lastIndex = m.index + m[0].length + (at ? at[0].length : 0);
    const path = at ? m[0] : m[0].replace(/[.-]+$/, ""); // "see foo.ts." links foo.ts
    if (path && fileLike(path)) out.push([m.index, m.index + path.length + (at ? at[0].length : 0)]);
  }
  return out;
}

// ---------------------------------------------------------------------------- Markdown
type MdNode = { type: string; value?: string; url?: string; children?: MdNode[]; data?: { hName?: string; hProperties?: Record<string, unknown> } };
const OPAQUE = new Set(["link", "linkReference", "code", "html", "definition", "image", "imageReference"]);

const mark = (node: MdNode, spec: string) => void (node.data = { ...node.data, hProperties: { ...node.data?.hProperties, dataPath: spec } });

function splitText(node: MdNode): MdNode[] {
  const text = node.value ?? "";
  const out: MdNode[] = [];
  let at = 0;
  for (const [start, end] of bareRefs(text)) {
    if (start > at) out.push({ type: "text", value: text.slice(at, start) });
    out.push({ type: "text", value: text.slice(start, end), data: { hName: "a", hProperties: { dataPath: text.slice(start, end) } } });
    at = end;
  }
  if (!out.length) return [node];
  if (at < text.length) out.push({ type: "text", value: text.slice(at) });
  return out;
}

function walk(node: MdNode) {
  if (node.type === "inlineCode") {
    const spec = codeRef(node.value ?? "");
    if (spec) mark(node, spec);
  } else if (node.type === "link") {
    const spec = linkRef(node.url ?? "");
    if (spec) mark(node, spec);
  } else if (node.children && !OPAQUE.has(node.type)) {
    node.children = node.children.flatMap((c) => (c.type === "text" ? splitText(c) : (walk(c), [c])));
  }
}

/** remark: puts a data-path on what may be a file (its <a> or <code>), for FILE_COMPONENTS to link it */
export const remarkFileRefs = () => (tree: MdNode) => walk(tree);

type Marked = { "data-path"?: string; children?: ReactNode };

export const FILE_COMPONENTS: Components = {
  a: ({ node: _n, ...props }) => {
    const { "data-path": spec, ...rest } = props as typeof props & Marked;
    const plain = rest.href ? <a {...rest} target="_blank" rel="noreferrer" /> : <>{rest.children}</>; // a path in the prose has none
    return spec ? <FileLink spec={spec} plain={plain}>{rest.children}</FileLink> : plain;
  },
  code: ({ node: _n, ...props }) => {
    const { "data-path": spec, ...rest } = props as typeof props & Marked;
    const code = <code {...rest} />;
    return spec ? <FileLink spec={spec} plain={code} code>{code}</FileLink> : code;
  },
};

// ---------------------------------------------------------------------------- which are files
type Found = { root: string; path: string } | null;
/** what the server said, by `pid \n cwd \n path` (a key missing: not answered yet); an answer stays shown while it
 *  is asked again, so a link doesn't blink each time its file is saved */
const useFound = create<{ found: Record<string, Found> }>(() => ({ found: {} }));
const STALE = 10_000; // ms an answer is trusted for a link that mounts: the watcher can't vouch for build/, dist/…
const answered = new Map<string, number>(); // key -> when its answer came; none: asked again when next shown
const shown = new Map<string, number>(); // key -> how many links on the page have it
const asked = new Set<string>(); // keys on their way to the server
const again = new Set<string>(); // ... asked again since: the answer on its way may be from before what changed
const fresh = new Set<string>(); // keys whose file may have come where the watcher doesn't look: past the server's file list
let queue: string[] = [];
let timer = 0;

const split = (key: string) => {
  const a = key.indexOf("\n");
  const b = key.indexOf("\n", a + 1);
  return { pid: key.slice(0, a), cwd: key.slice(a + 1, b), path: key.slice(b + 1) };
};
const same = (a: Found | undefined, b: Found) => a === b || (!!a && !!b && a.root === b.root && a.path === b.path);

function request(key: string) {
  asked.add(key);
  queue.push(key);
  timer ||= window.setTimeout(flush, 30); // the paths of every message on the page, in a request or a few
}

/** Something happened that may change its answer: asked again now if a link shows it, else when one next does. */
function renew(key: string) {
  fresh.add(key);
  if (asked.has(key)) again.add(key);
  else if (shown.has(key)) request(key);
  else answered.delete(key);
}

function flush() {
  timer = 0;
  const groups = new Map<string, string[]>();
  for (const key of queue) {
    const { pid, cwd } = split(key);
    const group = groups.get(`${pid}\n${cwd}`);
    if (group) group.push(key);
    else groups.set(`${pid}\n${cwd}`, [key]);
  }
  queue = [];
  for (const keys of groups.values())
    for (let i = 0; i < keys.length; i += 200) {
      const batch = keys.slice(i, i + 200);
      const { pid, cwd } = split(batch[0]);
      const paths = batch.map((k) => split(k).path);
      const past = batch.filter((k) => fresh.delete(k)).length > 0;
      // no answer (a server from before this, or one going away): what was known stays, the rest unlinked for now
      const settle = (files: Record<string, Found> | null) => {
        useFound.setState((s) => {
          const found = { ...s.found };
          batch.forEach((k, j) => {
            const answer = files ? (files[paths[j]] ?? null) : (s.found[k] ?? null);
            if (!same(s.found[k], answer)) found[k] = answer; // the same one kept: its links needn't render again
          });
          return { found };
        });
        const now = Date.now();
        for (const k of batch) {
          asked.delete(k);
          answered.set(k, now);
          if (again.delete(k)) renew(k);
        }
      };
      api.resolvePaths(pid, cwd, paths, past).then((r) => settle(r.files), () => settle(null));
    }
}

/** Files changed (the watcher; the server's file lists already know): a path that wasn't a file may be one now, and
 *  one that was may be gone. */
function changed(paths: string[]) {
  const abs = new Set(paths);
  const names = new Set(paths.map(basename));
  for (const [key, hit] of Object.entries(useFound.getState().found)) {
    if (hit) {
      if (abs.has(`${hit.root}/${hit.path}`)) renew(key);
      continue;
    }
    const path = split(key).path.replace(/^(\.\/)+/, "");
    if (names.has(basename(path)) && paths.some((p) => (path.startsWith("/") ? p === path : p.endsWith(`/${path}`)))) renew(key);
  }
}
useApp.subscribe((s, prev) => void (s.fsVersion !== prev.fsVersion && changed(s.fsPaths)));
// what changed while the socket was down went unheard, and a restarted server may know more: all of it asked again
socket.onReconnect(() => Object.keys(useFound.getState().found).forEach(renew));

/** The paths shown for a conversation (`pid \n cwd \n`), asked again. */
function recheck(prefix: string) {
  for (const key of shown.keys()) if (key.startsWith(prefix)) renew(key);
}

let pressed = [0, 0]; // where the last press on a file link began: a click that has moved since is a drag, selecting the path

/** A path in a reply: a link once the server has found its file, as written until then (and if there is none). */
function FileLink({ spec, plain, code, children }: { spec: string; plain: ReactNode; code?: boolean; children: ReactNode }) {
  const scope = useContext(Scope);
  const ref = parseRef(spec);
  const key = scope ? `${scope.pid}\n${scope.cwd}\n${ref.path}` : "";
  const hit = useFound((s) => (key ? s.found[key] : null));
  useEffect(() => {
    if (!key) return;
    shown.set(key, (shown.get(key) ?? 0) + 1);
    // not answered yet, or long ago: a new message naming a file again is when one written since, where the watcher
    // doesn't look, is found (and the older links to it with it)
    const known = key in useFound.getState().found;
    if (!asked.has(key) && (!known || Date.now() - (answered.get(key) ?? 0) > STALE)) {
      if (known) fresh.add(key);
      request(key);
    }
    return () => {
      const n = (shown.get(key) ?? 1) - 1;
      if (n > 0) shown.set(key, n);
      else shown.delete(key);
    };
  }, [key]);
  if (!hit) return <>{plain}</>;
  const open = () => useApp.getState().openFile(hit.path, hit.root, ref.line ? { line: ref.line, column: ref.column } : undefined);
  // no tabIndex: Chrome starts no text selection in a focusable element, and paths are copied out of replies
  return (
    <a
      className={clsx("file-link", code && "code")}
      role="link"
      onMouseDown={(e) => (pressed = [e.clientX, e.clientY])}
      onClick={(e) => (e.preventDefault(), (e.detail === 0 || Math.hypot(e.clientX - pressed[0], e.clientY - pressed[1]) < 5) && open())}
    >
      {children}
    </a>
  );
}
