import type { JSX, MouseEvent as ReactMouseEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bot, ChevronDown, ChevronUp, ExternalLink, FolderGit2, GitBranch, GitBranchPlus, Lightbulb, Maximize2, Minimize2, RefreshCw, Sparkles, Trash2, X } from "lucide-react";
import { api, Branch, Commit, CommitDetail, CommitFile, GitSummary, GitView } from "../api";
import { useApp } from "../store";
import { clsx, displayName, languageFor, relTo, throttle, timeAgo } from "../util";
import { Spinner, useFullscreen } from "../components/ui";
import { DiffEditor } from "../components/LazyEditors";
import { DiffOptions } from "../components/DiffOptions";
import { FileNav, FileTree, useFileTree } from "../components/FileTree";
import { ChatView } from "../chat/ChatView";

const SIMPLIFY_PROMPT =
  "Look at this repository's branches and worktrees and simplify the commit graph for me: decide which branches are relevant to the current work and call git_view with them (plus highlights and a short note). Then give me a few bullets on what's going on.";

// ---------------------------------------------------------------- lane layout
interface Row {
  c: Commit;
  lane: number;
  before: (string | null)[];
  after: (string | null)[];
  parentLanes: number[];
}

function layout(commits: Commit[]): { rows: Row[]; width: number } {
  const rows: Row[] = [];
  let lanes: (string | null)[] = [];
  let width = 1;
  const known = new Set(commits.map((c) => c.sha));
  for (const c of commits) {
    let lane = lanes.indexOf(c.sha);
    if (lane === -1) {
      lane = lanes.indexOf(null);
      if (lane === -1) {
        lane = lanes.length;
        lanes.push(null);
      }
    }
    const before = [...lanes];
    lanes = lanes.map((s, i) => (s === c.sha && i !== lane ? null : s));
    const parentLanes: number[] = [];
    const parents = c.parents.filter((p) => known.has(p));
    if (parents.length === 0) lanes[lane] = null;
    parents.forEach((p, k) => {
      if (k === 0) {
        lanes[lane] = p;
        parentLanes.push(lane);
        return;
      }
      let pl = lanes.indexOf(p);
      if (pl === -1) {
        pl = lanes.indexOf(null);
        if (pl === -1) {
          pl = lanes.length;
          lanes.push(null);
        }
        lanes[pl] = p;
      }
      parentLanes.push(pl);
    });
    const after = [...lanes];
    rows.push({ c, lane, before, after, parentLanes });
    width = Math.max(width, before.length, after.length);
    while (lanes.length && lanes[lanes.length - 1] === null) lanes.pop();
  }
  return { rows, width: Math.min(width, 14) };
}

const H = 30;
const W = 14;
const LANE_COLORS = ["#D97757", "#3B6FA8", "#3F7D58", "#8B5CB8", "#A86B12", "#C2577A", "#2E8A8A", "#7A6F5A"];
const lx = (i: number) => 12 + i * W;
const curve = (x1: number, y1: number, x2: number, y2: number) => `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`;

function GraphCell({ r, width, highlighted, isHead }: { r: Row; width: number; highlighted: boolean; isHead: boolean }) {
  const cy = H / 2;
  const paths: JSX.Element[] = [];
  r.before.forEach((s, i) => {
    if (!s || i >= width) return;
    if (s === r.c.sha) paths.push(<path key={`in${i}`} d={curve(lx(i), 0, lx(r.lane), cy)} stroke={LANE_COLORS[i % 8]} />);
    else {
      const j = r.after.indexOf(s);
      paths.push(<path key={`th${i}`} d={curve(lx(i), 0, lx(j >= 0 ? j : i), H)} stroke={LANE_COLORS[i % 8]} />);
    }
  });
  r.parentLanes.forEach((pl, k) => {
    if (pl >= width) return;
    paths.push(<path key={`p${k}`} d={curve(lx(r.lane), cy, lx(pl), H)} stroke={LANE_COLORS[(k === 0 ? r.lane : pl) % 8]} />);
  });
  const color = LANE_COLORS[r.lane % 8];
  return (
    <svg width={lx(width) + 4} height={H} style={{ flex: "none" }}>
      <g fill="none" strokeWidth={2}>{paths}</g>
      {highlighted && <circle cx={lx(r.lane)} cy={cy} r={8} fill={color} opacity={0.22} />}
      <circle cx={lx(r.lane)} cy={cy} r={isHead ? 5 : 4} fill={isHead ? "var(--bg)" : color} stroke={color} strokeWidth={isHead ? 2.5 : 0} />
    </svg>
  );
}

function RefPills({ refs, branches }: { refs: string[]; branches: Map<string, Branch> }) {
  return (
    <>
      {refs.map((ref) => {
        if (ref.startsWith("HEAD -> ")) return <span key={ref} className="ref-pill head">{ref.slice(8)}</span>;
        if (ref === "HEAD") return <span key={ref} className="ref-pill head">HEAD</span>;
        if (ref.startsWith("tag: ")) return <span key={ref} className="ref-pill tag">{ref.slice(5)}</span>;
        const b = branches.get(ref);
        return <span key={ref} className={clsx("ref-pill", b?.remote ? "remote" : "local")}>{ref}</span>;
      })}
    </>
  );
}

// ---------------------------------------------------------------- panel
export function GitPanel() {
  const { project, gitVersion, toast, refreshProject } = useApp();
  const pid = project?.id ?? "";
  const [tab, setTab] = useState<"graph" | "worktrees">("graph");
  const [mode, setMode] = useState<"view" | "smart" | "all" | "custom">("view");
  const [custom, setCustom] = useState<string[]>([]);
  const [graph, setGraph] = useState<{ commits: Commit[]; head: string | null; refs: string[]; mode: string; view: GitView | null } | null>(null);
  const [sum, setSum] = useState<GitSummary | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<CommitDetail | null>(null);
  const [agentSid, setAgentSid] = useState<string | null>(null);
  const [agentOpen, setAgentOpen] = useState(false);
  const [agentBusy, setAgentBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [initBusy, setInitBusy] = useState(false);

  const load = useCallback(async () => {
    if (!pid) return;
    setLoading(true);
    try {
      const [g, s] = await Promise.all([api.gitGraph(pid, mode === "custom" ? "custom" : mode, mode === "custom" ? custom : undefined), api.gitSummary(pid)]);
      setGraph(g);
      setSum(s);
      if (s.is_repo !== useApp.getState().project?.is_git) void refreshProject(); // repo created / removed outside Atrium
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setLoading(false);
    }
  }, [pid, mode, custom, toast, refreshProject]);
  const loadThrottled = useMemo(() => throttle(() => void load(), 1500), [load]);
  const isGit = !!project?.is_git;
  useEffect(() => void load(), [load, isGit]);
  useEffect(() => {
    if (gitVersion) loadThrottled();
  }, [gitVersion, loadThrottled]);
  useEffect(() => {
    if (!sel) return setDetail(null);
    void api.gitCommit(pid, sel).then(setDetail).catch(() => setDetail(null));
  }, [pid, sel]);

  const { rows, width } = useMemo(() => layout(graph?.commits ?? []), [graph]);
  const branchMap = useMemo(() => new Map((sum?.branches ?? []).map((b) => [b.name, b])), [sum]);
  const view = graph?.view ?? null;
  const hl = useMemo(() => {
    const set = new Set<string>();
    if (mode !== "view" || !view) return set;
    for (const h of view.highlight) {
      for (const c of graph?.commits ?? []) {
        if (c.sha.startsWith(h) || c.refs.some((r) => r === h || r === `HEAD -> ${h}`)) set.add(c.sha);
      }
    }
    return set;
  }, [view, graph, mode]);

  const ensureAgent = async () => {
    if (agentSid) return agentSid;
    setAgentBusy(true);
    try {
      const { id } = await api.gitAgent(pid);
      setAgentSid(id);
      return id;
    } catch (e: any) {
      toast(e.message, "error");
      return null;
    } finally {
      setAgentBusy(false);
    }
  };
  const simplify = async () => {
    setAgentOpen(true);
    const id = await ensureAgent();
    if (!id) return;
    setMode("view");
    await api.send(pid, id, SIMPLIFY_PROMPT).catch((e) => toast(e.message, "error"));
  };
  useEffect(() => {
    if (agentOpen) void ensureAgent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentOpen]);
  useEffect(() => {
    setAgentSid(null);
    setAgentOpen(false);
    setSel(null);
    setMode("view");
  }, [pid]);

  if (!project) return null;
  if (!project.is_git)
    return (
      <div className="empty-state">
        <GitBranch size={28} />
        <h3>Not a git repository</h3>
        <div>Initialize one in {project.name} to get the graph, worktrees and commit tools.</div>
        <button
          className="btn btn-primary"
          style={{ marginTop: 10 }}
          disabled={initBusy}
          onClick={async () => {
            setInitBusy(true);
            try {
              await api.gitInit(pid);
              await refreshProject();
            } catch (e: any) {
              toast(e.message, "error");
            } finally {
              setInitBusy(false);
            }
          }}
        >
          {initBusy ? <Spinner /> : <GitBranchPlus size={14} />} Initialize repository
        </button>
      </div>
    );

  const shownRefs = graph?.refs ?? [];
  const toggleRef = (name: string) => {
    const base = mode === "custom" ? custom : shownRefs;
    setCustom(base.includes(name) ? base.filter((x) => x !== name) : [...base, name]);
    setMode("custom");
  };
  const localBranches = (sum?.branches ?? []).filter((b) => !b.remote);
  const remoteBranches = (sum?.branches ?? []).filter((b) => b.remote);

  return (
    <div className="panel-body">
      <div className="toolbar">
        <div className="seg">
          <button className={clsx(tab === "graph" && "on")} onClick={() => setTab("graph")}>
            <GitBranch size={13} /> Graph
          </button>
          <button className={clsx(tab === "worktrees" && "on")} onClick={() => setTab("worktrees")}>
            <FolderGit2 size={13} /> Worktrees {sum?.worktrees && sum.worktrees.length > 1 ? `(${sum.worktrees.length - 1})` : ""}
          </button>
        </div>
        {tab === "graph" && (
          <div className="seg">
            {(["view", "smart", "all", "custom"] as const).map((m) => (
              <button key={m} className={clsx(mode === m && "on")} onClick={() => (m === "custom" && !custom.length && setCustom(shownRefs), setMode(m))} title={m === "view" ? "The view chosen by the Git Agent (or you)" : undefined}>
                {m === "view" ? "Agent view" : m[0].toUpperCase() + m.slice(1)}
              </button>
            ))}
          </div>
        )}
        <span className="grow" />
        {loading && <Spinner />}
        <button className="btn btn-sm" onClick={() => void simplify()} title="Ask the Git Agent to pick the relevant branches">
          <Sparkles size={13} /> Simplify
        </button>
        <button className="icon-btn" onClick={() => void load()} title="Refresh">
          <RefreshCw size={14} />
        </button>
      </div>

      {tab === "graph" ? (
        <>
          {mode === "view" && view?.note && (
            <div className="note-box">
              <span className="ico">
                <Lightbulb size={14} />
              </span>
              <div className="grow">
                {view.note}
                <div className="faint" style={{ fontSize: 11.5, marginTop: 2 }}>
                  {view.source === "git-agent" ? "Git Agent" : "You"} · {timeAgo(view.updated_at)} ago
                </div>
              </div>
              <button className="icon-btn sm" title="Clear view" onClick={() => void api.gitResetView(pid).then(load)}>
                <X size={13} />
              </button>
            </div>
          )}
          <div style={{ padding: "8px 12px 6px" }} className="chips">
            {localBranches.map((b) => (
              <button key={b.name} className={clsx("chip-toggle", shownRefs.includes(b.name) && "on")} onClick={() => toggleRef(b.name)} title={b.subject}>
                {b.head && "● "}
                {b.name}
              </button>
            ))}
            {remoteBranches.slice(0, 12).map((b) => (
              <button key={b.name} className={clsx("chip-toggle", shownRefs.includes(b.name) && "on")} onClick={() => toggleRef(b.name)} style={{ opacity: 0.8 }}>
                {b.name}
              </button>
            ))}
            {mode === "custom" && (
              <button className="btn btn-sm btn-ghost" onClick={() => void api.gitSetView(pid, { branches: custom, note: "" }).then(() => setMode("view"))}>
                Save as view
              </button>
            )}
          </div>
          <div className="graph-wrap">
            {rows.map((r) => (
              <div key={r.c.sha} className={clsx("graph-row", sel === r.c.sha && "on", hl.has(r.c.sha) && "hl")} onClick={() => setSel(sel === r.c.sha ? null : r.c.sha)}>
                <GraphCell r={r} width={width} highlighted={hl.has(r.c.sha)} isHead={graph?.head === r.c.sha} />
                <div className="subj">
                  <RefPills refs={r.c.refs} branches={branchMap} />
                  <span className="ellipsis">{r.c.subject}</span>
                </div>
                <span className="meta">
                  {r.c.author} · {timeAgo(r.c.time)}
                </span>
                <span className="meta mono">{r.c.sha.slice(0, 7)}</span>
              </div>
            ))}
            {graph && rows.length === 0 && (
              <div className="empty-state">
                <h3>No commits yet</h3>
                <div>Stage and commit from Changes → Working tree, or ask an agent to.</div>
              </div>
            )}
          </div>
          {detail && <CommitView key={detail.sha} detail={detail} onClose={() => setSel(null)} />}
        </>
      ) : (
        <Worktrees summary={sum} onChanged={() => (void load(), void refreshProject())} />
      )}

      <div className="drawer" style={agentOpen ? { height: "46%" } : undefined}>
        <div className="drawer-head" onClick={() => setAgentOpen((o) => !o)}>
          <Bot size={14} className="muted" />
          <span className="grow">Git Agent</span>
          {agentBusy && <Spinner />}
          <span className="faint" style={{ fontWeight: 400, fontSize: 12 }}>
            Ask about history, branches, merges — it can drive the graph
          </span>
          {agentOpen ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
        </div>
        {agentOpen && agentSid && (
          <ChatView
            pid={pid}
            sid={agentSid}
            cwd={project.path}
            compact
            placeholder="e.g. Which branches are merged into main? Show me only what the agents are working on."
            emptyHint={
              <div className="muted" style={{ fontSize: 13, textAlign: "center", padding: 10 }}>
                The Git Agent investigates with read-only git commands and controls the graph above. Mutating commands still ask for your approval.
              </div>
            }
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- commit
const COMMIT_ST: Record<string, string> = { A: "created", D: "deleted", R: "renamed", C: "renamed" }; // M, T: modified
const commitPath = (f: CommitFile) => f.path;
const HEIGHT_KEY = "atrium.commitHeight";

/** The selected commit under the graph: its files as a tree, one file's diff side by side. */
function CommitView({ detail, onClose }: { detail: CommitDetail; onClose: () => void }) {
  const { project, toast, openFile } = useApp();
  const pid = project!.id;
  const ref = useRef<HTMLDivElement>(null);
  const [full, toggleFull] = useFullscreen(ref);
  const [height, setHeight] = useState(() => Number(localStorage.getItem(HEIGHT_KEY)) || 0.55); // share of the panel
  useEffect(() => localStorage.setItem(HEIGHT_KEY, String(height)), [height]);
  const tree = useFileTree(detail.files, commitPath);
  const [sel, setSel] = useState<string | null>(null);
  const cur = detail.files.find((f) => f.path === sel) ?? tree.order[0] ?? null;
  const [data, setData] = useState<{ original: string; modified: string } | null>(null);
  useEffect(() => {
    setData(null);
    if (!cur || cur.binary) return;
    let live = true;
    api.gitCommitFile(pid, detail.sha, cur.path, cur.orig).then(
      (d) => live && setData(d),
      (e) => toast(e.message, "error"),
    );
    return () => {
      live = false;
    };
  }, [pid, detail.sha, cur, toast]);

  const drag = (e: ReactMouseEvent) => {
    e.preventDefault();
    const el = ref.current;
    const box = el?.parentElement?.getBoundingClientRect();
    if (!el || !box) return;
    const bottom = el.getBoundingClientRect().bottom;
    const move = (ev: MouseEvent) => setHeight(Math.min(0.85, Math.max(0.2, (bottom - ev.clientY) / box.height)));
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const [subject, ...rest] = detail.message.split("\n");
  const body = rest.join("\n").trim();
  const added = detail.files.reduce((n, f) => n + f.added, 0);
  const removed = detail.files.reduce((n, f) => n + f.removed, 0);
  const parent = detail.parents[0] ?? null;
  return (
    <div ref={ref} className="commit-detail fs-host" style={{ height: `${height * 100}%` }}>
      <div className="hresizer" onMouseDown={drag} />
      <div className="hd">
        <div className="row">
          <b className="grow ellipsis" title={subject}>
            {subject}
          </b>
          <DiffOptions />
          <button className="icon-btn" title={full ? "Exit full screen (Esc)" : "Full screen"} onClick={toggleFull}>
            {full ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
          </button>
          <button className="icon-btn" title="Close" onClick={onClose}>
            <X size={15} />
          </button>
        </div>
        <div className="muted" style={{ fontSize: 12 }}>
          <span className="mono">{detail.sha.slice(0, 10)}</span> · {detail.author} · {new Date(detail.time * 1000).toLocaleString()} · {detail.files.length} file
          {detail.files.length === 1 ? "" : "s"} <span style={{ color: "var(--ok)" }}>+{added}</span> <span style={{ color: "var(--err)" }}>−{removed}</span>
          {detail.parents.length > 1 && <span className="faint"> · merge: compared with its first parent</span>}
        </div>
        {body && <pre className="code-block wrap msg-body">{body}</pre>}
      </div>
      <div className="split">
        <div className="side">
          {detail.files.length === 0 && <div className="muted" style={{ padding: "8px 12px", fontSize: 12.5 }}>No file changes.</div>}
          <FileTree
            nodes={tree.nodes}
            selected={cur?.path ?? null}
            onSelect={(f) => setSel(f.path)}
            renderFile={(f, name) => (
              <>
                <span className={`st ${COMMIT_ST[f.status] ?? "modified"}`}>{f.status}</span>
                <span className="nm">{name}</span>
                <span className="num">
                  {f.binary ? (
                    "bin"
                  ) : (
                    <>
                      <span className="a">+{f.added}</span> <span className="r">−{f.removed}</span>
                    </>
                  )}
                </span>
              </>
            )}
          />
        </div>
        <div className="content">
          {cur && (
            <>
              <div className="file-head">
                <FileNav index={tree.order.indexOf(cur)} count={tree.order.length} onGo={(i) => setSel(tree.order[i].path)} />
                <span className="mono ellipsis grow" title={cur.orig ? `${cur.orig} → ${cur.path}` : cur.path}>
                  {cur.orig ? `${cur.orig} → ${cur.path}` : cur.path}
                </span>
                <button className="btn btn-sm" title="Open the file as it is now" disabled={cur.status === "D"} onClick={() => openFile(cur.path, project!.path)}>
                  <ExternalLink size={12} /> Open
                </button>
              </div>
              <div className="editor-host">
                {cur.binary ? (
                  <div className="empty-state">Binary file — not shown.</div>
                ) : data ? (
                  <DiffEditor
                    id={`${detail.sha}:${cur.path}`}
                    original={data.original}
                    modified={data.modified}
                    language={languageFor(cur.path)}
                    labels={{
                      original: (
                        <>
                          <b>Before</b>
                          {parent ? <span className="mono">{parent.slice(0, 7)}</span> : <span className="faint">first commit</span>}
                          {cur.status === "A" && <span className="faint">· new file</span>}
                          {cur.orig && <span className="faint ellipsis">· {cur.orig}</span>}
                        </>
                      ),
                      modified: (
                        <>
                          <b>After</b> <span className="mono">{detail.sha.slice(0, 7)}</span>
                          {cur.status === "D" && <span className="faint">· deleted</span>}
                        </>
                      ),
                    }}
                  />
                ) : (
                  <div className="empty-state">
                    <Spinner />
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- worktrees
function Worktrees({ summary, onChanged }: { summary: GitSummary | null; onChanged: () => void }) {
  const { project, toast, sessions, selectSession, openFile } = useApp();
  const pid = project!.id;
  const [branch, setBranch] = useState("");
  const [base, setBase] = useState("");
  const [existing, setExisting] = useState(false);
  const [busy, setBusy] = useState(false);
  const wts = summary?.worktrees ?? [];

  const add = async () => {
    if (!branch.trim()) return;
    setBusy(true);
    try {
      const r = await api.gitAddWorktree(pid, { branch: branch.trim(), base: base.trim() || undefined, create: !existing });
      toast(`Worktree created at ${relTo(project!.path, r.path)}`, "ok");
      setBranch("");
      setBase("");
      onChanged();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };
  const remove = async (path: string) => {
    const agents = sessions.filter((s) => (s.cwd === path || s.cwd.startsWith(path + "/")) && s.role === "worker");
    const kept = agents.length ? `\n\nIts agents (${agents.map(displayName).join(", ")}) stay in the list: their next message asks where they continue.` : "";
    if (!confirm(`Remove worktree ${relTo(project!.path, path)}? (the branch is kept)${kept}`)) return;
    try {
      await api.gitRemoveWorktree(pid, path);
      onChanged();
    } catch (e: any) {
      if (e.status === 409) return toast(e.message, "error"); // an agent is working there
      if (confirm(`${e.message}\n\nForce removal (discards uncommitted changes in the worktree)?`)) {
        await api.gitRemoveWorktree(pid, path, true).catch((er) => toast(er.message, "error"));
        onChanged();
      }
    }
  };

  return (
    <div style={{ flex: 1, overflowY: "auto" }}>
      {wts.map((w) => {
        const agents = sessions.filter((s) => s.cwd === w.path && s.role === "worker");
        return (
          <div key={w.path} className="wt-row">
            <FolderGit2 size={16} className={w.main ? "muted" : ""} style={w.main ? undefined : { color: "var(--accent-strong)" }} />
            <div className="grow">
              <div className="row">
                <b className="mono" style={{ fontSize: 12.5 }}>{w.branch ?? "(detached)"}</b>
                {w.main && <span className="pill">main checkout</span>}
                {w.locked && <span className="pill warn">locked</span>}
                {w.prunable && <span className="pill err">prunable</span>}
              </div>
              <div className="muted mono ellipsis" style={{ fontSize: 11.5 }}>{w.main ? w.path : relTo(project!.path, w.path)}</div>
              {agents.length > 0 && (
                <div className="chips" style={{ marginTop: 4 }}>
                  {agents.map((a) => (
                    <button key={a.id} className="chip-toggle on" onClick={() => selectSession(a.id)} style={{ fontFamily: "var(--font-ui)" }}>
                      {a.name || a.title}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button className="btn btn-sm btn-ghost" onClick={() => openFile("", w.path)} title="Browse files">
              Files
            </button>
            {!w.main && (
              <button className="icon-btn sm" title="Remove worktree" onClick={() => void remove(w.path)}>
                <Trash2 size={13} />
              </button>
            )}
          </div>
        );
      })}
      <div style={{ padding: 12 }}>
        <div className="section-label" style={{ padding: "4px 0 8px" }}>
          <GitBranchPlus size={12} /> New worktree
        </div>
        <div className="row" style={{ marginBottom: 8 }}>
          <input className="input" placeholder={existing ? "existing branch" : "new branch name"} value={branch} onChange={(e) => setBranch(e.target.value)} />
          {!existing && <input className="input" style={{ width: 150 }} placeholder="base (HEAD)" value={base} onChange={(e) => setBase(e.target.value)} />}
        </div>
        <div className="row">
          <label className="row muted" style={{ fontSize: 12.5, gap: 5 }}>
            <input type="checkbox" checked={existing} onChange={(e) => setExisting(e.target.checked)} /> Check out an existing branch
          </label>
          <span className="grow" />
          <button className="btn btn-primary btn-sm" disabled={busy || !branch.trim()} onClick={() => void add()}>
            {busy && <Spinner />} Create
          </button>
        </div>
        <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>
          Created under <span className="mono">.atrium/worktrees/</span>. Start an agent in a worktree from the “New agent” page.
        </div>
      </div>
    </div>
  );
}
