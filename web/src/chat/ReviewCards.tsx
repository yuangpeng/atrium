import { createContext, useContext, useEffect, useRef, useState } from "react";
import { Check, ClipboardList, ListChecks, Send, X } from "lucide-react";
import { api, Item, ReviewFinding, ReviewRound, ReviewView } from "../api";
import { useApp } from "../store";
import { clsx } from "../util";
import { Markdown } from "../components/ui";

type ToolItem = Extract<Item, { kind: "tool" }>;
type ResultItem = Extract<Item, { kind: "tool_result" }>;

/** What the review drawer knows: the chat's review tool calls render live from it (statuses, drafts). */
export interface ReviewCtx {
  pid: string;
  worker: string; // the reviewed agent's session
  workerName: string;
  workerBusy: boolean; // a message sent now waits until its turn ends
  view: ReviewView | null;
  reload: () => void;
  jump: (file: string, line: number | null) => void;
}

export const ReviewContext = createContext<ReviewCtx | null>(null);

export const isReviewTool = (name: string) => name.startsWith("mcp__review__");

/** Scroll the chat (only it: scrollIntoView would move clipped ancestors too) to an element's top or bottom. */
function showInChat(el: HTMLElement | null, edge: "top" | "bottom") {
  const sc = el?.closest<HTMLElement>(".chat-scroll");
  if (!el || !sc) return;
  const r = el.getBoundingClientRect();
  const s = sc.getBoundingClientRect();
  if (edge === "top") sc.scrollTop += r.top - s.top - 8;
  else if (r.bottom > s.bottom) sc.scrollTop += Math.min(r.bottom - s.bottom + 8, r.top - s.top - 8);
}

const SEVERITIES = ["P0", "P1", "P2", "P3"] as const;
const SEVERITY_HINT: Record<string, string> = {
  P0: "Must be fixed before anything else",
  P1: "A real bug or regression",
  P2: "Should be fixed",
  P3: "Optional",
};

/** The message a set of adopted findings becomes (editable before it is sent); in the findings' language. */
export function composeMessage(fs: ReviewFinding[]): string {
  const zh = fs.some((f) => /[\u4e00-\u9fff]/.test(f.title + f.problem + f.suggestion));
  const [intro, problem, fix, outro] = zh
    ? ["请处理下面这些 review 建议：", "问题：", "改法：", "改完后简要说明每一条是怎么处理的。"]
    : ["Please address these review findings:", "Problem: ", "Fix: ", "Then say briefly how you handled each one."];
  const indent = (s: string) => s.trim().replace(/\n/g, "\n   ");
  const items = fs.map((f, i) =>
    [`${i + 1}. [${f.severity}] ${f.file}${f.line ? `:${f.line}` : ""} — ${f.title}`, `   ${problem}${indent(f.problem)}`, `   ${fix}${indent(f.suggestion)}`].join("\n"),
  );
  return `${intro}\n\n${items.join("\n\n")}\n\n${outro}`;
}

/** A review tool call in the reviewer's chat: findings (live when it is the round's latest report),
 *  a draft for the agent (with Send), or a decision recorded. */
export function ReviewToolBlock({ tool, result, latest }: { tool: ToolItem; result?: ResultItem; latest: boolean }) {
  const ctx = useContext(ReviewContext);
  const name = tool.name.replace(/^mcp__review__/, "");
  const inp = tool.input || {};
  if (result?.is_error)
    return (
      <div className="review-tool err">
        <X size={13} /> <span className="grow ellipsis">{result.content}</span>
      </div>
    );
  if (name === "report") {
    const round = ctx?.view?.rounds.find((r) => r.n === Number(inp.round));
    if (ctx && round && latest) return <FindingsCard round={round} />;
    const n = Array.isArray(inp.findings) ? inp.findings.length : 0;
    return (
      <div className="review-tool">
        <ClipboardList size={13} />
        <span className="grow ellipsis">
          Round {inp.round}: {n} finding{n === 1 ? "" : "s"}
          {latest ? "" : " — revised below"}
        </span>
      </div>
    );
  }
  if (name === "draft_message") return <DraftCard text={String(inp.message ?? "")} />;
  const ids = (inp.ids ?? []).map((i: string) => `#${i}`).join(", ");
  return (
    <div className="review-tool">
      <ListChecks size={13} />
      <span className="grow ellipsis">
        Round {inp.round}: {ids} → {inp.status}
      </span>
    </div>
  );
}

function FindingsCard({ round }: { round: ReviewRound }) {
  const ctx = useContext(ReviewContext)!;
  const toast = useApp((s) => s.toast);
  const [composing, setComposing] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // the round's findings open at their top (most severe first), not at the chat's end
  useEffect(() => showInChat(ref.current, "top"), [round.n]);
  const adopted = round.findings.filter((f) => f.status === "adopted");
  const counts = SEVERITIES.map((s) => [s, round.findings.filter((f) => f.severity === s).length] as const).filter(([, n]) => n > 0);
  const setStatus = async (f: ReviewFinding, status: "open" | "adopted" | "dismissed") => {
    try {
      await api.reviewFindings(ctx.pid, ctx.worker, round.n, [f.id], status);
      ctx.reload();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  return (
    <div ref={ref} className="review-card">
      <div className="rc-head">
        <ClipboardList size={14} />
        <span className="grow">
          Round {round.n} · {round.findings.length ? `${round.findings.length} finding${round.findings.length === 1 ? "" : "s"}` : "no findings"}
        </span>
        {counts.map(([s, n]) => (
          <span key={s} className={`sev ${s}`} title={SEVERITY_HINT[s]}>
            {s}
            {n > 1 ? ` ×${n}` : ""}
          </span>
        ))}
      </div>
      {round.summary && (
        <div className="rc-summary">
          <Markdown text={round.summary} />
        </div>
      )}
      {round.findings.map((f) => (
        <FindingRow key={f.id} f={f} onStatus={(s) => void setStatus(f, s)} />
      ))}
      {composing ? (
        <SendBox initial={composeMessage(adopted)} round={round.n} onClose={() => setComposing(false)} />
      ) : (
        adopted.length > 0 && (
          <div className="rc-foot">
            <span className="grow faint">{adopted.length} adopted</span>
            <button className="btn btn-sm btn-primary" onClick={() => setComposing(true)}>
              <Send size={12} /> Send to {ctx.workerName}…
            </button>
          </div>
        )
      )}
    </div>
  );
}

function FindingRow({ f, onStatus }: { f: ReviewFinding; onStatus: (s: "open" | "adopted" | "dismissed") => void }) {
  const ctx = useContext(ReviewContext)!;
  const loc = `${f.file}${f.line ? `:${f.line}${f.end_line && f.end_line !== f.line ? `–${f.end_line}` : ""}` : ""}`;
  return (
    <div className={clsx("finding", f.status)}>
      <div className="f-head">
        <span className={`sev ${f.severity}`} title={SEVERITY_HINT[f.severity]}>
          {f.severity}
        </span>
        <span className="f-title grow">
          <span className="faint">#{f.id}</span> {f.title}
        </span>
        {f.status === "sent" ? (
          <span className="pill ok">Sent</span>
        ) : (
          <span className="f-acts">
            <button
              className={clsx("btn btn-sm", f.status === "adopted" && "on")}
              title={f.status === "adopted" ? "Adopted: it goes to the agent with the next message (click to undo)" : "Adopt: send it to the agent"}
              onClick={() => onStatus(f.status === "adopted" ? "open" : "adopted")}
            >
              <Check size={12} /> {f.status === "adopted" ? "Adopted" : "Adopt"}
            </button>
            <button
              className={clsx("icon-btn sm", f.status === "dismissed" && "on")}
              title={f.status === "dismissed" ? "Dismissed (click to undo)" : "Dismiss"}
              onClick={() => onStatus(f.status === "dismissed" ? "open" : "dismissed")}
            >
              <X size={13} />
            </button>
          </span>
        )}
      </div>
      {f.status !== "dismissed" && (
        <>
          {f.file && (
            <button className="f-loc mono" title="Show in the diff" onClick={() => ctx.jump(f.file, f.line)}>
              {loc}
            </button>
          )}
          <div className="f-body">
            <Markdown text={f.problem} />
          </div>
          {f.suggestion && (
            <div className="f-fix">
              <Markdown text={f.suggestion} />
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** The reviewer's draft for the agent: nothing is sent until the human presses Send. */
function DraftCard({ text }: { text: string }) {
  const ctx = useContext(ReviewContext);
  const toast = useApp((s) => s.toast);
  const draft = ctx?.view?.drafts.filter((d) => d.text === text).pop();
  if (!ctx || !draft)
    return (
      <div className="review-tool">
        <Send size={13} /> <span className="grow ellipsis">Drafted a message for the agent</span>
      </div>
    );
  if (draft.status === "pending")
    return (
      <SendBox
        initial={draft.text}
        round={draft.round}
        draft={draft.id}
        onClose={() =>
          void api
            .reviewCancelDraft(ctx.pid, ctx.worker, draft.id)
            .then(ctx.reload)
            .catch((e) => toast(e.message, "error"))
        }
      />
    );
  const what = { sent: `Sent to ${ctx.workerName}`, cancelled: "Draft cancelled", replaced: "Draft replaced by a newer one" }[draft.status];
  return (
    <details className="review-tool sent">
      <summary>
        <Send size={13} /> <span className="grow ellipsis">{what}</span>
      </summary>
      <div className="sent-text">{draft.sent_text ?? draft.text}</div>
    </details>
  );
}

/** An editable message for the agent, sent (or queued while it works) only from here. */
function SendBox({ initial, round, draft, onClose }: { initial: string; round: number | null; draft?: number; onClose: () => void }) {
  const ctx = useContext(ReviewContext)!;
  const toast = useApp((s) => s.toast);
  const [text, setText] = useState(initial);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => showInChat(ref.current, "bottom"), []);
  const send = async () => {
    setBusy(true);
    try {
      const r = await api.reviewSend(ctx.pid, ctx.worker, { text, round, draft: draft ?? null });
      toast(r.queued ? `${ctx.workerName} is working: it gets the message when its turn ends` : `Sent to ${ctx.workerName}`, "ok");
      if (draft == null) onClose();
      ctx.reload();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div ref={ref} className="send-box">
      <div className="sb-head">
        <Send size={13} />
        <span className="grow ellipsis">
          Message to <b>{ctx.workerName}</b>
          <span className="faint"> · {draft != null ? "drafted by the reviewer, " : ""}edit before sending</span>
        </span>
      </div>
      <textarea className="textarea" value={text} rows={Math.min(14, Math.max(4, text.split("\n").length))} onChange={(e) => setText(e.target.value)} />
      <div className="sb-acts">
        <span className="grow faint ellipsis">{ctx.workerBusy ? "It is working: the message waits until its turn ends" : ""}</span>
        <button className="btn btn-sm btn-ghost" onClick={onClose} disabled={busy}>
          {draft != null ? "Discard" : "Cancel"}
        </button>
        <button className="btn btn-sm btn-primary" disabled={busy || !text.trim()} onClick={() => void send()}>
          <Send size={12} /> Send to {ctx.workerName}
        </button>
      </div>
    </div>
  );
}
