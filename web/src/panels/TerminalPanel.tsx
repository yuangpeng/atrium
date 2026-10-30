import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Plus, SquareTerminal, X } from "lucide-react";
import { api, ApiError, TerminalInfo } from "../api";
import { useApp } from "../store";
import { basename, clsx } from "../util";
import { Popover, Spinner } from "../components/ui";

const TerminalView = lazy(() => import("../terminal/TerminalView")); // xterm.js: only once a terminal is shown

/** Shells on the server, one per tab (atrium/terminal.py): they keep running while the page is away, and every
 *  page of the project sees the same ones. `visible`: the panel is the workspace's open one. */
export function TerminalPanel({ visible }: { visible: boolean }) {
  const { project, terminalVersion, toast } = useApp();
  const pid = project?.id ?? "";
  const [list, setList] = useState<TerminalInfo[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [titles, setTitles] = useState<Record<string, string>>({}); // what the shell calls its window (OSC 0/2)
  const [creating, setCreating] = useState(false);
  const asked = useRef(0);
  const started = useRef(false); // the first time the panel shows, with no terminal yet, it opens one itself
  // the project directory and its worktrees, labelled as in Files
  const roots = useMemo(
    () => project?.worktrees.length ? project.worktrees.map((w) => ({ path: w.path, label: w.main ? `${project.name} (${w.branch ?? "detached"})` : `⎇ ${w.branch ?? "detached"}` })) : project ? [{ path: project.path, label: project.name }] : [],
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
    setTitles({});
    started.current = false;
  }, [pid]);
  useEffect(() => void refresh(), [refresh, terminalVersion]);
  // the open tab went away (closed here or elsewhere, or its shell exited): the newest one takes its place
  useEffect(() => {
    if (list && !list.some((t) => t.id === active)) setActive(list[list.length - 1]?.id ?? null);
  }, [list, active]);

  const create = useCallback(
    async (root?: string) => {
      if (!pid) return;
      setCreating(true);
      try {
        const t = await api.createTerminal(pid, { root }); // sized by its view once it is in the page
        setList((l) => (l?.some((x) => x.id === t.id) ? l : [...(l ?? []), t]));
        setActive(t.id);
      } catch (e: any) {
        toast(e.message, "error");
      } finally {
        setCreating(false);
      }
    },
    [pid, toast],
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
  return (
    <div className="panel-body">
      <div className="term-bar">
        <div className="editor-tabs term-tabs">
          {list?.map((t) => {
            const branch = branchOf(t);
            return (
              <div
                key={t.id}
                className={clsx("editor-tab", t.id === active && "on", t.exit_code !== null && "exited")}
                onClick={() => setActive(t.id)}
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
          {roots.length > 1 ? (
            <Popover
              align="right"
              within=".workspace"
              trigger={(open, toggle) => (
                <button className={clsx("icon-btn term-new", open && "active")} disabled={creating} title="New terminal in…" aria-label="New terminal" onClick={toggle}>
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
      <div className="term-views">
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
          list.map((t) => (
            <div key={t.id} className="term-slot" style={{ display: t.id === active ? "flex" : "none" }}>
              <Suspense fallback={<div className="empty-state"><Spinner /></div>}>
                <TerminalView
                  pid={pid}
                  tid={t.id}
                  active={visible && t.id === active}
                  onTitle={(title) => setTitles((x) => ({ ...x, [t.id]: title }))}
                  onEnd={() => void refresh()}
                />
              </Suspense>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
