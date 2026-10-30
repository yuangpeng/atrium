import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp, Clock, MessageSquareText, Play, ScanSearch, Send } from "lucide-react";
import { api, ReviewSummary, ReviewView } from "../api";
import { useApp } from "../store";
import { clsx, displayName } from "../util";
import { ChatView } from "../chat/ChatView";
import { ReviewContext, ReviewCtx } from "../chat/ReviewCards";
import { Spinner, startRowResize } from "./ui";

const OPEN_KEY = "atrium.reviewOpen";
const HEIGHT_KEY = "atrium.reviewHeight";
const BUSY = new Set(["starting", "running", "waiting"]);

/** The Code Review Agent of one agent, under its diffs: rounds of findings, the discussion, and the
 *  messages that go back to the agent (only when the human sends them). */
export function ReviewDrawer({ pid, wsid, cwd, onJump }: { pid: string; wsid: string; cwd: string; onJump: (file: string, line: number | null) => void }) {
  const reviewVersion = useApp((s) => s.reviewVersion);
  const row = useApp((s) => s.sessions.find((x) => x.id === wsid));
  const toast = useApp((s) => s.toast);
  const [view, setView] = useState<ReviewView | null>(null);
  const [open, setOpen] = useState(() => localStorage.getItem(OPEN_KEY) !== "0");
  const [height, setHeight] = useState(() => Number(localStorage.getItem(HEIGHT_KEY)) || 0.46); // share of the column
  useEffect(() => localStorage.setItem(HEIGHT_KEY, String(height)), [height]);
  const ref = useRef<HTMLDivElement>(null);
  const [starting, setStarting] = useState(false);

  const load = useCallback(() => api.review(pid, wsid).then(setView).catch(() => setView(null)), [pid, wsid]);
  useEffect(() => {
    setView(null);
    void load();
  }, [load]);
  useEffect(() => {
    if (reviewVersion) void load();
  }, [reviewVersion, load]);

  const name = row ? displayName(row) : "the agent";
  const ctx: ReviewCtx = useMemo(
    () => ({ pid, worker: wsid, workerName: name, workerBusy: BUSY.has(row?.live ?? ""), view, reload: () => void load(), jump: onJump }),
    [pid, wsid, name, row?.live, view, load, onJump],
  );
  const last = view?.rounds[view.rounds.length - 1];
  const active = last?.state === "running" || last?.state === "queued";
  const gone = !!row?.workspace_gone; // its worktree was removed: nothing to review until it is moved

  const toggle = () =>
    setOpen((o) => {
      localStorage.setItem(OPEN_KEY, o ? "0" : "1");
      return !o;
    });
  const run = async () => {
    setStarting(true);
    try {
      const r = await api.reviewRun(pid, wsid);
      if ("skipped" in r) toast(`No review started: ${r.skipped}`, "info");
      if (!open) toggle();
      await load();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setStarting(false);
    }
  };
  const setAuto = async (auto: boolean) => {
    try {
      await api.reviewAuto(pid, wsid, auto);
      await load();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const outbox = async (send: boolean) => {
    try {
      await (send ? api.reviewDeliver(pid, wsid) : api.reviewCancelOutbox(pid, wsid));
      await load();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  return (
    <div ref={ref} className="drawer review-drawer" style={open ? { height: `${height * 100}%` } : undefined}>
      {open && <div className="hresizer" onPointerDown={(e) => startRowResize(e, ref, setHeight)} />}
      <div className="drawer-head" onClick={toggle}>
        <ScanSearch size={14} className="muted" />
        <span>Review</span>
        <span className="grow faint ellipsis rv-status">{statusLine(view, name)}</span>
        {view && (
          <label
            className="rv-auto"
            title="Review automatically when this agent reports its task done or waiting for you"
            onClick={(e) => e.stopPropagation()}
          >
            <input type="checkbox" checked={view.auto} onChange={(e) => void setAuto(e.target.checked)} /> Auto
          </label>
        )}
        <button
          className="btn btn-sm"
          disabled={starting || active || gone}
          title={gone ? "Its worktree was removed: move the agent (from its chat) first" : "Review this agent's changes now: those since the last review, else all of them"}
          onClick={(e) => (e.stopPropagation(), void run())}
        >
          {starting || active ? <Spinner /> : <Play size={12} />} Review
        </button>
        {open ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
      </div>
      {open && gone && (
        <div className="banner">
          <AlertTriangle size={13} /> {name}'s worktree was removed: reviews resume once it is moved (from its chat).
        </div>
      )}
      {open &&
        (view?.session ? (
          <ReviewContext.Provider value={ctx}>
            {view.outbox && (
              <div className="banner rv-outbox">
                <Send size={13} />
                <span className="grow ellipsis">{name} is working: your review message goes out when its turn ends.</span>
                <button className="btn btn-sm" onClick={() => void outbox(true)} title="Send it now, into the turn in progress">
                  Send now
                </button>
                <button className="btn btn-sm btn-ghost" onClick={() => void outbox(false)}>
                  Cancel
                </button>
              </div>
            )}
            <ChatView
              pid={pid}
              sid={view.session}
              cwd={cwd}
              compact
              modeLocked
              placeholder="Discuss the findings — e.g. adopt 1 and 3, drop 2, then draft the message"
            />
          </ReviewContext.Provider>
        ) : (
          <div className="empty-state rv-empty">
            <ScanSearch size={24} />
            <div>
              {view?.auto ? "A review starts when this agent reports its task done or waiting for you." : "Automatic reviews are off for this agent."} The reviewer only
              reads; you decide what goes back to the agent.
            </div>
            <button className="btn btn-sm btn-primary" disabled={starting || gone} onClick={() => void run()}>
              {starting ? <Spinner /> : <Play size={12} />} Review now
            </button>
          </div>
        ))}
    </div>
  );
}

function statusLine(view: ReviewView | null, name: string): string {
  const last = view?.rounds[view.rounds.length - 1];
  if (!view || !last) return view?.auto ? `Not reviewed yet · starts when ${name} reports done` : "Not reviewed yet";
  const open = last.findings.filter((f) => f.status === "open" || f.status === "adopted").length;
  switch (last.state) {
    case "queued":
      return `Round ${last.n} waiting to start`;
    case "running":
      return `Reviewing round ${last.n}…`;
    case "stopped":
      return `Round ${last.n} stopped`;
    case "failed":
      return `Round ${last.n} failed${last.error ? `: ${last.error}` : ""}`;
    default:
      if (!last.findings.length) return `Round ${last.n} · no findings`;
      return `Round ${last.n} · ${open ? `${open} of ${last.findings.length} to decide or send` : "all decided"}`;
  }
}

/** The review's state next to an agent in the review inbox. */
export function ReviewBadge({ r }: { r?: ReviewSummary }) {
  if (!r) return null;
  if (r.state === "running")
    return (
      <span className="rv-badge running" title="Reviewing…">
        <ScanSearch size={12} />
      </span>
    );
  if (r.state === "queued")
    return (
      <span className="rv-badge" title="Review waiting to start">
        <Clock size={12} />
      </span>
    );
  return (
    <>
      {r.outbox && (
        <span className="rv-badge" title="Review feedback waiting for the agent's turn to end">
          <Send size={12} />
        </span>
      )}
      {r.open > 0 && (
        <span className={clsx("rv-badge", "open")} title={`${r.open} review finding${r.open === 1 ? "" : "s"} to decide on or send`}>
          <MessageSquareText size={12} /> {r.open}
        </span>
      )}
    </>
  );
}
