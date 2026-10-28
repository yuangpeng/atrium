import type { JSX } from "react";
import { useEffect, useState } from "react";
import { Archive, ArchiveRestore, FileDiff, Files, FolderOpen, GitBranch, LayoutDashboard, MessageSquare, PanelRightClose, Plug, Unplug } from "lucide-react";
import { api } from "./api";
import { Panel, useApp } from "./store";
import { clsx, displayName } from "./util";
import { Rail, liveLabel, statusClass } from "./components/Rail";
import { Home } from "./components/Home";
import { ProjectPicker } from "./components/Dialogs";
import { SettingsDialog } from "./components/SettingsDialog";
import { ChatView } from "./chat/ChatView";
import { BoardPanel } from "./panels/BoardPanel";
import { FilesPanel } from "./panels/FilesPanel";
import { ChangesPanel } from "./panels/ChangesPanel";
import { GitPanel } from "./panels/GitPanel";

const TABS: { id: Panel; label: string; icon: JSX.Element }[] = [
  { id: "board", label: "Board", icon: <LayoutDashboard size={14} /> },
  { id: "files", label: "Files", icon: <Files size={14} /> },
  { id: "changes", label: "Changes", icon: <FileDiff size={14} /> },
  { id: "git", label: "Git", icon: <GitBranch size={14} /> },
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
  const { project, sessions, sessionId, toast, refreshSessions, reviewChanges, selectSession } = useApp();
  const s = sessions.find((x) => x.id === sessionId);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  if (!project || !sessionId) return null;
  const save = async () => {
    setEditing(false);
    if (!name.trim() || name === s?.name) return;
    await api.patchSession(project.id, sessionId, { name: name.trim() }).catch((e) => toast(e.message, "error"));
    void refreshSessions();
  };
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
        {s && s.harness !== "claude-code" && <span className={clsx("harness-badge", s.harness)}>{s.harness}</span>}
        {s?.branch && (
          <span className={clsx("pill mono hide-narrow", s.worktree && "accent")} title={s.cwd}>
            ⎇ {s.branch}
          </span>
        )}
      </div>
      <div className="actions">
        <button className="icon-btn" onClick={() => reviewChanges(sessionId)} title="Review this agent's changes">
          <FileDiff size={15} />
        </button>
        {s?.live ? (
          <button className="icon-btn" title="Detach (stop the agent process; the session stays resumable)" onClick={() => void api.closeSession(project.id, sessionId).then(refreshSessions)}>
            <Unplug size={15} />
          </button>
        ) : (
          <button className="icon-btn" title="Attach (resume the session now)" onClick={() => void api.attach(project.id, sessionId).then(refreshSessions).catch((e) => toast(e.message, "error"))}>
            <Plug size={15} />
          </button>
        )}
        <button
          className="icon-btn"
          title={s?.archived ? "Unarchive" : "Archive"}
          onClick={async () => {
            await api.patchSession(project.id, sessionId, { archived: !s?.archived });
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

/** Board / Files / Changes / Git — the large area on the left. */
function Workspace() {
  const { panel, setPanel, project, chatOpen, setChatOpen } = useApp();
  if (!project) return null;
  return (
    <main className="workspace">
      <div className="ws-tabs">
        {TABS.map((t) => (
          <button key={t.id} className={clsx("panel-tab", panel === t.id && "on")} onClick={() => setPanel(t.id)}>
            {t.icon} {t.label}
          </button>
        ))}
        <span className="grow" />
        <span className="ws-project ellipsis" title={project.path}>
          {project.name}
        </span>
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
  const { project, sessionId, sessions, chatWidth, setChatWidth } = useApp();
  const [drag, setDrag] = useState(false);
  useEffect(() => {
    if (!drag) return;
    const move = (e: MouseEvent) => setChatWidth(Math.max(MIN_CHAT, Math.min(window.innerWidth - RAIL_WIDTH - MIN_WORKSPACE, window.innerWidth - RAIL_WIDTH - e.clientX)));
    const up = () => setDrag(false);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [drag, setChatWidth]);
  if (!project) return null;
  const current = sessions.find((s) => s.id === sessionId);
  return (
    <section className="chatcol" style={{ width: chatWidth }}>
      <div className={clsx("resizer", drag && "drag")} onMouseDown={() => setDrag(true)} />
      {sessionId ? (
        <>
          <SessionHeader />
          <ChatView key={sessionId} pid={project.id} sid={sessionId} cwd={current?.cwd ?? project.path} />
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
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={clsx("toast", t.kind)}>
          {t.text}
        </div>
      ))}
    </div>
  );
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
      <Toasts />
    </div>
  );
}
