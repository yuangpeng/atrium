// Compactions: where an agent's context was compacted. The chat shows the conversation since the last
// one (what the agent still sees); each earlier part opens read-only under the agent in the rail, like a subagent.
import { History } from "lucide-react";
import { CompactionRow, SessionRow } from "../api";
import { useApp } from "../store";
import { clsx, displayName, timeAgo } from "../util";
import { fmtTokens } from "../chat/Composer";

/** Part `n` (1: the oldest) of an agent's conversation: what came before its n-th compaction. */
export const compactionLabel = (n: number) => `Compacted history #${n}`;

/** "Auto · 180k tokens": how the compaction came about. */
export function compactionFacts(c: CompactionRow): string {
  const how = c.trigger === "auto" ? "Auto (context full)" : c.trigger === "manual" ? "Manual" : "";
  return [how, c.tokens ? `${fmtTokens(c.tokens)} tokens` : ""].filter(Boolean).join(" · ");
}

/** "compacted 3h ago", "compacted just now", "compacted 9/30/2026" */
export function compactedWhen(c: CompactionRow): string {
  const t = timeAgo(c.at);
  return t && `compacted ${/^\d+[mhd]$/.test(t) ? `${t} ago` : t}`;
}

export function CompactionAvatar({ harness, size = 24 }: { harness: string; size?: number }) {
  return (
    <span className={clsx("sub-av compaction-av", `h-${harness}`)} style={{ width: size, height: size }}>
      <History size={Math.round(size * 0.56)} strokeWidth={2} />
    </span>
  );
}

export function CompactionTip({ c, n, parent }: { c: CompactionRow; n: number; parent: SessionRow }) {
  const facts = compactionFacts(c);
  return (
    <>
      <div className="tip-title">
        <CompactionAvatar harness={parent.harness} size={22} />
        <span className="grow ellipsis">{compactionLabel(n)}</span>
      </div>
      {c.first_prompt && <div className="tip-body">{c.first_prompt}</div>}
      {facts && <div className="tip-meta">{facts}</div>}
      <div className="tip-foot">
        <span className="ellipsis">↳ {displayName(parent)}</span>
        <span className="grow" />
        <span style={{ flex: "none" }}>{compactedWhen(c)}</span>
      </div>
    </>
  );
}

/** Opens the part of the selected agent's conversation that compaction `cid` closed, when Atrium lists it. */
export function useOpenCompaction(cid: string | null): (() => void) | null {
  const sessionId = useApp((s) => s.sessionId);
  const selectCompaction = useApp((s) => s.selectCompaction);
  const known = useApp((s) => !!cid && !!s.sessions.find((x) => x.id === s.sessionId)?.compactions?.some((c) => c.id === cid));
  return known && sessionId && cid ? () => selectCompaction(sessionId, cid) : null;
}
