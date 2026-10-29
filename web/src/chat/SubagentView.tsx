// A subagent's conversation, read-only: subagents take instructions from their parent only.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, CornerDownRight, Info, Sparkle } from "lucide-react";
import { api, Item, SubagentRow } from "../api";
import { socket } from "../ws";
import { useApp } from "../store";
import { displayName, throttle } from "../util";
import { Spinner } from "../components/ui";
import { Transcript, apply, empty, toBlocks } from "./ChatView";

export function SubagentView({ pid, sid, aid, cwd }: { pid: string; sid: string; aid: string; cwd: string }) {
  const parent = useApp((s) => s.sessions.find((x) => x.id === sid));
  const selectSession = useApp((s) => s.selectSession);
  const row = parent?.subagents?.find((a) => a.id === aid);
  const [data, setData] = useState<{ subagent: SubagentRow; items: Item[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useRef<() => void>(() => {});
  const toolUseId = useRef<string | null>(null);
  toolUseId.current = (row ?? data?.subagent)?.tool_use_id ?? null;
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // load, then reload (throttled) whenever the parent's live stream shows this subagent at work
  useEffect(() => {
    let alive = true;
    let busy = false;
    let again = false;
    setData(null);
    setError(null);
    stick.current = true;
    const load = async () => {
      if (busy) {
        again = true;
        return;
      }
      busy = true;
      try {
        const r = await api.subagent(pid, sid, aid);
        if (alive) {
          setData(r);
          setError(null);
        }
      } catch (e: any) {
        if (alive) setError(e.message || String(e));
      } finally {
        busy = false;
        if (again && alive) {
          again = false;
          void load();
        }
      }
    };
    reload.current = throttle(() => void load(), 1000);
    void load();
    const unsub = socket.subscribe(`session:${sid}`, (msg) => {
      if (msg.type === "item" && msg.item?.parent && msg.item.parent === toolUseId.current) reload.current();
    });
    return () => {
      alive = false;
      unsub();
    };
  }, [pid, sid, aid]);

  // ... and whenever the session list reports progress (transcript watcher, polling)
  const sig = `${row?.updated_at ?? ""}|${row?.status ?? ""}`;
  const lastSig = useRef(sig);
  useEffect(() => {
    if (sig === lastSig.current) return;
    lastSig.current = sig;
    reload.current();
  }, [sig]);

  const st = useMemo(() => (data ? data.items.reduce(apply, empty()) : empty()), [data]);
  const blocks = useMemo(() => toBlocks(st.items), [st.items]);
  const first = blocks[0]?.type === "item" && blocks[0].item.kind === "user" ? blocks[0].item : null; // the task, from the parent
  const sub = row ?? data?.subagent;
  const running = sub?.status === "running";

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [blocks.length, data]);
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  const parentName = parent ? displayName(parent) : "the parent agent";

  return (
    <div className="chat">
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="chat-inner">
          {!data && !error && (
            <div className="msg-system">
              <Spinner /> Loading subagent…
            </div>
          )}
          {error && <div className="msg-error">{error}</div>}
          {first?.kind === "user" && (
            <div className="sub-task">
              <div className="sub-task-head">
                <CornerDownRight size={13} /> Task from <b>{parentName}</b>
              </div>
              <div className="sub-task-body">{first.text}</div>
            </div>
          )}
          <Transcript blocks={first ? blocks.slice(1) : blocks} results={st.results} running={running} cwd={cwd} />
          {data && blocks.length === 0 && !running && <div className="msg-system">No messages recorded.</div>}
          {running && (
            <div className="working-line">
              <span className="spark">
                <Sparkle size={15} fill="currentColor" />
              </span>
              Working…
            </div>
          )}
        </div>
      </div>
      <div className="sub-foot">
        <Info size={13} style={{ flex: "none" }} />
        <span className="grow ellipsis">Read-only: a subagent takes instructions from its parent</span>
        <button className="btn btn-sm" title={`Back to ${parentName}`} onClick={() => selectSession(sid)}>
          <ArrowLeft size={13} /> Parent
        </button>
      </div>
    </div>
  );
}
