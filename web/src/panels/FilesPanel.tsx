import type { JSX } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Eye, EyeOff, File, FilePlus, Folder, FolderOpen, FolderPlus, Pencil, RefreshCw, Trash2, X } from "lucide-react";
import { api, ApiError, DirEntry } from "../api";
import { useApp } from "../store";
import { basename, clsx, languageFor, throttle } from "../util";
import { CodeEditor } from "../components/LazyEditors";

interface OpenFile {
  key: string;
  root: string;
  path: string; // relative to root
  abs: string;
  content: string;
  saved: string;
  sha: string | null;
  binary: boolean;
  diskChanged: boolean;
  line?: number;
}

export function FilesPanel() {
  const { project, fileRequest, fsVersion, fsPaths, toast } = useApp();
  const roots = useMemo(() => project?.worktrees.length ? project.worktrees.map((w) => ({ path: w.path, label: w.main ? `${project.name} (${w.branch ?? "detached"})` : `⎇ ${w.branch ?? "detached"}` })) : project ? [{ path: project.path, label: project.name }] : [], [project]);
  const [root, setRoot] = useState<string>(project?.path ?? "");
  const [hidden, setHidden] = useState(false);
  const [children, setChildren] = useState<Record<string, DirEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["."]));
  const [open, setOpen] = useState<OpenFile[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const pid = project?.id ?? "";

  useEffect(() => {
    setRoot(project?.path ?? "");
    setChildren({});
    setExpanded(new Set(["."]));
    setOpen([]);
    setActive(null);
  }, [project?.id]);

  const loadDir = useCallback(
    async (dir: string) => {
      if (!pid || !root) return;
      try {
        const res = await api.files(pid, dir === "." ? "" : dir, root, hidden);
        setChildren((c) => ({ ...c, [dir]: res.entries }));
      } catch (e: any) {
        if (e instanceof ApiError && e.status === 404) setChildren((c) => ({ ...c, [dir]: [] }));
      }
    },
    [pid, root, hidden],
  );

  useEffect(() => {
    setChildren({});
    void loadDir(".");
    expanded.forEach((d) => d !== "." && void loadDir(d));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root, hidden, pid]);

  // keep the tree and open buffers in sync with the disk
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const refreshTree = useMemo(() => throttle(() => expandedRef.current.forEach((d) => void loadDir(d)), 1200), [loadDir]);
  useEffect(() => {
    if (!fsVersion) return;
    refreshTree();
    const changed = new Set(fsPaths);
    setOpen((files) => {
      files.forEach((f) => {
        if (!changed.has(f.abs)) return;
        void api.file(pid, f.path, f.root).then((d) => {
          setOpen((cur) =>
            cur.map((x) => {
              if (x.key !== f.key || d.sha === x.sha) return x;
              if (x.content === x.saved) return { ...x, content: d.content ?? "", saved: d.content ?? "", sha: d.sha, diskChanged: false };
              return { ...x, diskChanged: true };
            }),
          );
        }).catch(() => undefined);
      });
      return files;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsVersion]);

  const openFile = useCallback(
    async (rootPath: string, rel: string, line?: number) => {
      const key = `${rootPath}::${rel}`;
      const existing = open.find((f) => f.key === key);
      if (existing) {
        setActive(key);
        if (line) setOpen((cur) => cur.map((f) => (f.key === key ? { ...f, line } : f)));
        return;
      }
      try {
        const d = await api.file(pid, rel, rootPath);
        const f: OpenFile = { key, root: rootPath, path: d.path, abs: d.abs, content: d.content ?? "", saved: d.content ?? "", sha: d.sha, binary: d.binary, diskChanged: false, line };
        setOpen((cur) => [...cur, f]);
        setActive(key);
      } catch (e: any) {
        toast(e.message, "error");
      }
    },
    [open, pid, toast],
  );

  // external "open this file" requests (from chat, changes, git)
  useEffect(() => {
    if (!fileRequest || !project) return;
    let p = fileRequest.path;
    let r = fileRequest.root ?? root;
    if (p.startsWith("/")) {
      const owner = [...roots].sort((a, b) => b.path.length - a.path.length).find((x) => p === x.path || p.startsWith(x.path + "/"));
      if (owner) {
        r = owner.path;
        p = p.slice(owner.path.length + 1);
      }
    } else if (fileRequest.root && fileRequest.root !== root) {
      r = fileRequest.root;
    }
    if (r !== root && roots.some((x) => x.path === r)) setRoot(r);
    if (p) void openFile(r, p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileRequest?.nonce]);

  const cur = open.find((f) => f.key === active) ?? null;

  const save = useCallback(
    async (f: OpenFile, force = false) => {
      try {
        const res = await api.writeFile(pid, { root: f.root, path: f.path, content: f.content, base_sha: f.sha, force });
        setOpen((all) => all.map((x) => (x.key === f.key ? { ...x, saved: x.content, sha: res.sha, diskChanged: false } : x)));
        toast(`Saved ${basename(f.path)}`, "ok");
      } catch (e: any) {
        if (e instanceof ApiError && e.status === 409) setOpen((all) => all.map((x) => (x.key === f.key ? { ...x, diskChanged: true } : x)));
        else toast(e.message, "error");
      }
    },
    [pid, toast],
  );

  const reload = async (f: OpenFile) => {
    const d = await api.file(pid, f.path, f.root);
    setOpen((all) => all.map((x) => (x.key === f.key ? { ...x, content: d.content ?? "", saved: d.content ?? "", sha: d.sha, diskChanged: false } : x)));
  };

  const close = (key: string) => {
    const f = open.find((x) => x.key === key);
    if (f && f.content !== f.saved && !confirm(`Discard unsaved changes to ${basename(f.path)}?`)) return;
    const rest = open.filter((x) => x.key !== key);
    setOpen(rest);
    if (active === key) setActive(rest[rest.length - 1]?.key ?? null);
  };

  const create = async (dir: string, kind: "file" | "dir") => {
    const name = prompt(kind === "file" ? "New file name" : "New folder name");
    if (!name) return;
    const path = dir === "." ? name : `${dir}/${name}`;
    try {
      await api.createFile(pid, { root, path, kind });
      await loadDir(dir);
      setExpanded((e) => new Set(e).add(dir));
      if (kind === "file") void openFile(root, path);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const rename = async (entry: DirEntry) => {
    const dst = prompt("Rename to", entry.path);
    if (!dst || dst === entry.path) return;
    try {
      await api.renameFile(pid, { root, src: entry.path, dst });
      refreshTree();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const remove = async (entry: DirEntry) => {
    if (!confirm(`Move ${entry.path} to .atrium/trash?`)) return;
    try {
      await api.deleteFile(pid, { root, path: entry.path });
      refreshTree();
      setOpen((all) => all.filter((f) => !(f.root === root && (f.path === entry.path || f.path.startsWith(entry.path + "/")))));
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const toggleDir = (path: string) => {
    setExpanded((e) => {
      const n = new Set(e);
      if (n.has(path)) n.delete(path);
      else {
        n.add(path);
        if (!children[path]) void loadDir(path);
      }
      return n;
    });
  };

  const renderDir = (dir: string, depth: number): JSX.Element[] =>
    (children[dir] ?? []).flatMap((e) => {
      const isOpen = expanded.has(e.path);
      const row = (
        <div
          key={e.path}
          className={clsx("tree-row", cur?.root === root && cur?.path === e.path && "on")}
          style={{ paddingLeft: 6 + depth * 14 }}
          onClick={() => (e.type === "dir" ? toggleDir(e.path) : void openFile(root, e.path))}
          title={e.path}
        >
          <span className="chev">{e.type === "dir" ? isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} /> : null}</span>
          <span className="fico">{e.type === "dir" ? isOpen ? <FolderOpen size={14} /> : <Folder size={14} /> : <File size={13} />}</span>
          <span className="nm">{e.name}</span>
          <span className="hover-actions" onClick={(ev) => ev.stopPropagation()}>
            {e.type === "dir" && (
              <button className="icon-btn sm" title="New file here" onClick={() => void create(e.path, "file")}>
                <FilePlus size={12} />
              </button>
            )}
            <button className="icon-btn sm" title="Rename / move" onClick={() => void rename(e)}>
              <Pencil size={12} />
            </button>
            <button className="icon-btn sm" title="Delete (to trash)" onClick={() => void remove(e)}>
              <Trash2 size={12} />
            </button>
          </span>
          {e.git && <span className={`g ${e.git}`}>{e.git}</span>}
        </div>
      );
      return e.type === "dir" && isOpen ? [row, ...renderDir(e.path, depth + 1)] : [row];
    });

  if (!project) return null;

  return (
    <div className="panel-body">
      <div className="split stack-files">
        <div className="side">
          <div className="toolbar" style={{ padding: "6px 8px", gap: 2, flexWrap: "nowrap" }}>
            {roots.length > 1 ? (
              <select className="select" style={{ height: 28, fontSize: 12.5, flex: 1, minWidth: 0 }} value={root} onChange={(e) => setRoot(e.target.value)}>
                {roots.map((r) => (
                  <option key={r.path} value={r.path}>
                    {r.label}
                  </option>
                ))}
              </select>
            ) : (
              <span className="title grow ellipsis" style={{ paddingLeft: 4 }}>{project.name}</span>
            )}
            <button className="icon-btn sm" title="New file" onClick={() => void create(".", "file")}>
              <FilePlus size={14} />
            </button>
            <button className="icon-btn sm" title="New folder" onClick={() => void create(".", "dir")}>
              <FolderPlus size={14} />
            </button>
            <button className="icon-btn sm" title={hidden ? "Hide .git / .atrium" : "Show hidden"} onClick={() => setHidden((h) => !h)}>
              {hidden ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
            <button className="icon-btn sm" title="Refresh" onClick={refreshTree}>
              <RefreshCw size={13} />
            </button>
          </div>
          <div className="tree">{renderDir(".", 0)}</div>
        </div>
        <div className="content">
          {open.length > 0 && (
            <div className="editor-tabs">
              {open.map((f) => (
                <div key={f.key} className={clsx("editor-tab", f.key === active && "on")} onClick={() => setActive(f.key)} title={`${f.root}/${f.path}`}>
                  {basename(f.path)}
                  {f.root !== project.path && <span className="faint mono" style={{ fontSize: 10.5 }}>⎇</span>}
                  {f.content !== f.saved ? <span className="dirty" /> : null}
                  <button className="icon-btn sm" onClick={(e) => (e.stopPropagation(), close(f.key))}>
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
          {cur?.diskChanged && (
            <div className="banner">
              <span className="grow">This file changed on disk (an agent may have edited it).</span>
              <button className="btn btn-sm" onClick={() => void reload(cur)}>
                Reload from disk
              </button>
              <button className="btn btn-sm" onClick={() => void save(cur, true)}>
                Keep mine & overwrite
              </button>
            </div>
          )}
          <div className="editor-host">
            {cur ? (
              cur.binary ? (
                <div className="empty-state">Binary file — not shown.</div>
              ) : (
                <CodeEditor
                  path={cur.key}
                  value={cur.content}
                  language={languageFor(cur.path)}
                  line={cur.line}
                  onChange={(v) => setOpen((all) => all.map((x) => (x.key === cur.key ? { ...x, content: v } : x)))}
                  onSave={() => {
                    const latest = open.find((x) => x.key === cur.key);
                    if (latest) void save(latest);
                  }}
                />
              )
            ) : (
              <div className="empty-state">
                <File size={28} />
                <h3>Pick a file to edit</h3>
                <div>
                  Edits are yours — <span className="kbd">⌘S</span> saves. Agent edits to open files show up live.
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
