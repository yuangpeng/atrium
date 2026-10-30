// The home page, when no project is open: what needs you across projects, then the pinned and the most opened
// projects with what their agents are doing, and the archived ones; open a folder or make a new one.
import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { Archive, ArchiveRestore, ChevronRight, FolderOpen, FolderPlus, Pin, PinOff, Plus, Search, Trash2 } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { AgentBrief, api, HomeProject } from "../api";
import { useApp } from "../store";
import { socket } from "../ws";
import { clsx, displayName, isMac, shortPath, throttle, timeAgo } from "../util";
import { AgentAvatar, glyphsFor, statusClass } from "./Rail";
import { Spinner } from "./ui";

// Project events that change what the page shows. A state change (running, waiting, done…) shows within a
// couple of seconds; a transcript growing (sessions_changed, while an agent works) only moves "updated", and
// files written (fs, git_changed) only the changed-files count, which the timer picks up.
const REFRESH_SOON = new Set(["session_state", "board", "tracker"]);
const REFRESH_LATER = new Set(["sessions_changed", "worktrees_changed"]);
const EVERY = 30_000;

const isRunning = (a: AgentBrief) => a.live === "running" || a.live === "starting";
/** Waiting for the human: an approval, or it said it waits or is blocked (and hasn't started working again since). */
const needsYou = (a: AgentBrief) => a.live === "waiting" || a.pending > 0 || ((a.status === "waiting" || a.status === "blocked") && !isRunning(a));

function agentLine(a: AgentBrief): string {
  if (a.live === "waiting" || a.pending > 0) return "needs approval";
  if (needsYou(a)) return a.doing ? `${a.status}: ${a.doing}` : a.status === "waiting" ? "waiting for you" : "blocked";
  if (isRunning(a)) return a.doing || "running";
  if (a.status === "done") return a.doing ? `done: ${a.doing}` : "done";
  return a.doing || (a.live === "idle" ? "attached" : "idle");
}

const rank = (a: AgentBrief) => (needsYou(a) ? 0 : isRunning(a) ? 1 : 2);
const byAttention = (x: AgentBrief, y: AgentBrief) => rank(x) - rank(y) || y.updated_at - x.updated_at;
const lastActive = (p: HomeProject) => Math.max(p.last_opened ?? 0, ...(p.agents ?? []).map((a) => a.updated_at));
const stop = (f: () => void) => (e: MouseEvent) => (e.stopPropagation(), f());

export function HomePage() {
  const { openProject, selectSession, setChatOpen, setPickerOpen, homeFocus, toast } = useApp();
  const [projects, setProjects] = useState<HomeProject[] | null>(null);
  const [query, setQuery] = useState("");
  const search = useRef<HTMLInputElement>(null);

  const live = useRef({ mounted: true, missed: false }); // missed: asked while the tab was hidden, fetched when it shows
  const { load, soon, later } = useMemo(() => {
    let asked = 0;
    const load = () => {
      if (!live.current.mounted) return; // a throttled call after leaving the page
      if (document.hidden) return void (live.current.missed = true);
      const n = ++asked;
      api.home().then((d) => n === asked && setProjects(d), (e) => toast(e.message || String(e), "error"));
    };
    return { load, soon: throttle(load, 2000), later: throttle(load, 10_000) };
  }, [toast]);
  useEffect(() => {
    const state = live.current;
    state.mounted = true;
    load();
    const timer = window.setInterval(load, EVERY);
    const shown = () => {
      if (!document.hidden && state.missed) {
        state.missed = false;
        load();
      }
    };
    document.addEventListener("visibilitychange", shown);
    return () => {
      state.mounted = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", shown);
    };
  }, [load]);
  // follow the shown projects' events
  const followed = (projects ?? []).filter((p) => p.exists && !p.archived_at).map((p) => p.id).join(",");
  useEffect(() => {
    const offs = followed.split(",").filter(Boolean).map((pid) =>
      socket.subscribe(`project:${pid}`, (msg) => (REFRESH_SOON.has(msg.type) ? soon() : REFRESH_LATER.has(msg.type) && later())),
    );
    offs.push(socket.onReconnect(soon));
    return () => offs.forEach((off) => off());
  }, [followed, soon, later]);
  useEffect(() => {
    if (homeFocus) search.current?.focus();
  }, [homeFocus]);

  const glyphs = useMemo(() => new Map((projects ?? []).map((p) => [p.id, glyphsFor(p.agents ?? [])] as [string, Map<string, LucideIcon>])), [projects]);
  const act = (req: Promise<unknown>) => void req.then(load, (e) => toast(e.message || String(e), "error"));
  const fail = (e: any) => toast(e.message || String(e), "error");
  const open = (p: HomeProject) => void openProject(p.id, true).catch(fail);
  const openAgent = (p: HomeProject, a: AgentBrief) => void openProject(p.id, true).then(() => selectSession(a.id), fail);
  const newAgent = (p: HomeProject) => void openProject(p.id, true).then(() => setChatOpen(true), fail);

  const all = projects ?? [];
  const q = query.trim().toLowerCase();
  const match = (p: HomeProject) => !q || p.name.toLowerCase().includes(q) || p.path.toLowerCase().includes(q);
  const current = all.filter((p) => !p.archived_at);
  const pinned = current.filter((p) => p.pinned_at && match(p)).sort((a, b) => a.pinned_at! - b.pinned_at!);
  const frequent = current.filter((p) => !p.pinned_at && match(p)).sort((a, b) => (b.open_count ?? 0) - (a.open_count ?? 0) || lastActive(b) - lastActive(a));
  const archived = all.filter((p) => p.archived_at && match(p)).sort((a, b) => b.archived_at! - a.archived_at!);
  const needs = current
    .flatMap((p) => (p.agents ?? []).filter(needsYou).map((a) => ({ p, a })))
    .filter(({ p, a }) => match(p) || displayName(a).toLowerCase().includes(q))
    .sort((x, y) => y.a.updated_at - x.a.updated_at);
  const agents = current.flatMap((p) => p.agents ?? []);
  const today = new Date().setHours(0, 0, 0, 0) / 1000;

  const card = (p: HomeProject) => (
    <ProjectCard
      key={p.id}
      p={p}
      onOpen={() => open(p)}
      onAgent={(a) => openAgent(p, a)}
      onNewAgent={() => newAgent(p)}
      onPin={() => act(api.markProject(p.id, { pinned: !p.pinned_at }))}
      onArchive={() => act(api.markProject(p.id, { archived: true }))}
      onRemove={() => act(api.removeProject(p.id))}
    />
  );

  return (
    <main className="workspace">
      <div className="homepage">
        <header className="home-top">
          <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="8" fill="#D97757" />
            <path d="M9 23 16 8l7 15M12 17h8" stroke="#fff" strokeWidth="2.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <h1>Atrium</h1>
          <label className="home-search">
            <Search size={14} />
            <input
              ref={search}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setQuery("");
                const first = [...pinned, ...frequent, ...archived].find((p) => p.exists);
                if (e.key === "Enter" && q && first) open(first);
              }}
              placeholder="Search projects"
            />
            <span className="kbd">{isMac ? "⌘K" : "Ctrl K"}</span>
          </label>
          <span className="grow" />
          <button className="btn" onClick={() => setPickerOpen("open")}>
            <FolderOpen size={14} /> Open folder…
          </button>
          <button className="btn btn-primary" onClick={() => setPickerOpen("create")}>
            <FolderPlus size={14} /> New folder…
          </button>
        </header>
        {projects === null ? (
          <div className="homepage-empty">
            <Spinner />
          </div>
        ) : (
          <>
            <div className="home-stats">
              <span>
                <span className="dot running" /> {agents.filter(isRunning).length} running
              </span>
              <span>
                <span className="dot waiting" /> {agents.filter(needsYou).length} need you
              </span>
              <span>
                <span className="dot done" /> {agents.filter((a) => a.status === "done" && a.updated_at >= today).length} done today
              </span>
              <span className="faint">
                {current.filter((p) => p.exists).length} project{current.filter((p) => p.exists).length === 1 ? "" : "s"}
              </span>
            </div>
            {needs.length > 0 && (
              <section>
                <div className="section-label">Needs you</div>
                <div className="needs-list">
                  {needs.map(({ p, a }) => (
                    <button key={a.id} className="needs-item" onClick={() => openAgent(p, a)}>
                      <AgentAvatar s={a} glyph={glyphs.get(p.id)?.get(a.id)} size={28} />
                      <span className="grow">
                        <span className="needs-name ellipsis">
                          {displayName(a)} <span className="faint">· {p.name}</span>
                        </span>
                        <span className="needs-line ellipsis">{agentLine(a)}</span>
                      </span>
                      <span className="faint">{timeAgo(a.updated_at)}</span>
                    </button>
                  ))}
                </div>
              </section>
            )}
            {pinned.length > 0 && (
              <section>
                <div className="section-label">Pinned</div>
                <div className="proj-grid">{pinned.map(card)}</div>
              </section>
            )}
            <section>
              <div className="section-label">Frequent</div>
              {frequent.length > 0 ? (
                <div className="proj-grid">{frequent.map(card)}</div>
              ) : (
                <div className="homepage-empty muted">{q ? "No project matches." : "No projects here yet: open a folder, or make a new one."}</div>
              )}
            </section>
            {archived.length > 0 && (
              <details className="home-archived" open={!!q}>
                <summary className="section-label">
                  <ChevronRight size={12} className="chev" /> Archived · {archived.length}
                </summary>
                {archived.map((p) => (
                  <div key={p.id} className="proj-row" onClick={() => p.exists && open(p)}>
                    <div className="grow">
                      <div style={{ fontWeight: 600 }}>{p.name}</div>
                      <div className="muted mono ellipsis" style={{ fontSize: 12 }}>
                        {shortPath(p.path)} {!p.exists && <span className="pill err">missing</span>}
                      </div>
                    </div>
                    <span className="faint" style={{ fontSize: 12 }} title="Archived">
                      {timeAgo(p.archived_at)}
                    </span>
                    <button className="icon-btn sm" title="Unarchive" onClick={stop(() => act(api.markProject(p.id, { archived: false })))}>
                      <ArchiveRestore size={13} />
                    </button>
                    <button className="icon-btn sm danger" title="Remove from the list (the folder stays)" onClick={stop(() => act(api.removeProject(p.id)))}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
              </details>
            )}
          </>
        )}
      </div>
    </main>
  );
}

function ProjectCard({ p, onOpen, onAgent, onNewAgent, onPin, onArchive, onRemove }: {
  p: HomeProject;
  onOpen: () => void;
  onAgent: (a: AgentBrief) => void;
  onNewAgent: () => void;
  onPin: () => void;
  onArchive: () => void;
  onRemove: () => void;
}) {
  const agents = [...(p.agents ?? [])].sort(byAttention);
  const active = lastActive(p);
  return (
    <div className={clsx("proj-card", !p.exists && "missing")} onClick={() => p.exists && onOpen()}>
      <div className="proj-card-head">
        <div className="grow">
          <div className="proj-card-name ellipsis">{p.name}</div>
          <div className="proj-card-path mono ellipsis">{shortPath(p.path)}</div>
        </div>
        <div className="proj-card-actions">
          {p.exists && (
            <button className="icon-btn sm" title={p.pinned_at ? "Unpin" : "Pin"} onClick={stop(onPin)}>
              {p.pinned_at ? <PinOff size={13} /> : <Pin size={13} />}
            </button>
          )}
          <button className="icon-btn sm" title="Archive" onClick={stop(onArchive)}>
            <Archive size={13} />
          </button>
          {!p.exists && (
            <button className="icon-btn sm danger" title="Remove from the list" onClick={stop(onRemove)}>
              <Trash2 size={13} />
            </button>
          )}
        </div>
      </div>
      {!p.exists ? (
        <div className="proj-card-headline">The folder is gone.</div>
      ) : p.error ? (
        <div className="proj-card-headline err">{p.error}</div>
      ) : (
        p.headline && <div className="proj-card-headline">{p.headline}</div>
      )}
      {agents.length > 0 && (
        <div className="proj-card-agents">
          {agents.slice(0, 3).map((a) => (
            <button key={a.id} className="proj-agent" onClick={stop(() => onAgent(a))} title={`${displayName(a)} — ${agentLine(a)}`}>
              <span className={clsx("dot", statusClass(a))} />
              <span className="proj-agent-name ellipsis">{displayName(a)}</span>
              <span className="proj-agent-line ellipsis">{agentLine(a)}</span>
            </button>
          ))}
          {agents.length > 3 && <div className="proj-more faint">+{agents.length - 3} more</div>}
        </div>
      )}
      {p.exists && (
        <div className="proj-card-foot">
          <span className="grow ellipsis">
            {p.git ? `⎇ ${p.git.branch ?? "?"} · ${p.git.changed ? `${p.git.changed} changed` : "clean"}` : "not a git repo"}
            {active > 0 && ` · ${timeAgo(active)}`}
          </span>
          <button className="btn btn-sm" onClick={stop(onNewAgent)}>
            <Plus size={13} /> New agent
          </button>
        </div>
      )}
    </div>
  );
}
