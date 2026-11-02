import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ExternalLink, FileCode2, History, LayoutDashboard, RefreshCw, RotateCw } from "lucide-react";
import { api, BoardPage, ProjectInfo, TrackerStatus } from "../api";
import { resolvedTheme, useApp } from "../store";
import { basename, clsx, fmtDuration, timeAgo } from "../util";
import { Popover, Spinner, useTick } from "../components/ui";

/** One of the project's HTML files, shown in place of the board. */
type Page = { root: string; path: string };

const dirOf = (path: string) => path.slice(0, Math.max(0, path.lastIndexOf("/")));

export function BoardPanel() {
  useTick(15000);
  const { project, boardVersion, trackerVersion, theme, selectSession, toast, fsVersion } = useApp();
  const frame = useRef<HTMLIFrameElement>(null);
  const [loaded, setLoaded] = useState(false);
  const [tracker, setTracker] = useState<TrackerStatus | null>(null);
  const [repush, setRepush] = useState(0);
  const [page, setPage] = useState<Page | null>(null); // null: the board, which is where the panel starts
  const [pageUrl, setPageUrl] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const pid = project?.id;

  useEffect(() => setPage(null), [pid]);

  // the page's address: asked for each time it is shown (it is a key that expires), and on a reload
  useEffect(() => {
    setPageUrl(null);
    if (!pid || !page) return;
    let alive = true;
    api
      .fileSite(pid, page.path, page.root)
      .then(({ url }) => alive && setPageUrl(url))
      .catch((e) => {
        if (!alive) return;
        toast(e instanceof Error ? e.message : String(e), "error");
        setPage(null);
      });
    return () => {
      alive = false;
    };
  }, [pid, page, reload, toast]);

  // written again (an agent regenerating a report, its stylesheet beside it): shown again
  useEffect(() => {
    if (!page) return;
    const dir = `${page.root}/${dirOf(page.path)}`.replace(/\/$/, "");
    if (useApp.getState().fsPaths.some((p) => dirOf(p) === dir)) setReload((n) => n + 1);
  }, [fsVersion]); // eslint-disable-line react-hooks/exhaustive-deps

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

  if (!pid || !project) return null;
  const last = tracker?.runs[0];
  const src = `${api.boardHtmlUrl(pid)}?theme=${resolvedTheme()}`;
  const where = page && [dirOf(page.path), page.root !== project.path && worktreeLabel(project, page.root)].filter(Boolean).join(" · ");

  return (
    <div className="panel-body">
      <div className="toolbar">
        <PagePicker pid={pid} project={project} page={page} onPick={setPage} />
        {page ? (
          <span className="muted board-note" title={where || undefined}>
            {where}
          </span>
        ) : (
        <span className="muted board-note">
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
        )}
        <span className="grow" />
        {page ? (
          <>
            <button className="icon-btn" onClick={() => setReload((n) => n + 1)} aria-label="Reload" data-tip="Reload the page">
              <RotateCw size={14} />
            </button>
            <a
              className={clsx("icon-btn", !pageUrl && "disabled")}
              href={pageUrl ?? undefined}
              target="_blank"
              rel="noreferrer"
              aria-label="Open the page in a new tab"
              data-tip="Open the page in a new tab"
            >
              <ExternalLink size={15} />
            </a>
          </>
        ) : (
          <>
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
          </>
        )}
      </div>
      {/* the board stays loaded under a page: back on it, its scroll and a half-typed TODO are as they were */}
      <iframe
        ref={frame}
        key={pid}
        className="board-frame"
        style={page ? { display: "none" } : undefined}
        src={src}
        sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
        onLoad={() => setLoaded(true)}
        title="Project board"
      />
      {page &&
        (pageUrl ? (
          // the key: a reload while the address stays the same
          <iframe
            key={`${pageUrl}#${reload}`}
            className="board-frame page-frame"
            src={pageUrl}
            sandbox="allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
            title={basename(page.path)}
          />
        ) : (
          <div className="board-frame page-loading">
            <Spinner />
          </div>
        ))}
    </div>
  );
}

function worktreeLabel(project: ProjectInfo, root: string): string {
  return `⎇ ${project.worktrees.find((w) => w.path === root)?.branch ?? basename(root)}`;
}

/** The toolbar's title: the board, or one of the project's HTML files, picked from its menu. */
function PagePicker({ pid, project, page, onPick }: { pid: string; project: ProjectInfo; page: Page | null; onPick: (p: Page | null) => void }) {
  return (
    <Popover
      width={380}
      within=".workspace"
      trigger={(open, toggle) => (
        <button className={clsx("page-pick", open && "on")} onClick={toggle} aria-expanded={open} aria-label="Show the board or an HTML file" data-tip="Show the board or one of the project's HTML files">
          <span className="title">{page ? basename(page.path) : "Board"}</span>
          <ChevronDown size={13} />
        </button>
      )}
    >
      {(close) => (
        <PageMenu
          pid={pid}
          project={project}
          page={page}
          onPick={(p) => {
            onPick(p);
            close();
          }}
        />
      )}
    </Popover>
  );
}

function PageMenu({ pid, project, page, onPick }: { pid: string; project: ProjectInfo; page: Page | null; onPick: (p: Page | null) => void }) {
  const [found, setFound] = useState<{ pages: BoardPage[]; truncated: boolean } | null>(null);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    void api
      .boardPages(pid)
      .then(setFound)
      .catch(() => setFound({ pages: [], truncated: false }));
  }, [pid]);
  // by checkout, the project's first; in each the latest first
  const groups = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const out = new Map<string, BoardPage[]>([[project.path, []]]);
    for (const p of found?.pages ?? []) {
      if (f && !p.path.toLowerCase().includes(f)) continue;
      out.set(p.root, [...(out.get(p.root) ?? []), p]);
    }
    return [...out].filter(([, list]) => list.length);
  }, [found, filter, project.path]);
  const all = found?.pages.length ?? 0;
  return (
    <div className="page-menu">
      <button className={clsx("menu-item pick-item", !page && "on")} onClick={() => onPick(null)}>
        <LayoutDashboard size={15} />
        <div className="grow">
          <div>Board</div>
          <div className="d">What the agents are doing, their results, the TODOs</div>
        </div>
        {!page && <Check size={14} />}
      </button>
      <div className="menu-sep" />
      {!found ? (
        <div className="page-note row">
          <Spinner /> Looking for HTML files…
        </div>
      ) : !all ? (
        <div className="page-note">No HTML files in the project.</div>
      ) : (
        <>
          {all > 8 && <input className="input page-filter" placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter the HTML files" />}
          <div className="page-list">
            {groups.map(([root, list]) => (
              <div key={root}>
                {(groups.length > 1 || root !== project.path) && <div className="section-label">{root === project.path ? project.name : worktreeLabel(project, root)}</div>}
                {list.map((p) => {
                  const on = page?.root === p.root && page.path === p.path;
                  return (
                    <button key={p.path} className={clsx("menu-item pick-item", on && "on")} onClick={() => onPick({ root: p.root, path: p.path })}>
                      <FileCode2 size={15} />
                      <div className="grow" style={{ minWidth: 0 }}>
                        <div className="ellipsis">{basename(p.path)}</div>
                        <div className="d ellipsis">{[dirOf(p.path), timeAgo(p.mtime)].filter(Boolean).join(" · ")}</div>
                      </div>
                      {on && <Check size={14} />}
                    </button>
                  );
                })}
              </div>
            ))}
            {!groups.length && <div className="page-note">None match.</div>}
            {found.truncated && <div className="page-note">The latest {all} are listed.</div>}
          </div>
        </>
      )}
    </div>
  );
}
