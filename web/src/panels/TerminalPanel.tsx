import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Columns2, Plus, Search, Square, SquareTerminal, X } from "lucide-react";
import { api, ApiError, TerminalInfo } from "../api";
import { useAppState } from "../store";
import { basename, clsx } from "../util";
import { Popover, Spinner } from "../components/ui";
import { useCompact } from "../layout";

const TerminalView = lazy(() => import("../terminal/TerminalView")); // xterm.js: only once a terminal is shown
const MAX_PANES = 3; // side by side (stacked in a narrow panel); a phone-sized screen shows one

/** Shells on the server, one per tab (atrium/terminal.py): they keep running while the page is away, and every
 *  page of the project sees the same ones. `visible`: the panel is the workspace's open one. */
export function TerminalPanel({ visible }: { visible: boolean }) {
  const { project, terminalVersion, toast, refreshProject } = useAppState("project", "terminalVersion", "toast", "refreshProject");
  const pid = project?.id ?? "";
  const [list, setList] = useState<TerminalInfo[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  // the terminals shown side by side, in order: the active one alone unless split. The active one is always among them
  const [panes, setPanes] = useState<string[]>([]);
  const activeRef = useRef(active);
  activeRef.current = active;
  const [findAsk, setFindAsk] = useState(0);
  const compact = useCompact();
  const [titles, setTitles] = useState<Record<string, string>>({}); // what the shell calls its window (OSC 0/2)
  const [creating, setCreating] = useState(false);
  const asked = useRef(0);
  const recheckRoots = useCallback(() => void refreshProject().catch(() => undefined), [refreshProject]);
  const started = useRef(false); // the first time the panel shows, with no terminal yet, it opens one itself
  // the project directory and its worktrees, labelled as in Files; not one whose folder is gone (git calls it prunable)
  const roots = useMemo(
    () => project?.worktrees.length ? project.worktrees.filter((w) => !w.prunable).map((w) => ({ path: w.path, label: w.main ? `${project.name} (${w.branch ?? "detached"})` : `⎇ ${w.branch ?? "detached"}` })) : project ? [{ path: project.path, label: project.name }] : [],
    [project],
  );

  const refresh = useCallback(async () => {
    if (!pid) return;
    const seq = ++asked.current;
    try {
      const fresh = await api.terminals(pid);
      if (seq === asked.current) setList(fresh);
    } catch {
      /* transient: the next change event tries again */
    }
  }, [pid]);

  useEffect(() => {
    setList(null);
    setActive(null);
    setPanes([]);
    setTitles({});
    started.current = false;
  }, [pid]);
  useEffect(() => void refresh(), [refresh, terminalVersion]);
  // the open tab went away (closed here or elsewhere, or its shell exited): a pane beside it takes its place, else the
  // newest terminal
  useEffect(() => {
    if (list && !list.some((t) => t.id === active)) setActive(panes.find((id) => list.some((t) => t.id === id)) ?? list[list.length - 1]?.id ?? null);
  }, [list, active, panes]);
  // the panes follow: those gone leave, and the active terminal is in one
  useEffect(() => {
    if (!list) return;
    setPanes((ps) => {
      let next = ps.filter((id) => list.some((t) => t.id === id));
      if (active && list.some((t) => t.id === active) && !next.includes(active)) next = next.length ? [...next, active].slice(-MAX_PANES) : [active];
      return next.length === ps.length && next.every((id, i) => id === ps[i]) ? ps : next;
    });
  }, [list, active]);
  /** A tab picked: it shows in the pane in use, or stays where it is if a pane shows it already. */
  const pick = (id: string) => {
    if (!panes.includes(id)) setPanes((ps) => (ps.length > 1 && active && ps.includes(active) ? ps.map((p) => (p === active ? id : p)) : [id]));
    setActive(id);
  };

  /** A new terminal: shown alone, or (`beside`: split) next to the one in use. */
  const create = useCallback(
    async (root?: string, beside = false) => {
      if (!pid) return;
      setCreating(true);
      try {
        const t = await api.createTerminal(pid, { root }); // sized by its view once it is in the page
        setList((l) => (l?.some((x) => x.id === t.id) ? l : [...(l ?? []), t]));
        setPanes((ps) => {
          const at = ps.indexOf(activeRef.current ?? "");
          return beside && at >= 0 ? [...ps.slice(0, at + 1), t.id, ...ps.slice(at + 1)].slice(0, MAX_PANES) : [t.id];
        });
        setActive(t.id);
      } catch (e: any) {
        toast(e.message, "error");
        recheckRoots(); // a worktree listed here that has gone since: the menu drops it
      } finally {
        setCreating(false);
      }
    },
    [pid, toast, recheckRoots],
  );

  useEffect(() => {
    if (!list || started.current) return;
    if (list.length) started.current = true; // there are some: never open one by itself later (closing the last one means it)
    else if (visible) {
      started.current = true;
      void create();
    }
  }, [list, visible, create]);

  const close = async (t: TerminalInfo) => {
    try {
      await api.closeTerminal(pid, t.id);
    } catch (e: any) {
      if (!(e instanceof ApiError && e.body?.code === "terminal_busy")) return toast(e.message, "error");
      if (!confirm(`“${e.body.command}” is still running in this terminal. Close it anyway?`)) return;
      try {
        await api.closeTerminal(pid, t.id, true);
      } catch (e2: any) {
        return toast(e2.message, "error");
      }
    }
    setList((l) => l?.filter((x) => x.id !== t.id) ?? l);
  };

  if (!project) return null;
  const branchOf = (t: TerminalInfo) => (t.root === project.path ? null : project.worktrees.find((w) => w.path === t.root)?.branch ?? basename(t.root));
  // a phone-sized screen shows one terminal: two half-size ones, each with its key row, leave room for neither
  const shownIds = compact ? (active ? [active] : []) : panes;
  const split = shownIds.length > 1;
  const activeRoot = list?.find((t) => t.id === active)?.root;
  // dark whichever theme Atrium is in: the dark colours apply to everything in here (styles.css, [data-theme="dark"])
  return (
    <div className="panel-body term-panel" data-theme="dark">
      <div className="term-bar">
        <div className="editor-tabs term-tabs">
          {list?.map((t) => {
            const branch = branchOf(t);
            return (
              <div
                key={t.id}
                className={clsx("editor-tab", t.id === active && "on", split && t.id !== active && shownIds.includes(t.id) && "paned", t.exit_code !== null && "exited")}
                onClick={() => pick(t.id)}
                title={`${t.root}${t.exit_code !== null ? ` — ended (${t.exit_code})` : ""}`}
              >
                <SquareTerminal size={12} className="faint" />
                <span className="term-title">{titles[t.id] || t.shell || "terminal"}</span>
                {branch && <span className="faint mono term-branch">⎇ {branch}</span>}
                <button className="icon-btn sm" title="Close terminal" aria-label="Close terminal" onClick={(e) => (e.stopPropagation(), void close(t))}>
                  <X size={12} />
                </button>
              </div>
            );
          })}
        </div>
        <div className="term-actions">
          {!!list?.length && (
            <button className="icon-btn" aria-label="Find in the terminal" data-tip="Find (⌘F)" onClick={() => setFindAsk(Date.now())}>
              <Search size={14} />
            </button>
          )}
          {!compact && split && (
            <button className="icon-btn" aria-label="Unsplit" data-tip="One terminal at a time" onClick={() => active && setPanes([active])}>
              <Square size={13} />
            </button>
          )}
          {!compact && !!list?.length && panes.length < MAX_PANES && (
            <button className="icon-btn" disabled={creating} aria-label="Split" data-tip="Split: a new terminal beside this one" onClick={() => void create(activeRoot, true)}>
              <Columns2 size={14} />
            </button>
          )}
          {roots.length > 1 ? (
            <Popover
              align="right"
              within=".workspace"
              trigger={(open, toggle) => (
                // the worktrees as they are now: one may have gone since the project was loaded
                <button className={clsx("icon-btn term-new", open && "active")} disabled={creating} title="New terminal in…" aria-label="New terminal" onClick={() => (open || recheckRoots(), toggle())}>
                  <Plus size={15} />
                  <ChevronDown size={11} />
                </button>
              )}
            >
              {(closeMenu) => (
                <>
                  <div className="pick-head">New terminal in</div>
                  {roots.map((r) => (
                    <button key={r.path} className="menu-item" title={r.path} onClick={() => (closeMenu(), void create(r.path))}>
                      <span className="ellipsis">{r.label}</span>
                    </button>
                  ))}
                </>
              )}
            </Popover>
          ) : (
            <button className="icon-btn" disabled={creating} title="New terminal" aria-label="New terminal" onClick={() => void create()}>
              <Plus size={15} />
            </button>
          )}
        </div>
      </div>
      <div className={clsx("term-views", split && "split")}>
        {list === null ? (
          <div className="empty-state">
            <Spinner />
          </div>
        ) : list.length === 0 ? (
          <div className="empty-state">
            <SquareTerminal size={28} />
            <h3>No terminal open</h3>
            <div>A shell on the server, in {project.name}. It keeps running when you leave or reload the page, and every device sees it.</div>
            <button className="btn" disabled={creating} onClick={() => void create()}>
              <Plus size={14} /> New terminal
            </button>
          </div>
        ) : (
          list.map((t) => {
            const at = shownIds.indexOf(t.id);
            return (
              // in the panes' order; the others stay mounted, hidden, with their scrollback
              <div key={t.id} className={clsx("term-slot", split && t.id === active && "focused")} style={{ display: at >= 0 ? "flex" : "none", order: at }}>
                <Suspense fallback={<div className="empty-state"><Spinner /></div>}>
                  <TerminalView
                    pid={pid}
                    tid={t.id}
                    shown={visible && at >= 0}
                    active={visible && t.id === active}
                    findAsk={t.id === active ? findAsk : 0}
                    onTitle={(title) => setTitles((x) => ({ ...x, [t.id]: title }))}
                    onEnd={() => void refresh()}
                    onFocus={() => setActive(t.id)}
                  />
                </Suspense>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
