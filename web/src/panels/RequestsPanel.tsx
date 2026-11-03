import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { ArrowDownLeft, ArrowLeft, ArrowUpRight, ChevronRight, Code, Copy, ListTree, Maximize2, Minimize2, RefreshCw, Trash2 } from "lucide-react";
import { api, CaptureBody, CaptureDetail, CaptureRow, CaptureUsage } from "../api";
import { useApp } from "../store";
import { socket } from "../ws";
import { clsx, displayName } from "../util";
import { JsonTree, LongText } from "../components/JsonTree";
import { Spinner, startColResize, useFullscreen, useInterval } from "../components/ui";
import { Answer, fmtBytes, fmtSeconds, fmtTokens, Message, parseAnswer, parseRequest, Part, Request, samePrefix, StreamEvent } from "./wire";

// The Requests tab: what the selected agent sent to its model API, call by call, and what came back — recorded by
// Atrium's capture proxy (capture.py). A list grouped by the user message each turn answers, beside the call picked.

type Tab = "overview" | "request" | "response" | "raw";
const TAB_KEY = "atrium.requestsTab";
const LIST_KEY = "atrium.requestsListWidth";
const SECTIONS_KEY = "atrium.requestsSections";
const NARROW = 720; // below this the list and a call take turns
const KIND: Record<string, [string, string]> = {
  agent: ["Agent", "The agent's own loop"],
  sub: ["Sub", "A subagent's call"],
  aux: ["Aux", "No tools: a side call, such as a title or a summary"],
  count: ["Count", "Token counting"],
};
const LIVE = new Set(["pending", "streaming"]);

const header = (list: [string, string][] | undefined, name: string) => list?.find(([k]) => k.toLowerCase() === name)?.[1] ?? "";
const clock = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });

function parseJson(text: string | undefined): unknown {
  if (text == null) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function upsert(rows: CaptureRow[], row: CaptureRow): CaptureRow[] {
  const i = rows.findIndex((r) => r.id === row.id);
  if (i < 0) return [...rows, row].sort((a, b) => a.started - b.started);
  const next = rows.slice();
  next[i] = row;
  return next;
}

/** Calls in a row that answer the same user message, the latest turn first (its calls in the order they were made). */
function turns(rows: CaptureRow[]) {
  const out: { key: string; turn: CaptureRow["turn"]; rows: CaptureRow[] }[] = [];
  for (const r of rows) {
    const key = r.turn?.id ?? "";
    let g = out[out.length - 1];
    if (!g || g.key !== key) out.push((g = { key, turn: r.turn ?? null, rows: [] }));
    g.rows.push(r);
  }
  return out.reverse();
}

function useNarrow(ref: RefObject<HTMLElement | null>) {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let frame = 0;
    const measure = () => setNarrow(el.clientWidth < NARROW);
    // in the next frame: what the change lays out is not measured again in this one (no ResizeObserver loop)
    const ro = new ResizeObserver(() => (cancelAnimationFrame(frame), (frame = requestAnimationFrame(measure))));
    ro.observe(el);
    measure();
    return () => (cancelAnimationFrame(frame), ro.disconnect());
  }, [ref]);
  return narrow;
}

export function RequestsPanel() {
  const { project, sessionId, sessions, toast } = useApp();
  const pid = project?.id;
  const ref = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [full, toggleFull] = useFullscreen(ref);
  const narrow = useNarrow(ref);
  const [data, setData] = useState<{ enabled: boolean; running: boolean; rows: CaptureRow[] } | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [width, setWidth] = useState(() => Number(localStorage.getItem(LIST_KEY)) || 0);
  const cache = useRef(new Map<string, CaptureDetail>()); // finished calls, which don't change
  useEffect(() => void (width && localStorage.setItem(LIST_KEY, String(width))), [width]);

  const load = useCallback(() => {
    if (!pid || !sessionId) return setData(null);
    api.requests(pid, sessionId).then(setData, (e) => (setData({ enabled: true, running: true, rows: [] }), toast(e.message, "error")));
  }, [pid, sessionId, toast]);
  useEffect(() => {
    setData(null);
    setSel(null);
    load();
  }, [load]);
  useEffect(() => socket.onReconnect(load), [load]);
  useEffect(() => {
    if (!pid) return;
    return socket.subscribe(`project:${pid}`, (msg) => {
      if (msg.type !== "capture" || msg.session_id !== sessionId) return;
      if (msg.cleared) return setData((d) => d && { ...d, rows: [] });
      setData((d) => d && { ...d, rows: upsert(d.rows, msg.row) });
    });
  }, [pid, sessionId]);
  // beside the list, a call is shown from the start: the latest
  useEffect(() => {
    if (!narrow && !sel && data?.rows.length) setSel(data.rows[data.rows.length - 1].id);
  }, [narrow, sel, data]);

  if (!project) return null;
  const rows = data?.rows ?? [];
  const row = rows.find((r) => r.id === sel) ?? null;
  const agent = sessions.find((s) => s.id === sessionId);
  const clear = async () => {
    if (!pid || !sessionId || !confirm(`Delete the ${rows.length} recorded calls of this agent?`)) return;
    try {
      await api.clearRequests(pid, sessionId);
      cache.current.clear();
      setSel(null);
      load();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const showList = !narrow || !row;
  return (
    <div ref={ref} className="panel-body fs-host req-panel">
      <div className="toolbar">
        <span className="title ellipsis req-title">{agent ? displayName(agent) : "Requests"}</span>
        {data && <span className="faint req-count">{rows.length} calls</span>}
        <span className="grow" />
        <button className="icon-btn" aria-label="Refresh" data-tip="Refresh" onClick={load} disabled={!sessionId}>
          <RefreshCw size={14} />
        </button>
        <button className="icon-btn danger" aria-label="Delete these calls" data-tip="Delete this agent's recorded calls" onClick={() => void clear()} disabled={!rows.length}>
          <Trash2 size={14} />
        </button>
        <button className={clsx("icon-btn", full && "active")} aria-label={full ? "Exit full screen" : "Full screen"} data-tip={full ? "Exit full screen (Esc)" : "Full screen"} onClick={toggleFull}>
          {full ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
      </div>
      {data && !data.running && <div className="banner">Model calls aren't being recorded: Atrium's capture proxy didn't start (see its log).</div>}
      {data && data.running && !data.enabled && (
        <div className="banner subtle">Capture is off (capture.enabled in Atrium's settings): agents started from now on aren't recorded.</div>
      )}
      {!sessionId ? (
        <div className="req-empty muted">Pick an agent: the calls it makes to its model show here.</div>
      ) : (
        <div className="split req-split">
          {showList && (
            <div ref={listRef} className={clsx("side wide req-list", narrow && "alone")} style={!narrow && width ? ({ "--list-w": `${width}px` } as React.CSSProperties) : undefined}>
              {!data && (
                <div className="req-pad">
                  <Spinner />
                </div>
              )}
              {data && !rows.length && <div className="req-empty muted">No model calls recorded for this agent yet.</div>}
              {turns(rows).map((g) => (
                <div key={g.key + g.rows[0].id} className="req-group">
                  <div className="req-turn" title={g.turn?.text}>
                    <span className="ellipsis grow">{g.turn?.text || "Before the first message"}</span>
                    <span className="faint">{g.rows.length}</span>
                  </div>
                  {g.rows.map((r) => (
                    <RowView key={r.id} r={r} on={r.id === sel} pick={() => setSel(r.id)} />
                  ))}
                </div>
              ))}
            </div>
          )}
          {!narrow && <div className="col-resizer" onPointerDown={(e) => startColResize(e, listRef, setWidth, 220, 360)} />}
          {(!narrow || row) && (
            <div className="content req-content">
              {row ? (
                <Detail key={row.id} pid={project.id} row={row} rows={rows} cache={cache.current} back={narrow ? () => setSel(null) : undefined} />
              ) : (
                <div className="req-empty muted">{rows.length ? "Pick a call to see what was sent and what came back." : ""}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Status({ r }: { r: CaptureRow }) {
  if (LIVE.has(r.state))
    return (
      <span className="pill info req-live">
        <span className="req-dot" />
        {r.state === "pending" ? "sending" : "streaming"}
      </span>
    );
  if (r.state === "cancelled") return <span className="pill" data-tip="The harness hung up before the answer ended (an interrupt, its own timeout)">cancelled</span>;
  if (r.state === "interrupted") return <span className="pill warn" data-tip="Atrium stopped before the answer ended">interrupted</span>;
  if (!r.status) return <span className="pill err" data-tip={r.error}>failed</span>;
  return <span className={clsx("pill mono", r.status >= 400 ? "err" : "ok")}>{r.status}</span>;
}

function Kind({ r }: { r: CaptureRow }) {
  const kind = r.summary?.kind ?? "";
  const k = KIND[kind];
  return k ? (
    <span className={`req-kind ${kind}`} data-tip={k[1]}>
      {k[0]}
    </span>
  ) : (
    <span className="req-kind">{r.method}</span>
  );
}

function usageText(u: CaptureUsage | undefined): string[] {
  if (!u) return [];
  const out: string[] = [];
  if (u.input != null) out.push(`${fmtTokens(u.input)} in` + (u.cached ? ` (${Math.round((100 * u.cached) / Math.max(u.input, 1))}% cached)` : ""));
  if (u.output != null) out.push(`${fmtTokens(u.output)} out`);
  return out;
}

function RowView({ r, on, pick }: { r: CaptureRow; on: boolean; pick: () => void }) {
  const s = r.summary;
  return (
    <button className={clsx("req-row", on && "on")} onClick={pick}>
      <span className="r1">
        <Kind r={r} />
        <span className="mono ellipsis grow">{s?.model || r.path}</span>
        <Status r={r} />
      </span>
      <span className="r2">
        <span>{clock(r.started)}</span>
        {r.duration != null && <span>{fmtSeconds(r.duration)}</span>}
        {usageText(s?.usage).map((t) => (
          <span key={t}>{t}</span>
        ))}
        {s?.thread && <span className="mono">thread …{s.thread.slice(-6)}</span>}
      </span>
      {s?.first && <span className="r3 ellipsis">{s.first}</span>}
    </button>
  );
}

// ---------------------------------------------------------------- one call

function Detail({ pid, row, rows, cache, back }: { pid: string; row: CaptureRow; rows: CaptureRow[]; cache: Map<string, CaptureDetail>; back?: () => void }) {
  const toast = useApp((s) => s.toast);
  const [d, setD] = useState<CaptureDetail | null>(() => cache.get(row.id) ?? null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<Tab>(() => (localStorage.getItem(TAB_KEY) as Tab) || "overview");
  const live = LIVE.has(row.state);
  const fetchIt = useCallback(() => {
    api.requestDetail(pid, row.id).then(
      (x) => {
        if (!LIVE.has(x.state)) cache.set(x.id, x);
        setD(x);
        setErr("");
      },
      (e) => setErr(e.message),
    );
  }, [pid, row.id, cache]);
  useEffect(() => {
    if (!cache.has(row.id)) fetchIt();
  }, [fetchIt, row.state, cache, row.id]);
  useInterval(fetchIt, live ? 1500 : null); // what has come of an answer still streaming
  const pickTab = (t: Tab) => (setTab(t), localStorage.setItem(TAB_KEY, t));
  const copy = (text: string, what: string) =>
    void navigator.clipboard.writeText(text).then(
      () => toast(`${what} copied`, "ok"),
      () => toast("Couldn't copy", "error"),
    );

  const api_ = row.summary?.api ?? "other";
  const req = useMemo(() => (d ? parseRequest(api_, parseJson(d.request.text)) : null), [d, api_]);
  const ans = useMemo(() => (d?.response.text != null ? parseAnswer(api_, d.response.text, header(d.resp_headers, "content-type"), d.marks) : null), [d, api_]);
  // the same kind of call before it (the same API, the same thread): what this one adds to it
  const prevRow = useMemo(() => {
    const i = rows.findIndex((r) => r.id === row.id);
    const s = row.summary;
    for (let j = i - 1; j >= 0; j--) {
      const p = rows[j].summary;
      if (p?.api === s?.api && p?.kind === s?.kind && p?.thread === s?.thread && p?.kind !== "count") return rows[j];
    }
    return null;
  }, [rows, row]);

  const s = row.summary;
  const tabs: [Tab, string, ReactNode][] = [
    ["overview", "Overview — the request laid out: settings, system prompt, tools, messages, and the answer", <ListTree size={15} />],
    ["request", "Request — URL, headers and body as sent", <ArrowUpRight size={15} />],
    ["response", "Response — status, headers and the stream's events as they came", <ArrowDownLeft size={15} />],
    ["raw", "Raw — both bodies as text", <Code size={15} />],
  ];
  return (
    <>
      <div className="req-head">
        <div className="r1">
          {back && (
            <button className="icon-btn" aria-label="Back to the calls" data-tip="Back to the calls" onClick={back}>
              <ArrowLeft size={15} />
            </button>
          )}
          <Kind r={row} />
          <span className="mono ellipsis grow req-path" title={`${row.method} ${row.host}${row.path}`}>
            {row.method} {row.path}
          </span>
          <Status r={row} />
        </div>
        <div className="r2 faint">
          <span className="mono">{row.host}</span>
          {s?.model && <span className="mono">{s.model}</span>}
          <span>{clock(row.started)}</span>
          {row.ttfb != null && <span data-tip="Until the answer's headers came">first byte {fmtSeconds(row.ttfb)}</span>}
          {row.duration != null && <span>{fmtSeconds(row.duration)}</span>}
          <span data-tip="Sent / received">
            {fmtBytes(row.req_bytes)} ↑ {row.resp_bytes != null ? `${fmtBytes(row.resp_bytes)} ↓` : ""}
          </span>
          {usageText(s?.usage).map((t) => (
            <span key={t}>{t}</span>
          ))}
          {s?.usage?.cache_write ? <span>{fmtTokens(s.usage.cache_write)} written to cache</span> : null}
          {s?.usage?.reasoning ? <span>{fmtTokens(s.usage.reasoning)} reasoning</span> : null}
        </div>
        {(row.error || s?.api_error) && <div className="req-error">{s?.api_error || row.error}</div>}
        <div className="seg icons req-tabs">
          {tabs.map(([t, tip, icon]) => (
            <button key={t} className={clsx(tab === t && "on")} onClick={() => pickTab(t)} aria-label={tip.split(" — ")[0]} data-tip={tip}>
              {icon}
            </button>
          ))}
        </div>
      </div>
      <div className="req-body">
        {err && <div className="req-error">{err}</div>}
        {!d && !err && (
          <div className="req-pad">
            <Spinner />
          </div>
        )}
        {d && tab === "overview" && <Overview pid={pid} req={req} ans={ans} prevRow={prevRow} cache={cache} stop={s?.stop} />}
        {d && tab === "request" && (
          <>
            <div className="req-url mono">
              {d.method} {d.url}
            </div>
            {d.narrowed && (
              <div className="req-note faint">
                Sent on with Accept-Encoding narrowed to what Atrium can read back; the harness offered “{d.narrowed}”.
              </div>
            )}
            <Headers list={d.req_headers} />
            <BodyView body={d.request} copy={copy} what="Request body" />
          </>
        )}
        {d && tab === "response" && (
          <>
            <Headers list={d.resp_headers} />
            {ans?.events ? <Events events={ans.events} /> : <BodyView body={d.response} copy={copy} what="Response body" />}
          </>
        )}
        {d && tab === "raw" && (
          <>
            <RawBody title="Request" body={d.request} copy={copy} />
            <RawBody title="Response" body={d.response} copy={copy} />
          </>
        )}
      </div>
    </>
  );
}

function Headers({ list }: { list: [string, string][] }) {
  if (!list.length) return null;
  return (
    <Section id="headers" title="Headers" meta={String(list.length)} closed>
      <table className="req-kv">
        <tbody>
          {list.map(([k, v], i) => (
            <tr key={i}>
              <td className="mono">{k}</td>
              <td className="mono">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function BodyView({ body, copy, what }: { body: CaptureBody; copy: (text: string, what: string) => void; what: string }) {
  if (body.text == null) return <Binary body={body} />;
  const v = parseJson(body.text);
  return (
    <Section
      id="body"
      title="Body"
      meta={fmtBytes(body.bytes)}
      action={
        <button className="icon-btn sm" aria-label={`Copy the ${what.toLowerCase()}`} data-tip="Copy" onClick={() => copy(body.text!, what)}>
          <Copy size={13} />
        </button>
      }
    >
      {v !== undefined ? <JsonTree value={v} open={1} /> : <LongText text={body.text} className="req-text mono" />}
    </Section>
  );
}

function Binary({ body }: { body: CaptureBody }) {
  return (
    <div className="req-note faint">
      {body.bytes ? `${fmtBytes(body.bytes)} that aren't text${body.encoding ? ` (${body.encoding}-encoded: Atrium can't decode that)` : ""}.` : "Empty."}
    </div>
  );
}

function RawBody({ title, body, copy }: { title: string; body: CaptureBody; copy: (text: string, what: string) => void }) {
  return (
    <Section
      id={`raw-${title}`}
      title={title}
      meta={fmtBytes(body.bytes)}
      action={
        body.text != null ? (
          <button className="icon-btn sm" aria-label={`Copy the ${title.toLowerCase()}`} data-tip="Copy" onClick={() => copy(body.text!, title)}>
            <Copy size={13} />
          </button>
        ) : undefined
      }
    >
      {body.text != null ? <LongText text={body.text} className="req-text mono" /> : <Binary body={body} />}
    </Section>
  );
}

function eventBrief(e: StreamEvent): string {
  const d = e.data as any;
  if (typeof d !== "object" || !d) return String(d ?? "");
  const delta = d.delta?.text ?? d.delta?.thinking ?? d.delta?.partial_json ?? (typeof d.delta === "string" ? d.delta : undefined);
  if (typeof delta === "string") return JSON.stringify(delta);
  const parts = d.candidates?.[0]?.content?.parts;
  if (Array.isArray(parts)) return parts.map((p: any) => (p.text != null ? JSON.stringify(p.text) : Object.keys(p)[0])).join(" ");
  return JSON.stringify(d).slice(0, 160);
}

function Events({ events }: { events: StreamEvent[] }) {
  const [n, setN] = useState(300);
  return (
    <Section id="events" title="Events" meta={String(events.length)}>
      <div className="req-events">
        {events.slice(0, n).map((e, i) => (
          <EventRow key={i} e={e} />
        ))}
        {events.length > n && (
          <button className="jt-more" onClick={() => setN(n + 1000)}>
            {events.length - n} more…
          </button>
        )}
      </div>
    </Section>
  );
}

function EventRow({ e }: { e: StreamEvent }) {
  const [open, setOpen] = useState(false);
  const name = e.name || (e.data as any)?.type || "data";
  return (
    <div className="req-event">
      <button className="req-event-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="at mono">{e.at != null ? `+${(e.at / 1000).toFixed(2)}s` : ""}</span>
        <span className="mono name">{name}</span>
        <span className="ellipsis grow faint">{eventBrief(e)}</span>
      </button>
      {open && <JsonTree value={e.data} open={3} />}
    </div>
  );
}

// ---------------------------------------------------------------- the request laid out

const sectionState: Record<string, boolean> = (() => {
  try {
    return JSON.parse(localStorage.getItem(SECTIONS_KEY) || "{}");
  } catch {
    return {};
  }
})();

/** A part of a call that folds; open or closed as it was left last (in any call). */
function Section({ id, title, meta, badge, closed, action, children }: { id: string; title: string; meta?: string; badge?: ReactNode; closed?: boolean; action?: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(sectionState[id] ?? !closed);
  const toggle = () => {
    sectionState[id] = !open;
    localStorage.setItem(SECTIONS_KEY, JSON.stringify(sectionState));
    setOpen(!open);
  };
  return (
    <div className={clsx("req-sec", open && "open")}>
      <div className="req-sec-head">
        <button className="req-sec-toggle" onClick={toggle} aria-expanded={open}>
          <ChevronRight size={13} className={clsx("jt-caret", open && "open")} />
          <span className="t">{title}</span>
          {meta && <span className="faint">{meta}</span>}
          {badge}
        </button>
        {action}
      </div>
      {open && <div className="req-sec-body">{children}</div>}
    </div>
  );
}

function Overview({ pid, req, ans, prevRow, cache, stop }: { pid: string; req: Request | null; ans: Answer | null; prevRow: CaptureRow | null; cache: Map<string, CaptureDetail>; stop?: string }) {
  const [prev, setPrev] = useState<Request | null>(null);
  const [showSame, setShowSame] = useState(false);
  const prevId = prevRow?.id;
  const api_ = prevRow?.summary?.api ?? "other";
  useEffect(() => {
    setPrev(null);
    if (!prevId) return;
    const use = (d: CaptureDetail) => setPrev(parseRequest(api_, parseJson(d.request.text)));
    const hit = cache.get(prevId);
    if (hit) return use(hit);
    api.requestDetail(pid, prevId).then((d) => (cache.set(d.id, d), use(d)), () => undefined);
  }, [pid, prevId, cache, api_]);
  if (!req) return <div className="req-note muted">This isn't a request to an API Atrium knows how to lay out: see Request and Raw.</div>;
  const same = prev ? samePrefix(prev.messages, req.messages) : 0;
  const changed = (a: unknown, b: unknown) => JSON.stringify(a) !== JSON.stringify(b);
  const diffBadge = (a: unknown, b: unknown) =>
    prev ? <span className={clsx("pill", changed(a, b) ? "warn" : "")}>{changed(a, b) ? "changed" : "as before"}</span> : null;
  const sysChars = req.system.reduce((n, p) => n + (p.text?.length ?? 0), 0);
  return (
    <div className="req-overview">
      <Section id="params" title="Settings" meta={String(req.params.length)}>
        <table className="req-kv">
          <tbody>
            {req.params.map(([k, v]) => (
              <tr key={k}>
                <td className="mono">{k}</td>
                <td>{v !== null && typeof v === "object" ? <JsonTree value={v} open={1} /> : <span className="mono">{JSON.stringify(v)}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      <Section id="system" title="System prompt" meta={`${sysChars.toLocaleString()} chars`} badge={diffBadge(prev?.system, req.system)} closed>
        {req.system.length ? req.system.map((p, i) => <PartView key={i} p={p} />) : <div className="req-note faint">None.</div>}
      </Section>
      <Section id="tools" title="Tools" meta={String(req.tools.length)} badge={diffBadge(prev?.tools, req.tools)} closed>
        {req.tools.length ? req.tools.map((t, i) => <ToolView key={i} t={t} />) : <div className="req-note faint">None.</div>}
      </Section>
      <Section id="messages" title="Messages" meta={String(req.messages.length)}>
        {same > 0 && (
          <button className="req-same" onClick={() => setShowSame(!showSame)} aria-expanded={showSame}>
            <ChevronRight size={13} className={clsx("jt-caret", showSame && "open")} />
            {same} as in the call before · {req.messages.length - same} new
          </button>
        )}
        {req.messages.map((m, i) => (i >= same || showSame ? <MessageView key={i} m={m} fresh={!!prev && i >= same} /> : null))}
      </Section>
      <Section id="answer" title="Answer" meta={stop}>
        {ans?.parts.length ? ans.parts.map((p, i) => <PartView key={i} p={p} />) : <div className="req-note faint">Nothing yet.</div>}
      </Section>
    </div>
  );
}

function ToolView({ t }: { t: { name: string; description?: string; schema?: unknown } }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="req-tool">
      <button className="req-tool-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <ChevronRight size={12} className={clsx("jt-caret", open && "open")} />
        <span className="mono name">{t.name}</span>
        <span className="ellipsis grow faint">{t.description?.split("\n")[0]}</span>
      </button>
      {open && (
        <div className="req-tool-body">
          {t.description && <LongText text={t.description} limit={4000} className="req-text" />}
          {t.schema !== undefined && <JsonTree value={t.schema} open={2} />}
        </div>
      )}
    </div>
  );
}

function MessageView({ m, fresh }: { m: Message; fresh: boolean }) {
  return (
    <div className={clsx("req-msg", fresh && "fresh")}>
      <div className={clsx("req-role", m.role)}>{m.role}</div>
      {m.parts.length ? m.parts.map((p, i) => <PartView key={i} p={p} />) : <div className="req-note faint">(empty)</div>}
    </div>
  );
}

const PART_HEAD: Record<string, string> = { thinking: "Thinking", call: "Calls", result: "Result", media: "", other: "" };

function PartView({ p }: { p: Part }) {
  const head = p.kind === "text" ? "" : [PART_HEAD[p.kind], p.title].filter(Boolean).join(" · ");
  return (
    <div className={clsx("req-part", p.kind, p.error && "err")}>
      {(head || p.cached) && (
        <div className="req-part-head">
          {head && <span className="ellipsis">{head}</span>}
          {p.cached && (
            <span className="pill info" data-tip="A cache breakpoint (cache_control): the prompt up to here is cached">
              cache
            </span>
          )}
        </div>
      )}
      {!!p.text && <LongText text={p.text} limit={4000} className="req-text" />}
      {p.data !== undefined && <JsonTree value={p.data} open={1} />}
    </div>
  );
}
