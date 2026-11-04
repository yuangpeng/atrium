// A file shown as itself in the Files panel: Markdown and notebooks rendered, HTML in a sandbox, CSV as a table,
// images, PDFs, video and audio. Loaded lazily (LazyEditors.tsx): it brings an HTML parser along for Markdown's HTML.
import type { MouseEvent, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown, { defaultUrlTransform, type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import { api } from "../api";
import { useApp } from "../store";
import { basename, type PreviewKind } from "../util";
import { HIGHLIGHT, Spinner } from "./ui";

export interface PreviewProps {
  kind: PreviewKind;
  pid: string;
  root: string;
  path: string; // relative to root
  content: string; // the buffer, unsaved edits included
  version: string | null; // changes when the file on disk does: a new URL for what is loaded from there
  site?: string | null; // html: where the saved page is served (fs.Sites); undefined while that is asked
}

export function FilePreview(p: PreviewProps) {
  switch (p.kind) {
    case "markdown":
      return (
        <div className="file-preview">
          <div className="md file-doc">
            <Doc text={p.content} pid={p.pid} root={p.root} path={p.path} />
          </div>
        </div>
      );
    case "notebook":
      return <Notebook {...p} />;
    case "html":
      return <HtmlPage content={p.content} site={p.site} name={basename(p.path)} />;
    case "csv":
      return <Table content={p.content} tabs={p.path.toLowerCase().endsWith(".tsv")} />;
    case "svg":
      return <Svg content={p.content} name={basename(p.path)} />;
    default:
      return <Media {...p} />;
  }
}

// ---- markdown --------------------------------------------------------------------------------------------------
// HTML in it is rendered as GitHub does (a README's centred logo, <details>), through GitHub's own allow-list: it
// runs in Atrium's page, so nothing may come through that could script it
// (and a notebook's attachment: images; a <style>'s text goes with it, not on the page)
const schema = {
  ...defaultSchema,
  strip: [...(defaultSchema.strip ?? []), "style"],
  protocols: { ...defaultSchema.protocols, src: [...(defaultSchema.protocols?.src ?? []), "attachment"] },
};

type HNode = { type: string; tagName?: string; value?: string; properties?: Record<string, unknown>; children?: HNode[] };
const textOf = (n: HNode): string => n.value ?? (n.children ?? []).map(textOf).join("");

/** ids for the headings, as GitHub makes them, so a table of contents' #links land */
function rehypeSlugs() {
  return (tree: HNode) => {
    const seen = new Map<string, number>();
    const walk = (n: HNode) => {
      if (n.tagName && /^h[1-6]$/.test(n.tagName) && !n.properties?.id) {
        const slug = textOf(n).toLowerCase().replace(/[^\p{L}\p{M}\p{N}\p{Pc}\- ]/gu, "").replace(/ /g, "-");
        const k = seen.get(slug);
        seen.set(slug, (k ?? -1) + 1);
        n.properties = { ...n.properties, id: k === undefined ? slug : `${slug}-${k + 1}` };
      }
      n.children?.forEach(walk);
    };
    walk(tree);
  };
}

type Plugins = NonNullable<Options["rehypePlugins"]>;
const PLUGINS: Plugins = [rehypeRaw, [rehypeSanitize, schema], rehypeSlugs, ...HIGHLIGHT];
const SAFE: Plugins = [rehypeRaw, [rehypeSanitize, schema]];
const REMARK = [remarkGfm];
const isUrl = (s: string) => /^([a-z][a-z\d+.-]*:|\/\/)/i.test(s);

/** a path relative to the file's folder (or, from "/", to its checkout), as one relative to the checkout */
function resolvePath(dir: string, href: string): string | null {
  const parts = href.startsWith("/") ? [] : dir.split("/").filter(Boolean);
  for (const raw of href.split("/")) {
    let seg = raw;
    try {
      seg = decodeURIComponent(raw);
    } catch {
      /* keep it as written */
    }
    if (!seg || seg === ".") continue;
    if (seg !== "..") parts.push(seg);
    else if (!parts.pop()) return null;
  }
  return parts.join("/");
}

/** a #link: the heading with that id, or the element an id of the HTML got clobbered to (sanitize's prefix) */
function jumpTo(e: MouseEvent<HTMLElement>, frag: string) {
  e.preventDefault();
  let id = frag;
  try {
    id = decodeURIComponent(frag);
  } catch {
    /* as written */
  }
  const box = e.currentTarget.closest(".file-preview");
  const el = box?.querySelector(`#${CSS.escape(id)}`) ?? box?.querySelector(`#${CSS.escape(`user-content-${id}`)}`);
  el?.scrollIntoView({ block: "start" });
}

function Doc({ text, pid, root, path, attachments }: { text: string; pid: string; root: string; path: string; attachments?: Record<string, Record<string, string>> }) {
  const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  const components = useMemo<Components>(() => {
    const src = (s?: string) => {
      if (!s) return s;
      if (s.startsWith("attachment:")) {
        const [mime, data] = Object.entries(attachments?.[s.slice(11)] ?? {})[0] ?? [];
        return mime ? `data:${mime};base64,${String(data).replace(/\s/g, "")}` : undefined;
      }
      const rel = isUrl(s) ? null : resolvePath(dir, s.split(/[?#]/)[0]);
      return rel === null ? s : api.rawFileUrl(pid, rel, root);
    };
    return {
      img: ({ node: _n, src: s, ...props }) => <img {...props} src={src(s)} loading="lazy" />,
      a: ({ node: _n, href, ...props }) => {
        if (!href) return <a {...props} />;
        if (href.startsWith("#")) return <a {...props} href={href} onClick={(e) => jumpTo(e, href.slice(1))} />;
        if (isUrl(href)) return <a {...props} href={href} target="_blank" rel="noreferrer" />;
        // another file of the project: open it here, at the line of a GitHub-style #L12
        const [file, frag = ""] = href.split("#");
        const rel = resolvePath(dir, file.split("?")[0]);
        const line = Number(/^L(\d+)/.exec(frag)?.[1]) || undefined;
        if (rel === null) return <a {...props} href={href} />;
        if (rel === path && !line) return <a {...props} href={href} onClick={(e) => jumpTo(e, frag)} />;
        return <a {...props} href={href} onClick={(e) => (e.preventDefault(), useApp.getState().openFile(rel, root, line ? { line } : undefined))} />;
      },
    };
  }, [pid, root, path, dir, attachments]);
  return (
    <ReactMarkdown
      remarkPlugins={REMARK}
      rehypePlugins={PLUGINS}
      components={components}
      urlTransform={(url) => (url.startsWith("attachment:") ? url : defaultUrlTransform(url))}
    >
      {text}
    </ReactMarkdown>
  );
}

/** HTML from a notebook's output (a DataFrame's table), through the same allow-list as Markdown's */
function SafeHtml({ html }: { html: string }) {
  const remark = useMemo(() => [() => (tree: { children: unknown[] }) => void (tree.children = [{ type: "html", value: html }])], [html]);
  return (
    <ReactMarkdown remarkPlugins={remark} rehypePlugins={SAFE}>
      {""}
    </ReactMarkdown>
  );
}

// ---- notebook ----------------------------------------------------------------------------------------------------
type Multi = string | string[];
interface NbOutput {
  output_type: "stream" | "execute_result" | "display_data" | "error" | "update_display_data";
  name?: string;
  text?: Multi;
  data?: Record<string, Multi>;
  ename?: string;
  evalue?: string;
  traceback?: string[];
}
interface NbCell {
  cell_type: "markdown" | "code" | "raw";
  source: Multi;
  outputs?: NbOutput[];
  execution_count?: number | null;
  attachments?: Record<string, Record<string, string>>;
}
interface Nb {
  cells?: NbCell[];
  metadata?: { kernelspec?: { language?: string }; language_info?: { name?: string } };
}

const join = (m?: Multi) => (Array.isArray(m) ? m.join("") : m ?? "");
const ANSI = /\x1b\[[\d;]*[A-Za-z]/g; // a traceback's colours

/** a code cell as a fenced block, one backtick longer than any run in it, for the Markdown pipeline to highlight */
function fenced(code: string, lang: string): string {
  const fence = "`".repeat(Math.max(3, ...[...code.matchAll(/`+/g)].map((m) => m[0].length + 1)));
  return `${fence}${lang}\n${code}\n${fence}`;
}

function Output({ o }: { o: NbOutput }) {
  if (o.output_type === "stream") return <pre className={o.name === "stderr" ? "nb-out stderr" : "nb-out"}>{join(o.text)}</pre>;
  if (o.output_type === "error") return <pre className="nb-out stderr">{(o.traceback?.join("\n") ?? `${o.ename}: ${o.evalue}`).replace(ANSI, "")}</pre>;
  const d = o.data ?? {};
  const img = ["image/png", "image/jpeg", "image/gif"].find((m) => d[m]);
  if (img) return <img className="nb-img" src={`data:${img};base64,${join(d[img]).replace(/\s/g, "")}`} alt="" />;
  if (d["image/svg+xml"]) return <img className="nb-img" src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(join(d["image/svg+xml"]))}`} alt="" />;
  const html = join(d["text/html"]);
  // a page that needs its scripts (a Plotly chart) is an empty box without them: its plain text says more
  if (html && (!/<script/i.test(html) || !d["text/plain"]))
    return (
      <div className="md nb-html">
        <SafeHtml html={html} />
      </div>
    );
  if (d["text/markdown"])
    return (
      <div className="md">
        <ReactMarkdown remarkPlugins={REMARK}>{join(d["text/markdown"])}</ReactMarkdown>
      </div>
    );
  const text = join(d["text/plain"] ?? d["text/latex"]);
  return text ? <pre className="nb-out">{text.replace(ANSI, "")}</pre> : null;
}

function Notebook({ content, pid, root, path }: PreviewProps) {
  const nb = useMemo<Nb | null>(() => {
    try {
      return JSON.parse(content);
    } catch {
      return null;
    }
  }, [content]);
  if (!nb?.cells) return <div className="empty-state">Not a notebook this preview can read (invalid JSON?). Its source is a tap away.</div>;
  const lang = nb.metadata?.kernelspec?.language ?? nb.metadata?.language_info?.name ?? "python";
  return (
    <div className="file-preview">
      <div className="file-doc nb-doc">
        {nb.cells.map((c, i) =>
          c.cell_type === "markdown" ? (
            <div key={i} className="md nb-cell">
              <Doc text={join(c.source)} pid={pid} root={root} path={path} attachments={c.attachments} />
            </div>
          ) : c.cell_type === "code" ? (
            <div key={i} className="nb-cell">
              <div className="nb-count">[{c.execution_count ?? " "}]</div>
              <div className="md">
                <ReactMarkdown rehypePlugins={HIGHLIGHT}>{fenced(join(c.source), lang)}</ReactMarkdown>
              </div>
              {!!c.outputs?.length && (
                <div className="nb-outs">
                  {c.outputs.map((o, j) => (
                    <Output key={j} o={o} />
                  ))}
                </div>
              )}
            </div>
          ) : (
            <pre key={i} className="nb-out nb-cell">
              {join(c.source)}
            </pre>
          ),
        )}
      </div>
    </div>
  );
}

// ---- html --------------------------------------------------------------------------------------------------------
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** the page as it is in the editor, with a <base> at its saved URL so that its CSS, scripts, images and links load
 *  from beside it. No referrer: a CDN it loads from learns nothing of Atrium. No text inflation (AGENTS.md). */
function withBase(html: string, base: string | null): string {
  const head =
    (base && !/<base\s[^>]*href/i.test(html) ? `<base href="${base}">` : "") +
    `<meta name="referrer" content="no-referrer"><style>html{-webkit-text-size-adjust:100%}</style>`;
  const at = /<head(\s[^>]*)?>/i.exec(html) ?? /<!doctype[^>]*>/i.exec(html); // never before the doctype: quirks mode
  return at ? html.slice(0, at.index + at[0].length) + head + html.slice(at.index + at[0].length) : head + html;
}

function HtmlPage({ content, site, name }: { content: string; site?: string | null; name: string }) {
  const text = useDebounced(content, 400); // typing doesn't reload it at every key
  const doc = useMemo(() => (site === undefined ? null : withBase(text, site)), [text, site]);
  if (doc === null)
    return (
      <div className="empty-state">
        <Spinner />
      </div>
    );
  // scripts run, in an opaque origin: neither Atrium's page nor its API (no cookie, no same-origin fetch) is in reach
  return <iframe className="file-frame" sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads" srcDoc={doc} title={name} />;
}

// ---- csv ---------------------------------------------------------------------------------------------------------
const MAX_ROWS = 2000;

function parseCsv(text: string, sep: string): { rows: string[][]; more: boolean } {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c !== '"') field += c;
      else if (text[i + 1] === '"') (field += '"'), i++;
      else quoted = false;
    } else if (c === '"' && field === "") quoted = true;
    else if (c === sep) row.push(field), (field = "");
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field), rows.push(row), (field = ""), (row = []);
      if (rows.length > MAX_ROWS) return { rows: rows.slice(0, MAX_ROWS), more: true };
    } else field += c;
  }
  if (field || row.length) row.push(field), rows.push(row);
  return { rows, more: false };
}

function Table({ content, tabs }: { content: string; tabs: boolean }) {
  const { rows, more } = useMemo(() => parseCsv(content, tabs ? "\t" : ","), [content, tabs]);
  if (!rows.length) return <div className="empty-state">Empty table.</div>;
  const [head, ...body] = rows;
  const cols = Math.max(...rows.map((r) => r.length));
  // a long cell is cut short: the mouse gets it whole in a tooltip, a finger by tapping it (focus: styles.css)
  const cells = (r: string[], Tag: "th" | "td") =>
    Array.from({ length: cols }, (_, j) => (
      <Tag key={j} title={r[j]} tabIndex={-1}>
        {r[j]}
      </Tag>
    ));
  return (
    <table className="csv-table">
      <thead>
        <tr>
          <th className="n" />
          {cells(head, "th")}
        </tr>
      </thead>
      <tbody>
        {body.map((r, i) => (
          <tr key={i}>
            <td className="n">{i + 1}</td>
            {cells(r, "td")}
          </tr>
        ))}
      </tbody>
      {more && (
        <tfoot>
          <tr>
            <td colSpan={cols + 1}>The first {MAX_ROWS} rows: the source has them all.</td>
          </tr>
        </tfoot>
      )}
    </table>
  );
}

// ---- images, PDF, video, audio -----------------------------------------------------------------------------------
function Svg({ content, name }: { content: string; name: string }) {
  // an <img>: an SVG's scripts don't run there
  const url = useMemo(() => URL.createObjectURL(new Blob([content], { type: "image/svg+xml" })), [content]);
  useEffect(() => () => URL.revokeObjectURL(url), [url]);
  return (
    <Frame>
      <img className="checker" src={url} alt={name} />
    </Frame>
  );
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <div className="file-preview">
      <div className="file-media">{children}</div>
    </div>
  );
}

function Media({ kind, pid, root, path, version }: PreviewProps) {
  const src = api.rawFileUrl(pid, path, root, version);
  const name = basename(path);
  if (kind === "pdf") return <iframe className="file-frame" src={src} title={name} />;
  return (
    <Frame>
      {kind === "video" ? (
        <video src={src} controls playsInline preload="metadata" />
      ) : kind === "audio" ? (
        <audio src={src} controls preload="metadata" />
      ) : (
        <img className="checker" src={src} alt={name} />
      )}
    </Frame>
  );
}
