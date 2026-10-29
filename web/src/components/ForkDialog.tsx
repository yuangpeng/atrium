// Fork a conversation: a new agent that starts with this one's history — all of it, or up to a
// message — working in a new worktree (with the files of that moment), an existing one, or the same directory.
import type { JSX, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { Folder, FolderGit2, GitBranchPlus, GitFork } from "lucide-react";
import { api, ForkWorkspace, SessionRow } from "../api";
import { ForkRequest, useApp } from "../store";
import { clsx, displayName, relTo } from "../util";
import { Modal, Spinner } from "./ui";

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "agent";
}

function excerpt(s: string, n: number): string {
  s = s.replace(/\s+/g, " ").trim();
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}

export function ForkDialog() {
  const req = useApp((s) => s.forkRequest);
  return req ? <ForkForm key={`${req.sid}:${req.at?.user_index ?? "latest"}`} req={req} /> : null;
}

function ForkForm({ req }: { req: ForkRequest }) {
  const { project, sessions, setForkRequest, refreshSessions, refreshProject, selectSession, toast } = useApp();
  const listed = sessions.find((s) => s.id === req.sid);
  const [fetched, setFetched] = useState<SessionRow | null>(null);
  useEffect(() => {
    // archived sessions are not in the list
    if (!listed && project) void api.sessions(project.id, true).then((rows) => setFetched(rows.find((r) => r.id === req.sid) ?? null), () => {});
  }, [listed, project, req.sid]);
  const src = listed ?? fetched;
  const srcName = src ? displayName(src) : "Agent";
  const isGit = !!project?.is_git;
  const worktrees = useMemo(() => (project?.worktrees ?? []).filter((w) => !w.bare), [project]);
  // the source's checkout: the deepest worktree holding its cwd (Atrium's worktrees live inside the project)
  const srcRoot = useMemo(() => {
    const cwd = src?.cwd ?? "";
    return [...worktrees].filter((w) => cwd === w.path || cwd.startsWith(w.path + "/")).sort((a, b) => b.path.length - a.path.length)[0]?.path;
  }, [worktrees, src?.cwd]);
  const others = worktrees.filter((w) => w.path !== srcRoot);

  const [branches, setBranches] = useState<Set<string>>(new Set());
  const [kind, setKind] = useState<ForkWorkspace["kind"]>(isGit ? "new" : "same");
  const [branchInput, setBranchInput] = useState<string | null>(null); // null: use the suggestion
  const [wt, setWt] = useState(others[0]?.path ?? "");
  const [carry, setCarry] = useState(true);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState(req.text ?? "");
  const [archive, setArchive] = useState(false);
  const [busy, setBusy] = useState(false);

  // suggest a branch name nobody uses yet
  useEffect(() => {
    if (project && isGit) void api.gitSummary(project.id).then((g) => setBranches(new Set((g.branches ?? []).map((b) => b.name))), () => {});
  }, [project, isGit]);
  const suggested = useMemo(() => {
    const taken = new Set([...branches, ...worktrees.map((w) => w.branch ?? "")]);
    const base = `fork/${slug(srcName)}`;
    let n = 1;
    while (taken.has(`${base}-${n}`)) n++;
    return `${base}-${n}`;
  }, [branches, worktrees, srcName]);
  const branch = branchInput ?? suggested;

  if (!project) return null;
  const close = () => !busy && setForkRequest(null);

  const submit = async () => {
    if (busy) return;
    if (kind === "new" && !branch.trim()) return toast("Give the new worktree a branch name", "error");
    if (kind === "worktree" && !wt) return toast("Pick a worktree", "error");
    const workspace: ForkWorkspace = kind === "new" ? { kind, branch: branch.trim() } : kind === "worktree" ? { kind, path: wt } : { kind };
    setBusy(true);
    try {
      const res = await api.forkSession(project.id, req.sid, {
        at: req.at, workspace, carry, name: name.trim(), prompt: prompt.trim(), archive_source: archive,
      });
      setForkRequest(null);
      if (res.worktree) void refreshProject();
      await refreshSessions();
      selectSession(res.id);
      if (res.error) toast(`Forked, but the first message failed: ${res.error}`, "error");
      else if (res.files === "current") toast("Forked. Atrium had no snapshot of that moment, so the worktree got the current files.", "info");
      else toast(res.worktree ? `Forked into ⎇ ${res.worktree.branch}` : "Forked", "ok");
    } catch (e: any) {
      toast(e.message || String(e), "error");
      setBusy(false);
    }
  };

  const option = (k: ForkWorkspace["kind"], icon: JSX.Element, title: string, desc: ReactNode, extra?: ReactNode) => (
    <div className={clsx("fork-opt", kind === k && "on")} onClick={() => setKind(k)}>
      <input type="radio" name="fork-ws" checked={kind === k} onChange={() => setKind(k)} />
      <div className="grow">
        <div className="t">
          {icon} {title}
        </div>
        <div className="d">{desc}</div>
        {kind === k && extra}
      </div>
    </div>
  );
  const onBranch = src?.branch ? <span className="mono">⎇ {src.branch}</span> : null;

  return (
    <Modal
      title={
        <span className="row" style={{ gap: 8 }}>
          <GitFork size={17} /> Fork “{srcName}”
        </span>
      }
      onClose={close}
      footer={
        <>
          <label className="check grow" title="The original is archived after the fork: the conversation moves rather than copies">
            <input type="checkbox" checked={archive} onChange={(e) => setArchive(e.target.checked)} /> Archive the original
          </label>
          <button className="btn" onClick={close} disabled={busy}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => void submit()} disabled={busy}>
            {busy && <Spinner />} Fork
          </button>
        </>
      }
    >
      <div className="fork-from">
        {req.at ? (
          <>
            Forks before message {req.at.user_index + 1}
            {req.text ? <> — <q>{excerpt(req.text, 80)}</q></> : null}: the new agent remembers everything up to there.
          </>
        ) : (
          <>Forks from the latest point: the new agent remembers the whole conversation.</>
        )}{" "}
        The original stays as it is.
      </div>

      <div className="field">
        <label>Where the fork works</label>
        <div className="fork-opts">
          {isGit &&
            option(
              "new",
              <GitBranchPlus size={14} />,
              "New worktree",
              <>Its own checkout on a new branch from {onBranch ?? "the original's commit"}: ideas run in parallel without touching each other's files.</>,
              <div className="sub" onClick={(e) => e.stopPropagation()}>
                <input className="input mono" value={branch} onChange={(e) => setBranchInput(e.target.value)} placeholder="branch name" />
                <label className="check">
                  <input type="checkbox" checked={carry} onChange={(e) => setCarry(e.target.checked)} />
                  {req.at ? "Restore the files as they were at that point (Atrium's turn snapshots)" : "Bring the current files, uncommitted changes included"}
                </label>
              </div>,
            )}
          {isGit &&
            others.length > 0 &&
            option(
              "worktree",
              <FolderGit2 size={14} />,
              "Existing worktree",
              "Carry on in another checkout, with the files it has.",
              <select className="select" style={{ marginTop: 8 }} value={wt} onClick={(e) => e.stopPropagation()} onChange={(e) => setWt(e.target.value)}>
                {others.map((w) => (
                  <option key={w.path} value={w.path}>
                    {w.main ? "Project directory" : `⎇ ${w.branch ?? "(detached)"}`} · {relTo(project.path, w.path)}
                  </option>
                ))}
              </select>,
            )}
          {option(
            "same",
            <Folder size={14} />,
            "Same directory",
            <>Shares the files with the original{onBranch ? <> on {onBranch}</> : null}. Fine to discuss alternatives; two agents editing the same files collide.</>,
          )}
        </div>
      </div>

      <div className="field">
        <label>Name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Optional — the Tracker names it from what it does" />
      </div>
      <div className="field">
        <label>First message</label>
        <textarea
          autoFocus
          className="textarea"
          rows={3}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Optional — what this fork should try. Empty: the fork waits for you."
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        {req.at && req.text ? <div className="hint">The message you forked before — edit it to try something else (⌘↵ forks).</div> : null}
      </div>
    </Modal>
  );
}
