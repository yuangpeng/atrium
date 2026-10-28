// Composer building blocks shared by the chat view and the new-agent page:
// attachments (paste / drop / pick → uploaded to ~/.atrium/uploads) and the reasoning-effort picker.
import { useCallback, useRef, useState } from "react";
import { FileText, Gauge, Paperclip, X } from "lucide-react";
import { api, Attachment, Harness, Upload } from "../api";
import { useApp } from "../store";
import { clsx } from "../util";
import { Spinner } from "../components/ui";

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
  return (
    <button className="icon-btn composer-icon" onClick={onClick} title="Attach files or images (you can also paste or drop them)">
      <Paperclip size={16} />
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

export function effortOptions(harness: Harness | undefined, model: string | null | undefined): { efforts: string[]; fallback: string | null } {
  if (!harness) return { efforts: [], fallback: null };
  const m = harness.models.find((x) => x.id === (model ?? "")) ?? harness.models.find((x) => x.id === "");
  return { efforts: m?.efforts?.length ? m.efforts : harness.efforts, fallback: m?.default_effort ?? null };
}

const EFFORT_LABEL: Record<string, string> = { low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Max", ultra: "Ultra", minimal: "Minimal", none: "None" };
export const effortLabel = (e: string) => EFFORT_LABEL[e] ?? e;

/** Reasoning-effort picker; "" = the harness default (config file). */
export function EffortPicker({ efforts, value, fallback, onChange }: { efforts: string[]; value: string; fallback?: string | null; onChange: (v: string) => void }) {
  void fallback;
  if (!efforts.length) return null;
  // "" defers to the harness config (which may override the model's default), so no level is shown
  const label = value ? effortLabel(value) : "Auto";
  return (
    <label className={clsx("chip effort-chip", value && "set")} title="Reasoning effort">
      <Gauge size={13} />
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Auto (from config)</option>
        {efforts.map((e) => (
          <option key={e} value={e}>
            {effortLabel(e)}
          </option>
        ))}
      </select>
    </label>
  );
}
