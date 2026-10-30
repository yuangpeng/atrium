import { useEffect, useState } from "react";
import { ArrowUp, Folder, FolderGit2 } from "lucide-react";
import { api } from "../api";
import { useApp } from "../store";
import { shortPath } from "../util";
import { Modal, Spinner } from "./ui";

/** Open a folder as a project, or make a new one (in the folder being browsed), git-initialised by default. */
export function ProjectPicker() {
  const { pickerOpen, addProject, setPickerOpen, toast } = useApp();
  const creating = pickerOpen === "create";
  const [path, setPath] = useState("");
  const [dir, setDir] = useState<Awaited<ReturnType<typeof api.browse>> | null>(null);
  const [name, setName] = useState("");
  const [git, setGit] = useState(true);
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

  const open = async (p: string, create?: { git: boolean }) => {
    setBusy(true);
    try {
      await addProject(p, create);
    } catch (e: any) {
      toast(e.message || String(e), "error");
    } finally {
      setBusy(false);
    }
  };
  const newPath = dir && name.trim() ? `${dir.path.replace(/\/$/, "")}/${name.trim()}` : "";

  return (
    <Modal
      title={creating ? "New folder" : "Open a folder"}
      onClose={() => setPickerOpen(false)}
      wide
      footer={
        creating ? (
          <>
            <label className="check grow">
              <input type="checkbox" checked={git} onChange={(e) => setGit(e.target.checked)} /> git init
            </label>
            <button className="btn btn-primary" disabled={!newPath || busy} onClick={() => void open(newPath, { git })}>
              {busy && <Spinner />} Create and open
            </button>
          </>
        ) : (
          <>
            <span className="muted grow" style={{ fontSize: 12.5 }}>
              Every directory is a project: its sessions, board, files and git live together.
            </span>
            <button className="btn btn-primary" disabled={!path || busy} onClick={() => void open(path)}>
              {busy && <Spinner />} Open this folder
            </button>
          </>
        )
      }
    >
      {creating && (
        <div className="field">
          <label>Name</label>
          <input
            className="input"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && newPath && !busy && void open(newPath, { git })}
            placeholder="my-project"
          />
          <div className="hint mono ellipsis">{newPath ? shortPath(newPath) : `in ${shortPath(dir?.path ?? "")}`}</div>
        </div>
      )}
      <div className="section-label" style={{ paddingLeft: 2 }}>{creating ? "In" : "Browse"}</div>
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
          <div key={d.path} className="dir-row" onClick={() => void browse(d.path)} onDoubleClick={() => !creating && void open(d.path)}>
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
