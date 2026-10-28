import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, FolderGit2, GitBranchPlus } from "lucide-react";
import { api } from "../api";
import { useApp } from "../store";
import { clsx, relTo } from "../util";
import { Popover, Spinner } from "./ui";
import { AttachButton, AttachmentTray, EffortPicker, effortOptions, useAttachments } from "../chat/Composer";

type Workspace = { kind: "root" } | { kind: "worktree"; path: string; branch: string | null } | { kind: "new"; branch: string; base: string };

export function Home() {
  const { project, meta, selectSession, toast, refreshSessions, refreshProject } = useApp();
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [ws, setWs] = useState<Workspace>({ kind: "root" });
  const [harnessId, setHarnessId] = useState<string>(meta?.settings.defaults?.harness || "claude-code");
  const defaults = meta?.settings.harnesses?.[harnessId] ?? {};
  const [mode, setMode] = useState<string>(defaults.permission_mode || "");
  const [model, setModel] = useState<string>(defaults.model || "");
  const [effort, setEffort] = useState<string>(defaults.effort || "");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const att = useAttachments();

  // switching harness loads that harness's defaults from ~/.atrium/settings.json
  useEffect(() => {
    const d = meta?.settings.harnesses?.[harnessId] ?? {};
    const h = meta?.harnesses.find((x) => x.id === harnessId);
    setMode(d.permission_mode || h?.permission_modes[0]?.id || "");
    setModel(d.model || "");
    setEffort(d.effort || "");
  }, [harnessId, meta]);

  useEffect(() => {
    ref.current?.focus();
  }, [project?.id]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 280) + "px";
  }, [prompt]);

  if (!project || !meta) return null;
  const harness = meta.harnesses.find((h) => h.id === harnessId) ?? meta.harnesses[0];
  const worktrees = project.worktrees.filter((w) => !w.main);
  const currentBranch = project.worktrees.find((w) => w.main)?.branch || "HEAD";

  const { efforts, fallback } = effortOptions(harness, model);
  const canStart = !!prompt.trim() && !busy && !att.uploading && harness.available;

  const start = async () => {
    if (!canStart) return;
    if (ws.kind === "new" && !ws.branch.trim()) return toast("Give the new worktree a branch name", "error");
    setBusy(true);
    try {
      const res = await api.createSession(project.id, {
        name: name.trim(),
        prompt: prompt.trim(),
        harness: harnessId,
        mode,
        model: model || undefined,
        effort: effort || undefined,
        attachments: att.ids,
        cwd: ws.kind === "worktree" ? ws.path : undefined,
        worktree: ws.kind === "new" ? { branch: ws.branch.trim(), base: ws.base || undefined, create: true } : undefined,
      });
      setPrompt("");
      setName("");
      att.clear();
      if (ws.kind === "new") void refreshProject();
      await refreshSessions();
      selectSession(res.id);
    } catch (e: any) {
      toast(e.message || String(e), "error");
    } finally {
      setBusy(false);
    }
  };

  const wsLabel =
    ws.kind === "root" ? currentBranch : ws.kind === "worktree" ? `⎇ ${ws.branch ?? relTo(project.path, ws.path)}` : `+ ${ws.branch || "new worktree"}`;
  return (
    <div className="home">
      <div className="home-empty">
        {/* the Atrium mark, same as the favicon */}
        <svg className="home-logo" viewBox="0 0 32 32" aria-label="Atrium">
          <rect width="32" height="32" rx="8" fill="currentColor" />
          <path d="M9 23 16 8l7 15M12 17h8" stroke="white" strokeWidth="2.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>

      <div className="composer">
        <div className={clsx("composer-box", att.dragging && "dragging")} {...att.dropProps}>
          {att.picker}
          <input className="name-input" placeholder="Agent name (optional)" title="Leave empty and the Tracker names it" value={name} onChange={(e) => setName(e.target.value)} />
          <textarea
            ref={ref}
            className="composer-input"
            rows={3}
            placeholder="Describe the task"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onPaste={att.onPaste}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !e.shiftKey) && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void start();
              }
            }}
          />
          <AttachmentTray items={att.items} onRemove={att.remove} />
          {ws.kind === "new" && (
            <div className="row" style={{ marginTop: 8 }}>
              <input className="input" style={{ height: 30 }} placeholder="new branch, e.g. feature/auth" value={ws.branch} onChange={(e) => setWs({ ...ws, branch: e.target.value })} />
              <span className="muted" style={{ fontSize: 12.5 }}>from</span>
              <input className="input" style={{ height: 30, width: 160 }} placeholder={currentBranch} value={ws.base} onChange={(e) => setWs({ ...ws, base: e.target.value })} />
            </div>
          )}
          <div className="composer-bar">
            <div className="bar-chips">
            <AttachButton onClick={att.open} />
            {meta.harnesses.length > 1 && (
              <label className={clsx("chip", "harness-chip")} title={harness.available ? "Harness" : harness.unavailable_reason ?? ""}>
                {harness.label}
                <select value={harnessId} onChange={(e) => setHarnessId(e.target.value)}>
                  {meta.harnesses.map((h) => (
                    <option key={h.id} value={h.id} disabled={!h.available}>
                      {h.label}
                      {h.available ? "" : " (unavailable)"}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {project.is_git ? (
              <Popover
                up
                width={300}
                trigger={(_o, toggle) => (
                  <button className="chip" onClick={toggle} title="Where the agent works">
                    <FolderGit2 size={13} /> <span className="ellipsis" style={{ maxWidth: 160 }}>{wsLabel}</span>
                  </button>
                )}
              >
                {(close) => (
                  <>
                    <button className={clsx("menu-item", ws.kind === "root" && "on")} onClick={() => (setWs({ kind: "root" }), close())}>
                      <div>
                        <div>Project directory</div>
                        <div className="d">{currentBranch} · shares the working tree</div>
                      </div>
                    </button>
                    {worktrees.map((w) => (
                      <button
                        key={w.path}
                        className={clsx("menu-item", ws.kind === "worktree" && ws.path === w.path && "on")}
                        onClick={() => (setWs({ kind: "worktree", path: w.path, branch: w.branch }), close())}
                      >
                        <div>
                          <div>⎇ {w.branch ?? "(detached)"}</div>
                          <div className="d mono">{relTo(project.path, w.path)}</div>
                        </div>
                      </button>
                    ))}
                    <div className="menu-sep" />
                    <button className={clsx("menu-item", ws.kind === "new" && "on")} onClick={() => (setWs({ kind: "new", branch: "", base: "" }), close())}>
                      <GitBranchPlus size={14} />
                      <div>
                        <div>New worktree…</div>
                        <div className="d">Isolated checkout on a new branch</div>
                      </div>
                    </button>
                  </>
                )}
              </Popover>
            ) : (
              <span className="chip" style={{ cursor: "default" }}>
                <FolderGit2 size={13} /> {project.name}
              </span>
            )}
            <label className="chip" title="Permission mode">
              {harness.permission_modes.find((m) => m.id === mode)?.label ?? mode}
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                {harness.permission_modes.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="chip" title="Model">
              {harness.models.find((m) => m.id === model)?.label ?? (model || "Default model")}
              <select value={model} onChange={(e) => setModel(e.target.value)}>
                {harness.models.map((m) => (
                  <option key={m.id || "d"} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
            </div>
            <div className="bar-end">
              <EffortPicker efforts={efforts} value={effort} fallback={fallback} onChange={setEffort} />
              <button className="send-btn" disabled={!canStart} onClick={() => void start()} title="Start agent (Enter)">
                {busy ? <Spinner /> : <ArrowUp size={17} strokeWidth={2.4} />}
              </button>
            </div>
          </div>
        </div>
        {!harness.available && <div className="composer-hint err">{harness.unavailable_reason}</div>}
      </div>
    </div>
  );
}
