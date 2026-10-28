// The rail on the far right: project, new agent, one avatar per agent (status at a glance,
// details on hover), the full agent list as a flyout, the system agents, settings.
import type { ReactNode } from "react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, GitBranch, LayoutList, Monitor, Moon, Plus, Search, Settings, Sun, Workflow, X } from "lucide-react";
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

const isActive = (s: SessionRow) => s.live === "running" || s.live === "waiting" || s.live === "starting" || s.pending > 0;

function initials(s: SessionRow): string {
  const words = displayName(s).replace(/\bagent\b/gi, "").match(/[\p{L}\p{N}]+/gu) ?? [];
  const [a, b] = words;
  if (a && b) return (a.slice(0, 1) + b.slice(0, 1)).toUpperCase();
  const w = a ?? "?";
  return /^\p{Script=Han}/u.test(w) ? w.slice(0, 1) : w.slice(0, 2).replace(/^./, (c) => c.toUpperCase());
}

function hue(key: string): number {
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

// ---------------------------------------------------------------- hover card
function HoverCard({ anchor, children }: { anchor: HTMLElement; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = anchor.getBoundingClientRect();
    const h = el.offsetHeight;
    el.style.right = `${window.innerWidth - r.left + 10}px`;
    el.style.top = `${Math.max(8, Math.min(window.innerHeight - h - 8, r.top + r.height / 2 - h / 2))}px`;
    el.style.opacity = "1";
  });
  return createPortal(
    <div ref={ref} className="rail-tip">
      {children}
    </div>,
    document.body,
  );
}

function AgentTip({ s }: { s: SessionRow }) {
  const live = liveLabel(s);
  const text = s.doing || s.first_prompt;
  return (
    <>
      <div className="tip-title">
        <span className={clsx("dot", statusClass(s))} />
        <span className={clsx("grow", !s.name && "unnamed")}>{displayName(s)}</span>
        <span className={clsx("harness-badge", s.harness)}>{s.harness === "claude-code" ? "Claude" : s.harness}</span>
      </div>
      {(live || s.status) && (
        <div className="tip-meta">
          {[live, s.status && s.status !== "idle" ? s.status : "", s.progress ? `${s.progress}%` : ""].filter(Boolean).join(" · ")}
        </div>
      )}
      {s.progress != null && s.progress > 0 && (
        <div className={clsx("bar", s.status === "done" && "done")} style={{ margin: "6px 0 2px" }}>
          <span style={{ width: `${s.progress}%` }} />
        </div>
      )}
      {text && <div className="tip-body">{text}</div>}
      <div className="tip-foot">
        {s.branch && (
          <span className="mono">
            ⎇ {s.branch}
            {s.worktree ? " · worktree" : ""}
          </span>
        )}
        <span className="grow" />
        <span>{timeAgo(s.updated_at)} ago</span>
      </div>
      {s.pending > 0 && <div className="tip-alert">{s.pending} permission request{s.pending > 1 ? "s" : ""} waiting</div>}
    </>
  );
}

function Tip({ title, sub, hint }: { title: ReactNode; sub?: ReactNode; hint?: string }) {
  return (
    <>
      <div className="tip-title">{title}</div>
      {sub && <div className="tip-body">{sub}</div>}
      {hint && <div className="tip-foot">{hint}</div>}
    </>
  );
}

// ---------------------------------------------------------------- flyout with the full list
function AgentsFlyout({ onClose }: { onClose: () => void }) {
  const { sessions, sessionId, selectSession, showArchived, setShowArchived } = useApp();
  const [filter, setFilter] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const down = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (ref.current && !ref.current.contains(t) && !t.closest(".rail-flyout-toggle")) onClose();
    };
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", down);
    window.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      window.removeEventListener("keydown", key);
    };
  }, [onClose]);
  const workers = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return sessions.filter(
      (s) => s.role === "worker" && (!f || displayName(s).toLowerCase().includes(f) || (s.first_prompt || "").toLowerCase().includes(f) || (s.branch || "").toLowerCase().includes(f)),
    );
  }, [sessions, filter]);
  const active = workers.filter(isActive);
  const rest = workers.filter((s) => !isActive(s));
  const item = (s: SessionRow) => (
    <button key={s.id} className={clsx("sb-item", s.id === sessionId && "active")} onClick={() => (selectSession(s.id), onClose())} title={s.cwd}>
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
          {s.worktree && s.branch && <span className="mono" style={{ color: "var(--accent-strong)", marginRight: 5 }}>⎇ {s.branch}</span>}
          {s.doing || s.first_prompt || <span className="faint">—</span>}
        </div>
      </div>
    </button>
  );
  return (
    <div ref={ref} className="rail-flyout">
      <div className="fly-head">
        <b className="grow">Agents</b>
        <button className={clsx("icon-btn sm", showArchived && "active")} title={showArchived ? "Hide archived" : "Show archived"} onClick={() => setShowArchived(!showArchived)}>
          <Archive size={13} />
        </button>
        <button className="icon-btn sm" onClick={onClose} title="Close">
          <X size={14} />
        </button>
      </div>
      <div className="row" style={{ position: "relative", padding: "0 10px 6px" }}>
        <Search size={13} className="faint" style={{ position: "absolute", left: 20 }} />
        <input autoFocus className="input" style={{ paddingLeft: 26, height: 30, fontSize: 12.5 }} placeholder="Filter agents" value={filter} onChange={(e) => setFilter(e.target.value)} />
      </div>
      <div className="fly-scroll">
        {active.length > 0 && <div className="sb-title">Active</div>}
        {active.map(item)}
        {rest.length > 0 && <div className="sb-title">{active.length ? "Others" : "All"}</div>}
        {rest.map(item)}
        {workers.length === 0 && <div className="faint" style={{ padding: "8px 12px", fontSize: 12.5 }}>{filter ? "No match." : "No sessions in this directory yet."}</div>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- rail
export function Rail() {
  useTick(30000);
  const { project, sessions, sessionId, selectSession, setPickerOpen, setSettingsOpen, theme, setTheme, setPanel } = useApp();
  const [tip, setTip] = useState<{ el: HTMLElement; content: ReactNode } | null>(null);
  const [flyout, setFlyout] = useState(false);
  const workers = useMemo(() => {
    const w = sessions.filter((s) => s.role === "worker" && !s.archived);
    return [...w.filter(isActive), ...w.filter((s) => !isActive(s))];
  }, [sessions]);
  const live = sessions.filter((s) => s.live).length;
  const hoverProps = (content: ReactNode) => ({
    onMouseEnter: (e: React.MouseEvent<HTMLElement>) => setTip({ el: e.currentTarget, content }),
    onMouseLeave: () => setTip(null),
  });
  const click = (fn: () => void) => () => (setTip(null), fn());
  const ThemeIcon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;

  return (
    <nav className="rail">
      <button
        className="rail-project"
        onClick={click(() => setPickerOpen(true))}
        {...hoverProps(<Tip title={project?.name ?? "No project open"} sub={project ? <span className="mono">{shortPath(project.path)}</span> : undefined} hint="Click to switch project" />)}
      >
        {project ? project.name.replace(/[^\p{L}\p{N}]/gu, "").slice(0, 2).toUpperCase() || "·" : <Plus size={16} />}
      </button>
      {project && (
        <>
          <button className={clsx("rail-btn rail-new", !sessionId && "on")} onClick={click(() => selectSession(null))} {...hoverProps(<Tip title="New agent" sub="Start an agent in this project" />)}>
            <Plus size={18} strokeWidth={2.2} />
          </button>
          <div className="rail-sep" />
          <div className="rail-agents">
            {workers.map((s) => {
              const sc = statusClass(s);
              return (
                <button
                  key={s.id}
                  className={clsx("rail-agent", s.harness !== "claude-code" && "alt", s.id === sessionId && "on")}
                  style={{ ["--h" as string]: hue(s.name || s.id) }}
                  onClick={click(() => selectSession(s.id))}
                  {...hoverProps(<AgentTip s={s} />)}
                >
                  <span className="av">{initials(s)}</span>
                  {sc && <span className={clsx("st dot", sc)} />}
                  {s.pending > 0 && <span className="pend">{s.pending}</span>}
                </button>
              );
            })}
          </div>
          <button className={clsx("rail-btn rail-flyout-toggle", flyout && "on")} onClick={click(() => setFlyout((o) => !o))} {...hoverProps(<Tip title="All agents" sub={`${workers.length} agent${workers.length === 1 ? "" : "s"} · search, archived`} />)}>
            <LayoutList size={16} />
          </button>
          <div className="rail-sep" />
          <button className="rail-btn" onClick={click(() => setPanel("board"))} {...hoverProps(<Tip title="Tracker Agent" sub="Keeps the board up to date from every session's transcript" hint="Click to open the board" />)}>
            <Workflow size={16} />
          </button>
          <button className="rail-btn" onClick={click(() => setPanel("git"))} {...hoverProps(<Tip title="Git Agent" sub="Graph, branches & worktrees" hint="Click to open Git" />)}>
            <GitBranch size={16} />
          </button>
        </>
      )}
      <span className="grow" />
      <button className="rail-btn" onClick={click(() => setSettingsOpen(true))} {...hoverProps(<Tip title="Settings" sub="Atrium, Claude Code and Codex config files" />)}>
        <Settings size={16} />
      </button>
      <Popover
        up
        align="right"
        trigger={(_o, toggle) => (
          <button className="rail-btn" onClick={click(toggle)} {...hoverProps(<Tip title="Theme" sub={theme[0].toUpperCase() + theme.slice(1)} />)}>
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
      <div className={clsx("rail-live", live > 0 && "on")} {...hoverProps(<Tip title={`${live} live session${live === 1 ? "" : "s"}`} sub="Agent processes currently attached" />)}>
        {live}
      </div>
      {tip && <HoverCard anchor={tip.el}>{tip.content}</HoverCard>}
      {flyout && project && <AgentsFlyout onClose={() => setFlyout(false)} />}
    </nav>
  );
}
