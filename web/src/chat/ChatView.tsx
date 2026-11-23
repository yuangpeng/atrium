import type { JSX, MouseEvent, ReactNode } from "react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, ArrowUpRight, Brain, ChevronRight, ChevronUp, FoldVertical, GitFork, Goal as GoalIcon, Info, Pencil, Sparkle, Square } from "lucide-react";
import { api, EditAt, ForkAt, Item, MoveWorkspace, SessionSettings, Upload } from "../api";
import { useApp } from "../store";
import { EMPTY_REPLY, Workspace, useDrafts } from "../drafts";
import { clsx, fmtClock, fmtDuration, samePoint } from "../util";
import { Markdown, Spinner, StreamingMarkdown, useDay } from "../components/ui";
import { useOpenCompaction } from "../components/Compactions";
import { HarnessHandoff } from "../components/HarnessHandoff";
import { FileLinks } from "../components/FileLinks";
import { BoardToolRow, PermissionCard, ToolRow, isBoardTool, useOpenSubagent } from "./ToolViews";
import { ReviewToolBlock, isReviewTool } from "./ReviewCards";
import {
  AttachButton, AttachmentTray, ContextRing, EffortPicker, HarnessChip, MessageAttachments, ModelPicker, NewWorktreeFields, PendingUpload, PermissionPicker,
  WorkspaceChip, compatibleEffort, effortOptions, useAttachments, useChatFloor,
} from "./Composer";
import { mergeCommands, useAtriumCommands, useSlashCommands, useSlashMenu } from "./SlashMenu";
import { useVoiceInput } from "./Voice";
import { MessageEditor } from "./MessageEditor";
import { modelRoutes } from "./modelRoutes";
import { ScrollAids } from "./ScrollAids";
import { GoalBar } from "./GoalBar";
import { Outgoing, newMessageId, post, sentAttachments, useOutbox, useOutgoing } from "./outbox";
import { ChatState, updateChat, useChat } from "./chatStore";

type UserItem = Extract<Item, { kind: "user" }>;
type ToolItem = Extract<Item, { kind: "tool" }>;
type ResultItem = Extract<Item, { kind: "tool_result" }>;
type PermItem = Extract<Item, { kind: "permission" }>;
type SystemItem = Extract<Item, { kind: "system" }>;

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
      return (a as typeof b).tool === b.tool && p.results[b.tool.id] === n.results[b.tool.id];
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
  if (b.type === "board") return <BoardToolRow tool={b.tool} result={results[b.tool.id]} />;
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

/** A message on its way (outbox), where it lands in the chat and as it will show there: faded until the server has it.
 *  The room of the buttons it will have (`fork`, `edit`) is kept, so that its text wraps as it will then. */
function OutgoingMessage({ o, fork, edit }: { o: Outgoing; fork: boolean; edit: boolean }) {
  return (
    <div className={clsx("msg-user-row outgoing", !o.sent && "sending")}>
      {(fork || edit) && (
        <div className="msg-actions" aria-hidden style={{ visibility: "hidden" }}>
          {fork && <span className="icon-btn sm" />}
          {edit && <span className="icon-btn sm" />}
        </div>
      )}
      <div className={clsx("msg-user", o.command && "command")}>
        {o.uploads.length ? <MessageAttachments items={sentAttachments(o.uploads)} /> : null}
        {o.command ? <CommandText text={o.text} /> : o.text}
      </div>
    </div>
  );
}

const errorText = (e: unknown) => (e instanceof Error && e.message) || String(e);

/** Info line; a subagent's end links to it (it can land turns after its Agent row, which has a link of its own).
 *  A compaction is a rule across the chat that opens the conversation before it; a summary's text folds. What happens
 *  to the thread's goal, and each turn it starts, gets the goal's mark. */
function SystemLine({ item }: { item: SystemItem }) {
  const done = item.subtype === "task_done";
  const open = useOpenSubagent(done ? (item.tool_use_id ?? null) : null, done && item.agent_id ? [item.agent_id] : undefined);
  const earlier = useOpenCompaction(item.subtype === "compact" ? (item.compaction ?? null) : null);
  const Icon = item.subtype === "goal" ? GoalIcon : Info;
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
          <Icon size={12} style={{ flex: "none" }} />
          <span>{item.text}</span>
          <ChevronRight size={12} className="chev" />
        </summary>
        <div className="msg-output">
          <Markdown text={item.detail} />
        </div>
      </details>
    );
  return (
    <div className={clsx("msg-system", item.subtype === "goal" && item.goal && `goal-${item.goal}`)}>
      <Icon size={12} style={{ flex: "none" }} />
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
  // the conversation, kept while other agents are opened (./chatStore): back to it, it shows at once
  const { st, row: loadedRow, settings, loading } = useChat(pid, sid);
  const setSt = (fn: (s: ChatState) => ChatState) => updateChat(pid, sid, (c) => ({ ...c, st: fn(c.st) }));
  const setSettings = (fn: (s: SessionSettings) => SessionSettings) => updateChat(pid, sid, (c) => ({ ...c, settings: fn(c.settings) }));
  // the session's row: the store's, else the one loaded with the conversation (the store leaves archived ones out)
  const listed = useApp((s) => s.sessions.find((x) => x.id === sid));
  const row = listed ?? loadedRow;
  const harnessId = row?.harness ?? null;
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
  // where the agent carries on once its directory is gone (a removed worktree): picked here, applied on send
  const [ws, setWs] = useState<Workspace | null>(null);
  // another harness to continue in, while its handoff is reviewed
  const [handoffTarget, setHandoffTarget] = useState<{ harnessId: string; model: string } | null>(null);
  const [modelChanging, setModelChanging] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // the session this chat shows (null once it's gone): a message that failed to go is written again here, if still here
  const here = useRef<string | null>(sid);
  useEffect(() => {
    here.current = sid;
    return () => void (here.current = null);
  }, [sid]);

  // another session in this chat (the Review drawer's): its end first, as a chat opening
  const shownSid = useRef(sid);
  if (shownSid.current !== sid) {
    shownSid.current = sid;
    stick.current = true;
  }

  const onScroll = () => {
    const el = scrollRef.current;
    if (el?.clientHeight) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; // (none: hidden)
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

  // messages on their way (outbox): shown until their own items come
  const outgoing = useOutgoing(sid);
  const shown = useMemo(() => outgoing.filter((o) => !st.seen.has(o.id)), [outgoing, st.seen]);
  useEffect(() => {
    if (shown.length < outgoing.length) useOutbox.getState().settle(sid, st.seen);
  }, [sid, outgoing, shown, st.seen]);
  // a message being changed: the conversation before it, until the server has gone back there too (and the chat
  // reloaded); the turn in progress, if any, is being stopped meanwhile
  const items = useMemo(() => {
    let end = st.items.length;
    for (const o of shown) {
      const i = o.edit ? st.items.findIndex((it) => "id" in it && it.id === o.edit) : -1;
      if (i >= 0 && i < end) end = i;
    }
    return end < st.items.length ? st.items.slice(0, end) : st.items;
  }, [st.items, shown]);
  const cut = items !== st.items;
  const streaming = cut ? "" : st.streaming;
  const pendingList = cut ? [] : Object.values(st.pending);

  // autoscroll when pinned to the bottom
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items.length, shown.length, streaming, pendingList.length, loading]);
  // kept hidden while another agent's chat is open (App's ChatColumn), then shown again: where it was read to (a hidden
  // box loses its scroll position)
  const readTo = useRef<number | null>(null);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && !stick.current && readTo.current !== null) el.scrollTop = readTo.current;
    return () => void (readTo.current = el ? el.scrollTop : null);
  }, []);

  const running = st.state === "running" || st.state === "starting" || st.state === "waiting";
  const blocks = useMemo(() => toBlocks(items), [items]);
  const harness = meta?.harnesses.find((h) => h.id === (harnessId ?? "claude-code")) ?? meta?.harnesses[0];
  const canFork = forkable && harness?.can_fork !== false;
  const forks = useMemo(() => (canFork ? forkPoints(items) : null), [items, canFork]);
  const onFork = useCallback((at: ForkAt, text: string) => setForkRequest({ sid, at, text }), [sid, setForkRequest]);
  const steps = useMemo(() => (canFork ? stepForkPoints(items) : null), [items, canFork]);
  const onForkStep = useCallback((at: ForkAt, step: string) => setForkRequest({ sid, at, step }), [sid, setForkRequest]);
  // changing a message: the one being changed, if any, and what was written in its place last time if that didn't go
  const canEdit = !!editable && !!harness?.can_edit;
  const edits = useMemo(() => (canEdit ? editPoints(items) : null), [items, canEdit]);
  const hasUser = useMemo(() => items.some((it) => it.kind === "user" && !it.command), [items]); // the first one can't be forked from
  const [editing, setEditing] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<{ text: string; uploads: Upload[] } | null>(null);
  const openEditor = useCallback((id: string | null) => {
    setEditing(id);
    setEditDraft(null);
  }, []);
  // another session in this chat (the Review drawer's): nothing picked or being changed there. Not when it is only shown
  // again (a chat kept hidden: App's ChatColumn), which finds it as it was left
  const pickedFor = useRef(sid);
  useEffect(() => {
    if (pickedFor.current === sid) return;
    pickedFor.current = sid;
    setWs(null);
    setHandoffTarget(null);
    openEditor(null);
  }, [sid, openEditor]);
  // for a change that failed: whether its message is still there to change again, and nothing else is being changed
  const now = useRef({ items: st.items, editing });
  useLayoutEffect(() => void (now.current = { items: st.items, editing }));
  const sendEdit = useCallback(
    (it: UserItem, at: EditAt, text: string, uploads: Upload[]) => {
      stick.current = true;
      openEditor(null);
      const id = newMessageId();
      post(sid, { id, text, uploads, edit: it.id }, () => api.editMessage(pid, sid, at, text, uploads.map((u) => u.id), id), {
        error: (e) => toast(errorText(e), "error"),
        // its editor again, if its message is still there and nothing else is being changed (else the reply box)
        back: () => {
          if (here.current !== sid || now.current.editing || !now.current.items.some((x) => "id" in x && x.id === it.id)) return false;
          now.current.editing = it.id; // (another one coming back with it finds this one open)
          setEditing(it.id);
          setEditDraft({ text, uploads });
          return true;
        },
      });
    },
    [pid, sid, toast, openEditor],
  );
  const editor = useCallback(
    (it: UserItem, at: EditAt) => (
      <MessageEditor
        text={it.text}
        attachments={it.attachments ?? []}
        draft={editDraft}
        later={(edits?.size ?? 0) - at.user_index - 1}
        running={running}
        onClose={() => openEditor(null)}
        onSend={(text, uploads) => sendEdit(it, at, text, uploads)}
      />
    ),
    [edits, running, editDraft, openEditor, sendEdit],
  );
  const gone = !!row?.workspace_gone;
  // a message on its way moves the agent: what is sent meanwhile goes after it, where it went (and the pick stays until then)
  const moving = outgoing.some((o) => o.move && o.sent === undefined);
  const moveTo: MoveWorkspace | null =
    !gone || !ws || moving ? null : ws.kind === "new" ? { kind: "new", branch: ws.branch.trim(), base: ws.base.trim() || undefined } : ws;
  const placed = !gone || moving || (!!moveTo && (moveTo.kind !== "new" || !!moveTo.branch)); // a removed worktree: somewhere picked to go on
  const handoffBlocked = running || outgoing.length > 0 || modelChanging || loading || !!editing || att.uploading || gone || !!row?.pending;
  const target = meta?.harnesses.find((h) => h.id === handoffTarget?.harnessId);
  const canHandoff = row?.role === "worker" && forkable && !modeLocked;
  // a new worktree for an agent that lost its own starts out on that worktree's branch name
  const pickWs = (w: Workspace) => setWs(w.kind === "new" && !w.branch && row?.worktree && row.branch ? { ...w, branch: row.branch } : w);

  // `override`: a slash command picked in the menu, sent instead of what's typed. The message shows in the chat at
  // once and the composer is free for the next one; what the server does first (start the agent again, move it) goes
  // on meanwhile, and the message comes back here if it fails (outbox)
  const send = (override?: string) => {
    const t = (override ?? text).trim();
    if (!(t || att.ids.length) || att.uploading || handoffTarget || modelChanging) return;
    if (forkable && atrium.run(t)) return setText(""); // an agent's chat (not the Git agent's)
    if (!placed) return;
    if (held(t)) return void (override !== undefined && setText(override)); // (a command picked in the menu waits there)
    const id = newMessageId();
    const uploads = att.items.flatMap((x) => (x.upload ? [x.upload] : []));
    const command = isCommand(t);
    const move = moveTo;
    stick.current = true;
    setText("");
    att.clear();
    post(
      sid,
      { id, text: t, uploads, command, move: !!move },
      async () => {
        if (move) {
          await api.moveSession(pid, sid, move);
          if (move.kind === "new") void refreshProject();
          await refreshSessions(); // its row no longer says gone: the pick has done its job
          if (here.current === sid) setWs(null);
        }
        return api.send(pid, sid, t, uploads.map((u) => u.id), id);
      },
      { error: (e) => toast(errorText(e), "error") },
    );
    if (!matchMedia("(pointer: coarse)").matches) inputRef.current?.focus(); // a finger that tapped send put the keyboard away
  };

  // the goal bar's buttons: /goal commands, sent as such (what's typed stays)
  const goalCommand = useCallback(
    (cmd: string) => {
      if (cmd === "/goal clear" && !confirm("Clear the goal? Codex stops working toward it and forgets it.")) return;
      stick.current = true;
      api.send(pid, sid, cmd).catch((e: any) => toast(e.message || String(e), "error"));
    },
    [pid, sid, toast],
  );

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
  const shownModel = settings.model || st.model || defaults.model || harness?.config_model || "";
  const efforts = effortOptions(harness, shownModel || null);
  const baseEffort = defaults.effort || st.effort || harness?.config_effort; // in effect while no effort is picked
  const setModel = async (pick: string) => {
    if (running || outgoing.length > 0 || modelChanging || loading || !placed || !!editing || att.uploading || !!row?.pending) return false;
    // Resolve Default explicitly: Codex turn/start otherwise keeps the thread's previous model.
    const model = pick || defaults.model || harness?.config_model || "";
    if (!model) return false;
    if (model === shownModel) return true;
    setModelChanging(true);
    try {
      await api.setModel(pid, sid, model);
      setSettings((current) => ({ ...current, model }));
      const oldEffort = settings.effort || baseEffort;
      const effort = compatibleEffort(harness, model, oldEffort);
      if (oldEffort && oldEffort !== effort) {
        if (!await setEffort(effort)) {
          toast("Model changed; choose a supported reasoning effort before sending.", "error");
          return true;
        }
      }
      toast("Model changed. Your next message uses " + model + ".", "ok");
      return true;
    } catch (e: any) {
      toast(e.message || String(e), "error");
      return false;
    } finally {
      setModelChanging(false);
    }
  };
  // slash commands: Atrium's own in an agent's chat, then the harness's
  const atrium = useAtriumCommands({
    pid, sid, harness: harness?.id, cwd, running, canFork: harness?.can_fork !== false, efforts, effort: settings.effort || baseEffort || "", setEffort,
  });
  const skills = harness?.id === "codex";
  const harnessCommands = useSlashCommands(pid, { sid }, text.startsWith("/") || (skills && text.includes("$")));
  const commands = useMemo(() => mergeCommands(forkable ? atrium.commands : [], harnessCommands), [forkable, atrium.commands, harnessCommands]);
  // one of the harness's slash commands (as far as the chat knows them)
  const isCommand = (t: string) => /^\/\S/.test(t) && !!commands?.some((c) => c.prefix === "/" && [c.name, ...c.aliases].includes(t.slice(1).split(/\s/)[0]!));
  // a slash command doesn't wait behind a message on its way, nor a message behind one: were the first to fail, both
  // would come back in the reply box as one text, the message the command's arguments (only for that moment)
  const unsent = outgoing.filter((o) => o.sent === undefined);
  const held = (t: string) => unsent.length > 0 && (isCommand(t.trim()) || unsent.some((o) => o.command));
  const canSend = !handoffTarget && !modelChanging && (!!text.trim() || att.ids.length > 0) && !att.uploading && placed && !held(text);
  const slash = useSlashMenu({ text, setText, input: inputRef, skills, commands, onRun: (t) => send(t) });
  const voice = useVoiceInput({ pid, sid, text, setText, input: inputRef });
  const barRef = useChatFloor(running && !canSend); // the queue button's room while it's away: typing doesn't move the column

  return (
    <div className={clsx("chat", compact && "compact")}>
      {target && row && <HarnessHandoff key={sid + ":" + target.id + ":" + handoffTarget?.model} pid={pid} source={row} target={target} initialModel={handoffTarget?.model} blocked={handoffBlocked}
        draft={text} hasAttachments={att.items.length > 0} onClose={() => setHandoffTarget(null)} />}
      <div className="chat-body">
        <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
          <div className="chat-inner">
            {loading && (
              <div className="msg-system">
                <Spinner /> Loading conversation…
              </div>
            )}
            {!loading && blocks.length === 0 && !shown.length && emptyHint /* (not under a first message on its way) */}
            <Transcript
              blocks={blocks} results={st.results} running={running} cwd={cwd} forks={forks} onFork={onFork}
              steps={steps} onForkStep={onForkStep} edits={edits} editing={editing} onEdit={openEditor} editor={editor}
            />
            {shown.map((o, k) => {
              // the buttons it will have: not a slash command's, nor a fork from the message that opens the conversation
              const opens = !hasUser && shown.slice(0, k).every((x) => x.command);
              return <OutgoingMessage key={o.id} o={o} fork={!!forks && !o.command && !opens} edit={!!edits && !o.command} />;
            })}
            {streaming && (
              <div className="msg-text stream-cursor">
                <StreamingMarkdown text={streaming} />
              </div>
            )}
            <FileLinks cwd={cwd}>
              {pendingList.map((p) => (
                <PermissionCard key={p.id} perm={p} cwd={cwd} onDecide={(d) => decide(p, d)} />
              ))}
            </FileLinks>
            {(running || shown.length > 0) && !streaming && pendingList.length === 0 && (
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
        {st.goal && <GoalBar goal={st.goal} running={running} onCommand={goalCommand} />}
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
                send();
              }
            }}
          />
          {gone && ws?.kind === "new" && project && <NewWorktreeFields project={project} ws={ws} onChange={setWs} />}
          {voice.strip}
          <div className="composer-bar" ref={barRef}>
            <div className="bar-chips">
            <AttachButton onClick={att.open} />
            {harness && !modeLocked && <PermissionPicker harness={harness} value={shownMode} onChange={(m) => void setMode(m)} detached={!st.mode} />}
            {project && row && !compact && <WorkspaceChip project={project} session={row} pick={ws} onPick={pickWs} />}
            {harness && !compact && (meta?.harnesses.length ?? 0) > 1 && <HarnessChip harness={harness} />}
            {harness && !compact && <ModelPicker harness={harness} value={shownModel} defaultModel={defaults.model || harness.config_model || null}
              routes={canHandoff && meta ? modelRoutes(meta.harnesses, meta.settings.defaults.harness) : undefined}
              onChange={(model, next = harness) => {
                if (next.id === harness.id) return setModel(model);
                if (!canHandoff || handoffBlocked) return false;
                setHandoffTarget({ harnessId: next.id, model });
                return true;
              }} disabled={running || outgoing.length > 0 || modelChanging || loading || !placed || !!editing || att.uploading || !!row?.pending}
              hint={running ? "Finish or stop this turn to change models." : !placed ? "Choose a workspace before changing models." : "Models using another runtime continue in a new conversation with your context."} />}
            </div>
            <div className="bar-end">
            <ContextRing used={st.usage?.used} max={st.usage?.max} />
            <EffortPicker efforts={efforts} value={settings.effort} base={baseEffort} onChange={(e) => void setEffort(e)} />
            {voice.button}
            {running ? (
              <>
                {canSend && (
                  <button className="send-btn" onClick={() => send()} title={harnessId === "codex" ? "Steer the running turn" : "Queue message"}>
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
                onClick={() => send()}
                title={!placed ? "Pick where the agent continues first" : held(text) ? "Once the message on its way has gone" : moveTo ? "Send (Enter) — moves the agent there first" : st.state ? "Send (Enter)" : "Send (Enter) — resumes the session"}
              >
                <ArrowUp size={17} strokeWidth={2.4} />
              </button>
            )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
});
