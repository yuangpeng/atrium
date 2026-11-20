import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, Harness, SessionRow } from "../api";
import { useAppState } from "../store";
import { useDrafts } from "../drafts";
import { compatibleEffort, EffortPicker, effortOptions, ModelPicker, PermissionPicker } from "../chat/Composer";
import { handoffContext, handoffPrompt } from "../chat/harnessHandoff";
import { displayName } from "../util";
import { Modal, Spinner } from "./ui";

const busyState = (state: string | null) => state === "starting" || state === "running" || state === "waiting";

export function HarnessHandoff({ pid, source, target, blocked, draft, hasAttachments, onClose }: {
  pid: string; source: SessionRow; target: Harness; blocked: boolean; draft: string; hasAttachments: boolean; onClose: () => void;
}) {
  const { meta, toast, refreshSessions, selectSession } = useAppState("meta", "toast", "refreshSessions", "selectSession");
  const defaults = meta?.settings.harnesses[target.id] ?? {};
  const [name, setName] = useState(() => displayName(source) + " · " + target.label);
  const [context, setContext] = useState("");
  const [instruction, setInstruction] = useState(draft.trim() || "Continue the task from the latest point. Check what is complete and carry out the remaining work.");
  const [model, setModel] = useState(defaults.model || "");
  const [effort, setEffort] = useState(() => compatibleEffort(target, defaults.model || target.config_model, defaults.effort || target.config_effort));
  const [mode, setMode] = useState(() => target.permission_modes.find((m) => m.id === defaults.permission_mode)?.id || target.permission_modes[0]?.id || "");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [contextCwd, setContextCwd] = useState(source.cwd);
  const efforts = effortOptions(target, model || target.config_model);
  const baseEffort = compatibleEffort(target, model || target.config_model, defaults.effort || target.config_effort);
  const close = () => !busy && onClose();

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError("");
    // Read the available snapshot; some harnesses expose only the latest compaction segment.
    void api.session(pid, source.id, controller.signal).then((snap) => {
      if (controller.signal.aborted) return;
      const row = snap.session ?? source;
      if (row.workspace_gone) throw new Error("Choose a workspace in the original chat before switching harnesses.");
      if (busyState(snap.state) || busyState(row.live) || snap.pending.length) throw new Error("Finish or stop the original turn before switching harnesses.");
      setContext(handoffContext(row, snap.items));
      setContextCwd(row.cwd);
    }).catch((e: any) => {
      if (!controller.signal.aborted) setLoadError(e.message || String(e));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [pid, source.id, reload]);

  const submit = async () => {
    if (busy || loading || loadError || blocked || !target.available || !context.trim() || !instruction.trim()) return;
    setBusy(true);
    try {
      const snap = await api.session(pid, source.id);
      const row = snap.session ?? source;
      if (busyState(snap.state) || busyState(row.live) || snap.pending.length) throw new Error("The original agent is working. Finish or stop its turn first.");
      if (row.workspace_gone || row.cwd !== contextCwd) throw new Error("The original workspace changed. Close and reopen this handoff to use its current directory.");
      const prompt = handoffPrompt(context, instruction);
      // Create first without sending, so a failed first message cannot cause a retry to make a duplicate agent.
      const res = await api.createSession(pid, {
        name: name.trim(), harness: target.id, cwd: contextCwd, mode,
        model: model || undefined, effort: efforts.length ? effort || baseEffort || undefined : undefined,
      });
      let sendError = "";
      try { await api.send(pid, res.id, prompt, []); }
      catch (e: any) {
        sendError = e.message || String(e);
        useDrafts.getState().updateReply(res.id, (d) => ({ ...d, text: prompt }));
      }
      onClose();
      selectSession(res.id);
      void refreshSessions().catch(() => toast("Agent created. Reload the agent list if it is missing.", "info"));
      if (sendError) toast("Agent created, but the handoff message failed: " + sendError + ". The message is saved in its draft; retry there.", "error");
      else toast("Continued in " + target.label + ". The original conversation is kept.", "ok");
    } catch (e: any) {
      toast(e.message || String(e), "error");
    } finally { setBusy(false); }
  };

  return createPortal(
    <div className="harness-handoff" role="dialog" aria-modal="true" aria-label={"Switch to " + target.label}>
      <Modal title={"Switch to " + target.label} onClose={close} footer={<>
        <div className="handoff-pickers composer-bar"><div className="bar-chips">
          <PermissionPicker within=".modal" harness={target} value={mode} onChange={(m) => !busy && setMode(m)} />
          <ModelPicker within=".modal" harness={target} value={model} defaultModel={defaults.model || target.config_model || ""} disabled={busy}
            onChange={(m) => { setModel(m); setEffort(compatibleEffort(target, m || defaults.model || target.config_model, effort)); }} />
          <EffortPicker within=".modal" efforts={efforts} value={effort} base={baseEffort} onChange={(e) => !busy && setEffort(e || baseEffort)} />
        </div></div>
        <button className="btn" disabled={busy} onClick={close}>Cancel</button>
        <button className="btn btn-primary" disabled={busy || loading || !!loadError || blocked || !target.available || !context.trim() || !instruction.trim()} onClick={() => void submit()}>
          {busy && <Spinner />} Continue in {target.label}
        </button>
      </>}>
        <p className="handoff-intro">Creates a new conversation with {target.label} in the same directory. The original conversation and its draft are kept.</p>
        <div className="hint handoff-cwd">{contextCwd}</div>
        <div className="field">
          <label htmlFor="handoff-name">Name</label>
          <input id="handoff-name" className="input" value={name} disabled={busy} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="handoff-context">Context to hand over</label>
          {loading ? <div className="hint"><Spinner /> Loading conversation…</div> : loadError ? <div className="hint err">{loadError} <button className="sub-link inline" onClick={() => setReload((n) => n + 1)}>Retry</button></div> : <>
            <textarea id="handoff-context" className="textarea handoff-context" rows={6} value={context} disabled={busy} onChange={(e) => setContext(e.target.value)} />
            <div className="hint">Review and edit these text excerpts. Long history may be omitted or truncated. Native tool state, hidden reasoning and attachment contents are not transferred.</div>
          </>}
        </div>
        <div className="field">
          <label htmlFor="handoff-instruction">What to do next</label>
          <textarea id="handoff-instruction" className="textarea" rows={3} value={instruction} disabled={busy} onChange={(e) => setInstruction(e.target.value)} />
          {hasAttachments && <div className="hint">Your unsent attachments remain in the original draft. Attach them in the new chat if needed.</div>}
        </div>
        {(blocked || !target.available) && <div className="hint err">{!target.available ? target.unavailable_reason || "This harness is unavailable." : "Finish or stop the original turn before continuing."}</div>}
      </Modal>
    </div>, document.body,
  );
}
