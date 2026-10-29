// Composer building blocks shared by the chat view and the new-agent page:
// attachments (paste / drop / pick → uploaded to ~/.atrium/uploads), the icon-only harness / permission-mode
// pickers, the read-only model chip (and a started agent's harness and workspace), the reasoning-effort slider
// and the context gauge.
import type { CSSProperties } from "react";
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import { Check, ChevronDown, ClipboardList, FilePen, FileText, FolderGit2, Hand, Paperclip, RotateCcw, Shield, ShieldAlert, ShieldCheck, X, createLucideIcon } from "lucide-react";
import { api, Attachment, Harness, ProjectInfo, SessionRow, Upload } from "../api";
import { useApp } from "../store";
import { clsx, relTo, shortPath } from "../util";
import { Popover, Spinner, useTip } from "../components/ui";
import { HarnessIcon, ModelIcon } from "../components/BrandIcons";

export interface PendingUpload {
  key: string;
  name: string;
  mime: string;
  preview?: string; // object URL for local image previews (the upload's URL once restored from a saved draft)
  upload?: Upload;
  error?: string;
}

type SetItems = (fn: (xs: PendingUpload[]) => PendingUpload[]) => void;

/** Pending attachments, held by the component unless `state` keeps them elsewhere (the new-agent draft). */
export function useAttachments(state?: [PendingUpload[], SetItems]) {
  const toast = useApp((s) => s.toast);
  const local = useState<PendingUpload[]>([]);
  const [items, setItems]: [PendingUpload[], SetItems] = state ?? local;
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const add = useCallback(
    (files: FileList | File[]) => {
      for (const file of Array.from(files)) {
        const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const name = file.name && file.name !== "image.png" ? file.name : `pasted-${new Date().toISOString().slice(11, 19).replace(/:/g, "")}.${(file.type.split("/")[1] || "png").replace("jpeg", "jpg")}`;
        const preview = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
        setItems((xs) => [...xs, { key, name, mime: file.type, preview }]);
        api
          .upload(file, name)
          .then((upload) => setItems((xs) => xs.map((x) => (x.key === key ? { ...x, upload } : x))))
          .catch((e) => {
            toast(`Upload failed: ${e.message || e}`, "error");
            setItems((xs) => xs.filter((x) => x.key !== key));
          });
      }
    },
    [toast, setItems],
  );

  const remove = (key: string) =>
    setItems((xs) => {
      const x = xs.find((i) => i.key === key);
      if (x?.preview) URL.revokeObjectURL(x.preview);
      return xs.filter((i) => i.key !== key);
    });
  const clear = () => {
    items.forEach((x) => x.preview && URL.revokeObjectURL(x.preview));
    setItems(() => []);
  };

  const onPaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files);
    if (files.length) {
      e.preventDefault();
      add(files);
    }
  };
  const dropProps = {
    onDragOver: (e: React.DragEvent) => {
      if (Array.from(e.dataTransfer.types).includes("Files")) {
        e.preventDefault();
        setDragging(true);
      }
    },
    onDragLeave: (e: React.DragEvent) => {
      if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) setDragging(false);
    },
    onDrop: (e: React.DragEvent) => {
      if (e.dataTransfer.files.length) {
        e.preventDefault();
        add(e.dataTransfer.files);
      }
      setDragging(false);
    },
  };
  const uploading = items.some((x) => !x.upload);
  const ids = items.filter((x) => x.upload).map((x) => x.upload!.id);

  const picker = (
    <input
      ref={inputRef}
      type="file"
      multiple
      hidden
      onChange={(e) => {
        if (e.target.files) add(e.target.files);
        e.target.value = "";
      }}
    />
  );
  const open = () => inputRef.current?.click();
  return { items, add, remove, clear, onPaste, dropProps, dragging, uploading, ids, picker, open };
}

export function AttachmentTray({ items, onRemove }: { items: PendingUpload[]; onRemove: (key: string) => void }) {
  if (!items.length) return null;
  return (
    <div className="attach-tray">
      {items.map((x) => (
        <div key={x.key} className={clsx("attach-chip", x.preview && "image")} title={x.upload?.path ?? x.name}>
          {x.preview ? <img src={x.preview} alt={x.name} /> : <FileText size={14} />}
          {!x.preview && <span className="ellipsis">{x.name}</span>}
          {!x.upload && <span className="attach-busy"><Spinner /></span>}
          <button className="attach-x" onClick={() => onRemove(x.key)} title="Remove">
            <X size={11} />
          </button>
        </div>
      ))}
    </div>
  );
}

export function AttachButton({ onClick }: { onClick: () => void }) {
  const [tipProps, tip] = useTip(
    <>
      <div>Attach files or images</div>
      <div className="k">or paste / drop them</div>
    </>,
  );
  return (
    <button className="icon-btn composer-icon" onClick={onClick} aria-label="Attach files or images" {...tipProps}>
      <Paperclip size={16} />
      {tip}
    </button>
  );
}

/** Attachments shown inside a sent user message. */
export function MessageAttachments({ items }: { items: Attachment[] }) {
  return (
    <div className="msg-attachments">
      {items.map((a, i) =>
        a.mime?.startsWith("image") && a.url ? (
          <a key={i} href={a.url} target="_blank" rel="noreferrer" className="msg-attach-img" title={a.path || a.name}>
            <img src={a.url} alt={a.name} />
          </a>
        ) : (
          <span key={i} className="msg-attach-file" title={a.path || a.name}>
            <FileText size={13} /> {a.name}
          </span>
        ),
      )}
    </div>
  );
}

/** The catalog entry for a model id, also when the id carries a routing tag or "[1m]" ("gpt-6.1-sol:tag"). */
function catalogModel(harness: Harness | undefined, id: string | null | undefined) {
  if (!harness || !id) return undefined;
  const base = id.split(/[:[]/)[0];
  return harness.models.find((m) => m.id && m.id === id) ?? harness.models.find((m) => m.id && m.id === base);
}

/** Effort levels for a model: its own list from the catalog, else the harness's. */
export function effortOptions(harness: Harness | undefined, model: string | null | undefined): string[] {
  if (!harness) return [];
  const m = catalogModel(harness, model) ?? harness.models.find((x) => x.id === "");
  return m?.efforts?.length ? m.efforts : harness.efforts;
}

const EFFORT_LABEL: Record<string, string> = { low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Max", ultra: "Ultra", minimal: "Minimal", none: "None" };
export const effortLabel = (e: string) => EFFORT_LABEL[e] ?? e;

/** Effort as bars filled up to the level: one size for every level (the name is in the tooltip and the menu). */
function EffortBars({ level, levels }: { level: number; levels: number }) {
  const n = Math.max(levels, 2);
  const w = Math.min(2.4, 14 / (n * 1.6));
  const gap = (14 - n * w) / (n - 1);
  return (
    <svg className="effort-bars" width="16" height="16" viewBox="0 0 16 16" aria-hidden>
      {Array.from({ length: n }, (_, i) => {
        const h = 4 + (9 * i) / (n - 1);
        return <rect key={i} className={i <= level ? "on" : undefined} x={1 + i * (w + gap)} y={14 - h} width={w} height={h} rx={w / 2} />;
      })}
    </svg>
  );
}

/** Reasoning effort as a slider (after Codex's). `value` "" = no pick: `base` (from the config files) applies. */
export function EffortPicker({ efforts, value, base, onChange }: { efforts: string[]; value: string; base?: string | null; onChange: (v: string) => void }) {
  const [drag, setDrag] = useState<number | null>(null); // stop under the pointer while dragging
  const track = useRef<HTMLDivElement>(null);
  const shown = value || base || "";
  const [tipProps, tip] = useTip(
    <>
      <div>
        <span className="k">Reasoning effort:</span> {shown ? effortLabel(shown) : "Default"}
      </div>
      <div className="k">{value ? "custom" : "from config"}</div>
    </>,
  );
  if (!efforts.length) return null;
  const idx = drag ?? efforts.indexOf(shown);
  const last = efforts.length - 1;
  const frac = (i: number) => (last ? i / last : 0);
  const stopAt = (x: number) => {
    const r = track.current!.getBoundingClientRect();
    const pad = r.height / 2; // stops run between the centres of the rounded ends
    return Math.max(0, Math.min(last, Math.round(((x - r.left - pad) / (r.width - 2 * pad)) * last)));
  };
  const pick = (e: string) => e !== shown && onChange(e === base ? "" : e); // picking the config's level drops the override
  const title = idx >= 0 ? effortLabel(efforts[idx]!) : shown ? effortLabel(shown) : "Effort";
  return (
    <Popover
      up
      align="right"
      within=".composer-box"
      width={264}
      trigger={(open, toggle) => (
        <button className={clsx("chip effort-chip", open && "open")} onClick={toggle} aria-label={`Reasoning effort: ${title}`} {...tipProps}>
          <EffortBars level={efforts.indexOf(shown)} levels={efforts.length} />
          <ChevronDown size={12} />
          {tip}
        </button>
      )}
    >
      {() => (
        <div className="effort-pop">
          <div className="effort-title">
            <EffortBars level={idx} levels={efforts.length} />
            {title}
          </div>
          <div className="effort-sub">{value ? "Custom" : "Default (from config)"}</div>
          <button className="icon-btn sm effort-reset" disabled={!value} onClick={() => onChange("")} title={base ? `Reset to ${effortLabel(base)} (from config)` : "Reset"}>
            <RotateCcw size={14} />
          </button>
          <div
            ref={track}
            className="effort-track"
            role="slider"
            tabIndex={0}
            aria-label="Reasoning effort"
            aria-valuemin={0}
            aria-valuemax={last}
            aria-valuenow={Math.max(idx, 0)}
            aria-valuetext={title}
            style={{ "--p": frac(Math.max(idx, 0)) } as CSSProperties}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              setDrag(stopAt(e.clientX));
            }}
            onPointerMove={(e) => drag !== null && setDrag(stopAt(e.clientX))}
            onPointerUp={(e) => {
              if (drag === null) return;
              setDrag(null);
              pick(efforts[stopAt(e.clientX)]!);
            }}
            onPointerCancel={() => setDrag(null)}
            onKeyDown={(e) => {
              const d = e.key === "ArrowRight" || e.key === "ArrowUp" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowDown" ? -1 : 0;
              if (!d) return;
              e.preventDefault();
              pick(efforts[Math.max(0, Math.min(last, (idx < 0 ? 0 : idx) + d))]!);
            }}
          >
            {idx >= 0 && <span className="effort-fill" />}
            {efforts.map((e, i) => (
              <span key={e} className={clsx("effort-stop", idx >= 0 && i <= idx && "on")} style={{ "--p": frac(i) } as CSSProperties} />
            ))}
            {idx >= 0 && <span className="effort-thumb" />}
          </div>
        </div>
      )}
    </Popover>
  );
}

/** Short model name: the catalog's label, else e.g. "claude-opus-5-5:tag" → "Opus 5.5". */
export function modelName(harness: Harness | undefined, id: string | null | undefined): string {
  if (!id) return "Default model";
  const label = catalogModel(harness, id)?.label;
  if (label) return label;
  const base = id.split(/[:[]/)[0]!; // drop routing tags and "[1m]"
  const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
  let m = base.match(/^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/); // claude-opus-4-1-20250805
  if (m) return `${cap(m[1]!)} ${m[2]}${m[3] ? `.${m[3]}` : ""}`;
  m = base.match(/^claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)(?:-\d{8})?$/); // claude-3-5-sonnet-20241022
  if (m) return `${cap(m[3]!)} ${m[1]}${m[2] ? `.${m[2]}` : ""}`;
  return /^[a-z]+$/.test(base) ? cap(base) : base.replace(/^gpt/, "GPT");
}

/** The model, read-only: its maker's mark + name while the chip row has room, else the mark (name on hover). */
export function ModelChip({ harness, model, faded }: { harness: Harness; model: string | null | undefined; faded?: boolean }) {
  const name = modelName(harness, model);
  const maker = model || (harness.id === "claude-code" ? "claude" : null); // Claude Code's default is a Claude model
  const ref = useRef<HTMLSpanElement>(null);
  const full = useRef<HTMLSpanElement>(null); // invisible copy with the name: the width the chip would need
  const [roomy, setRoomy] = useState(true);
  const [tipProps, tip] = useTip(
    <>
      <div>
        <span className="k">Model:</span> {name}
      </div>
      {model && model !== name && <div className="k mono">{model}</div>}
    </>,
  );
  useLayoutEffect(() => {
    const row = ref.current?.parentElement;
    if (!row) return;
    const fit = () => {
      const others = [...row.children].filter((c) => c !== ref.current).reduce((w, c) => w + (c as HTMLElement).offsetWidth, 0);
      const gaps = (parseFloat(getComputedStyle(row).columnGap) || 0) * (row.children.length - 1);
      setRoomy(row.clientWidth - others - gaps >= (full.current?.offsetWidth ?? 0));
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(row);
    for (const c of row.children) ro.observe(c);
    return () => ro.disconnect();
  }, [name]);
  return (
    <span ref={ref} className={clsx("chip model-chip fixed", !roomy && "icon-chip", faded && "faded")} aria-label={`Model: ${name}`} {...tipProps}>
      <ModelIcon model={maker} />
      {roomy && <span>{name}</span>}
      <span ref={full} className="chip model-chip measure" aria-hidden>
        <ModelIcon model={maker} />
        <span>{name}</span>
      </span>
      {tip}
    </span>
  );
}

export const fmtTokens = (n: number) => (n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));

/** How full the context window is, as a ring (after Codex's); the numbers on hover. */
export function ContextRing({ used, max }: { used?: number | null; max?: number | null }) {
  const f = used && max ? Math.min(1, used / max) : 0;
  const pct = Math.round(f * 100);
  const [tipProps, tip] = useTip(
    <>
      <div className="k">Context window:</div>
      <div>
        {pct}% used ({100 - pct}% left)
      </div>
      <div>
        {fmtTokens(used ?? 0)} / {fmtTokens(max ?? 0)} tokens used
      </div>
    </>,
  );
  if (!used || !max) return null;
  const c = 2 * Math.PI * 6.5;
  return (
    <span className={clsx("ctx-ring", f >= 0.9 ? "full" : f >= 0.75 && "high")} aria-label={`Context window: ${pct}% used`} {...tipProps}>
      {tip}
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden>
        <circle className="track" cx="9" cy="9" r="6.5" />
        <circle className="arc" cx="9" cy="9" r="6.5" strokeDasharray={`${f * c} ${c}`} transform="rotate(-90 9 9)" />
      </svg>
    </span>
  );
}

/** Harness picker: only the harness's mark in the composer; the menu names each one. */
export function HarnessPicker({ harnesses, value, onChange }: { harnesses: Harness[]; value: string; onChange: (id: string) => void }) {
  const cur = harnesses.find((h) => h.id === value) ?? harnesses[0];
  const [tipProps, tip] = useTip(
    cur && (
      <>
        <div>
          <span className="k">Harness:</span> {cur.label}
        </div>
        {!cur.available && <div className="k">{cur.unavailable_reason || "unavailable"}</div>}
      </>
    ),
  );
  if (!cur) return null;
  return (
    <Popover
      up
      within=".composer-box"
      width={220}
      trigger={(open, toggle) => (
        <button className={clsx("chip icon-chip", open && "open")} onClick={toggle} aria-label={`Harness: ${cur.label}`} {...tipProps}>
          <HarnessIcon id={cur.id} />
          {tip}
        </button>
      )}
    >
      {(close) =>
        harnesses.map((h) => (
          <button key={h.id} className="menu-item pick-item" disabled={!h.available} onClick={() => (onChange(h.id), close())}>
            <HarnessIcon id={h.id} />
            <div className="grow">
              <div>{h.label}</div>
              {!h.available && <div className="d">{h.unavailable_reason || "Unavailable"}</div>}
            </div>
            {h.id === cur.id && <Check size={15} />}
          </button>
        ))
      }
    </Popover>
  );
}

/** A started agent's harness, read-only: what the picker showed when it started. */
export function HarnessChip({ harness }: { harness: Harness }) {
  const [tipProps, tip] = useTip(
    <>
      <div>
        <span className="k">Harness:</span> {harness.label}
      </div>
      <div className="k">set when the agent started</div>
    </>,
  );
  return (
    <span className="chip icon-chip fixed" aria-label={`Harness: ${harness.label}`} {...tipProps}>
      <HarnessIcon id={harness.id} />
      {tip}
    </span>
  );
}

/** Where a started agent works, read-only, labelled like the new-agent page's picker: the branch, or ⎇ a worktree's. */
export function WorkspaceChip({ project, session }: { project: ProjectInfo; session: SessionRow }) {
  const worktree = project.is_git && session.worktree;
  const branch = session.branch || project.worktrees.find((w) => w.main)?.branch || "HEAD";
  const label = !project.is_git ? project.name : worktree ? `⎇ ${session.branch ?? relTo(project.path, session.cwd)}` : branch;
  const [tipProps, tip] = useTip(
    <>
      <div>
        <span className="k">Works in:</span> {worktree ? "Worktree" : "Project directory"}
      </div>
      {worktree ? (
        <div className="k mono">{relTo(project.path, session.cwd)}</div>
      ) : project.is_git ? (
        <div className="k">{branch} · shares the working tree</div>
      ) : (
        <div className="k mono">{shortPath(project.path)}</div>
      )}
      <div className="k">set when the agent started</div>
    </>,
  );
  return (
    <span className="chip fixed" aria-label={`Works in: ${label}`} {...tipProps}>
      <FolderGit2 size={13} /> <span className="ellipsis" style={{ maxWidth: 160 }}>{label}</span>
      {tip}
    </span>
  );
}

// Codex's "Approve for me" mark: a terminal prompt in lucide's shield
const ShieldTerminal = createLucideIcon("ShieldTerminal", [
  ["path", { d: "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z", key: "shield" }],
  ["path", { d: "m9 9.5 2.5 2.5L9 14.5", key: "chevron" }],
  ["path", { d: "M13 14.5h2.5", key: "cursor" }],
]);

// Per harness and mode: the menu icon, a one-line summary, and whether the mode drops every guard rail
// (tinted, like Codex's "Full access"). Modes missing here get a plain shield and no summary.
const MODE_INFO: Record<string, Record<string, { icon: LucideIcon; desc: string; danger?: boolean }>> = {
  "claude-code": {
    default: { icon: Hand, desc: "Ask before editing files or running commands" },
    acceptEdits: { icon: FilePen, desc: "Edit files without asking; ask for commands" },
    plan: { icon: ClipboardList, desc: "Explore and plan; no edits until you approve" },
    auto: { icon: ShieldCheck, desc: "No prompts; a classifier blocks risky actions" },
    bypassPermissions: { icon: ShieldAlert, desc: "Unrestricted access to any file and command", danger: true },
  },
  // the Codex app's wording
  codex: {
    auto: { icon: Hand, desc: "Always ask to edit external files and use the internet" },
    "auto-review": { icon: ShieldTerminal, desc: "Only ask for actions detected as potentially unsafe" },
    "full-access": { icon: ShieldAlert, desc: "Unrestricted access to the internet and any file on your computer", danger: true },
  },
};
const MODE_DOCS: Record<string, string> = {
  "claude-code": "https://code.claude.com/docs/en/permission-modes",
  codex: "https://developers.openai.com/codex/permissions",
};

/** Permission mode: only the mode's icon in the composer; the menu (after Codex's) explains each mode. */
export function PermissionPicker({ harness, value, onChange, detached }: { harness: Harness; value: string; onChange: (mode: string) => void; detached?: boolean }) {
  const info = MODE_INFO[harness.id] ?? {};
  const label = harness.permission_modes.find((m) => m.id === value)?.label ?? value;
  const Icon = info[value]?.icon ?? Shield;
  const docs = MODE_DOCS[harness.id];
  const [tipProps, tip] = useTip(
    <>
      <div>
        <span className="k">Permissions:</span> {label || "Not set"}
      </div>
      {detached && <div className="k">applies when the session is attached</div>}
    </>,
  );
  return (
    <Popover
      up
      within=".composer-box"
      width="max-content"
      trigger={(open, toggle) => (
        <button
          className={clsx("chip icon-chip", open && "open", detached && "faded", info[value]?.danger && "danger")}
          onClick={toggle}
          aria-label={`Permissions: ${label}`}
          {...tipProps}
        >
          <Icon size={15} />
          {tip}
        </button>
      )}
    >
      {(close) => (
        <>
          <div className="pick-head">
            <span className="grow">How should {harness.label} actions be approved?</span>
            {docs && (
              <a href={docs} target="_blank" rel="noreferrer">
                Learn more
              </a>
            )}
          </div>
          {harness.permission_modes.map((m) => {
            const mi = info[m.id];
            const MIcon = mi?.icon ?? Shield;
            return (
              <button key={m.id} className={clsx("menu-item pick-item", mi?.danger && "danger")} onClick={() => (onChange(m.id), close())}>
                <MIcon size={16} />
                <div className="grow">
                  <div>{m.label}</div>
                  {mi && <div className="d">{mi.desc}</div>}
                </div>
                {m.id === value && <Check size={15} />}
              </button>
            );
          })}
        </>
      )}
    </Popover>
  );
}
