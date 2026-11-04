// Changing a message already sent, in place of it in the chat: the conversation goes back to just before it and
// carries on with what is sent from here (api.editMessage). The files the agent changed since stay as they are.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, X } from "lucide-react";
import { api, Attachment, EditAt } from "../api";
import { useApp } from "../store";
import { clsx } from "../util";
import { Spinner } from "../components/ui";
import { AttachButton, AttachmentTray, PendingUpload, useAttachments } from "./Composer";

const UPLOADS = "/api/uploads/";

/** The message's attachments as the composer holds them: an upload is sent again as it is. */
function keptUploads(items: Attachment[]): PendingUpload[] {
  return items.flatMap((a, i) => {
    if (!a.url?.startsWith(UPLOADS)) return [];
    const id = decodeURIComponent(a.url.slice(UPLOADS.length));
    const upload = { id, name: a.name, path: a.path ?? id, mime: a.mime, size: 0, url: a.url };
    return [{ key: `kept-${i}`, name: a.name, mime: a.mime, preview: a.mime.startsWith("image/") ? a.url : undefined, upload }];
  });
}

export function MessageEditor({ pid, sid, text: sent, attachments, at, later, running, onClose, onSent }: {
  pid: string;
  sid: string;
  text: string;
  attachments: Attachment[];
  at: EditAt;
  later: number; // the user's messages after this one, which go too
  running: boolean;
  onClose: () => void;
  onSent: () => void;
}) {
  const toast = useApp((s) => s.toast);
  const [text, setText] = useState(sent);
  const held = useState<PendingUpload[]>(() => keptUploads(attachments));
  const att = useAttachments(held);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // an image the transcript holds itself (Claude's history) comes as an upload (written out when the history is read);
  // one still inline, which couldn't be written, is uploaded again; one with neither can't be sent again
  const [lost, setLost] = useState(0);
  useEffect(() => {
    const inline = attachments.filter((a) => a.url?.startsWith("data:"));
    setLost(attachments.filter((a) => !a.url?.startsWith(UPLOADS) && !a.url?.startsWith("data:")).length);
    void Promise.all(inline.map((a) => fetch(a.url!).then((r) => r.blob()).then((b) => new File([b], `${a.name}.${(b.type.split("/")[1] || "png").replace("jpeg", "jpg")}`, { type: b.type }))))
      .then((files) => files.length && att.add(files))
      .catch(() => setLost((n) => n + inline.length));
    // once, as it opens
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, Math.max(160, window.innerHeight * 0.5)) + "px";
  }, [text]);
  useEffect(() => {
    const el = inputRef.current;
    el?.focus();
    el?.setSelectionRange(el.value.length, el.value.length);
  }, []);

  const canSend = (!!text.trim() || att.ids.length > 0) && !att.uploading && !busy;
  const send = async () => {
    if (!canSend) return;
    setBusy(true);
    try {
      await api.editMessage(pid, sid, at, text.trim(), att.ids);
      onSent();
    } catch (e: any) {
      toast(e.message || String(e), "error");
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  const dropped = later ? `this message and the ${later === 1 ? "one" : later} after it are` : "this message is";
  return (
    <div className={clsx("composer-box msg-edit", att.dragging && "dragging")} {...att.dropProps}>
      {att.picker}
      <AttachmentTray items={att.items} onRemove={att.remove} />
      <textarea
        ref={inputRef}
        className="composer-input"
        rows={1}
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onPaste={att.onPaste}
        onKeyDown={(e) => {
          if (e.key === "Escape" && !busy) {
            e.preventDefault();
            onClose();
          } else if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void send();
          }
        }}
      />
      <div className="msg-edit-hint">
        {running ? "The turn in progress stops, and the" : "The"} conversation goes back to here: {dropped} replaced by what you send. Files stay as
        they are.{lost ? ` ${lost === 1 ? "An attachment" : `${lost} attachments`} can't be sent again.` : ""}
      </div>
      <div className="composer-bar">
        <div className="bar-chips">
          <AttachButton onClick={att.open} />
        </div>
        <div className="bar-end">
          <button className="icon-btn" onClick={onClose} disabled={busy} aria-label="Cancel" data-tip="Cancel (Esc)">
            <X size={16} />
          </button>
          <button className="send-btn" onClick={() => void send()} disabled={!canSend} aria-label="Send" data-tip="Send instead (Enter)">
            {busy ? <Spinner /> : <ArrowUp size={17} strokeWidth={2.4} />}
          </button>
        </div>
      </div>
    </div>
  );
}
