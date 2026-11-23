// "Open in" an editor of this computer (VS Code, Cursor…) at the cursor, through the editor's own URL scheme. The
// choice is the device's (localStorage): what is installed is the computer's, not the server's. With Atrium on
// another machine, an editor's Remote SSH opens the file there. Not on a touch screen (responsive.css): a phone has no
// such editor to hand the file to.
import { useState } from "react";
import { ChevronDown, SquareCode } from "lucide-react";
import { editorBridge } from "../editorStatus";
import { clsx } from "../util";
import { Popover } from "./ui";

type EditorId = "vscode" | "cursor" | "vscode-insiders" | "windsurf" | "zed" | "vscode-ssh" | "cursor-ssh";
const EDITORS: { id: EditorId; label: string; ssh?: boolean }[] = [
  { id: "vscode", label: "VS Code" },
  { id: "cursor", label: "Cursor" },
  { id: "vscode-insiders", label: "VS Code Insiders" },
  { id: "windsurf", label: "Windsurf" },
  { id: "zed", label: "Zed" },
  { id: "vscode-ssh", label: "VS Code over SSH", ssh: true },
  { id: "cursor-ssh", label: "Cursor over SSH", ssh: true },
];
const KEY = "atrium.localEditor";
const HOST_KEY = "atrium.localEditor.sshHost";
// the page from this computer: a local editor finds the file at the same path
const local = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(location.hostname);

const encodePath = (p: string) => p.split("/").map(encodeURIComponent).join("/");

/** The editor's link to the file at a line and column (`vscode://file/abs/path:12:3`, Remote SSH's form for a host). */
export function editorUrl(id: EditorId, abs: string, line: number, column: number, host = ""): string {
  const at = `${encodePath(abs)}:${line}:${column}`;
  if (id === "vscode-ssh" || id === "cursor-ssh") return `${id.slice(0, -4)}://vscode-remote/ssh-remote+${encodeURIComponent(host)}${at}`;
  return `${id}://file${at}`;
}

export function OpenInEditor({ abs }: { abs: string }) {
  const [choice, setChoice] = useState<EditorId>(() => {
    const saved = localStorage.getItem(KEY) as EditorId | null;
    return saved && EDITORS.some((e) => e.id === saved) ? saved : "vscode";
  });
  const host = localStorage.getItem(HOST_KEY) || "";
  const label = EDITORS.find((e) => e.id === choice)?.label ?? "VS Code";

  const open = (id: EditorId) => {
    let sshHost = localStorage.getItem(HOST_KEY) || "";
    if (EDITORS.find((e) => e.id === id)?.ssh && !sshHost) {
      sshHost = prompt("The host Atrium's machine has in your ~/.ssh/config (the one Remote SSH connects to):", location.hostname)?.trim() || "";
      if (!sshHost) return;
      localStorage.setItem(HOST_KEY, sshHost);
    }
    localStorage.setItem(KEY, id);
    setChoice(id);
    const at = editorBridge.cursor?.() ?? { line: 1, column: 1 };
    const a = document.createElement("a"); // a link, not a navigation: the page stays if nothing handles the scheme
    a.href = editorUrl(id, abs, at.line, at.column, sshHost);
    a.click();
  };

  return (
    <span className="open-local">
      <button className="icon-btn sm" aria-label={`Open in ${label}`} data-tip={`Open in ${label}, at the cursor`} onClick={() => open(choice)}>
        <SquareCode size={14} />
      </button>
      <Popover
        align="right"
        within=".workspace"
        trigger={(isOpen, toggle) => (
          <button className={clsx("icon-btn sm open-local-pick", isOpen && "active")} aria-label="Open in another editor" data-tip="Open in another editor" onClick={toggle}>
            <ChevronDown size={11} />
          </button>
        )}
      >
        {(close) => (
          <>
            <div className="pick-head">Open in</div>
            {!local && <div className="open-local-note">Atrium runs on {location.hostname}: an editor here opens this path on your computer; over SSH, on that machine.</div>}
            {EDITORS.map((e) => (
              <button key={e.id} className={clsx("menu-item", e.id === choice && "on")} onClick={() => (close(), open(e.id))}>
                {e.label}
                {e.ssh && host && <span className="faint">{host}</span>}
              </button>
            ))}
            {host && (
              <button className="menu-item faint" onClick={() => (localStorage.removeItem(HOST_KEY), close())}>
                Forget the SSH host
              </button>
            )}
          </>
        )}
      </Popover>
    </span>
  );
}
