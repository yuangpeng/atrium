// Subagents: the agents a session spawned (Claude's Agent tool, Codex sub-agent threads).
// Read-only in Atrium: they open under their parent in the rail (with its compacted history), and in the chat column.
import type { CSSProperties, ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import { BookOpen, Bot, ChevronUp, FlaskConical, Map as MapIcon, ScanEye, ScanSearch } from "lucide-react";
import { CompactionRow, SessionRow, SubagentRow, SubagentStatus } from "../api";
import { useApp } from "../store";
import { clsx, displayName, timeAgo } from "../util";
import { CompactionAvatar, CompactionTip, compactionLabel } from "./Compactions";

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

type Entry = { sub: SubagentRow } | { compaction: CompactionRow; n: number };

/** What opens under an agent in the rail: its subagents (running first, then newest), then its compacted
 *  history (newest first). Always mounted, so it can slide open. */
export function RailSubagents({ parent, open, hover, onPick, onPickCompaction }: {
  parent: SessionRow;
  open: boolean;
  hover: Hover;
  onPick: (a: SubagentRow) => void;
  onPickCompaction: (c: CompactionRow) => void;
}) {
  const sessionId = useApp((s) => s.sessionId);
  const subagentId = useApp((s) => s.subagentId);
  const compactionId = useApp((s) => s.compactionId);
  const [all, setAll] = useState(false);
  const entries = useMemo<Entry[]>(
    () => [
      ...[...parent.subagents]
        .sort((x, y) => Number(y.status === "running") - Number(x.status === "running") || (y.created_at ?? y.updated_at) - (x.created_at ?? x.updated_at))
        .map((sub) => ({ sub })),
      ...(parent.compactions ?? []).map((compaction, i) => ({ compaction, n: i + 1 })).reverse(),
    ],
    [parent.subagents, parent.compactions],
  );
  const shown = all ? entries : entries.slice(0, MAX_SHOWN);
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
        {shown.map((e, i) =>
          "sub" in e ? (
            <button
              key={e.sub.id}
              className={clsx("rail-sub", parent.id === sessionId && e.sub.id === subagentId && "on")}
              style={{ "--i": i } as CSSProperties}
              tabIndex={tab}
              aria-label={`${subagentLabel(e.sub)}: ${e.sub.description}`}
              onClick={() => onPick(e.sub)}
              {...hover(<SubagentTip a={e.sub} parent={parent} />)}
            >
              <SubagentAvatar a={e.sub} harness={parent.harness} />
              <span className={clsx("st dot", SUBAGENT_STATUS[e.sub.status].dot)} />
            </button>
          ) : (
            <button
              key={e.compaction.id}
              className={clsx("rail-sub", parent.id === sessionId && e.compaction.id === compactionId && "on")}
              style={{ "--i": i } as CSSProperties}
              tabIndex={tab}
              aria-label={compactionLabel(e.n)}
              onClick={() => onPickCompaction(e.compaction)}
              {...hover(<CompactionTip c={e.compaction} n={e.n} parent={parent} />)}
            >
              <CompactionAvatar harness={parent.harness} />
            </button>
          ),
        )}
        {entries.length > MAX_SHOWN && (
          <button
            className="rail-sub-more"
            tabIndex={tab}
            onClick={() => setAll((v) => !v)}
            {...hover(<div className="tip-title">{all ? "Show fewer" : `${entries.length - MAX_SHOWN} more`}</div>)}
          >
            {all ? <ChevronUp size={12} /> : `+${entries.length - MAX_SHOWN}`}
          </button>
        )}
      </div>
    </div>
  );
}
