import { useEffect, useRef, useState } from "react";
import { ExternalLink, History, RefreshCw } from "lucide-react";
import { api, TrackerStatus } from "../api";
import { resolvedTheme, useApp } from "../store";
import { fmtDuration, timeAgo } from "../util";
import { Popover, Spinner, useTick } from "../components/ui";

export function BoardPanel() {
  useTick(15000);
  const { project, boardVersion, trackerVersion, theme, selectSession, toast } = useApp();
  const frame = useRef<HTMLIFrameElement>(null);
  const [loaded, setLoaded] = useState(false);
  const [tracker, setTracker] = useState<TrackerStatus | null>(null);
  const [repush, setRepush] = useState(0);
  const pid = project?.id;

  // push fresh board HTML into the iframe (it swaps content in place, keeping scroll)
  useEffect(() => {
    if (!pid || !loaded) return;
    let alive = true;
    fetch(api.boardHtmlUrl(pid), { cache: "no-store" })
      .then((r) => r.text())
      .then((html) => alive && frame.current?.contentWindow?.postMessage({ type: "atrium:board-html", html }, "*"));
    return () => {
      alive = false;
    };
  }, [pid, boardVersion, loaded, repush]);

  useEffect(() => {
    frame.current?.contentWindow?.postMessage({ type: "atrium:theme", theme: resolvedTheme() }, "*");
  }, [theme]);

  useEffect(() => {
    if (!pid) return;
    void api.tracker(pid).then(setTracker).catch(() => undefined);
  }, [pid, trackerVersion]);

  useEffect(() => {
    // the board's TODO list asks for its changes: it can't call the API itself (its sandbox has no login)
    const todo = async (m: { action?: string; id?: string; text?: string }) => {
      if (!pid) return;
      const id = String(m.id ?? "");
      try {
        if (m.action === "add") await api.addTodo(pid, String(m.text ?? ""));
        else if (m.action === "done" || m.action === "reopen") await api.updateTodo(pid, id, { done: m.action === "done" });
        else if (m.action === "accept") await api.updateTodo(pid, id, { accept: true });
        else if (m.action === "remove") await api.deleteTodo(pid, id);
      } catch (e) {
        toast(e instanceof Error ? e.message : String(e), "error");
        setRepush((n) => n + 1); // the board shows the TODO as it is again, not greyed out waiting
      }
    };
    const onMsg = (e: MessageEvent) => {
      if (e.source !== frame.current?.contentWindow) return;
      if (e.data?.type === "atrium:open-session" && e.data.sessionId) selectSession(e.data.sessionId);
      if (e.data?.type === "atrium:todo") void todo(e.data);
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, [selectSession, pid, toast]);

  if (!pid) return null;
  const last = tracker?.runs[0];
  const src = `${api.boardHtmlUrl(pid)}?theme=${resolvedTheme()}`;

  return (
    <div className="panel-body">
      <div className="toolbar">
        <span className="title">Board</span>
        <span className="muted" style={{ fontSize: 12 }}>
          {tracker?.running ? (
            <span className="row" style={{ gap: 6 }}>
              <Spinner /> Tracker is reading the latest activity…
            </span>
          ) : last ? (
            <>
              Tracker {last.error ? <span className="pill err">failed</span> : "ran"} {timeAgo(last.ts)} ago
              {tracker && tracker.enabled && tracker.next_run > Date.now() / 1000 && <> · next in {Math.max(1, Math.round((tracker.next_run - Date.now() / 1000) / 60))}m</>}
            </>
          ) : (
            "Tracker hasn't run yet"
          )}
        </span>
        <span className="grow" />
        <Popover
          align="right"
          width={360}
          trigger={(_o, toggle) => (
            <button className="icon-btn" onClick={toggle} aria-label="Tracker runs" data-tip="Tracker runs">
              <History size={15} />
            </button>
          )}
        >
          {() => (
            <div style={{ maxHeight: 360, overflowY: "auto", fontSize: 12.5 }}>
              <div className="section-label">Tracker Agent runs</div>
              {(tracker?.runs ?? []).map((r) => (
                <div key={r.ts} style={{ padding: "6px 12px", borderBottom: "1px solid var(--border)" }}>
                  <div className="row">
                    <b>{r.reason}</b>
                    <span className="faint">{new Date(r.ts * 1000).toLocaleTimeString()}</span>
                    <span className="grow" />
                    {r.cost_usd != null && <span className="faint">${r.cost_usd.toFixed(3)}</span>}
                    <span className="faint">{fmtDuration(r.duration_ms)}</span>
                  </div>
                  <div className={r.error ? "" : "muted"} style={r.error ? { color: "var(--err)" } : undefined}>
                    {r.error ?? r.skipped ?? `${r.sessions} session(s), ${r.events} events → ${r.updated_agents ?? 0} agent(s) updated`}
                  </div>
                </div>
              ))}
              {!tracker?.runs.length && <div className="muted" style={{ padding: 12 }}>No runs yet.</div>}
            </div>
          )}
        </Popover>
        <button
          className="icon-btn"
          disabled={tracker?.running}
          onClick={async () => {
            await api.runTracker(pid);
            toast("Tracker started", "info");
          }}
          aria-label="Track now"
          data-tip="Track now — read what the agents did since the last run and update the board"
        >
          <RefreshCw size={14} />
        </button>
        <a className="icon-btn" href={src} target="_blank" rel="noreferrer" aria-label="Open board.html in a new tab" data-tip="Open the board in a new tab">
          <ExternalLink size={15} />
        </a>
      </div>
      <iframe
        ref={frame}
        key={pid}
        className="board-frame"
        src={src}
        sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
        onLoad={() => setLoaded(true)}
        title="Project board"
      />
    </div>
  );
}
