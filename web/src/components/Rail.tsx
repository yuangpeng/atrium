// The rail on the far right: project, new agent, one avatar per agent (status at a glance,
// details on hover), the full agent list as a flyout, the system agents, settings.
import type { ReactNode } from "react";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useShallow } from "zustand/react/shallow";
import type { LucideIcon } from "lucide-react";
import {
  Anchor, Archive, ArchiveRestore, Atom, Bird, Bug, Cat, Cherry, Citrus, Clover, Compass, Cookie, Dog, Feather, Fish, Flame, Flower2, FolderOpen, Gem,
  Ghost, GitBranch, LayoutList, Leaf, Monitor, Moon, Mountain, Orbit, Origami, Rabbit, Rocket, Sailboat, Search, Settings, Shell,
  Snail, Snowflake, Sprout, SquarePen, Squirrel, Star, Sun, Telescope, Trash2, Turtle, Workflow, X,
} from "lucide-react";
import { api, SessionRow } from "../api";
import { useApp } from "../store";
import { EMPTY_REPLY, hasContent, hasReply, useDrafts } from "../drafts";
import { clsx, displayName, tidyNote, timeAgo } from "../util";
import { Popover, useInterval, useTick } from "./ui";
import { RailSubagents } from "./Subagents";

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

// ---------------------------------------------------------------- avatars
// Every agent gets a little emblem: a glyph picked per agent on a circle coloured by its harness.
const GLYPHS: LucideIcon[] = [
  Cat, Dog, Bird, Fish, Rabbit, Squirrel, Turtle, Snail, Bug, Shell, Feather, Leaf, Flower2, Sprout, Clover, Cherry,
  Citrus, Star, Orbit, Rocket, Ghost, Origami, Gem, Anchor, Compass, Mountain, Snowflake, Flame, Cookie, Sailboat, Telescope, Atom,
];

function hash(key: string): number {
  let h = 2166136261;
  for (const ch of key) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return h >>> 0;
}

/** Glyph per session: preferred by id, else the next free one, assigned in creation order so agents keep theirs.
 *  Archived ones go last, so showing them doesn't change the others'. */
export function useGlyphs(): Map<string, LucideIcon> {
  const sessions = useApp((s) => s.sessions);
  return useMemo(() => {
    const out = new Map<string, LucideIcon>();
    const used = new Set<number>();
    const ordered = sessions.filter((s) => s.role === "worker").sort((a, b) => Number(a.archived) - Number(b.archived) || (a.created_at ?? a.updated_at) - (b.created_at ?? b.updated_at) || a.id.localeCompare(b.id));
    for (const s of ordered) {
      let i = hash(s.id) % GLYPHS.length;
      for (let n = 0; n < GLYPHS.length && used.has(i); n++) i = (i + 1) % GLYPHS.length;
      used.add(i);
      out.set(s.id, GLYPHS[i]!);
    }
    return out;
  }, [sessions]);
}

export function AgentAvatar({ s, glyph, size = 32 }: { s: SessionRow; glyph?: LucideIcon; size?: number }) {
  const Glyph = glyph ?? GLYPHS[hash(s.id) % GLYPHS.length]!;
  return (
    <span className={clsx("agent-av", `h-${s.harness}`)} style={{ width: size, height: size }}>
      <Glyph size={Math.round(size * 0.5)} strokeWidth={1.9} />
    </span>
  );
}

// ---------------------------------------------------------------- hover card
function HoverCard({ anchor, children }: { anchor: HTMLElement; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = anchor.getBoundingClientRect();
    const h = el.offsetHeight;
    const left = (anchor.closest(".rail") ?? anchor).getBoundingClientRect().left; // flush with the rail, whatever the button's size
    el.style.right = `${window.innerWidth - left + 8}px`;
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

/** An agent's unsent reply while its composer isn't on screen: the first line, else what's attached. */
function useDraftAway(sid: string): string | null {
  const away = useApp((st) => st.sessionId !== sid || !!st.subagentId || !!st.compactionId);
  const d = useDrafts((st) => st.bySession[sid]);
  if (!away || !d || !hasReply(d)) return null;
  const n = d.attachments.length;
  return d.text.trim().split("\n")[0] || `${n} attachment${n === 1 ? "" : "s"}`;
}

function AgentTip({ s, glyph }: { s: SessionRow; glyph?: LucideIcon }) {
  const live = liveLabel(s);
  const text = s.doing || s.first_prompt;
  const draft = useDraftAway(s.id);
  return (
    <>
      <div className="tip-title">
        <AgentAvatar s={s} glyph={glyph} size={22} />
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
            <span className={clsx(s.workspace_gone && "ws-gone")}>⎇ {s.branch}</span>
            {s.workspace_gone ? (s.worktree ? " · worktree removed" : " · removed") : s.worktree ? " · worktree" : ""}
          </span>
        )}
        <span className="grow" />
        <span>{timeAgo(s.updated_at)} ago</span>
      </div>
      {s.pending > 0 && <div className="tip-alert">{s.pending} permission request{s.pending > 1 ? "s" : ""} waiting</div>}
      {draft && (
        <div className="tip-draft">
          <SquarePen size={12} />
          <span className="ellipsis">Draft: {draft}</span>
        </div>
      )}
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
  const { project, meta, sessions, sessionId, subagentId, compactionId, selectSession, showArchived, setShowArchived, refreshSessions, toast } = useApp();
  const glyphs = useGlyphs();
  // agents with an unsent reply, but not the one whose composer is on screen
  const drafted = useDrafts(useShallow((d) => Object.keys(d.bySession).filter((k) => hasReply(d.bySession[k]) && (k !== sessionId || !!subagentId || !!compactionId))));
  const updateReply = useDrafts((d) => d.updateReply);
  const setArchived = async (s: SessionRow, archived: boolean) => {
    if (!project) return;
    const res = await api.patchSession(project.id, s.id, { archived }).catch((e) => void toast(e.message, "error"));
    const note = tidyNote(res?.worktree);
    if (note) toast(...note);
    if (archived && s.id === sessionId) selectSession(null);
    void refreshSessions();
  };
  const remove = async (s: SessionRow) => {
    if (!project) return;
    const harness = meta?.harnesses.find((h) => h.id === s.harness)?.label ?? s.harness;
    const live = s.live ? " The running agent is stopped first." : "";
    if (!confirm(`Delete “${displayName(s)}”?\n\nThe conversation is removed from ${harness} as well.${live} A copy of the transcript is kept in .atrium/trash/sessions.`)) return;
    try {
      await api.deleteSession(project.id, s.id);
      updateReply(s.id, () => EMPTY_REPLY);
      if (s.id === sessionId) selectSession(null);
      toast(`Deleted ${displayName(s)}`, "ok");
    } catch (e: any) {
      toast(e.message || String(e), "error");
    }
    void refreshSessions();
  };
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
  const active = workers.filter((s) => !s.archived && isActive(s));
  const rest = workers.filter((s) => !s.archived && !isActive(s));
  const archived = workers.filter((s) => s.archived);
  const item = (s: SessionRow) => (
    <div key={s.id} className={clsx("sb-row", s.id === sessionId && "active")}>
      <button className={clsx("sb-item", s.id === sessionId && "active", s.archived && "archived")} onClick={() => (selectSession(s.id), onClose())} title={s.cwd}>
        <span className="fly-av">
          <AgentAvatar s={s} glyph={glyphs.get(s.id)} size={26} />
          {statusClass(s) && <span className={clsx("st dot", statusClass(s))} />}
        </span>
        <div className="body">
          <div className="name">
            <span className={clsx("ellipsis", !s.name && "unnamed")} style={{ flex: 1 }}>
              {displayName(s)}
            </span>
            {s.harness !== "claude-code" && <span className={clsx("harness-badge", s.harness)}>{s.harness}</span>}
            {s.pending > 0 && <span className="badge-n">{s.pending}</span>}
            {drafted.includes(s.id) && (
              <span className="draft-mark" title="Unsent draft">
                <SquarePen size={12} />
              </span>
            )}
            <span className="faint" style={{ fontSize: 11, fontWeight: 400 }}>
              {timeAgo(s.updated_at)}
            </span>
          </div>
          <div className="sub ellipsis">
            {s.worktree && s.branch && (
              <span className={clsx("mono", s.workspace_gone && "ws-gone")} style={{ color: "var(--accent-strong)", marginRight: 5 }} title={s.workspace_gone ? "Its worktree was removed" : undefined}>
                ⎇ {s.branch}
              </span>
            )}
            {s.doing || s.first_prompt || <span className="faint">—</span>}
          </div>
        </div>
      </button>
      <div className="sb-actions">
        <button className="icon-btn sm" title={s.archived ? "Unarchive" : "Archive"} onClick={() => void setArchived(s, !s.archived)}>
          {s.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
        </button>
        <button className="icon-btn sm danger" title="Delete" onClick={() => void remove(s)}>
          <Trash2 size={14} />
        </button>
      </div>
    </div>
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
        {archived.length > 0 && <div className="sb-title">Archived</div>}
        {archived.map(item)}
        {workers.length === 0 && <div className="faint" style={{ padding: "8px 12px", fontSize: 12.5 }}>{filter ? "No match." : "No sessions in this directory yet."}</div>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- rail
export function Rail() {
  useTick(30000);
  const { project, sessions, sessionId, subagentId, compactionId, selectSession, selectSubagent, selectCompaction, refreshSessions, setPickerOpen, setSettingsOpen, theme, setTheme, setPanel } =
    useApp();
  const draftWaiting = useDrafts((s) => hasContent(s.byProject[project?.id ?? ""]));
  const [tip, setTip] = useState<{ el: HTMLElement; content: ReactNode } | null>(null);
  const [flyout, setFlyout] = useState(false);
  const [collapsed, setCollapsed] = useState(false); // the selected agent's subagents open under it, unless clicked shut
  const glyphs = useGlyphs();
  const workers = useMemo(() => {
    const w = sessions.filter((s) => s.role === "worker" && !s.archived);
    return [...w.filter(isActive), ...w.filter((s) => !isActive(s))];
  }, [sessions]);
  const childOpen = !!subagentId || !!compactionId; // a subagent or compacted history in the chat column, not the agent's chat
  useEffect(() => setCollapsed(false), [sessionId, subagentId, compactionId]);
  // subagent progress rarely triggers a session event (and can only be guessed for CLI sessions): poll meanwhile
  useInterval(() => void refreshSessions(), workers.some((s) => s.subagents?.some((a) => a.status === "running")) ? 4000 : null);
  const pickAgent = (s: SessionRow) => (s.id === sessionId && !childOpen ? setCollapsed((c) => !c) : selectSession(s.id));
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
        className="rail-btn"
        onClick={() => setPickerOpen(true)}
        aria-label="Open project"
      >
        <FolderOpen size={17} />
      </button>
      {project && (
        <>
          <button className={clsx("rail-btn rail-new", !sessionId && "on")} onClick={() => selectSession(null)} aria-label={draftWaiting ? "New agent (draft saved)" : "New agent"}>
            <SquarePen size={17} />
            {draftWaiting && sessionId && <span className="draft-dot" />}
          </button>
          <div className="rail-sep" />
          <div className="rail-agents">
            {workers.map((s) => {
              const sc = statusClass(s);
              const subs = s.subagents ?? [];
              const kids = subs.length + (s.compactions?.length ?? 0);
              const open = s.id === sessionId && !collapsed && kids > 0;
              return (
                <Fragment key={s.id}>
                  <button
                    className={clsx("rail-agent", s.id === sessionId && !childOpen && "on", s.id === sessionId && childOpen && "parent-on")}
                    onClick={click(() => pickAgent(s))}
                    aria-expanded={kids ? open : undefined}
                    {...hoverProps(<AgentTip s={s} glyph={glyphs.get(s.id)} />)}
                  >
                    <AgentAvatar s={s} glyph={glyphs.get(s.id)} />
                    {sc && <span className={clsx("st dot", sc)} />}
                    {s.pending > 0 && <span className="pend">{s.pending}</span>}
                    {kids > 0 && !open && (
                      <span className={clsx("subs-n", subs.some((a) => a.status === "running") && "on")}>{kids}</span>
                    )}
                  </button>
                  {kids > 0 && (
                    <RailSubagents
                      parent={s}
                      open={open}
                      hover={hoverProps}
                      onPick={(a) => (setTip(null), selectSubagent(s.id, a.id))}
                      onPickCompaction={(c) => (setTip(null), selectCompaction(s.id, c.id))}
                    />
                  )}
                </Fragment>
              );
            })}
          </div>
          <button className={clsx("rail-btn rail-flyout-toggle", flyout && "on")} onClick={() => setFlyout((o) => !o)} aria-label="All agents">
            <LayoutList size={16} />
          </button>
          <div className="rail-sep" />
          <button className="rail-btn" onClick={() => setPanel("board")} aria-label="Tracker Agent">
            <Workflow size={16} />
          </button>
          <button className="rail-btn" onClick={() => setPanel("git")} aria-label="Git Agent">
            <GitBranch size={16} />
          </button>
        </>
      )}
      <span className="grow" />
      <button className="rail-btn" onClick={() => setSettingsOpen(true)} aria-label="Settings">
        <Settings size={16} />
      </button>
      <Popover
        up
        align="right"
        trigger={(_o, toggle) => (
          <button className="rail-btn" onClick={toggle} aria-label="Theme">
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
