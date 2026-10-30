import type { JSX, ReactNode } from "react";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, ArchiveRestore, ArrowLeft, ChevronRight, FileDiff, Files, FolderOpen, GitBranch, GitFork, LayoutDashboard, MessageSquare, PanelRightClose, Plug, Unplug } from "lucide-react";
import { api, loginUrl, SessionRow } from "./api";
import { Panel, useApp } from "./store";
import { clsx, displayName, tidyNote } from "./util";
import { Rail, liveLabel, statusClass } from "./components/Rail";
import { Home } from "./components/Home";
import { ProjectPicker } from "./components/Dialogs";
import { ForkDialog } from "./components/ForkDialog";
import { useFullscreenElement } from "./components/ui";
import { SettingsDialog } from "./components/SettingsDialog";
import { ChatView } from "./chat/ChatView";
import { SubagentView } from "./chat/SubagentView";
import { CompactionView } from "./chat/CompactionView";
import { SUBAGENT_STATUS, SubagentAvatar, subagentLabel } from "./components/Subagents";
import { CompactionAvatar, compactedWhen, compactionFacts, compactionLabel } from "./components/Compactions";
import { BoardPanel } from "./panels/BoardPanel";
import { FilesPanel } from "./panels/FilesPanel";
import { ChangesPanel } from "./panels/ChangesPanel";
import { GitPanel } from "./panels/GitPanel";

const TABS: { id: Panel; label: string; icon: JSX.Element }[] = [
  { id: "board", label: "Board", icon: <LayoutDashboard size={16} /> },
  { id: "files", label: "Files", icon: <Files size={16} /> },
  { id: "changes", label: "Changes", icon: <FileDiff size={16} /> },
  { id: "git", label: "Git", icon: <GitBranch size={16} /> },
];
const RAIL_WIDTH = 52;
const MIN_CHAT = 340;
const MIN_WORKSPACE = 420;

function CloseChat() {
  const setChatOpen = useApp((s) => s.setChatOpen);
  return (
    <button className="icon-btn" title="Hide chat" onClick={() => setChatOpen(false)}>
      <PanelRightClose size={15} />
    </button>
  );
}

function SessionHeader() {
  const { project, sessions, sessionId, toast, refreshSessions, reviewChanges, selectSession, setForkRequest, meta } = useApp();
  const s = sessions.find((x) => x.id === sessionId);
  const canFork = meta?.harnesses.find((h) => h.id === s?.harness)?.can_fork !== false;
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  if (!project || !sessionId) return null;
  const save = async () => {
    setEditing(false);
    if (!name.trim() || name === s?.name) return;
    await api.patchSession(project.id, sessionId, { name: name.trim() }).catch((e) => toast(e.message, "error"));
    void refreshSessions();
  };
  const busy = s?.live === "running" || s?.live === "waiting" || s?.live === "starting";
  return (
    <div className="topbar bordered">
      <div className="t-name">
        {s && <span className={clsx("dot", statusClass(s))} title={liveLabel(s) || undefined} />}
        {editing ? (
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onBlur={() => void save()} onKeyDown={(e) => (e.key === "Enter" ? void save() : e.key === "Escape" && setEditing(false))} />
        ) : (
          <span className="editable ellipsis" title="Rename agent" onClick={() => (setName(s?.name || s?.title || ""), setEditing(true))}>
            {s ? displayName(s) : "Session"}
          </span>
        )}
        {s?.branch && (
          <span className={clsx("pill mono hide-narrow", s.worktree && "accent", s.workspace_gone && "gone")} title={s.workspace_gone ? `${s.cwd} (removed)` : s.cwd}>
            ⎇ {s.branch}
          </span>
        )}
        {s?.forked_from && (
          <button
            className="pill fork-pill"
            disabled={!s.forked_from.exists}
            title={`Forked from “${s.forked_from.name || "another agent"}”${s.forked_from.exists ? " — open it" : " (deleted since)"}`}
            onClick={() => selectSession(s.forked_from!.session)}
          >
            <GitFork size={11} />
            <span className="hide-narrow ellipsis">{s.forked_from.name || "fork"}</span>
          </button>
        )}
      </div>
      <div className="actions">
        {canFork && (
          <button
            className="icon-btn"
            disabled={busy}
            onClick={() => setForkRequest({ sid: sessionId, at: null })}
            title={busy ? "Fork — wait for the turn to end (or stop it); or fork from an earlier message" : "Fork this conversation (try another idea in parallel)"}
          >
            <GitFork size={15} />
          </button>
        )}
        <button className="icon-btn" onClick={() => reviewChanges(sessionId)} title="Review this agent's changes">
          <FileDiff size={15} />
        </button>
        {s?.live ? (
          <button className="icon-btn" title="Detach (stop the agent process; the session stays resumable)" onClick={() => void api.closeSession(project.id, sessionId).then(refreshSessions)}>
            <Unplug size={15} />
          </button>
        ) : (
          <button
            className="icon-btn"
            title={s?.workspace_gone ? "Its worktree was removed: pick where it continues in the reply box" : "Attach (resume the session now)"}
            disabled={s?.workspace_gone}
            onClick={() => void api.attach(project.id, sessionId).then(refreshSessions).catch((e) => toast(e.message, "error"))}
          >
            <Plug size={15} />
          </button>
        )}
        <button
          className="icon-btn"
          title={s?.archived ? "Unarchive" : "Archive"}
          onClick={async () => {
            const res = await api.patchSession(project.id, sessionId, { archived: !s?.archived }).catch((e) => void toast(e.message, "error"));
            const note = tidyNote(res?.worktree);
            if (note) toast(...note);
            await refreshSessions();
            if (!s?.archived) selectSession(null);
          }}
        >
          {s?.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
        </button>
        <CloseChat />
      </div>
    </div>
  );
}

/** Agent › what opened under it (a subagent, compacted history); the way back is the agent's name or the arrow. */
function ChildHeader({ parent, children }: { parent: SessionRow | undefined; children: ReactNode }) {
  const selectSession = useApp((s) => s.selectSession);
  const back = () => parent && selectSession(parent.id);
  return (
    <div className="topbar bordered">
      <div className="t-name">
        <button className="icon-btn sm" title={`Back to ${parent ? displayName(parent) : "the agent"}`} onClick={back}>
          <ArrowLeft size={15} />
        </button>
        {parent && (
          <>
            <button className="crumb ellipsis" onClick={back}>
              {displayName(parent)}
            </button>
            <ChevronRight size={14} className="faint" style={{ flex: "none" }} />
          </>
        )}
        {children}
      </div>
      <div className="actions">
        <CloseChat />
      </div>
    </div>
  );
}

function SubagentHeader({ parent, aid }: { parent: SessionRow | undefined; aid: string }) {
  const sub = parent?.subagents?.find((a) => a.id === aid);
  return (
    <ChildHeader parent={parent}>
      {sub && parent && <SubagentAvatar a={sub} harness={parent.harness} size={22} />}
      <span className="ellipsis" title={sub?.description}>
        {sub ? subagentLabel(sub) : "Subagent"}
      </span>
      {sub && <span className={clsx("dot", SUBAGENT_STATUS[sub.status].dot)} title={SUBAGENT_STATUS[sub.status].label} />}
    </ChildHeader>
  );
}

function CompactionHeader({ parent, cid }: { parent: SessionRow | undefined; cid: string }) {
  const n = parent?.compactions?.findIndex((c) => c.id === cid) ?? -1;
  const c = parent?.compactions?.[n];
  return (
    <ChildHeader parent={parent}>
      {parent && <CompactionAvatar harness={parent.harness} size={22} />}
      <span style={{ flex: "none" }} title={c ? compactionFacts(c) : undefined}>
        {n >= 0 ? compactionLabel(n + 1) : "Compacted history"}
      </span>
      {c?.at && <span className="pill hide-narrow">{compactedWhen(c)}</span>}
    </ChildHeader>
  );
}

/** The project's name; hover shows the full path, click copies it. */
function ProjectPath({ path, name }: { path: string; name: string }) {
  const toast = useApp((s) => s.toast);
  return (
    <button
      className="ws-project"
      data-path={path}
      onClick={() => void navigator.clipboard.writeText(path).then(() => toast("Path copied", "ok"), () => toast("Couldn't copy the path", "error"))}
    >
      <span className="ellipsis">{name}</span>
    </button>
  );
}

/** Board / Files / Changes / Git — the large area on the left. */
function Workspace() {
  const { panel, setPanel, project, chatOpen, setChatOpen } = useApp();
  if (!project) return null;
  return (
    <main className="workspace">
      <div className="ws-tabs">
        {TABS.map((t) => (
          <button key={t.id} className={clsx("panel-tab icon-only", panel === t.id && "on")} onClick={() => setPanel(t.id)} aria-label={t.label}>
            {t.icon}
          </button>
        ))}
        <span className="grow" />
        <ProjectPath path={project.path} name={project.name} />
        {!chatOpen && (
          <button className="icon-btn" title="Show chat" onClick={() => setChatOpen(true)}>
            <MessageSquare size={15} />
          </button>
        )}
      </div>
      {/* keep panels mounted so editors/graphs don't lose state when switching tabs */}
      <div className="panel-body" style={{ display: panel === "board" ? "flex" : "none" }}>
        <BoardPanel />
      </div>
      <div className="panel-body" style={{ display: panel === "files" ? "flex" : "none" }}>
        <FilesPanel />
      </div>
      <div className="panel-body" style={{ display: panel === "changes" ? "flex" : "none" }}>
        {panel === "changes" && <ChangesPanel />}
      </div>
      <div className="panel-body" style={{ display: panel === "git" ? "flex" : "none" }}>
        {panel === "git" && <GitPanel />}
      </div>
    </main>
  );
}

/** The selected agent's conversation, or the new-agent composer — right of the workspace. */
function ChatColumn() {
  const { project, sessionId, subagentId, compactionId, sessions, chatWidth, setChatWidth } = useApp();
  const [drag, setDrag] = useState(false);
  useEffect(() => {
    if (!drag) return;
    const move = (e: PointerEvent) => setChatWidth(Math.max(MIN_CHAT, Math.min(window.innerWidth - RAIL_WIDTH - MIN_WORKSPACE, window.innerWidth - RAIL_WIDTH - e.clientX)));
    const up = () => setDrag(false);
    document.body.classList.add("col-resizing");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    window.addEventListener("blur", up); // switched away mid-drag: the release may never reach the page
    return () => {
      document.body.classList.remove("col-resizing");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      window.removeEventListener("blur", up);
    };
  }, [drag, setChatWidth]);
  if (!project) return null;
  const current = sessions.find((s) => s.id === sessionId);
  return (
    <section className="chatcol" style={{ width: chatWidth }}>
      <div
        className={clsx("resizer", drag && "drag")}
        onPointerDown={(e) => {
          if (e.button !== 0) return;
          // A fast pointer gets past the 6px handle onto the board, an iframe, which would take the moves and the
          // release (the splitter stalls, then sticks to the pointer). Capture keeps them coming here;
          // .col-resizing also turns iframes off, for browsers whose capture stops at frames.
          e.currentTarget.setPointerCapture(e.pointerId);
          setDrag(true);
        }}
        onLostPointerCapture={() => setDrag(false)}
      />
      {sessionId && subagentId ? (
        <>
          <SubagentHeader parent={current} aid={subagentId} />
          <SubagentView key={`${sessionId}/${subagentId}`} pid={project.id} sid={sessionId} aid={subagentId} cwd={current?.cwd ?? project.path} />
        </>
      ) : sessionId && compactionId ? (
        <>
          <CompactionHeader parent={current} cid={compactionId} />
          <CompactionView key={`${sessionId}/${compactionId}`} pid={project.id} sid={sessionId} cid={compactionId} cwd={current?.cwd ?? project.path} />
        </>
      ) : sessionId ? (
        <>
          <SessionHeader />
          <ChatView key={sessionId} pid={project.id} sid={sessionId} cwd={current?.cwd ?? project.path} forkable />
        </>
      ) : (
        <>
          <div className="topbar">
            <div className="t-name">New agent</div>
            <div className="actions">
              <CloseChat />
            </div>
          </div>
          <Home />
        </>
      )}
    </section>
  );
}

function NoProject() {
  const setPickerOpen = useApp((s) => s.setPickerOpen);
  return (
    <main className="workspace">
      <div className="empty-state">
        <h3>Open a project</h3>
        <div>Atrium works per directory: its agents, board, files and git.</div>
        <button className="btn btn-primary" style={{ marginTop: 10 }} onClick={() => setPickerOpen(true)}>
          <FolderOpen size={14} /> Open…
        </button>
      </div>
    </main>
  );
}

function Toasts() {
  const toasts = useApp((s) => s.toasts);
  const loggedOut = useApp((s) => s.loggedOut);
  const fullscreen = useFullscreenElement(); // only the fullscreen element is visible: show them in there
  const list = (
    <div className="toasts">
      {loggedOut && (
        <div className="toast error">
          You were logged out. <a href={loginUrl()}>Log in again</a>
        </div>
      )}
      {toasts.map((t) => (
        <div key={t.id} className={clsx("toast", t.kind)}>
          {t.text}
        </div>
      ))}
    </div>
  );
  return fullscreen ? createPortal(list, fullscreen) : list;
}

export function App() {
  const { init, project, pickerOpen, settingsOpen, chatOpen, chatWidth, setChatWidth } = useApp();
  useEffect(() => {
    void init();
  }, [init]);
  // keep the workspace usable when the window shrinks
  useEffect(() => {
    const fit = () => {
      const max = window.innerWidth - RAIL_WIDTH - MIN_WORKSPACE;
      if (chatWidth > max) setChatWidth(Math.max(MIN_CHAT, max));
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [chatWidth, setChatWidth]);

  return (
    <div className="app">
      {project ? <Workspace /> : <NoProject />}
      {project && chatOpen && <ChatColumn />}
      <Rail />
      {pickerOpen && <ProjectPicker />}
      {settingsOpen && <SettingsDialog />}
      <ForkDialog />
      <Toasts />
    </div>
  );
}
