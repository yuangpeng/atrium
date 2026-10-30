// Settings = the real config files, edited raw: Atrium's settings.json next to each
// harness's own (~/.claude/settings.json, ~/.codex/config.toml, ~/.gemini/antigravity-cli/settings.json,
// project-level overrides).
import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Copy, FileCog, RotateCcw } from "lucide-react";
import { api, ConfigFileData, ConfigFileInfo } from "../api";
import { useApp } from "../store";
import { clsx, shortPath } from "../util";
import { Modal, Spinner } from "./ui";
import { CodeEditor } from "./LazyEditors";

const GROUPS: { harness: string; label: string }[] = [
  { harness: "atrium", label: "Atrium" },
  { harness: "claude-code", label: "Claude Code" },
  { harness: "codex", label: "Codex" },
  { harness: "antigravity", label: "Antigravity" },
];
const SCOPE_LABEL: Record<string, string> = { user: "user", project: "project", local: "local" };

function localError(file: ConfigFileInfo, text: string): string | null {
  if (file.language !== "json" || !text.trim()) return null;
  try {
    const v = JSON.parse(text);
    return v && typeof v === "object" && !Array.isArray(v) ? null : "The top level must be a JSON object";
  } catch (e: any) {
    return e.message || "Invalid JSON";
  }
}

export function SettingsDialog() {
  const { setSettingsOpen, toast, project, refreshMeta } = useApp();
  const [files, setFiles] = useState<ConfigFileInfo[]>([]);
  const [active, setActive] = useState<string>("atrium");
  const [data, setData] = useState<ConfigFileData | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const pid = project?.id;

  useEffect(() => {
    void api.configFiles(pid).then(setFiles).catch((e) => toast(e.message, "error"));
  }, [pid, toast]);

  const load = useCallback(
    async (id: string) => {
      setData(null);
      setServerError(null);
      try {
        const d = await api.configFile(id, pid);
        setData(d);
        setText(d.content || (d.language === "json" ? "{\n  \n}\n" : ""));
      } catch (e: any) {
        toast(e.message, "error");
      }
    },
    [pid, toast],
  );
  useEffect(() => {
    void load(active);
  }, [active, load]);

  const original = data ? data.content || (data.language === "json" ? "{\n  \n}\n" : "") : "";
  const dirty = !!data && text !== original;
  const jsonError = data ? localError(data, text) : null;
  const file = files.find((f) => f.id === active);

  const select = (id: string) => {
    if (id === active) return;
    if (dirty && !confirm("Discard unsaved changes to this file?")) return;
    setActive(id);
  };
  const close = () => {
    if (dirty && !confirm("Discard unsaved changes?")) return;
    setSettingsOpen(false);
  };

  const save = useCallback(async () => {
    if (!data || busy || !dirty) return;
    if (jsonError) return toast(jsonError, "error");
    setBusy(true);
    setServerError(null);
    try {
      const d = await api.saveConfigFile(data.id, { content: text, base_sha: data.sha, pid });
      setData(d);
      setText(d.content);
      setFiles((fs) => fs.map((f) => (f.id === d.id ? { ...f, exists: true } : f)));
      toast(d.backup ? `Saved · previous version backed up` : "Saved", "ok");
      void refreshMeta();
    } catch (e: any) {
      setServerError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [data, busy, dirty, jsonError, text, pid, toast, refreshMeta]);

  const grouped = useMemo(() => GROUPS.map((g) => ({ ...g, files: files.filter((f) => f.harness === g.harness) })).filter((g) => g.files.length), [files]);
  const error = jsonError || serverError;

  return (
    <Modal
      title="Settings"
      xl
      bodyClass="settings-body"
      onClose={close}
      footer={
        <>
          <span className={clsx("settings-status grow", error && "err")}>
            {error ? (
              <>
                <AlertTriangle size={13} /> {error}
              </>
            ) : dirty ? (
              "Unsaved changes · ⌘S to save"
            ) : data ? (
              data.exists ? (
                <>
                  <Check size={13} /> Saved on disk
                </>
              ) : (
                "This file does not exist yet — saving creates it"
              )
            ) : null}
          </span>
          <button className="btn btn-ghost" disabled={!dirty || busy} onClick={() => (setText(original), setServerError(null))}>
            <RotateCcw size={13} /> Revert
          </button>
          <button className="btn btn-primary" disabled={!dirty || busy || !!jsonError} onClick={() => void save()}>
            {busy && <Spinner />} Save
          </button>
        </>
      }
    >
      <div className="settings-files">
        {grouped.map((g) => (
          <div key={g.harness}>
            <div className="section-label">{g.label}</div>
            {g.files.map((f) => (
              <button key={f.id} className={clsx("settings-file", f.id === active && "active")} onClick={() => select(f.id)} title={f.path}>
                <FileCog size={14} className="muted" />
                <div className="grow" style={{ minWidth: 0 }}>
                  <div className="row" style={{ gap: 6 }}>
                    <span className="ellipsis">{f.path.split("/").slice(-1)[0]}</span>
                    <span className="pill">{SCOPE_LABEL[f.scope] ?? f.scope}</span>
                  </div>
                  <div className="d ellipsis">{f.exists ? shortPath(f.path) : "not created"}</div>
                </div>
                {f.id === active && dirty && <span className="dirty-dot" />}
              </button>
            ))}
          </div>
        ))}
      </div>
      <div className="settings-editor">
        {file && (
          <div className="settings-head">
            <div className="row" style={{ gap: 8 }}>
              <span className="mono ellipsis" style={{ fontSize: 12.5 }}>{file.path}</span>
              <button className="icon-btn sm" title="Copy path" onClick={() => void navigator.clipboard.writeText(file.path).then(() => toast("Path copied", "ok"))}>
                <Copy size={12} />
              </button>
            </div>
            <div className="muted" style={{ fontSize: 12.5 }}>{file.hint}</div>
          </div>
        )}
        <div className="settings-monaco">
          {data ? (
            <CodeEditor
              path={`config://${data.id}/${data.path.split("/").slice(-1)[0]}`}
              value={text}
              language={data.language === "toml" ? "ini" : "json"}
              onChange={setText}
              onSave={() => void save()}
            />
          ) : (
            <div className="empty-state">
              <Spinner />
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
