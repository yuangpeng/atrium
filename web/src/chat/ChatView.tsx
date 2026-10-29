import type { JSX } from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, ArrowUpRight, Brain, ChevronRight, Info, Sparkle, Square } from "lucide-react";
import { api, Item, LiveState, SessionSettings } from "../api";
import { socket } from "../ws";
import { useApp } from "../store";
import { clsx, fmtDuration } from "../util";
import { Markdown, Spinner } from "../components/ui";
import { BoardToolRow, PermissionCard, ToolRow, isBoardTool, useOpenSubagent } from "./ToolViews";
import { AttachButton, AttachmentTray, EffortPicker, MessageAttachments, effortOptions, useAttachments } from "./Composer";

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
}

export const empty = (): ChatState => ({
  items: [], seen: new Set(), results: {}, pending: {}, resolved: new Set(), streaming: "", state: null, mode: null, model: null,
});

export function apply(st: ChatState, it: Item): ChatState {
  switch (it.kind) {
    case "delta":
      return { ...st, streaming: st.streaming + it.text };
    case "state":
      return { ...st, state: it.state, streaming: it.state === "running" ? st.streaming : "" };
    case "tool_result":
      return { ...st, results: { ...st.results, [it.id]: it } };
    case "permission": {
      if (st.resolved.has(it.id)) return st;
      return { ...st, pending: { ...st.pending, [it.id]: it } };
    }
    case "permission_resolved": {
      const pending = { ...st.pending };
      delete pending[it.id];
      const resolved = new Set(st.resolved);
      resolved.add(it.id);
      return { ...st, pending, resolved };
    }
    default: {
      if (st.seen.has(it.id)) {
        // harnesses may re-send a tool call once its input is final (upsert)
        if (it.kind === "tool") return { ...st, items: st.items.map((x) => (x.kind === "tool" && x.id === it.id ? it : x)) };
        return st;
      }
      const seen = new Set(st.seen);
      seen.add(it.id);
      const next: ChatState = { ...st, items: [...st.items, it], seen };
      if (it.kind === "text") next.streaming = "";
      if (it.kind === "system" && it.subtype === "init") {
        next.mode = it.permission_mode ?? next.mode;
        next.model = it.model ?? next.model;
      }
      return next;
    }
  }
}

export type Block =
  | { type: "tools"; key: string; tools: ToolItem[] }
  | { type: "board"; key: string; tool: ToolItem }
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
      const last = out[out.length - 1];
      if (last?.type === "tools") last.tools.push(it);
      else out.push({ type: "tools", key: it.id, tools: [it] });
    } else if ("id" in it && it.kind !== "permission" && it.kind !== "permission_resolved") {
      out.push({ type: "item", key: it.id, item: it });
    }
  }
  return out;
}

export function Transcript({ blocks, results, running, cwd }: { blocks: Block[]; results: Record<string, ResultItem>; running: boolean; cwd: string }) {
  const rendered = useMemo(
    () =>
      blocks.map((b) => {
        if (b.type === "tools")
          return (
            <div className="tools" key={b.key}>
              {b.tools.map((t) => (
                <ToolRow key={t.id} tool={t} result={results[t.id]} cwd={cwd} running={running} />
              ))}
            </div>
          );
        if (b.type === "board") return <BoardToolRow key={b.key} tool={b.tool} />;
        const it = b.item;
        switch (it.kind) {
          case "user":
            return (
              <div className="msg-user" key={b.key}>
                {it.attachments?.length ? <MessageAttachments items={it.attachments} /> : null}
                {it.text}
              </div>
            );
          case "text":
            return (
              <div className="msg-text" key={b.key}>
                <Markdown text={it.text} />
              </div>
            );
          case "thinking":
            return (
              <details className="msg-thinking" key={b.key}>
                <summary>
                  <Brain size={13} /> Thinking <ChevronRight size={12} />
                </summary>
                <div className="content">{it.text}</div>
              </details>
            );
          case "system":
            return <SystemLine key={b.key} item={it} />;
          case "result":
            return (
              <div className="msg-result" key={b.key}>
                {it.is_error ? <span className="pill err">{it.subtype}</span> : <span>Done</span>}
                {it.turns ? <span>{it.turns} turns</span> : null}
                {it.duration_ms ? <span>{fmtDuration(it.duration_ms)}</span> : null}
                {it.cost_usd ? <span>${it.cost_usd.toFixed(3)}</span> : null}
                {it.text && <span>{it.text}</span>}
              </div>
            );
          case "error":
            return (
              <div className="msg-error" key={b.key}>
                {it.text}
              </div>
            );
          default:
            return null;
        }
      }),
    [blocks, results, running, cwd],
  );
  return <>{rendered}</>;
}

/** Info line; a subagent's end links to it (it can land turns after its Agent row, which has a link of its own). */
function SystemLine({ item }: { item: SystemItem }) {
  const open = useOpenSubagent(item.subtype === "task_done" ? (item.tool_use_id ?? null) : null);
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

export function ChatView({ pid, sid, cwd, compact, placeholder, emptyHint }: { pid: string; sid: string; cwd: string; compact?: boolean; placeholder?: string; emptyHint?: JSX.Element }) {
  const meta = useApp((s) => s.meta);
  const toast = useApp((s) => s.toast);
  const [st, setSt] = useState<ChatState>(empty);
  const [harnessId, setHarnessId] = useState<string | null>(null);
  const [settings, setSettings] = useState<SessionSettings>({ model: "", effort: "", mode: "" });
  const att = useAttachments();
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // load snapshot + live stream (buffer live items until the snapshot lands)
  useEffect(() => {
    let alive = true;
    let ready = false;
    const buffer: Item[] = [];
    setSt(empty());
    setLoading(true);
    stick.current = true;
    const unsub = socket.subscribe(`session:${sid}`, (msg) => {
      if (msg.type === "settings") setSettings(msg.settings);
      if (msg.type !== "item") return;
      if (!ready) buffer.push(msg.item);
      else setSt((s) => apply(s, msg.item));
    });
    const load = () =>
      api
        .session(pid, sid)
        .then((snap) => {
          if (!alive) return;
          let s = empty();
          for (const it of snap.items) s = apply(s, it);
          for (const it of buffer) s = apply(s, it);
          // drop permissions the server no longer considers pending
          const pend = new Set(snap.pending);
          s.pending = Object.fromEntries(Object.entries(s.pending).filter(([k]) => pend.has(k) || buffer.some((b) => b.kind === "permission" && b.id === k)));
          s.state = snap.live ? snap.state : null;
          if (!s.model && snap.settings?.model) s.model = snap.settings.model;
          setSt(s);
          setHarnessId(snap.session?.harness ?? null);
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

  const running = st.state === "running" || st.state === "starting" || st.state === "waiting";
  const blocks = useMemo(() => toBlocks(st.items), [st.items]);
  const harness = meta?.harnesses.find((h) => h.id === (harnessId ?? "claude-code")) ?? meta?.harnesses[0];
  const { efforts, fallback } = effortOptions(harness, settings.model || null);
  const canSend = (!!text.trim() || att.ids.length > 0) && !att.uploading && !sending;

  const send = useCallback(async () => {
    const t = text.trim();
    if (!canSend) return;
    setSending(true);
    stick.current = true;
    try {
      await api.send(pid, sid, t, att.ids);
      setText("");
      att.clear();
    } catch (e: any) {
      toast(e.message || String(e), "error");
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }, [text, canSend, pid, sid, toast, att]);

  const setEffort = async (effort: string) => {
    try {
      await api.setEffort(pid, sid, effort);
      setSettings((x) => ({ ...x, effort }));
    } catch (e: any) {
      toast(e.message, "error");
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
  const setModel = async (model: string) => {
    try {
      await api.setModel(pid, sid, model);
      setSt((s) => ({ ...s, model: model || s.model }));
      setSettings((x) => ({ ...x, model }));
      toast(`Model → ${model || "default"}`, "ok");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };


  // detached sessions: show what the next attach will use (session settings, else the harness defaults)
  const defaults = meta?.settings.harnesses?.[harness?.id ?? ""] ?? {};
  const shownMode = st.mode || settings.mode || defaults.permission_mode || "";
  const modeLabel = harness?.permission_modes.find((m) => m.id === shownMode)?.label ?? (shownMode || "Mode");
  const shownModel = st.model || settings.model || defaults.model || "";
  const modelLabel = shownModel ? harness?.models.find((m) => m.id === shownModel)?.label ?? shownModel.replace(/^claude-/, "") : "Default model";
  const pendingList = Object.values(st.pending);

  return (
    <div className={clsx("chat", compact && "compact")}>
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="chat-inner">
          {loading && (
            <div className="msg-system">
              <Spinner /> Loading conversation…
            </div>
          )}
          {!loading && blocks.length === 0 && emptyHint}
          <Transcript blocks={blocks} results={st.results} running={running} cwd={cwd} />
          {st.streaming && (
            <div className="msg-text stream-cursor">
              <Markdown text={st.streaming} />
            </div>
          )}
          {pendingList.map((p) => (
            <PermissionCard key={p.id} perm={p} cwd={cwd} onDecide={(d) => decide(p, d)} />
          ))}
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

      <div className="composer">
        <div className={clsx("composer-box", att.dragging && "dragging")} {...att.dropProps}>
          {att.picker}
          <AttachmentTray items={att.items} onRemove={att.remove} />
          <textarea
            ref={inputRef}
            className="composer-input"
            rows={1}
            placeholder={placeholder ?? (running ? (harnessId === "codex" ? "Steer the running turn…" : "Queue a follow-up…") : "Reply…")}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onPaste={att.onPaste}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-bar">
            <div className="bar-chips">
            <AttachButton onClick={att.open} />
            {harness && (
              <label className={clsx("chip", !st.mode && "faded")} title={st.mode ? "Permission mode" : "Permission mode (applies when the session is attached)"}>
                {modeLabel}
                <select value={shownMode} onChange={(e) => void setMode(e.target.value)}>
                  {!shownMode && <option value="">Mode…</option>}
                  {harness.permission_modes.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {harness && !compact && (
              <label className={clsx("chip", !st.model && "faded")} title={shownModel || "Model"}>
                <span className="ellipsis" style={{ maxWidth: 160 }}>{modelLabel}</span>
                <select value="" onChange={(e) => void setModel(e.target.value)}>
                  <option value="" disabled>
                    Switch model…
                  </option>
                  {harness.models.map((m) => (
                    <option key={m.id || "default"} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            </div>
            <div className="bar-end">
            <EffortPicker efforts={efforts} value={settings.effort} fallback={fallback} onChange={(e) => void setEffort(e)} />
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
              <button className="send-btn" disabled={!canSend} onClick={() => void send()} title={st.state ? "Send (Enter)" : "Send (Enter) — resumes the session"}>
                {sending ? <Spinner /> : <ArrowUp size={17} strokeWidth={2.4} />}
              </button>
            )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
