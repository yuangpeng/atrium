// Subagents: the agents a session spawned (Claude's Agent tool, Codex sub-agent threads).
// Read-only in Atrium: they open under their parent in the rail, and in the chat column.
import type { CSSProperties, ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import { BookOpen, Bot, ChevronUp, FlaskConical, Map as MapIcon, ScanEye, ScanSearch } from "lucide-react";
import { SessionRow, SubagentRow, SubagentStatus } from "../api";
import { useApp } from "../store";
import { clsx, displayName, timeAgo } from "../util";

const GLYPHS: [RegExp, LucideIcon][] = [
  [/explor|search|research|scout/i, ScanSearch],
  [/plan|architect/i, MapIcon],
  [/review|audit|critic|verif/i, ScanEye],
  [/test|\bqa\b/i, FlaskConical],
  [/doc|writ/i, BookOpen],
];

export const subagentGlyph = (a: SubagentRow): LucideIcon => GLYPHS.find(([re]) => re.test(a.agent_type || ""))?.[1] ?? Bot;

/** Short label: the Codex nickname, else the agent type. */
export const subagentLabel = (a: SubagentRow) => a.name || a.agent_type || "Subagent";

export const SUBAGENT_STATUS: Record<SubagentStatus, { label: string; dot: string; pill: string }> = {
  running: { label: "Running", dot: "running", pill: "accent" },
  done: { label: "Done", dot: "done", pill: "ok" },
  failed: { label: "Failed", dot: "error", pill: "err" },
  stopped: { label: "Stopped", dot: "stopped", pill: "" },
};

export function SubagentAvatar({ a, harness, size = 24 }: { a: SubagentRow; harness: string; size?: number }) {
  const Glyph = subagentGlyph(a);
  return (
    <span className={clsx("sub-av", `h-${harness}`)} style={{ width: size, height: size }}>
      <Glyph size={Math.round(size * 0.56)} strokeWidth={2} />
    </span>
  );
}

export function SubagentTip({ a, parent }: { a: SubagentRow; parent: SessionRow }) {
  const st = SUBAGENT_STATUS[a.status];
  return (
    <>
      <div className="tip-title">
        <SubagentAvatar a={a} harness={parent.harness} size={22} />
        <span className="grow ellipsis">{subagentLabel(a)}</span>
        <span className={clsx("pill", st.pill)}>{st.label}</span>
      </div>
      {a.description && <div className="tip-body">{a.description}</div>}
      <div className="tip-foot">
        <span className="ellipsis">↳ {displayName(parent)}</span>
        <span className="grow" />
        {a.model && <span className="mono">{a.model}</span>}
        <span>{timeAgo(a.updated_at)}</span>
      </div>
    </>
  );
}

const MAX_SHOWN = 6;
type Hover = (content: ReactNode) => { onMouseEnter: (e: React.MouseEvent<HTMLElement>) => void; onMouseLeave: () => void };

/** An agent's subagents, opened under it in the rail (running first, then newest). Always mounted, so it can slide open. */
export function RailSubagents({ parent, open, hover, onPick }: { parent: SessionRow; open: boolean; hover: Hover; onPick: (a: SubagentRow) => void }) {
  const sessionId = useApp((s) => s.sessionId);
  const subagentId = useApp((s) => s.subagentId);
  const [all, setAll] = useState(false);
  const subs = useMemo(
    () =>
      [...parent.subagents].sort(
        (x, y) => Number(y.status === "running") - Number(x.status === "running") || (y.created_at ?? y.updated_at) - (x.created_at ?? x.updated_at),
      ),
    [parent.subagents],
  );
  const shown = all ? subs : subs.slice(0, MAX_SHOWN);
  const tab = open ? 0 : -1;
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }), 230); // after the slide
    return () => clearTimeout(t);
  }, [open, all]);
  return (
    <div ref={ref} className={clsx("rail-subs", `h-${parent.harness}`, open && "open")} aria-hidden={!open}>
      <div className="rail-subs-inner">
        {shown.map((a, i) => (
          <button
            key={a.id}
            className={clsx("rail-sub", parent.id === sessionId && a.id === subagentId && "on")}
            style={{ "--i": i } as CSSProperties}
            tabIndex={tab}
            aria-label={`${subagentLabel(a)}: ${a.description}`}
            onClick={() => onPick(a)}
            {...hover(<SubagentTip a={a} parent={parent} />)}
          >
            <SubagentAvatar a={a} harness={parent.harness} />
            <span className={clsx("st dot", SUBAGENT_STATUS[a.status].dot)} />
          </button>
        ))}
        {subs.length > MAX_SHOWN && (
          <button
            className="rail-sub-more"
            tabIndex={tab}
            onClick={() => setAll((v) => !v)}
            {...hover(<div className="tip-title">{all ? "Show fewer" : `${subs.length - MAX_SHOWN} older subagents`}</div>)}
          >
            {all ? <ChevronUp size={12} /> : `+${subs.length - MAX_SHOWN}`}
          </button>
        )}
      </div>
    </div>
  );
}
