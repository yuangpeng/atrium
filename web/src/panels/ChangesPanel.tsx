import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCheck, CheckCircle2, Circle, ExternalLink, GitCommitHorizontal, Maximize2, Minimize2, Minus, Plus, RefreshCw, Undo2 } from "lucide-react";
import { AgentChanges, api, ChangeDetail, ChangeFile, GitStatusFile, GitSummary, ReviewSummary } from "../api";
import { useApp } from "../store";
import { agentColor, basename, clsx, displayName, languageFor, shortPath, throttle } from "../util";
import { DiffEditor } from "../components/LazyEditors";
import { DiffOptions } from "../components/DiffOptions";
import { FileNav, FileTree, useFileTree } from "../components/FileTree";
import { ReviewBadge, ReviewDrawer } from "../components/ReviewDrawer";
import { Spinner, useFullscreen } from "../components/ui";

const ST: Record<string, string> = { created: "A", modified: "M", deleted: "D", "created+deleted": "–" };

const BASELINE_LABEL: Record<string, string> = { snapshot: "git snapshot", transcript: "from transcript", head: "HEAD", unknown: "unknown" };
const BASELINE_HINT: Record<string, string> = {
  snapshot: "Atrium snapshotted the working tree when the turn started (no commits or refs are created)",
  transcript: "Reconstructed from what the agent's transcript recorded (no snapshot was available, e.g. a CLI session)",
  head: "Neither a snapshot nor the transcript had it, so the diff is against the last commit",
  unknown: "The content before this session could not be determined",
};

// tree paths: relative to the agent's cwd; files outside it keep their (home-shortened) absolute path
const changePath = (f: ChangeFile) => (f.rel.startsWith("/") ? shortPath(f.rel) : f.rel);
const gitPath = (f: GitStatusFile) => f.path;
const toReview = (f: ChangeFile) => !f.reviewed && !f.unchanged;

export function ChangesPanel() {
  const [mode, setMode] = useState<"agents" | "tree">("agents");
  const { project } = useApp();
  const ref = useRef<HTMLDivElement>(null);
  const [full, toggleFull] = useFullscreen(ref);
  if (!project) return null;
  return (
    <div ref={ref} className="panel-body fs-host">
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
        <DiffOptions />
        <button className="icon-btn" title={full ? "Exit full screen (Esc)" : "Full-screen review"} onClick={toggleFull}>
          {full ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
      </div>
      {mode === "agents" ? <AgentChangesView /> : <WorkingTreeView />}
    </div>
  );
}

// ---------------------------------------------------------------- per agent
function AgentChangesView() {
  const { project, sessions, sessionId, changesSession, fsVersion, reviewVersion, toast, openFile } = useApp();
  const pid = project!.id;
  const [agents, setAgents] = useState<AgentChanges[] | null>(null);
  const [reviews, setReviews] = useState<Record<string, ReviewSummary>>({});
  const [reveal, setReveal] = useState<{ path: string; line: number; nonce: number } | null>(null); // a review finding's line
  const [sid, setSid] = useState<string | null>(changesSession ?? sessionId);
  const [files, setFiles] = useState<ChangeFile[] | null>(null);
  const [cwd, setCwd] = useState("");
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<ChangeDetail | null>(null);
  const [edited, setEdited] = useState<string | null>(null);
  const tree = useFileTree(files, changePath);

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
  useEffect(() => void api.reviews(pid).then(setReviews).catch(() => setReviews({})), [pid, reviewVersion]);
  // nothing picked yet: start with the first agent that still has something to review
  useEffect(() => {
    if (!sid && agents?.length) setSid((agents.find((a) => a.unreviewed > 0) ?? agents[0]).session.id);
  }, [agents, sid]);
  useEffect(() => {
    setSel(null);
    setFiles(null); // not the previous agent's list: the first file would get selected from it
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
    if (!sel && tree.order.length) setSel((tree.order.find(toReview) ?? tree.order[0]).path);
  }, [tree, sel]);

  const cur = files?.find((f) => f.path === sel) ?? null;
  const row = sessions.find((s) => s.id === sid);
  const pick = (path: string) => {
    setSel(path);
    setReveal(null);
  };
  // a review finding: its file in the diff when the agent changed it, else in the editor
  const jump = useCallback(
    (file: string, line: number | null) => {
      const rel = file.replace(/^\.\//, "");
      const abs = rel.startsWith("/") ? rel : `${cwd.replace(/\/$/, "")}/${rel}`;
      const f = files?.find((x) => x.path === abs || x.rel === rel);
      if (!f) return openFile(abs, cwd || undefined, line ? { line } : undefined);
      setSel(f.path);
      setReveal(line ? { path: f.path, line, nonce: Date.now() } : null);
    },
    [files, cwd, openFile],
  );

  const review = async (fs: ChangeFile[], reviewed: boolean) => {
    // once the selected file is done, move on to the next one still to review (in tree order)
    const done = new Set(fs.map((f) => f.path));
    const i = tree.order.findIndex((f) => f.path === sel);
    const next = reviewed && sel && done.has(sel) ? [...tree.order.slice(i + 1), ...tree.order.slice(0, i)].find((f) => !done.has(f.path) && toReview(f)) : undefined;
    try {
      await Promise.all(fs.map((f) => api.reviewChange(pid, sid!, f.path, reviewed)));
    } catch (e: any) {
      toast(e.message, "error");
    }
    await loadFiles();
    void loadAgents();
    if (next) setSel(next.path);
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
            <ReviewBadge r={reviews[a.session.id]} />
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
            <FileTree
              nodes={tree.nodes}
              selected={cur ? changePath(cur) : null}
              onSelect={(f) => pick(f.path)}
              rowClass={(f) => !toReview(f) && "reviewed"}
              renderFile={(f, name) => (
                <>
                  <span className={`st ${f.status}`}>{ST[f.status]}</span>
                  <span className="nm">{name}</span>
                  {f.detected && <span className="pill" title="Changed during the agent's turn outside its edit tools (shell commands, generators…) — found by comparing git snapshots">shell</span>}
                  {f.drift && <AlertTriangle size={12} style={{ color: "var(--warn)", flex: "none" }} />}
                  <span className="num">
                    <span className="a">+{f.added}</span> <span className="r">−{f.removed}</span>
                  </span>
                  <button className="icon-btn sm" title={f.reviewed ? "Mark as not reviewed" : "Mark reviewed"} onClick={(e) => (e.stopPropagation(), void review([f], !f.reviewed))}>
                    {f.reviewed ? <CheckCircle2 size={14} style={{ color: "var(--ok)" }} /> : <Circle size={14} />}
                  </button>
                </>
              )}
              renderDir={(d) => {
                const open = d.files.filter(toReview);
                if (!open.length) return <CheckCircle2 size={13} className="done" />;
                return (
                  <>
                    <span className="acts">
                      <button className="icon-btn sm" title={`Mark all ${open.length} in ${d.name}/ reviewed`} onClick={(e) => (e.stopPropagation(), void review(open, true))}>
                        <CheckCheck size={13} />
                      </button>
                    </span>
                    <span className="cnt" title={`${open.length} to review`}>
                      {open.length}
                    </span>
                  </>
                );
              }}
            />
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
                <FileNav index={tree.order.indexOf(cur)} count={tree.order.length} onGo={(i) => pick(tree.order[i].path)} />
                <span className="grow" />
                {edited != null && (
                  <button className="btn btn-sm btn-primary" onClick={() => void saveEdit()}>
                    Save
                  </button>
                )}
                <button className="btn btn-sm" title="Open in editor" onClick={() => openFile(cur.path, cwd)} disabled={!detail.exists || detail.gone}>
                  <ExternalLink size={12} /> Edit
                </button>
                <button className="btn btn-sm btn-danger" onClick={() => void revert(cur)} title="Restore pre-agent content" disabled={detail.baseline_source === "unknown" || detail.gone}>
                  <Undo2 size={12} /> Revert
                </button>
                <button className={clsx("btn btn-sm", cur.reviewed ? "" : "btn-primary")} onClick={() => void review([cur], !cur.reviewed)}>
                  <CheckCircle2 size={12} /> {cur.reviewed ? "Reviewed" : "Mark reviewed"}
                </button>
              </div>
            </div>
            {detail.gone && (
              <div className="banner">
                <AlertTriangle size={13} /> The worktree this agent worked in has been removed: this is the file as the agent left it, read-only.
              </div>
            )}
            {cur.drift && (
              <div className="banner">
                <AlertTriangle size={13} /> The file has changed since this agent's last edit (you or another agent edited it). The diff shows everything since before the agent started.
              </div>
            )}
            <div className="editor-host">
              <DiffEditor
                id={`${sid}:${cur.path}:${detail.sha}`}
                original={detail.baseline ?? ""}
                modified={detail.current ?? ""}
                language={languageFor(cur.path)}
                editable={detail.exists && !detail.gone}
                labels={{
                  original: (
                    <>
                      <b>Before</b> the agent
                      <span className="pill" title={BASELINE_HINT[detail.baseline_source]}>
                        {BASELINE_LABEL[detail.baseline_source]}
                      </span>
                    </>
                  ),
                  modified: detail.gone ? (
                    <>
                      <b>As the agent left it</b> <span className="faint ellipsis">· its worktree was removed</span>
                    </>
                  ) : detail.exists ? (
                    <>
                      <b>Now</b> on disk <span className="faint ellipsis">· editable, ⌘S saves</span>
                    </>
                  ) : (
                    <>
                      <b>Now</b> deleted
                    </>
                  ),
                }}
                onChange={(v) => setEdited(v === (detail.current ?? "") ? null : v)}
                onSave={() => void saveEdit()}
                reveal={reveal?.path === cur.path ? reveal : undefined}
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
        {sid && row?.role === "worker" && <ReviewDrawer pid={pid} wsid={sid} cwd={cwd || row.cwd} onJump={jump} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- git working tree
function WorkingTreeView() {
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

  const files = sum?.status?.files;
  const staged = useMemo(() => (files ?? []).filter((f) => f.index !== "." && f.index !== "?"), [files]);
  const unstaged = useMemo(() => (files ?? []).filter((f) => f.worktree !== "." || f.index === "?"), [files]);
  const stagedTree = useFileTree(staged, gitPath);
  const changesTree = useFileTree(unstaged, gitPath);
  // previous / next walks both lists, staged first
  const order = useMemo(
    () => [...stagedTree.order.map((f) => ({ path: f.path, staged: true })), ...changesTree.order.map((f) => ({ path: f.path, staged: false }))],
    [stagedTree, changesTree],
  );
  const index = sel ? order.findIndex((x) => x.path === sel.path && x.staged === sel.staged) : -1;
  // a file that was just staged / unstaged stays selected on its new side
  useEffect(() => {
    if (sum && sel && index < 0) setSel(order.find((x) => x.path === sel.path) ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order]);

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

  // stage / unstage / discard one file, or every file in a folder
  const actions = (fs: GitStatusFile[], isStaged: boolean, dir?: string) => {
    const paths = fs.map((f) => f.path);
    if (isStaged)
      return (
        <button className="icon-btn sm" title={dir ? `Unstage everything in ${dir}/` : "Unstage"} onClick={(e) => (e.stopPropagation(), void act(() => api.gitUnstage(pid, paths, root)))}>
          <Minus size={13} />
        </button>
      );
    const tracked = fs.filter((f) => f.index !== "?").map((f) => f.path);
    return (
      <>
        {tracked.length > 0 && (
          <button
            className="icon-btn sm"
            title={dir ? `Discard changes in ${dir}/` : "Discard changes"}
            onClick={(e) => {
              e.stopPropagation();
              const what = dir ? `${tracked.length} file${tracked.length === 1 ? "" : "s"} in ${dir}/` : tracked[0];
              if (confirm(`Discard working-tree changes to ${what}?`)) void act(() => api.gitDiscard(pid, tracked, root));
            }}
          >
            <Undo2 size={12} />
          </button>
        )}
        <button className="icon-btn sm" title={dir ? `Stage everything in ${dir}/` : "Stage"} onClick={(e) => (e.stopPropagation(), void act(() => api.gitStage(pid, paths, root)))}>
          <Plus size={13} />
        </button>
      </>
    );
  };

  const fileTree = (tree: ReturnType<typeof useFileTree<GitStatusFile>>, isStaged: boolean) => (
    <FileTree
      nodes={tree.nodes}
      selected={sel?.staged === isStaged ? sel.path : null}
      onSelect={(f) => setSel({ path: f.path, staged: isStaged })}
      renderFile={(f, name) => {
        const code = isStaged ? f.index : f.index === "?" ? "?" : f.worktree;
        return (
          <>
            <span className={`st ${code === "A" || code === "?" ? "created" : code === "D" ? "deleted" : code === "R" ? "renamed" : "modified"}`}>{code === "?" ? "U" : code}</span>
            <span className="nm">{name}</span>
            {f.orig && (
              <span className="orig faint ellipsis" title={`renamed from ${f.orig}`}>
                ← {basename(f.orig)}
              </span>
            )}
            <span className="acts">{actions([f], isStaged)}</span>
          </>
        );
      }}
      renderDir={(d) => <span className="acts">{actions(d.files, isStaged, d.path)}</span>}
    />
  );

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
          {fileTree(stagedTree, true)}
          <div className="section-label">
            <span className="grow">Changes ({unstaged.length})</span>
            {unstaged.length > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={() => void act(() => api.gitStage(pid, unstaged.map((f) => f.path), root))}>
                Stage all
              </button>
            )}
          </div>
          {fileTree(changesTree, false)}
          {files?.length === 0 && <div className="muted" style={{ padding: "4px 12px", fontSize: 12.5 }}>Working tree clean.</div>}
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
              <FileNav index={index} count={order.length} onGo={(i) => setSel(order[i])} />
              <span className="mono ellipsis grow" style={{ fontSize: 12.5 }} title={sel.path}>
                {sel.path}
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
                labels={{
                  original: <b>HEAD</b>,
                  modified: sel.staged ? (
                    <>
                      <b>Index</b> <span className="faint">· staged</span>
                    </>
                  ) : (
                    <>
                      <b>Working tree</b> <span className="faint ellipsis">· editable, ⌘S saves</span>
                    </>
                  ),
                }}
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
