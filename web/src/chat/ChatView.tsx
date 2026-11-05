import type { JSX, MouseEvent, ReactNode } from "react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, ArrowUpRight, Brain, ChevronRight, ChevronUp, FoldVertical, GitFork, Info, Pencil, Sparkle, Square } from "lucide-react";
import { api, EditAt, ForkAt, Item, LiveState, MoveWorkspace, SessionRow, SessionSettings } from "../api";
import { socket } from "../ws";
import { useApp } from "../store";
import { EMPTY_REPLY, Workspace, useDrafts } from "../drafts";
import { clsx, fmtClock, fmtDuration, samePoint } from "../util";
import { Markdown, Spinner, StreamingMarkdown, useDay } from "../components/ui";
import { useOpenCompaction } from "../components/Compactions";
import { FileLinks } from "../components/FileLinks";
import { BoardToolRow, PermissionCard, ToolRow, isBoardTool, useOpenSubagent } from "./ToolViews";
import { ReviewToolBlock, isReviewTool } from "./ReviewCards";
import {
  AttachButton, AttachmentTray, ContextRing, EffortPicker, HarnessChip, MessageAttachments, ModelChip, NewWorktreeFields, PendingUpload, PermissionPicker,
  WorkspaceChip, effortOptions, useAttachments, useChatFloor,
} from "./Composer";
import { mergeCommands, useAtriumCommands, useSlashCommands, useSlashMenu } from "./SlashMenu";
import { useVoiceInput } from "./Voice";
import { MessageEditor } from "./MessageEditor";
import { ScrollAids } from "./ScrollAids";

type UserItem = Extract<Item, { kind: "user" }>;
type ToolItem = Extract<Item, { kind: "tool" }>;
type ResultItem = Extract<Item, { kind: "tool_result" }>;
type PermItem = Extract<Item, { kind: "permission" }>;
type SystemItem = Extract<Item, { kind: "system" }>;

export interface ChatState {
  items: Item[];
  seen: Set<string>;
  results: Record<string, ResultItem>;
  pending: Record<string, PermItem>;
  resolved: Set<string>;
  streaming: string;
  state: LiveState | null;
  mode: string | null;
  model: string | null;
  effort: string | null; // the thread's effort when nothing overrides it, if the harness says (Codex)
  usage: { used: number | null; max: number | null } | null;
}

export const empty = (): ChatState => ({
  items: [], seen: new Set(), results: {}, pending: {}, resolved: new Set(), streaming: "", state: null, mode: null, model: null, effort: null, usage: null,
});

/** The chat with `its` applied, in order. What they change is copied once, not once per item: a long conversation's
 *  snapshot, a frame's worth of live items. */
export function applyAll(st: ChatState, its: Item[]): ChatState {
  const next: ChatState = { ...st };
  let items: Item[] | null = null, seen: Set<string> | null = null, resolved: Set<string> | null = null;
  let results: ChatState["results"] | null = null, pending: ChatState["pending"] | null = null;
  let changed = false; // an item seen before changes nothing: the same state, and no render
  for (const it of its) {
    switch (it.kind) {
      case "delta":
        next.streaming += it.text;
        break;
      case "state":
        next.state = it.state;
        if (it.state !== "running") next.streaming = "";
        break;
      case "usage":
        next.usage = { used: it.used, max: it.max };
        break;
      case "tool_result":
        (results ??= { ...st.results })[it.id] = it;
        break;
      case "permission":
        if ((resolved ?? st.resolved).has(it.id)) continue;
        (pending ??= { ...st.pending })[it.id] = it;
        break;
      case "permission_resolved":
        delete (pending ??= { ...st.pending })[it.id];
        (resolved ??= new Set(st.resolved)).add(it.id);
        break;
      default: {
        if ((seen ?? st.seen).has(it.id)) {
          // harnesses may re-send a tool call once its input is final (upsert)
          const i = it.kind === "tool" ? (items ?? st.items).findIndex((x) => x.kind === "tool" && x.id === it.id) : -1;
          if (i < 0) continue;
          (items ??= [...st.items])[i] = it;
          break;
        }
        (seen ??= new Set(st.seen)).add(it.id);
        (items ??= [...st.items]).push(it);
        if (it.kind === "text") next.streaming = "";
        if (it.kind === "system" && it.subtype === "init") {
          next.mode = it.permission_mode ?? next.mode;
          next.model = it.model ?? next.model;
          next.effort = it.effort ?? next.effort;
        }
      }
    }
    changed = true;
  }
  if (!changed) return st;
  if (items) next.items = items;
  if (seen) next.seen = seen;
  if (results) next.results = results;
  if (pending) next.pending = pending;
  if (resolved) next.resolved = resolved;
  return next;
}

export type Block =
  | { type: "tools"; key: string; tools: ToolItem[] }
  | { type: "board"; key: string; tool: ToolItem }
  | { type: "review"; key: string; tool: ToolItem; latest: boolean } // latest: the last report of its round
  | { type: "item"; key: string; item: Item };

export function toBlocks(items: Item[]): Block[] {
  const out: Block[] = [];
  for (const it of items) {
    if ("parent" in it && it.parent) continue; // a subagent's own steps: shown in its view, not the parent's
    if (it.kind === "tool") {
      if (isBoardTool(it.name)) {
        out.push({ type: "board", key: it.id, tool: it });
        continue;
      }
      if (isReviewTool(it.name)) {
        out.push({ type: "review", key: it.id, tool: it, latest: true });
        continue;
      }
      const last = out[out.length - 1];
      if (last?.type === "tools") last.tools.push(it);
      else out.push({ type: "tools", key: it.id, tools: [it] });
    } else if ("id" in it && it.kind !== "permission" && it.kind !== "permission_resolved") {
      out.push({ type: "item", key: it.id, item: it });
    }
  }
  // a round's findings show once, live, at its last report; earlier reports of it were revised
  const reported = new Set<string>();
  for (let i = out.length - 1; i >= 0; i--) {
    const b = out[i];
    if (b.type !== "review" || !b.tool.name.endsWith("__report")) continue;
    const round = String(b.tool.input?.round);
    if (reported.has(round)) out[i] = { ...b, latest: false };
    reported.add(round);
  }
  return out;
}

/** Where "fork from here" cuts before each user message (but the first): after the last item the
 *  harness produced before it — its id points into the transcript. Subagent steps don't count. */
function forkPoints(items: Item[]): Map<string, ForkAt> {
  const out = new Map<string, ForkAt>();
  let anchor: string | null = null;
  let n = 0;
  for (const it of items) {
    if (it.kind === "user" && !it.command) {
      if (n > 0) out.set(it.id, { anchor, user_index: n });
      n++;
    } else if ((it.kind === "text" || it.kind === "thinking" || it.kind === "tool") && !("parent" in it && it.parent)) {
      anchor = it.id;
    }
  }
  return out;
}

/** Where "fork from here" on one of the agent's replies or tool calls cuts: after the model step holding it (its
 *  reply and what its tools returned), in the middle of a turn too. A subagent's items are its own conversation's. */
function stepForkPoints(items: Item[]): Map<string, ForkAt> {
  const out = new Map<string, ForkAt>();
  let n = 0;
  for (const it of items) {
    if (it.kind === "user" && !it.command) n++;
    else if (n > 0 && (it.kind === "text" || it.kind === "tool") && !("parent" in it && it.parent)) {
      out.set(it.id, { anchor: it.id, user_index: n - 1, after: true });
    }
  }
  return out;
}

/** The button that forks after one of the agent's steps (a reply's; a tool's is in its row, ToolViews). */
export function StepForkButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      className="icon-btn sm step-fork"
      aria-label="Fork from here"
      data-tip="Fork from here: a new agent that remembers everything up to this step, and does what you tell it next"
      onClick={(e) => (e.stopPropagation(), onClick())}
    >
      <GitFork size={13} />
    </button>
  );
}

/** Where editing each of the user's messages (but slash commands) goes back to: as for a fork, after the last
 *  item the harness produced before it (a compaction too: what came before it is a summary). */
function editPoints(items: Item[]): Map<string, EditAt> {
  const out = new Map<string, EditAt>();
  let anchor: string | null = null;
  for (const it of items) {
    if (it.kind === "user" && !it.command) {
      out.set(it.id, { item: it.id, anchor, user_index: out.size });
    } else if (
      ((it.kind === "text" || it.kind === "thinking" || it.kind === "tool") && !("parent" in it && it.parent)) ||
      (it.kind === "system" && it.subtype === "compact")
    ) {
      anchor = it.id;
    }
  }
  return out;
}

/** When a message was sent, above it: shown on hover, and for a finger when it taps the message. */
function MsgTime({ ts }: { ts?: number }) {
  if (!ts) return null;
  return (
    <time className="msg-time" dateTime={new Date(ts * 1000).toISOString()}>
      {fmtClock(ts)}
    </time>
  );
}

/** What a tap on a message is not for: its own controls, and text being selected. */
const TAPPABLE = "a, button, summary, input, textarea, select, label, img, video, .tool-row, [role='button']";

/** How much of a conversation is rendered at first: its end, this many steps of it (a message, a tool call), and as
 *  much again each time the reader nears the top of what is rendered. A session thousands of steps long would
 *  otherwise build hundreds of thousands of elements on every click on it, and keep them for every keystroke. */
const WINDOW = 200;

const weight = (b: Block) => (b.type === "tools" ? b.tools.length : 1);

/** Where rendering starts for the `n` steps before block `end` (or as many as there are). */
function windowStart(blocks: Block[], n: number, end = blocks.length): number {
  let i = Math.min(end, blocks.length);
  for (let w = 0; i > 0 && w < n; ) w += weight(blocks[--i]);
  return i;
}

/** The conversation; with `forks` and `onFork`, user messages get a "fork from here" button, and the agent's replies
 *  and tool calls one too (`steps`, `onForkStep`); with `edits` and `onEdit` one that changes the message, `editor`
 *  standing in for the one being changed (`editing`). Only its end is rendered at first (WINDOW): what is above comes
 *  in as the reader scrolls up to it, inside a `.chat-scroll`. */
export function Transcript({ blocks, results, running, cwd, forks, onFork, steps, onForkStep, edits, editing, onEdit, editor }: {
  blocks: Block[];
  results: Record<string, ResultItem>;
  running: boolean;
  cwd: string;
  forks?: Map<string, ForkAt> | null;
  onFork?: (at: ForkAt, text: string) => void;
  steps?: Map<string, ForkAt> | null;
  onForkStep?: (at: ForkAt, step: string) => void;
  edits?: Map<string, EditAt> | null;
  editing?: string | null;
  onEdit?: (id: string) => void;
  editor?: (item: UserItem, at: EditAt) => ReactNode;
}) {
  // a finger has no hover: a tap on a message shows its time, until it taps it again or another one
  const [tapped, setTapped] = useState<string | null>(null);
  const onTap = useCallback((key: string, e: MouseEvent) => {
    if (!window.matchMedia("(hover: none)").matches || (e.target as Element).closest(TAPPABLE)) return;
    if (!window.getSelection()?.isCollapsed) return;
    setTapped((k) => (k === key ? null : key));
  }, []);
  const day = useDay();
  // Where rendering starts: set once the conversation is there, then kept while it grows (what is on screen never
  // moves); earlier when the reader scrolls up to it. A conversation cut short (a message changed) starts it again at
  // its end, which then stays put too.
  const [start, setStart] = useState<number | null>(null);
  const end = windowStart(blocks, WINDOW);
  const from = start === null ? end : Math.min(start, end);
  useEffect(() => {
    if (start === null ? blocks.length > 0 : end < start) setStart(end);
  }, [start, blocks.length, end]);
  const moreRef = useRef<HTMLButtonElement>(null);
  // the scroll box, the block that was first, and where it was: steps that came in below meanwhile don't count
  const keep = useRef<{ box: HTMLElement; el: Element; top: number } | null>(null);
  const more = useCallback(() => {
    const box = moreRef.current?.closest<HTMLElement>(".chat-scroll");
    const el = moreRef.current?.nextElementSibling;
    if (box && el) keep.current = { box, el, top: el.getBoundingClientRect().top };
    setStart(windowStart(blocks, WINDOW, from));
  }, [blocks, from]);
  // what came in above leaves the view where it was (this runs before the chat's own scrolling to its end)
  useLayoutEffect(() => {
    const k = keep.current;
    keep.current = null;
    if (k && k.el.isConnected) k.box.scrollTop += k.el.getBoundingClientRect().top - k.top;
  }, [from]);
  // near the top of what is rendered: more of it (observed again after each step, in case it is still in view)
  useEffect(() => {
    const el = moreRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((es) => void (es.some((e) => e.isIntersecting) && more()), {
      root: el.closest(".chat-scroll"),
      rootMargin: "600px 0px 0px 0px",
    });
    io.observe(el);
    return () => io.disconnect();
  }, [more]);
  const hidden = useMemo(() => blocks.slice(0, from).reduce((n, b) => n + weight(b), 0), [blocks, from]);
  // the same elements while nothing here changes (a keystroke in the composer); else each block renders again only
  // if what it shows changed (sameBlock): a new item renders itself, not the whole conversation
  const rendered = useMemo(
    () =>
      blocks.slice(from).map((b) => (
        <BlockView
          key={b.key} b={b} results={results} running={running} cwd={cwd} day={day} shown={tapped === b.key} onTap={onTap}
          forks={forks} onFork={onFork} steps={steps} onForkStep={onForkStep}
          edits={edits} onEdit={onEdit} editing={b.type === "item" && b.item.kind === "user" && editing === b.item.id} editor={editor}
        />
      )),
    [blocks, from, results, running, cwd, day, tapped, onTap, forks, onFork, steps, onForkStep, edits, onEdit, editing, editor],
  );
  return (
    <FileLinks cwd={cwd} running={running}>
      {from > 0 && (
        <button ref={moreRef} className="chat-earlier" onClick={more}>
          <ChevronUp size={13} /> Earlier messages <span className="n">{hidden.toLocaleString()} more</span>
        </button>
      )}
      {rendered}
    </FileLinks>
  );
}

interface BlockProps {
  b: Block;
  results: Record<string, ResultItem>;
  running: boolean;
  cwd: string;
  day: string; // today (useDay): a message's time gets its date once it is yesterday's
  shown: boolean; // its time, tapped open
  onTap: (key: string, e: MouseEvent) => void;
  forks?: Map<string, ForkAt> | null;
  onFork?: (at: ForkAt, text: string) => void;
  steps?: Map<string, ForkAt> | null;
  onForkStep?: (at: ForkAt, step: string) => void;
  edits?: Map<string, EditAt> | null;
  onEdit?: (id: string) => void;
  editing: boolean;
  editor?: (item: UserItem, at: EditAt) => ReactNode;
}

/** Whether a block shows the same as before: its items and their results are the same objects (blocks are made
 *  again for every change, the items in them are kept), and so are the handlers of its buttons; where they fork or
 *  edit is worked out again for every change too, so it is compared by what it is. */
function sameBlock(p: BlockProps, n: BlockProps): boolean {
  if (p.cwd !== n.cwd || p.day !== n.day || p.shown !== n.shown || p.onTap !== n.onTap || p.b.type !== n.b.type) return false;
  const a = p.b, b = n.b;
  const step = (q: BlockProps, id: string) => (q.onForkStep ? q.steps?.get(id) : undefined);
  const sameStep = (id: string) => p.onForkStep === n.onForkStep && samePoint(step(p, id), step(n, id));
  switch (b.type) {
    case "tools": {
      const prev = (a as typeof b).tools;
      if (prev.length !== b.tools.length) return false;
      if (b.tools.some((t, i) => t !== prev[i] || p.results[t.id] !== n.results[t.id] || !sameStep(t.id))) return false;
      return p.running === n.running || b.tools.every((t) => n.results[t.id]); // a call still waiting spins while the turn runs
    }
    case "board":
      return (a as typeof b).tool === b.tool;
    case "review":
      return (a as typeof b).tool === b.tool && (a as typeof b).latest === b.latest && p.results[b.tool.id] === n.results[b.tool.id];
    case "item": {
      const it = b.item;
      if ((a as typeof b).item !== it) return false;
      if (it.kind === "text") return sameStep(it.id);
      if (it.kind !== "user") return true;
      return (
        p.onFork === n.onFork && samePoint(p.forks?.get(it.id), n.forks?.get(it.id)) &&
        p.onEdit === n.onEdit && samePoint(p.edits?.get(it.id), n.edits?.get(it.id)) &&
        p.editing === n.editing && (!n.editing || p.editor === n.editor)
      );
    }
  }
}

const BlockView = memo(function BlockView({ b, results, running, cwd, shown, onTap, forks, onFork, steps, onForkStep, edits, onEdit, editing, editor }: BlockProps) {
  const timed = { onClick: (e: MouseEvent) => onTap(b.key, e), "data-time": shown ? "shown" : undefined };
  if (b.type === "tools")
    return (
      <div className="timed" {...timed}>
        <div className="tools">
          {b.tools.map((t) => (
            <ToolRow key={t.id} tool={t} result={results[t.id]} cwd={cwd} running={running} at={onForkStep ? steps?.get(t.id) : undefined} onForkStep={onForkStep} />
          ))}
        </div>
        <MsgTime ts={b.tools[0].ts} />
      </div>
    );
  if (b.type === "board") return <BoardToolRow tool={b.tool} />;
  if (b.type === "review") return <ReviewToolBlock tool={b.tool} result={results[b.tool.id]} latest={b.latest} />;
  const it = b.item;
  switch (it.kind) {
    case "user": {
      const fork = forks?.get(it.id);
      const edit = onEdit ? edits?.get(it.id) : undefined;
      if (edit && editing && editor) return <div className="msg-edit-row">{editor(it, edit)}</div>;
      return (
        <div className="msg-user-row timed" {...timed}>
          {(edit || (fork && onFork)) && (
            <div className="msg-actions">
              {fork && onFork && (
                <button className="icon-btn sm" aria-label="Fork from here" data-tip="Fork from here: a new agent that remembers everything before this message" onClick={() => onFork(fork, it.text)}>
                  <GitFork size={13} />
                </button>
              )}
              {edit && (
                <button className="icon-btn sm" aria-label="Edit" data-tip="Edit: the conversation goes back to here and carries on with what you send instead" onClick={() => onEdit?.(it.id)}>
                  <Pencil size={13} />
                </button>
              )}
            </div>
          )}
          <div className={clsx("msg-user", it.command && "command")}>
            {it.attachments?.length ? <MessageAttachments items={it.attachments} /> : null}
            {it.command ? <CommandText text={it.text} /> : it.text}
          </div>
          <MsgTime ts={it.ts} />
        </div>
      );
    }
    case "text": {
      const at = onForkStep && steps?.get(it.id);
      return (
        <div className="msg-text timed" {...timed}>
          <Markdown text={it.text} />
          <MsgTime ts={it.ts} />
          {at && <StepForkButton onClick={() => onForkStep(at, it.text)} />}
        </div>
      );
    }
    case "command_output":
      return (
        <div className="msg-output">
          <Markdown text={it.text} />
        </div>
      );
    case "thinking":
      return (
        <details className="msg-thinking timed" {...timed}>
          <summary>
            <Brain size={13} /> Thinking <ChevronRight size={12} />
            <MsgTime ts={it.ts} />
          </summary>
          <div className="content">{it.text}</div>
        </details>
      );
    case "system":
      return <SystemLine item={it} />;
    case "result":
      return (
        <div className="msg-result timed" {...timed}>
          <MsgTime ts={it.ts} />
          {it.is_error ? <span className="pill err">{it.subtype}</span> : <span>Done</span>}
          {it.turns ? <span>{it.turns} turns</span> : null}
          {it.duration_ms ? <span>{fmtDuration(it.duration_ms)}</span> : null}
          {it.cost_usd ? <span>${it.cost_usd.toFixed(3)}</span> : null}
          {it.text && <span>{it.text}</span>}
        </div>
      );
    case "error":
      return (
        <div className="msg-error timed" {...timed}>
          {it.text}
          <MsgTime ts={it.ts} />
        </div>
      );
    default:
      return null;
  }
}, sameBlock);

/** A slash command as sent: the command, then its arguments. */
function CommandText({ text }: { text: string }) {
  const cut = text.search(/\s/);
  return (
    <>
      <span className="cmd-name">{cut < 0 ? text : text.slice(0, cut)}</span>
      {cut < 0 ? "" : text.slice(cut)}
    </>
  );
}

/** Info line; a subagent's end links to it (it can land turns after its Agent row, which has a link of its own).
 *  A compaction is a rule across the chat that opens the conversation before it; a summary's text folds. */
function SystemLine({ item }: { item: SystemItem }) {
  const done = item.subtype === "task_done";
  const open = useOpenSubagent(done ? (item.tool_use_id ?? null) : null, done && item.agent_id ? [item.agent_id] : undefined);
  const earlier = useOpenCompaction(item.subtype === "compact" ? (item.compaction ?? null) : null);
  if (item.subtype === "compact")
    return (
      <div className="msg-compact">
        <FoldVertical size={12} style={{ flex: "none" }} />
        <span>
          {item.text}
          {earlier && (
            <button className="sub-link inline" title="What came before, read-only (the agent keeps a summary)" onClick={earlier}>
              Earlier messages <ArrowUpRight size={12} />
            </button>
          )}
        </span>
      </div>
    );
  if (item.detail)
    return (
      <details className="msg-system-detail">
        <summary className="msg-system">
          <Info size={12} style={{ flex: "none" }} />
          <span>{item.text}</span>
          <ChevronRight size={12} className="chev" />
        </summary>
        <div className="msg-output">
          <Markdown text={item.detail} />
        </div>
      </details>
    );
  return (
    <div className="msg-system">
      <Info size={12} style={{ flex: "none" }} />
      <span>
        {item.text}
        {open && (
          <button className="sub-link inline" onClick={open}>
            Open <ArrowUpRight size={12} />
          </button>
        )}
      </span>
    </div>
  );
}

export const ChatView = memo(function ChatView({ pid, sid, cwd, compact, forkable, editable, modeLocked, placeholder, emptyHint }: {
  pid: string;
  sid: string;
  cwd: string;
  compact?: boolean;
  forkable?: boolean;
  editable?: boolean; // the user's messages can be changed (not the Review agent's: its rounds drive it)
  modeLocked?: boolean; // the session's permissions can't change (the Review agent is read-only)
  placeholder?: string;
  emptyHint?: JSX.Element;
}) {
  const meta = useApp((s) => s.meta);
  const project = useApp((s) => s.project);
  const toast = useApp((s) => s.toast);
  const setForkRequest = useApp((s) => s.setForkRequest);
  const refreshSessions = useApp((s) => s.refreshSessions);
  const refreshProject = useApp((s) => s.refreshProject);
  const [st, setSt] = useState<ChatState>(empty);
  // the session's row: the store's, else the one loaded with the conversation (the store leaves archived ones out)
  const listed = useApp((s) => s.sessions.find((x) => x.id === sid));
  const [loadedRow, setLoadedRow] = useState<SessionRow | null>(null);
  const row = listed ?? loadedRow;
  const harnessId = row?.harness ?? null;
  const [settings, setSettings] = useState<SessionSettings>({ model: "", effort: "", mode: "" });
  // what's typed here is the session's draft, kept outside this component (../drafts)
  const draft = useDrafts((s) => s.bySession[sid]) ?? EMPTY_REPLY;
  const updateReply = useDrafts((s) => s.updateReply);
  const text = draft.text;
  const setText = (text: string) => updateReply(sid, (d) => ({ ...d, text }));
  const setAttachments = useCallback(
    (fn: (xs: PendingUpload[]) => PendingUpload[]) => updateReply(sid, (d) => ({ ...d, attachments: fn(d.attachments) })),
    [sid, updateReply],
  );
  const att = useAttachments([draft.attachments, setAttachments]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  // where the agent carries on once its directory is gone (a removed worktree): picked here, applied on send
  const [ws, setWs] = useState<Workspace | null>(null);
  useEffect(() => setWs(null), [sid]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // load snapshot + live stream (buffer live items until the snapshot lands)
  useEffect(() => {
    let alive = true;
    let ready = false;
    const buffer: Item[] = [];
    // live items go in once a frame, together: a burst of them, or a reply streamed token by token, renders once
    let queued: Item[] = [];
    let frame = 0;
    const flush = () => {
      cancelAnimationFrame(frame);
      frame = 0;
      const its = queued;
      queued = [];
      setSt((s) => applyAll(s, its));
    };
    const queue = (it: Item) => {
      queued.push(it);
      if (queued.length >= 1000) flush(); // a hidden page gets no frames until it is shown
      else frame ||= requestAnimationFrame(flush);
    };
    setSt(empty());
    setLoadedRow(null);
    setLoading(true);
    stick.current = true;
    const unsub = socket.subscribe(`session:${sid}`, (msg) => {
      if (msg.type === "settings") setSettings(msg.settings);
      if (msg.type === "reload") {
        // a message was changed: the conversation is the one up to it, from here on (new items wait for it)
        ready = false;
        void load();
      }
      if (msg.type !== "item") return;
      if (!ready) buffer.push(msg.item);
      else queue(msg.item);
    });
    const load = () =>
      api
        .session(pid, sid)
        .then((snap) => {
          if (!alive) return;
          // what was still queued came before this was asked for: the snapshot has it
          cancelAnimationFrame(frame);
          frame = 0;
          queued = [];
          const s = applyAll(empty(), [...snap.items, ...buffer]);
          // drop permissions the server no longer considers pending
          const pend = new Set(snap.pending);
          s.pending = Object.fromEntries(Object.entries(s.pending).filter(([k]) => pend.has(k) || buffer.some((b) => b.kind === "permission" && b.id === k)));
          s.state = snap.live ? snap.state : null;
          if (!s.model && snap.settings?.model) s.model = snap.settings.model;
          setSt(s);
          setLoadedRow(snap.session);
          if (snap.settings) setSettings(snap.settings);
          ready = true;
          buffer.length = 0;
        })
        .catch((e) => toast(String(e.message || e), "error"))
        .finally(() => alive && setLoading(false));
    void load();
    const offRe = socket.onReconnect(() => {
      ready = false;
      void load();
    });
    return () => {
      alive = false;
      cancelAnimationFrame(frame);
      unsub();
      offRe();
    };
  }, [pid, sid, toast]);

  // autoscroll when pinned to the bottom
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [st.items.length, st.streaming, Object.keys(st.pending).length, loading]);
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  // autosize composer
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 280) + "px";
  }, [text]);
  // back to an unsent draft: carry on typing at its end (not in the Git Agent's drawer, which opens with its tab)
  useEffect(() => {
    const el = inputRef.current;
    if (compact || !el?.value) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [sid, compact]);

  const running = st.state === "running" || st.state === "starting" || st.state === "waiting";
  const blocks = useMemo(() => toBlocks(st.items), [st.items]);
  const harness = meta?.harnesses.find((h) => h.id === (harnessId ?? "claude-code")) ?? meta?.harnesses[0];
  const canFork = forkable && harness?.can_fork !== false;
  const forks = useMemo(() => (canFork ? forkPoints(st.items) : null), [st.items, canFork]);
  const onFork = useCallback((at: ForkAt, text: string) => setForkRequest({ sid, at, text }), [sid, setForkRequest]);
  const steps = useMemo(() => (canFork ? stepForkPoints(st.items) : null), [st.items, canFork]);
  const onForkStep = useCallback((at: ForkAt, step: string) => setForkRequest({ sid, at, step }), [sid, setForkRequest]);
  // changing a message: the one being changed, if any
  const canEdit = !!editable && !!harness?.can_edit;
  const edits = useMemo(() => (canEdit ? editPoints(st.items) : null), [st.items, canEdit]);
  const [editing, setEditing] = useState<string | null>(null);
  useEffect(() => setEditing(null), [sid]);
  const editor = useCallback(
    (it: UserItem, at: EditAt) => (
      <MessageEditor
        pid={pid}
        sid={sid}
        text={it.text}
        attachments={it.attachments ?? []}
        at={at}
        later={(edits?.size ?? 0) - at.user_index - 1}
        running={running}
        onClose={() => setEditing(null)}
        onSent={() => {
          stick.current = true;
          setEditing(null);
        }}
      />
    ),
    [pid, sid, edits, running],
  );
  const gone = !!row?.workspace_gone;
  const moveTo: MoveWorkspace | null = !gone || !ws ? null : ws.kind === "new" ? { kind: "new", branch: ws.branch.trim(), base: ws.base.trim() || undefined } : ws;
  const placed = !gone || (!!moveTo && (moveTo.kind !== "new" || !!moveTo.branch)); // a removed worktree: somewhere picked to go on
  const canSend = (!!text.trim() || att.ids.length > 0) && !att.uploading && !sending && placed;
  // a new worktree for an agent that lost its own starts out on that worktree's branch name
  const pickWs = (w: Workspace) => setWs(w.kind === "new" && !w.branch && row?.worktree && row.branch ? { ...w, branch: row.branch } : w);

  // `override`: a slash command picked in the menu, sent instead of what's typed
  const send = async (override?: string) => {
    const t = (override ?? text).trim();
    if (!(t || att.ids.length) || att.uploading || sending) return;
    if (forkable && atrium.run(t)) return setText(""); // an agent's chat (not the Git agent's)
    if (!placed) return;
    setSending(true);
    stick.current = true;
    try {
      if (moveTo) {
        await api.moveSession(pid, sid, moveTo);
        setWs(null);
        if (moveTo.kind === "new") void refreshProject();
        await refreshSessions();
      }
      await api.send(pid, sid, t, att.ids);
      setText("");
      att.clear();
    } catch (e: any) {
      toast(e.message || String(e), "error");
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  };

  const setEffort = async (effort: string) => {
    try {
      await api.setEffort(pid, sid, effort);
      setSettings((x) => ({ ...x, effort }));
      return true;
    } catch (e: any) {
      toast(e.message, "error");
      return false;
    }
  };

  const decide = async (perm: PermItem, d: Record<string, unknown>) => {
    try {
      await api.permission(pid, sid, { request_id: perm.id, ...d });
      if (d.then_mode) {
        await api.setMode(pid, sid, String(d.then_mode));
        setSt((s) => ({ ...s, mode: String(d.then_mode) }));
      }
    } catch (e: any) {
      toast(e.message || String(e), "error");
    }
  };

  const setMode = async (mode: string) => {
    try {
      await api.setMode(pid, sid, mode);
      setSt((s) => ({ ...s, mode }));
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  // detached sessions: show what the next attach will use (session settings, else the harness defaults)
  const defaults = meta?.settings.harnesses?.[harness?.id ?? ""] ?? {};
  const shownMode = st.mode || settings.mode || defaults.permission_mode || "";
  const shownModel = st.model || settings.model || defaults.model || harness?.config_model || "";
  const efforts = effortOptions(harness, shownModel || null);
  const baseEffort = defaults.effort || st.effort || harness?.config_effort; // in effect while no effort is picked
  const pendingList = Object.values(st.pending);
  // slash commands: Atrium's own in an agent's chat, then the harness's
  const atrium = useAtriumCommands({
    pid, sid, harness: harness?.id, cwd, running, canFork: harness?.can_fork !== false, efforts, effort: settings.effort || baseEffort || "", setEffort,
  });
  const skills = harness?.id === "codex";
  const harnessCommands = useSlashCommands(pid, { sid }, text.startsWith("/") || (skills && text.includes("$")));
  const commands = useMemo(() => mergeCommands(forkable ? atrium.commands : [], harnessCommands), [forkable, atrium.commands, harnessCommands]);
  const slash = useSlashMenu({ text, setText, input: inputRef, skills, commands, onRun: (t) => void send(t) });
  const voice = useVoiceInput({ pid, sid, text, setText, input: inputRef });
  const barRef = useChatFloor(running && !canSend); // the queue button's room while it's away: typing doesn't move the column

  return (
    <div className={clsx("chat", compact && "compact")}>
      <div className="chat-body">
        <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
          <div className="chat-inner">
            {loading && (
              <div className="msg-system">
                <Spinner /> Loading conversation…
              </div>
            )}
            {!loading && blocks.length === 0 && emptyHint}
            <Transcript
              blocks={blocks} results={st.results} running={running} cwd={cwd} forks={forks} onFork={onFork}
              steps={steps} onForkStep={onForkStep} edits={edits} editing={editing} onEdit={setEditing} editor={editor}
            />
            {st.streaming && (
              <div className="msg-text stream-cursor">
                <StreamingMarkdown text={st.streaming} />
              </div>
            )}
            <FileLinks cwd={cwd}>
              {pendingList.map((p) => (
                <PermissionCard key={p.id} perm={p} cwd={cwd} onDecide={(d) => decide(p, d)} />
              ))}
            </FileLinks>
            {running && !st.streaming && pendingList.length === 0 && (
              <div className="working-line">
                <span className="spark">
                  <Sparkle size={15} fill="currentColor" />
                </span>
                Working…
              </div>
            )}
          </div>
        </div>
        <ScrollAids target={scrollRef} onEnd={() => void (stick.current = true)} />
      </div>

      <div className="composer">
        <div className={clsx("composer-box", att.dragging && "dragging")} {...att.dropProps}>
          {att.picker}
          {slash.menu}
          <AttachmentTray items={att.items} onRemove={att.remove} />
          <textarea
            ref={inputRef}
            className="composer-input"
            rows={1}
            placeholder={
              placeholder ??
              (gone && !ws
                ? "Its worktree was removed: pick where it continues (below), then reply…"
                : running
                  ? harnessId === "codex"
                    ? "Steer the running turn…"
                    : "Queue a follow-up…"
                  : "Reply, or / for commands")
            }
            value={text}
            onChange={(e) => setText(e.target.value)}
            onSelect={slash.onSelect}
            onPaste={att.onPaste}
            onKeyDown={(e) => {
              if (slash.onKeyDown(e)) return;
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send();
              }
            }}
          />
          {gone && ws?.kind === "new" && project && <NewWorktreeFields project={project} ws={ws} onChange={setWs} />}
          {voice.strip}
          <div className="composer-bar" ref={barRef}>
            <div className="bar-chips">
            <AttachButton onClick={att.open} />
            {/* the new-agent page's chips, in its order; harness and workspace can't change any more (the
                workspace can once it's gone) */}
            {harness && !modeLocked && <PermissionPicker harness={harness} value={shownMode} onChange={(m) => void setMode(m)} detached={!st.mode} />}
            {project && row && !compact && <WorkspaceChip project={project} session={row} pick={ws} onPick={pickWs} />}
            {harness && !compact && (meta?.harnesses.length ?? 0) > 1 && <HarnessChip harness={harness} />}
            {harness && !compact && <ModelChip harness={harness} model={shownModel} faded={!st.model} />}
            </div>
            <div className="bar-end">
            <ContextRing used={st.usage?.used} max={st.usage?.max} />
            <EffortPicker efforts={efforts} value={settings.effort} base={baseEffort} onChange={(e) => void setEffort(e)} />
            {voice.button}
            {running ? (
              <>
                {canSend && (
                  <button className="send-btn" disabled={sending} onClick={() => void send()} title={harnessId === "codex" ? "Steer the running turn" : "Queue message"}>
                    <ArrowUp size={17} strokeWidth={2.4} />
                  </button>
                )}
                <button className={clsx("send-btn stop")} onClick={() => void api.interrupt(pid, sid)} title="Stop">
                  <Square size={13} fill="currentColor" />
                </button>
              </>
            ) : (
              <button
                className="send-btn"
                disabled={!canSend}
                onClick={() => void send()}
                title={gone && !moveTo ? "Pick where the agent continues first" : moveTo ? "Send (Enter) — moves the agent there first" : st.state ? "Send (Enter)" : "Send (Enter) — resumes the session"}
              >
                {sending ? <Spinner /> : <ArrowUp size={17} strokeWidth={2.4} />}
              </button>
            )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
});
