// How diffs are shown (Changes and Git share it), remembered across reloads. No Monaco here:
// the toggles live in the main bundle, the lazy DiffEditor reads the same store.
import type { ReactNode } from "react";
import { Columns2, FoldVertical, Rows2, Space, WrapText } from "lucide-react";
import { create } from "zustand";
import { useLayout } from "../layout";
import { clsx } from "../util";

export interface DiffPrefs {
  sideBySide: boolean;
  hideUnchanged: boolean;
  ignoreWhitespace: boolean;
  wrap: boolean;
}

const KEY = "atrium.diff";
const DEFAULTS: DiffPrefs = { sideBySide: true, hideUnchanged: true, ignoreWhitespace: false, wrap: false };

function load(): DiffPrefs {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch {
    return DEFAULTS;
  }
}

export const useDiffPrefs = create<DiffPrefs & { update: (p: Partial<DiffPrefs>) => void }>((set, get) => ({
  ...load(),
  update: (p) => {
    set(p);
    const { sideBySide, hideUnchanged, ignoreWhitespace, wrap } = get();
    localStorage.setItem(KEY, JSON.stringify({ sideBySide, hideUnchanged, ignoreWhitespace, wrap }));
  },
}));

/** Side-by-side / inline, plus collapse-unchanged, ignore-whitespace and wrap toggles. */
export function DiffOptions() {
  const p = useDiffPrefs();
  const single = useLayout() === "single"; // there, a diff is always inline: the choice would do nothing
  const toggle = (key: "hideUnchanged" | "ignoreWhitespace" | "wrap", title: string, icon: ReactNode) => (
    <button className={clsx("icon-btn", p[key] && "active")} title={title} aria-pressed={p[key]} onClick={() => p.update({ [key]: !p[key] })}>
      {icon}
    </button>
  );
  return (
    <>
      {!single && (
        <div className="seg">
          <button className={clsx(p.sideBySide && "on")} title="Side by side: before on the left, after on the right" onClick={() => p.update({ sideBySide: true })}>
            <Columns2 size={14} />
          </button>
          <button className={clsx(!p.sideBySide && "on")} title="Inline: one column, removed lines above added ones" onClick={() => p.update({ sideBySide: false })}>
            <Rows2 size={14} />
          </button>
        </div>
      )}
      {toggle("hideUnchanged", "Collapse unchanged lines", <FoldVertical size={15} />)}
      {toggle("ignoreWhitespace", "Ignore leading / trailing whitespace changes", <Space size={15} />)}
      {toggle("wrap", "Wrap long lines", <WrapText size={15} />)}
    </>
  );
}
