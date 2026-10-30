// Whether the editors take typing: Files, and the editable side of a diff in Changes. On a touch screen they start
// locked — a tap meant to scroll or select would otherwise focus the editor, bring up the keyboard and type into
// the file. Not remembered: every visit starts that way. No Monaco here (the button lives in the main bundle).
import { Lock, LockOpen } from "lucide-react";
import { create } from "zustand";
import { clsx } from "../util";

export const useEditLock = create<{ locked: boolean; setLocked: (v: boolean) => void }>((set) => ({
  locked: matchMedia("(pointer: coarse)").matches,
  setLocked: (locked) => set({ locked }),
}));

/** Locks the editors (read-only) or unlocks them. */
export function LockToggle() {
  const { locked, setLocked } = useEditLock();
  return (
    <button
      className={clsx("icon-btn sm lock-toggle", !locked && "active")}
      aria-pressed={!locked}
      aria-label={locked ? "Unlock to edit" : "Lock (read-only)"}
      data-tip={locked ? "Read-only — unlock to edit" : "Editing — lock to stop stray edits"}
      onClick={() => setLocked(!locked)}
    >
      {locked ? <Lock size={14} /> : <LockOpen size={14} />}
    </button>
  );
}
