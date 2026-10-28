import type { JSX } from "react";
import { useEffect, useRef, useState } from "react";
import {
  Archive, ArchiveRestore, FileDiff, Files, GitBranch, LayoutDashboard, Maximize2, Minimize2, PanelRightClose, PanelRightOpen, Plug, Unplug,
} from "lucide-react";
import { api } from "./api";
import { Panel, useApp } from "./store";
import { clsx, displayName, shortPath } from "./util";
import { Sidebar, liveLabel, statusClass } from "./components/Sidebar";
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

function PanelToggles() {
  const { panel, togglePanel } = useApp();
  return (
    <>
      {TABS.map((t) => (
        <button key={t.id} className={clsx("icon-btn", panel === t.id && "active")} title={t.label} onClick={() => togglePanel(t.id)}>
          {t.icon}
        </button>
      ))}
    </>
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
        {s && <span className={clsx("dot", statusClass(s))} />}
        {editing ? (
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onBlur={() => void save()} onKeyDown={(e) => (e.key === "Enter" ? void save() : e.key === "Escape" && setEditing(false))} />
        ) : (
          <span className="editable ellipsis" title="Rename agent" onClick={() => (setName(s?.name || s?.title || ""), setEditing(true))}>
            {s ? displayName(s) : "Session"}
          </span>
        )}
        {s && <span className={clsx("harness-badge", s.harness)}>{s.harness === "claude-code" ? "Claude Code" : s.harness}</span>}
        {s && !s.name && <span className="pill hide-narrow">unnamed</span>}
        {s?.branch && (
          <span className={clsx("pill mono", s.worktree && "accent")} title={s.cwd}>
            ⎇ {s.branch}
            <span className="hide-narrow">{s.worktree ? " · worktree" : ""}</span>
          </span>
        )}
        {s && liveLabel(s) && <span className={clsx("pill hide-narrow", s.live === "waiting" || s.pending ? "warn" : "ok")}>{liveLabel(s)}</span>}
        {s?.status && s.progress != null && s.progress > 0 && <span className="pill hide-narrow">{s.progress}%</span>}
      </div>
      <div className="actions">
        <button className="btn btn-sm btn-ghost" onClick={() => reviewChanges(sessionId)} title="Review this agent's file changes">
          <FileDiff size={14} /> <span className="hide-narrow">Review changes</span>
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
          title={s?.archived ? "Unarchive" : "Archive (hide from the list and the board)"}
          onClick={async () => {
            await api.patchSession(project.id, sessionId, { archived: !s?.archived });
            await refreshSessions();
            if (!s?.archived) selectSession(null);
          }}
        >
          {s?.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
        </button>
        <span className="sep" />
        <PanelToggles />
      </div>
    </div>
  );
}

function WorkspacePanel() {
  const { panel, setPanel, panelMax, setPanelMax, panelWidth, setPanelWidth, project } = useApp();
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!drag) return;
    const move = (e: MouseEvent) => setPanelWidth(Math.max(360, Math.min(window.innerWidth - 520, window.innerWidth - e.clientX)));
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
  }, [drag, setPanelWidth]);
  if (!panel || !project) return null;
  return (
    <div ref={ref} className={clsx("panel", panelMax && "max")} style={panelMax ? undefined : { width: panelWidth }}>
      {!panelMax && <div className={clsx("resizer", drag && "drag")} onMouseDown={() => setDrag(true)} />}
      <div className="panel-tabs">
        {TABS.map((t) => (
          <button key={t.id} className={clsx("panel-tab", panel === t.id && "on")} onClick={() => setPanel(t.id)}>
            {t.icon} {t.label}
          </button>
        ))}
        <span className="grow" />
        <button className="icon-btn" title={panelMax ? "Restore" : "Maximize"} onClick={() => setPanelMax(!panelMax)}>
          {panelMax ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
        <button className="icon-btn" title="Close panel" onClick={() => setPanel(null)}>
          <PanelRightClose size={15} />
        </button>
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
    </div>
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
  const { init, project, sessionId, sessions, pickerOpen, settingsOpen, panel, panelMax, setPanel } = useApp();
  useEffect(() => {
    void init();
  }, [init]);
  const current = sessions.find((s) => s.id === sessionId);
  const cwd = current?.cwd ?? project?.path ?? "";

  return (
    <div className="app">
      <Sidebar />
      <div className={clsx("main", panelMax && panel && "hidden")}>
        {project && sessionId ? (
          <>
            <SessionHeader />
            <ChatView key={sessionId} pid={project.id} sid={sessionId} cwd={cwd} />
          </>
        ) : project ? (
          <>
            <div className="topbar">
              <div className="t-name muted" style={{ fontSize: 14 }}>
                {project.name} <span className="faint mono" style={{ fontSize: 12 }}>{shortPath(project.path)}</span>
              </div>
              <div className="actions">
                <PanelToggles />
                {!panel && (
                  <button className="icon-btn" title="Open board" onClick={() => setPanel("board")}>
                    <PanelRightOpen size={15} />
                  </button>
                )}
              </div>
            </div>
            <Home />
          </>
        ) : (
          <div className="empty-state">
            <h3>Open a project to begin</h3>
            <div>Atrium is organised around directories — pick one to see its agents, board, files and git.</div>
          </div>
        )}
      </div>
      <WorkspacePanel />
      {pickerOpen && <ProjectPicker />}
      {settingsOpen && <SettingsDialog />}
      <Toasts />
    </div>
  );
}
