import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, FolderGit2 } from "lucide-react";
import { api } from "../api";
import { useApp } from "../store";
import { EMPTY_DRAFT, NewAgentDraft, Workspace, useDrafts } from "../drafts";
import { clsx } from "../util";
import { Popover, Spinner } from "./ui";
import {
  AttachButton, AttachmentTray, EffortPicker, HarnessPicker, ModelChip, NewWorktreeFields, PendingUpload, PermissionPicker, WorkspaceMenu, WorkspacePickChip, effortOptions,
  useAttachments, workspaceLabel,
} from "../chat/Composer";
import { mergeCommands, useSlashCommands, useSlashMenu } from "../chat/SlashMenu";

export function Home() {
  const { project, meta, selectSession, toast, refreshSessions, refreshProject } = useApp();
  // what's typed and picked here is the project's draft, kept outside this component (../drafts)
  const pid = project?.id ?? "";
  const draft = useDrafts((s) => s.byProject[pid]) ?? EMPTY_DRAFT;
  const update = useDrafts((s) => s.update);
  const patch = (p: Partial<NewAgentDraft>) => update(pid, (d) => ({ ...d, ...p }));
  const setAttachments = useCallback(
    (fn: (xs: PendingUpload[]) => PendingUpload[]) => update(pid, (d) => ({ ...d, attachments: fn(d.attachments) })),
    [pid, update],
  );
  const att = useAttachments([draft.attachments, setAttachments]);
  const { name, prompt } = draft;
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = ref.current;
    el?.focus();
    el?.setSelectionRange(el.value.length, el.value.length); // carry on at the end of a restored draft
  }, [project?.id]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 280) + "px";
  }, [prompt]);

  // slash commands: those of the harness the agent gets, where it will work
  const harnessId = draft.harness ?? (meta?.settings.defaults?.harness || "claude-code");
  const draftWs = draft.ws;
  const wsCwd = draftWs.kind === "worktree" && project?.worktrees.some((w) => w.path === draftWs.path) ? draftWs.path : project?.path;
  const skills = harnessId === "codex";
  const harnessCommands = useSlashCommands(pid, { harness: harnessId, cwd: wsCwd }, prompt.startsWith("/") || (skills && prompt.includes("$")));
  const commands = useMemo(() => mergeCommands([], harnessCommands), [harnessCommands]);
  const slash = useSlashMenu({ text: prompt, setText: (t) => patch({ prompt: t }), input: ref, skills, commands, onRun: (t) => void start(t) });

  if (!project || !meta) return null;
  // picks left unset follow the harness's defaults from ~/.atrium/settings.json
  const harness = meta.harnesses.find((h) => h.id === (draft.harness ?? (meta.settings.defaults?.harness || "claude-code"))) ?? meta.harnesses[0];
  const defaults = meta.settings.harnesses?.[harness.id] ?? {};
  const mode = draft.mode ?? (defaults.permission_mode || harness.permission_modes[0]?.id || "");
  const model = draft.model ?? (defaults.model || "");
  // an effort picked here; without one, Atrium's default for the harness, else what its config files say
  const effort = draft.effort ?? "";
  const baseEffort = defaults.effort || harness.config_effort || "";
  const worktrees = project.worktrees.filter((w) => !w.main);
  // a worktree removed since the draft was written falls back to the project directory
  const saved = draft.ws;
  const ws: Workspace = saved.kind === "worktree" && !worktrees.some((w) => w.path === saved.path) ? { kind: "root" } : saved;
  const setWs = (ws: Workspace) => patch({ ws });

  const efforts = effortOptions(harness, model || harness.config_model);
  const canStart = !!prompt.trim() && !busy && !att.uploading && harness.available;

  // `override`: a slash command picked in the menu, sent instead of what's typed
  const start = async (override?: string) => {
    const text = (override ?? prompt).trim();
    if (!text || busy || att.uploading || !harness.available) return;
    if (ws.kind === "new" && !ws.branch.trim()) return toast("Give the new worktree a branch name", "error");
    setBusy(true);
    try {
      const res = await api.createSession(project.id, {
        name: name.trim(),
        prompt: text,
        harness: harness.id,
        mode,
        model: model || undefined,
        effort: effort || defaults.effort || undefined,
        attachments: att.ids,
        cwd: ws.kind === "worktree" ? ws.path : undefined,
        worktree: ws.kind === "new" ? { branch: ws.branch.trim(), base: ws.base || undefined, create: true } : undefined,
      });
      att.clear();
      update(project.id, () => EMPTY_DRAFT);
      if (ws.kind === "new") void refreshProject();
      await refreshSessions();
      selectSession(res.id);
    } catch (e: any) {
      toast(e.message || String(e), "error");
    } finally {
      setBusy(false);
    }
  };

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
          {slash.menu}
          <input className="name-input" placeholder="Agent name (optional)" title="Leave empty and the Tracker names it" value={name} onChange={(e) => patch({ name: e.target.value })} />
          <textarea
            ref={ref}
            className="composer-input"
            rows={3}
            placeholder="Describe the task, or / for commands"
            value={prompt}
            onChange={(e) => patch({ prompt: e.target.value })}
            onSelect={slash.onSelect}
            onPaste={att.onPaste}
            onKeyDown={(e) => {
              if (slash.onKeyDown(e)) return;
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !e.shiftKey) && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void start();
              }
            }}
          />
          <AttachmentTray items={att.items} onRemove={att.remove} />
          {ws.kind === "new" && <NewWorktreeFields project={project} ws={ws} onChange={setWs} />}
          <div className="composer-bar">
            <div className="bar-chips">
            <AttachButton onClick={att.open} />
            {/* another harness: back to its defaults */}
            {meta.harnesses.length > 1 && (
              <HarnessPicker harnesses={meta.harnesses} value={harness.id} onChange={(id) => patch({ harness: id, mode: undefined, model: undefined, effort: undefined })} />
            )}
            <PermissionPicker harness={harness} value={mode} onChange={(mode) => patch({ mode })} />
            {project.is_git ? (
              <Popover
                up
                width={300}
                trigger={(_o, toggle) => <WorkspacePickChip label={workspaceLabel(project, ws)} onClick={toggle} />}
              >
                {(close) => <WorkspaceMenu project={project} value={ws} onPick={(ws) => (setWs(ws), close())} />}
              </Popover>
            ) : (
              <span className="chip" style={{ cursor: "default" }}>
                <FolderGit2 size={13} /> {project.name}
              </span>
            )}
            <ModelChip harness={harness} model={model || harness.config_model} />
            </div>
            <div className="bar-end">
              <EffortPicker efforts={efforts} value={effort} base={baseEffort} onChange={(effort) => patch({ effort })} />
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
