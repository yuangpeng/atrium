import { useMemo, useState } from "react";
import {
  Archive, ChevronsLeft, ChevronsRight, FolderOpen, GitBranch, Monitor, Moon, Plus, Search, Settings, Sun, Workflow,
} from "lucide-react";
import { SessionRow } from "../api";
import { useApp } from "../store";
import { clsx, displayName, shortPath, timeAgo } from "../util";
import { Popover, useTick } from "./ui";

export function statusClass(s: SessionRow): string {
  if (s.live === "waiting" || s.pending) return "waiting";
  if (s.live === "running" || s.live === "starting") return "running";
  if (s.live === "error") return "error";
  if (s.status === "blocked") return "blocked";
  if (s.status === "done") return "done";
  if (s.live === "idle") return "idle-live";
  return "";
}

export function liveLabel(s: SessionRow): string {
  if (s.live === "waiting" || s.pending) return "needs approval";
  if (s.live === "running") return "running";
  if (s.live === "starting") return "starting";
  if (s.live === "idle") return "attached";
  return "";
}

function SessionItem({ s, active, onClick }: { s: SessionRow; active: boolean; onClick: () => void }) {
  const sub = s.doing || (s.first_prompt ? s.first_prompt : "");
  const showBar = s.progress != null && s.progress > 0 && s.status !== "idle";
  return (
    <button className={clsx("sb-item", active && "active")} onClick={onClick} title={s.cwd}>
      <span className={clsx("dot", statusClass(s))} />
      <div className="body">
        <div className="name">
          <span className={clsx("ellipsis", !s.name && "unnamed")} style={{ flex: 1 }}>
            {displayName(s)}
          </span>
          {s.harness !== "claude-code" && <span className={clsx("harness-badge", s.harness)}>{s.harness}</span>}
          {s.pending > 0 && <span className="badge-n">{s.pending}</span>}
          <span className="faint" style={{ fontSize: 11, fontWeight: 400 }}>
            {timeAgo(s.updated_at)}
          </span>
        </div>
        <div className="sub ellipsis">
          {s.worktree && s.branch && (
            <span className="mono" style={{ color: "var(--accent-strong)", marginRight: 5 }}>
              ⎇ {s.branch}
            </span>
          )}
          {sub || <span className="faint">—</span>}
        </div>
        {showBar && (
          <div className={clsx("bar", s.status === "done" && "done")}>
            <span style={{ width: `${s.progress}%` }} />
          </div>
        )}
      </div>
    </button>
  );
}

export function Sidebar() {
  useTick(30000);
  const { project, sessions, sessionId, selectSession, setPickerOpen, setSettingsOpen, showArchived, setShowArchived, theme, setTheme, sidebarOpen, setSidebarOpen, setPanel } =
    useApp();
  const [filter, setFilter] = useState("");

  const { workers, system } = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const match = (s: SessionRow) =>
      !f || displayName(s).toLowerCase().includes(f) || (s.first_prompt || "").toLowerCase().includes(f) || (s.branch || "").toLowerCase().includes(f);
    return {
      workers: sessions.filter((s) => s.role === "worker" && match(s)),
      system: sessions.filter((s) => s.role !== "worker"),
    };
  }, [sessions, filter]);

  const active = workers.filter((s) => s.live === "running" || s.live === "waiting" || s.live === "starting" || s.pending > 0);
  const rest = workers.filter((s) => !active.includes(s));

  if (!sidebarOpen) {
    return (
      <aside className="sidebar collapsed">
        <div className="sb-head" style={{ padding: "12px 10px" }}>
          <button className="icon-btn" onClick={() => setSidebarOpen(true)} title="Expand sidebar">
            <ChevronsRight size={16} />
          </button>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 6 }}>
          <button className="icon-btn" onClick={() => selectSession(null)} title="New agent">
            <Plus size={17} />
          </button>
          <button className="icon-btn" onClick={() => setPickerOpen(true)} title="Open project">
            <FolderOpen size={16} />
          </button>
        </div>
      </aside>
    );
  }

  const ThemeIcon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;

  return (
    <aside className="sidebar">
      <div className="sb-head">
        <div className="sb-logo">
          <span className="mark">
            <svg width="13" height="13" viewBox="0 0 32 32">
              <path d="M7 26 16 6l9 20M11 18h10" stroke="white" strokeWidth="3.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          Atrium
        </div>
        <button className="icon-btn" onClick={() => setSidebarOpen(false)} title="Collapse sidebar">
          <ChevronsLeft size={16} />
        </button>
      </div>

      <button className="sb-project" onClick={() => setPickerOpen(true)} title="Switch project">
        <FolderOpen size={16} className="muted" />
        <div className="grow">
          <div className="name ellipsis">{project?.name ?? "Open a project…"}</div>
          {project && <div className="path ellipsis">{shortPath(project.path)}</div>}
        </div>
      </button>

      {project && (
        <button className="sb-new" onClick={() => selectSession(null)}>
          <span className="plus">
            <Plus size={14} strokeWidth={2.6} />
          </span>
          New agent
        </button>
      )}

      <div className="sb-scroll">
        {project && (
          <>
            {sessions.length > 6 && (
              <div className="row" style={{ position: "relative" }}>
                <Search size={13} className="faint" style={{ position: "absolute", left: 13 }} />
                <input className="input sb-filter" style={{ paddingLeft: 26 }} placeholder="Filter agents" value={filter} onChange={(e) => setFilter(e.target.value)} />
              </div>
            )}
            {active.length > 0 && <div className="sb-title">Active</div>}
            {active.map((s) => (
              <SessionItem key={s.id} s={s} active={s.id === sessionId} onClick={() => selectSession(s.id)} />
            ))}
            <div className="sb-title">
              <span className="grow">Agents</span>
              <button className={clsx("icon-btn sm", showArchived && "active")} title={showArchived ? "Hide archived" : "Show archived"} onClick={() => setShowArchived(!showArchived)}>
                <Archive size={13} />
              </button>
            </div>
            {rest.map((s) => (
              <SessionItem key={s.id} s={s} active={s.id === sessionId} onClick={() => selectSession(s.id)} />
            ))}
            {workers.length === 0 && <div className="faint" style={{ padding: "6px 12px", fontSize: 12.5 }}>No sessions in this directory yet.</div>}

            <div className="sb-title">System</div>
            <button className="sb-item" onClick={() => setPanel("board")}>
              <Workflow size={14} className="muted" style={{ marginTop: 3 }} />
              <div className="body">
                <div className="name">Tracker Agent</div>
                <div className="sub">Keeps the board up to date</div>
              </div>
            </button>
            <button
              className={clsx("sb-item", system.some((s) => s.id === sessionId) && "active")}
              onClick={() => setPanel("git")}
            >
              <GitBranch size={14} className="muted" style={{ marginTop: 3 }} />
              <div className="body">
                <div className="name">Git Agent</div>
                <div className="sub">Graph, branches & worktrees</div>
              </div>
            </button>
          </>
        )}
      </div>

      <div className="sb-foot">
        <button className="icon-btn" onClick={() => setSettingsOpen(true)} title="Settings">
          <Settings size={16} />
        </button>
        <Popover
          up
          trigger={(_o, toggle) => (
            <button className="icon-btn" onClick={toggle} title="Theme">
              <ThemeIcon size={16} />
            </button>
          )}
        >
          {(close) =>
            (["system", "light", "dark"] as const).map((t) => (
              <button key={t} className={clsx("menu-item", theme === t && "on")} onClick={() => (setTheme(t), close())}>
                {t === "system" ? <Monitor size={14} /> : t === "light" ? <Sun size={14} /> : <Moon size={14} />}
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))
          }
        </Popover>
        <span className="grow" />
        <span className="faint" style={{ fontSize: 11 }}>
          {sessions.filter((s) => s.live).length} live
        </span>
      </div>
    </aside>
  );
}
