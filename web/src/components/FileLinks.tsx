// The files an agent names in what it writes, as links: a tap opens one in the Files panel, shown as itself when it
// renders (Markdown, a page, a picture…), at the line when one is given (`api.ts:42`, `api.ts#L42`). A path in
// backticks, a Markdown link's or one in the prose itself; only those the server finds a file for are linked
// (fs.resolve_paths: relative to the agent's directory, or the end of one file's path in its checkout).
import type { ReactNode } from "react";
import { createContext, useContext, useEffect, useMemo } from "react";
import type { Components } from "react-markdown";
import { create } from "zustand";
import { api } from "../api";
import { useApp } from "../store";
import { basename, clsx } from "../util";
import { socket } from "../ws";

/** The conversation's project and agent directory, which its paths are taken from; none: nothing is linked. */
const Scope = createContext<{ pid: string; cwd: string } | null>(null);

/** Round an agent's conversation: the files it names in Markdown (components/ui) become links. */
export function FileLinks({ cwd, children }: { cwd: string; children: ReactNode }) {
  const pid = useApp((s) => s.project?.id);
  const scope = useMemo(() => (pid ? { pid, cwd } : null), [pid, cwd]);
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
    let end = m.index + m[0].length;
    RUN_AT.lastIndex = end;
    const at = RUN_AT.exec(text);
    if (at) end += at[0].length;
    else end -= m[0].length - m[0].replace(/[.-]+$/, "").length; // the full stop after it
    RUN.lastIndex = end;
    if (fileLike(text.slice(m.index, at ? m.index + m[0].length : end))) out.push([m.index, end]);
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
/** what the server said, by `pid \n cwd \n path`; a key missing is being asked (or is to be, again) */
const useFound = create<{ found: Record<string, Found> }>(() => ({ found: {} }));
const asked = new Set<string>(); // keys on their way to the server
const fresh = new Set<string>(); // keys whose file may have just come: past the server's cached file list
let queue: string[] = [];
let timer = 0;

const split = (key: string) => {
  const a = key.indexOf("\n");
  const b = key.indexOf("\n", a + 1);
  return { pid: key.slice(0, a), cwd: key.slice(a + 1, b), path: key.slice(b + 1) };
};

function ask(key: string) {
  if (asked.has(key)) return;
  asked.add(key);
  queue.push(key);
  timer ||= window.setTimeout(flush, 30); // the paths of every message on the page, in a request or a few
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
      const settle = (files: Record<string, Found>) => {
        batch.forEach((k) => (asked.delete(k), fresh.delete(k)));
        useFound.setState((s) => ({ found: { ...s.found, ...Object.fromEntries(batch.map((k, j) => [k, files[paths[j]] ?? null])) } }));
      };
      // a server that can't say (one from before this): nothing is linked, until it is back
      api.resolvePaths(pid, cwd, paths, batch.some((k) => fresh.has(k))).then((r) => settle(r.files), () => settle({}));
    }
}

/** Files that changed: a path that wasn't one may be now, and one that was may be gone. Asked again (those mounted). */
function forget(changed: string[], all = false) {
  const paths = new Set(changed);
  const names = new Set(changed.map(basename));
  const again: string[] = [];
  for (const [key, hit] of Object.entries(useFound.getState().found)) {
    if (hit) {
      if (paths.has(`${hit.root}/${hit.path}`)) again.push(key);
      continue;
    }
    const path = split(key).path.replace(/^(\.\/)+/, "");
    if (all || (names.has(basename(path)) && changed.some((p) => (path.startsWith("/") ? p === path : p.endsWith(`/${path}`))))) {
      again.push(key);
      fresh.add(key);
    }
  }
  if (!again.length) return;
  useFound.setState((s) => {
    const found = { ...s.found };
    again.forEach((k) => delete found[k]);
    return { found };
  });
}
useApp.subscribe((s, prev) => void (s.fsVersion !== prev.fsVersion && forget(s.fsPaths)));
socket.onReconnect(() => forget([], true)); // what changed meanwhile went unheard; a restarted server may know more

let pressed = [0, 0]; // where the last press on a file link began: a click that has moved since is a drag, selecting the path

/** A path in a reply: a link once the server has found its file, as written until then (and if there is none). */
function FileLink({ spec, plain, code, children }: { spec: string; plain: ReactNode; code?: boolean; children: ReactNode }) {
  const scope = useContext(Scope);
  const ref = parseRef(spec);
  const key = scope ? `${scope.pid}\n${scope.cwd}\n${ref.path}` : "";
  const hit = useFound((s) => (key ? s.found[key] : null));
  useEffect(() => {
    if (key && hit === undefined) ask(key);
  }, [key, hit]);
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
