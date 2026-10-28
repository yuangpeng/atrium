import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Circle, Columns2, ExternalLink, GitCommitHorizontal, Minus, Plus, RefreshCw, Rows2, Undo2 } from "lucide-react";
import { AgentChanges, api, ChangeDetail, ChangeFile, GitStatusFile, GitSummary } from "../api";
import { useApp } from "../store";
import { agentColor, basename, clsx, displayName, languageFor, throttle } from "../util";
import { DiffEditor } from "../components/LazyEditors";
import { Spinner } from "../components/ui";

const ST: Record<string, string> = { created: "A", modified: "M", deleted: "D", "created+deleted": "–" };

const BASELINE_LABEL: Record<string, string> = { snapshot: "git snapshot", transcript: "from transcript", head: "HEAD", unknown: "unknown" };
const BASELINE_HINT: Record<string, string> = {
  snapshot: "Atrium snapshotted the working tree when the turn started (no commits or refs are created)",
  transcript: "Reconstructed from what the agent's transcript recorded (no snapshot was available, e.g. a CLI session)",
  head: "Neither a snapshot nor the transcript had it, so the diff is against the last commit",
  unknown: "The content before this session could not be determined",
};

export function ChangesPanel() {
  const [mode, setMode] = useState<"agents" | "tree">("agents");
  const [sideBySide, setSideBySide] = useState(true);
  const { project } = useApp();
  if (!project) return null;
  return (
    <div className="panel-body">
      <div className="toolbar">
        <div className="seg">
          <button className={clsx(mode === "agents" && "on")} onClick={() => setMode("agents")}>
            By agent
          </button>
          {project.is_git && (
            <button className={clsx(mode === "tree" && "on")} onClick={() => setMode("tree")}>
              Working tree
            </button>
          )}
        </div>
        <span className="grow" />
        <button className="icon-btn" title={sideBySide ? "Inline diff" : "Side-by-side diff"} onClick={() => setSideBySide((v) => !v)}>
          {sideBySide ? <Rows2 size={15} /> : <Columns2 size={15} />}
        </button>
      </div>
      {mode === "agents" ? <AgentChangesView sideBySide={sideBySide} /> : <WorkingTreeView sideBySide={sideBySide} />}
    </div>
  );
}

// ---------------------------------------------------------------- per agent
function AgentChangesView({ sideBySide }: { sideBySide: boolean }) {
  const { project, sessions, sessionId, changesSession, fsVersion, toast, openFile } = useApp();
  const pid = project!.id;
  const [agents, setAgents] = useState<AgentChanges[] | null>(null);
  const [sid, setSid] = useState<string | null>(changesSession ?? sessionId);
  const [files, setFiles] = useState<ChangeFile[] | null>(null);
  const [cwd, setCwd] = useState("");
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<ChangeDetail | null>(null);
  const [edited, setEdited] = useState<string | null>(null);

  useEffect(() => {
    if (changesSession) setSid(changesSession);
  }, [changesSession]);
  useEffect(() => {
    if (!changesSession && sessionId) setSid(sessionId);
  }, [sessionId, changesSession]);

  const loadAgents = useCallback(() => api.projectChanges(pid).then(setAgents).catch(() => setAgents([])), [pid]);
  const loadFiles = useCallback(async () => {
    if (!sid) return setFiles(null);
    try {
      const r = await api.changes(pid, sid);
      setFiles(r.files);
      setCwd(r.cwd);
    } catch {
      setFiles([]);
    }
  }, [pid, sid]);
  const loadDetail = useCallback(async () => {
    if (!sid || !sel) return setDetail(null);
    try {
      setDetail(await api.changeFile(pid, sid, sel));
      setEdited(null);
    } catch {
      setDetail(null);
    }
  }, [pid, sid, sel]);

  useEffect(() => void loadAgents(), [loadAgents]);
  useEffect(() => {
    setSel(null);
    void loadFiles();
  }, [loadFiles]);
  useEffect(() => void loadDetail(), [loadDetail]);
  const refreshAll = useMemo(
    () =>
      throttle(() => {
        void loadAgents();
        void loadFiles();
      }, 2000),
    [loadAgents, loadFiles],
  );
  useEffect(() => {
    if (fsVersion) refreshAll();
  }, [fsVersion, refreshAll]);
  useEffect(() => {
    if (files && !sel && files.length) setSel(files.find((f) => !f.reviewed && !f.unchanged)?.path ?? files[0].path);
  }, [files, sel]);

  const cur = files?.find((f) => f.path === sel) ?? null;
  const row = sessions.find((s) => s.id === sid);

  const review = async (f: ChangeFile, reviewed: boolean) => {
    await api.reviewChange(pid, sid!, f.path, reviewed);
    await loadFiles();
    void loadAgents();
    if (reviewed && files) {
      const next = files.find((x) => x.path !== f.path && !x.reviewed && !x.unchanged);
      if (next) setSel(next.path);
    }
  };
  const revert = async (f: ChangeFile) => {
    const what = f.status === "created" ? "delete this new file (moved to .atrium/trash)" : "restore the file to how it was before this agent touched it";
    if (!confirm(`Revert ${f.rel}?\nThis will ${what}. Your later edits to it are lost.`)) return;
    try {
      await api.revertChange(pid, sid!, f.path);
      toast(`Reverted ${f.rel}`, "ok");
      await loadFiles();
      await loadDetail();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const saveEdit = async () => {
    if (edited == null || !detail) return;
    try {
      await api.writeFile(pid, { path: detail.path, content: edited, base_sha: detail.sha });
      toast(`Saved ${basename(detail.path)}`, "ok");
      await loadDetail();
      void loadFiles();
    } catch (e: any) {
      toast(e.status === 409 ? "File changed on disk — reloaded" : e.message, "error");
      void loadDetail();
    }
  };

  return (
    <div className="split stack">
      <div className="side wide">
        <div className="section-label">Agents with changes</div>
        {agents === null && (
          <div style={{ padding: 12 }}>
            <Spinner />
          </div>
        )}
        {agents?.length === 0 && <div className="muted" style={{ padding: "4px 12px", fontSize: 12.5 }}>No agent has edited files yet.</div>}
        {agents?.map((a) => (
          <div key={a.session.id} className={clsx("agent-pick", a.session.id === sid && "on")} onClick={() => setSid(a.session.id)}>
            <span className="dot" style={{ background: agentColor(a.session.id) }} />
            <span className="grow ellipsis">{displayName(a.session)}</span>
            <span className="mono faint" style={{ fontSize: 11 }}>
              <span style={{ color: "var(--ok)" }}>+{a.added}</span> <span style={{ color: "var(--err)" }}>−{a.removed}</span>
            </span>
            {a.unreviewed > 0 ? <span className="pill warn">{a.unreviewed}</span> : <CheckCircle2 size={14} style={{ color: "var(--ok)" }} />}
          </div>
        ))}
        {sid && (
          <>
            <div className="section-label">
              <span className="grow ellipsis">{row ? displayName(row) : "Files"}</span>
              <button className="icon-btn sm" title="Refresh" onClick={() => void loadFiles()}>
                <RefreshCw size={12} />
              </button>
            </div>
            {files === null && (
              <div style={{ padding: 12 }}>
                <Spinner />
              </div>
            )}
            {files?.length === 0 && <div className="muted" style={{ padding: "4px 12px", fontSize: 12.5 }}>This agent hasn't changed any files.</div>}
            {files?.map((f) => (
              <div key={f.path} className={clsx("change-row", f.path === sel && "on", (f.reviewed || f.unchanged) && "reviewed")} onClick={() => setSel(f.path)} title={f.path}>
                <span className={`st ${f.status}`}>{ST[f.status]}</span>
                <span className="p">{f.rel}</span>
                {f.detected && <span className="pill" title="Changed during the agent's turn outside its edit tools (shell commands, generators…) — found by comparing git snapshots">shell</span>}
                {f.drift && <AlertTriangle size={12} style={{ color: "var(--warn)", flex: "none" }} />}
                <span className="num">
                  <span className="a">+{f.added}</span> <span className="r">−{f.removed}</span>
                </span>
                <button className="icon-btn sm" title={f.reviewed ? "Mark as not reviewed" : "Mark reviewed"} onClick={(e) => (e.stopPropagation(), void review(f, !f.reviewed))}>
                  {f.reviewed ? <CheckCircle2 size={14} style={{ color: "var(--ok)" }} /> : <Circle size={14} />}
                </button>
              </div>
            ))}
          </>
        )}
      </div>
      <div className="content">
        {cur && detail ? (
          <>
            <div className="diff-head">
              <div className="r1">
                <span className={`pill ${cur.status === "created" ? "ok" : cur.status === "deleted" ? "err" : "warn"}`}>{cur.status}</span>
                <span className="mono ellipsis grow" style={{ fontSize: 12.5 }} title={cur.path}>
                  {cur.rel}
                </span>
                <span className="faint" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                  {cur.detected && !cur.edits ? "changed by shell / tools" : `${cur.edits} edit${cur.edits === 1 ? "" : "s"} · ${cur.tools.join(", ")}`}
                </span>
              </div>
              <div className="r2">
              {edited != null && (
                <button className="btn btn-sm btn-primary" onClick={() => void saveEdit()}>
                  Save
                </button>
              )}
              <button className="btn btn-sm" title="Open in editor" onClick={() => openFile(cur.path, cwd)} disabled={!detail.exists}>
                <ExternalLink size={12} /> Edit
              </button>
              <button className="btn btn-sm btn-danger" onClick={() => void revert(cur)} title="Restore pre-agent content" disabled={detail.baseline_source === "unknown"}>
                <Undo2 size={12} /> Revert
              </button>
              <button className={clsx("btn btn-sm", cur.reviewed ? "" : "btn-primary")} onClick={() => void review(cur, !cur.reviewed)}>
                <CheckCircle2 size={12} /> {cur.reviewed ? "Reviewed" : "Mark reviewed"}
              </button>
              </div>
            </div>
            {cur.drift && (
              <div className="banner">
                <AlertTriangle size={13} /> The file has changed since this agent's last edit (you or another agent edited it). The diff shows everything since before the agent started.
              </div>
            )}
            <div className="banner info" style={{ fontSize: 12 }}>
              Left: before {row ? displayName(row) : "the agent"} touched it <span className="pill" title={BASELINE_HINT[detail.baseline_source]}>{BASELINE_LABEL[detail.baseline_source]}</span> · Right: on disk now (editable, ⌘S to save)
            </div>
            <div className="editor-host">
              <DiffEditor
                id={`${sid}:${cur.path}:${detail.sha}`}
                original={detail.baseline ?? ""}
                modified={detail.current ?? ""}
                language={languageFor(cur.path)}
                editable={detail.exists}
                sideBySide={sideBySide}
                onChange={(v) => setEdited(v === (detail.current ?? "") ? null : v)}
                onSave={() => void saveEdit()}
              />
            </div>
          </>
        ) : (
          <div className="empty-state">
            <GitCommitHorizontal size={28} />
            <h3>Review what each agent changed</h3>
            <div>Pick an agent and a file. Baselines come from git snapshots taken at every turn, falling back to the agent's transcript — so CLI sessions work too.</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- git working tree
function WorkingTreeView({ sideBySide }: { sideBySide: boolean }) {
  const { project, gitVersion, toast, openFile } = useApp();
  const pid = project!.id;
  const [root, setRoot] = useState(project!.path);
  const [sum, setSum] = useState<GitSummary | null>(null);
  const [sel, setSel] = useState<{ path: string; staged: boolean } | null>(null);
  const [diff, setDiff] = useState<{ original: string; current: string; editable: boolean } | null>(null);
  const [edited, setEdited] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const [amend, setAmend] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api.gitSummary(pid, root).then(setSum).catch((e) => toast(e.message, "error")), [pid, root, toast]);
  const loadThrottled = useMemo(() => throttle(() => void load(), 1500), [load]);
  useEffect(() => void load(), [load]);
  useEffect(() => {
    if (gitVersion) loadThrottled();
  }, [gitVersion, loadThrottled]);
  const loadDiff = useCallback(async () => {
    if (!sel) return setDiff(null);
    try {
      setDiff(await api.gitDiff(pid, sel.path, root, sel.staged));
      setEdited(null);
    } catch (e: any) {
      toast(e.message, "error");
    }
  }, [pid, root, sel, toast]);
  useEffect(() => void loadDiff(), [loadDiff, gitVersion]);

  const files = sum?.status?.files ?? [];
  const staged = files.filter((f) => f.index !== "." && f.index !== "?");
  const unstaged = files.filter((f) => f.worktree !== "." || f.index === "?");

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const fileRow = (f: GitStatusFile, isStaged: boolean) => {
    const code = isStaged ? f.index : f.index === "?" ? "?" : f.worktree;
    return (
      <div key={(isStaged ? "s:" : "u:") + f.path} className={clsx("change-row", sel?.path === f.path && sel.staged === isStaged && "on")} onClick={() => setSel({ path: f.path, staged: isStaged })}>
        <span className={`st ${code === "A" || code === "?" ? "created" : code === "D" ? "deleted" : "modified"}`}>{code === "?" ? "U" : code}</span>
        <span className="p">{f.path}</span>
        {isStaged ? (
          <button className="icon-btn sm" title="Unstage" onClick={(e) => (e.stopPropagation(), void act(() => api.gitUnstage(pid, [f.path], root)))}>
            <Minus size={13} />
          </button>
        ) : (
          <>
            {f.index !== "?" && (
              <button
                className="icon-btn sm"
                title="Discard changes"
                onClick={(e) => {
                  e.stopPropagation();
                  if (confirm(`Discard working-tree changes to ${f.path}?`)) void act(() => api.gitDiscard(pid, [f.path], root));
                }}
              >
                <Undo2 size={12} />
              </button>
            )}
            <button className="icon-btn sm" title="Stage" onClick={(e) => (e.stopPropagation(), void act(() => api.gitStage(pid, [f.path], root)))}>
              <Plus size={13} />
            </button>
          </>
        )}
      </div>
    );
  };

  const commit = () =>
    act(async () => {
      await api.gitCommitCreate(pid, msg, root, amend);
      setMsg("");
      setAmend(false);
      toast("Committed", "ok");
    });

  const saveEdit = async () => {
    if (edited == null || !sel) return;
    try {
      await api.writeFile(pid, { root, path: sel.path, content: edited });
      toast(`Saved ${basename(sel.path)}`, "ok");
      await loadDiff();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  return (
    <div className="split stack">
      <div className="side wide">
        {project!.worktrees.length > 1 && (
          <div style={{ padding: "8px 8px 0" }}>
            <select className="select" style={{ height: 30, fontSize: 12.5 }} value={root} onChange={(e) => (setRoot(e.target.value), setSel(null))}>
              {project!.worktrees.map((w) => (
                <option key={w.path} value={w.path}>
                  {w.main ? `${project!.name} (${w.branch})` : `⎇ ${w.branch ?? w.path}`}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="section-label">
          <span className="grow">
            {sum?.status?.branch ?? ""} {sum?.status && (sum.status.ahead || sum.status.behind) ? `↑${sum.status.ahead} ↓${sum.status.behind}` : ""}
          </span>
          <button className="icon-btn sm" onClick={() => void load()} title="Refresh">
            <RefreshCw size={12} />
          </button>
        </div>
        <div style={{ flex: 1, overflowY: "auto" }}>
          <div className="section-label">
            <span className="grow">Staged ({staged.length})</span>
            {staged.length > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={() => void act(() => api.gitUnstage(pid, staged.map((f) => f.path), root))}>
                Unstage all
              </button>
            )}
          </div>
          {staged.map((f) => fileRow(f, true))}
          <div className="section-label">
            <span className="grow">Changes ({unstaged.length})</span>
            {unstaged.length > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={() => void act(() => api.gitStage(pid, unstaged.map((f) => f.path), root))}>
                Stage all
              </button>
            )}
          </div>
          {unstaged.map((f) => fileRow(f, false))}
          {files.length === 0 && <div className="muted" style={{ padding: "4px 12px", fontSize: 12.5 }}>Working tree clean.</div>}
        </div>
        <div className="commit-box">
          <textarea className="textarea" rows={3} placeholder="Commit message" value={msg} onChange={(e) => setMsg(e.target.value)} onKeyDown={(e) => e.key === "Enter" && (e.metaKey || e.ctrlKey) && msg.trim() && void commit()} />
          <div className="row">
            <label className="row muted" style={{ fontSize: 12.5, gap: 5 }}>
              <input type="checkbox" checked={amend} onChange={(e) => setAmend(e.target.checked)} /> Amend
            </label>
            <span className="grow" />
            <button className="btn btn-primary btn-sm" disabled={busy || !msg.trim() || (!staged.length && !amend)} onClick={() => void commit()}>
              <GitCommitHorizontal size={13} /> Commit
            </button>
          </div>
        </div>
      </div>
      <div className="content">
        {sel && diff ? (
          <>
            <div className="toolbar">
              <span className="mono ellipsis grow" style={{ fontSize: 12.5 }}>
                {sel.path} <span className="faint">{sel.staged ? "· HEAD → index" : "· HEAD → working tree"}</span>
              </span>
              {edited != null && (
                <button className="btn btn-sm btn-primary" onClick={() => void saveEdit()}>
                  Save
                </button>
              )}
              <button className="btn btn-sm" onClick={() => openFile(sel.path, root)}>
                <ExternalLink size={12} /> Edit
              </button>
            </div>
            <div className="editor-host">
              <DiffEditor
                id={`${root}:${sel.path}:${sel.staged}`}
                original={diff.original}
                modified={diff.current}
                language={languageFor(sel.path)}
                editable={diff.editable}
                sideBySide={sideBySide}
                onChange={(v) => setEdited(v === diff.current ? null : v)}
                onSave={() => void saveEdit()}
              />
            </div>
          </>
        ) : (
          <div className="empty-state">
            <GitCommitHorizontal size={28} />
            <h3>Stage, review, commit</h3>
            <div>Select a file to see its diff against HEAD.</div>
          </div>
        )}
      </div>
    </div>
  );
}
