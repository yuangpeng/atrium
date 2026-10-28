import { useEffect, useState } from "react";
import { ArrowUp, Folder, FolderGit2, Trash2 } from "lucide-react";
import { api } from "../api";
import { useApp } from "../store";
import { shortPath, timeAgo } from "../util";
import { Modal, Spinner } from "./ui";

export function ProjectPicker() {
  const { projects, addProject, openProject, setPickerOpen, loadProjects, toast, project } = useApp();
  const [path, setPath] = useState("");
  const [dir, setDir] = useState<Awaited<ReturnType<typeof api.browse>> | null>(null);
  const [busy, setBusy] = useState(false);

  const browse = async (p?: string) => {
    try {
      const d = await api.browse(p);
      setDir(d);
      setPath(d.path);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  useEffect(() => {
    void browse(undefined);
  }, []);

  const open = async (p: string) => {
    setBusy(true);
    try {
      await addProject(p);
    } catch (e: any) {
      toast(e.message || String(e), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Open a project"
      onClose={() => project && setPickerOpen(false)}
      wide
      footer={
        <>
          <span className="muted grow" style={{ fontSize: 12.5 }}>
            Every directory is a project: its sessions, board, files and git live together.
          </span>
          <button className="btn btn-primary" disabled={!path || busy} onClick={() => void open(path)}>
            {busy && <Spinner />} Open this folder
          </button>
        </>
      }
    >
      {projects.length > 0 && (
        <>
          <div className="section-label" style={{ paddingLeft: 2 }}>Recent</div>
          <div style={{ marginBottom: 14 }}>
            {projects.map((p) => (
              <div key={p.id} className="proj-row" onClick={() => (void openProject(p.id), setPickerOpen(false))}>
                <span className="ic">
                  <FolderGit2 size={16} />
                </span>
                <div className="grow">
                  <div style={{ fontWeight: 600 }}>{p.name}</div>
                  <div className="muted mono ellipsis" style={{ fontSize: 12 }}>
                    {shortPath(p.path)} {p.exists === false && <span className="pill err">missing</span>}
                  </div>
                </div>
                <span className="faint" style={{ fontSize: 12 }}>{timeAgo(p.last_opened)}</span>
                <button
                  className="icon-btn sm"
                  title="Remove from list (files are untouched)"
                  onClick={async (e) => {
                    e.stopPropagation();
                    await api.removeProject(p.id);
                    await loadProjects();
                    if (project?.id === p.id) await openProject(null);
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        </>
      )}
      <div className="section-label" style={{ paddingLeft: 2 }}>Browse</div>
      <div className="row" style={{ marginBottom: 8 }}>
        <button className="icon-btn" disabled={!dir?.parent} onClick={() => dir?.parent && void browse(dir.parent)} title="Up">
          <ArrowUp size={15} />
        </button>
        <input
          className="input mono"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void browse(path)}
          placeholder="/path/to/project"
        />
      </div>
      <div className="dir-list">
        {dir?.dirs.map((d) => (
          <div key={d.path} className="dir-row" onClick={() => void browse(d.path)} onDoubleClick={() => void open(d.path)}>
            {d.git ? <FolderGit2 size={15} style={{ color: "var(--accent-strong)" }} /> : <Folder size={15} className="muted" />}
            <span className="grow">{d.name}</span>
            {d.git && <span className="pill accent">git</span>}
          </div>
        ))}
        {dir && dir.dirs.length === 0 && <div className="dir-row muted">No sub-directories</div>}
      </div>
    </Modal>
  );
}
