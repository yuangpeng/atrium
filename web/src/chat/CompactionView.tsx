// The part of an agent's conversation that a compaction closed, read-only: the agent now sees it only
// as the summary that opens the next part. Parts link up: each opens with the compaction before it.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowUpRight, FoldVertical, Info } from "lucide-react";
import { api, CompactionRow, Item } from "../api";
import { useApp } from "../store";
import { displayName } from "../util";
import { Spinner } from "../components/ui";
import { compactionLabel } from "../components/Compactions";
import { Transcript, apply, empty, toBlocks } from "./ChatView";

export function CompactionView({ pid, sid, cid, cwd }: { pid: string; sid: string; cid: string; cwd: string }) {
  const parent = useApp((s) => s.sessions.find((x) => x.id === sid));
  const selectSession = useApp((s) => s.selectSession);
  const selectCompaction = useApp((s) => s.selectCompaction);
  const [data, setData] = useState<{ compaction: CompactionRow & { index: number }; items: Item[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // a closed part doesn't change: load it once
  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    api.compaction(pid, sid, cid).then(
      (r) => alive && setData(r),
      (e) => alive && setError(e.message || String(e)),
    );
    return () => {
      alive = false;
    };
  }, [pid, sid, cid]);

  const st = useMemo(() => (data ? data.items.reduce(apply, empty()) : empty()), [data]);
  const blocks = useMemo(() => toBlocks(st.items), [st.items]);
  // open at its end, as a chat does: where the compaction cut it
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && data) el.scrollTop = el.scrollHeight;
  }, [data]);

  const parts = parent?.compactions ?? [];
  const at = parts.findIndex((c) => c.id === cid);
  const next = at >= 0 ? parts[at + 1] : undefined; // the part after, unless that's the chat
  const parentName = parent ? displayName(parent) : "the agent";

  return (
    <div className="chat">
      <div className="chat-scroll" ref={scrollRef}>
        <div className="chat-inner">
          {!data && !error && (
            <div className="msg-system">
              <Spinner /> Loading the compacted history…
            </div>
          )}
          {error && <div className="msg-error">{error}</div>}
          <Transcript blocks={blocks} results={st.results} running={false} cwd={cwd} />
          {data && blocks.length === 0 && <div className="msg-system">No messages recorded.</div>}
          {data && (
            <div className="msg-compact">
              <FoldVertical size={12} style={{ flex: "none" }} />
              <span>
                Compacted here
                <button className="sub-link inline" onClick={() => (next ? selectCompaction(sid, next.id) : selectSession(sid))}>
                  {next ? compactionLabel(at + 2) : "Current chat"} <ArrowUpRight size={12} />
                </button>
              </span>
            </div>
          )}
        </div>
      </div>
      <div className="sub-foot">
        <Info size={13} style={{ flex: "none" }} />
        <span className="grow ellipsis">Read-only: the agent keeps only a summary</span>
        <button className="btn btn-sm" title={`Back to ${parentName}`} onClick={() => selectSession(sid)}>
          <ArrowLeft size={13} /> Agent
        </button>
      </div>
    </div>
  );
}
