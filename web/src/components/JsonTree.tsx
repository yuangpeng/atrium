import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { clsx } from "../util";

const LONG = 600; // characters of a string shown before "more"
const MANY = 200; // children of an object or array shown before "more"

/** JSON as a tree that opens a level at a time: big bodies (a whole conversation) stay cheap until opened. Long
 *  strings keep their line breaks and wrap, so nothing scrolls sideways. */
export function JsonTree({ value, open = 1 }: { value: unknown; open?: number }) {
  return (
    <div className="jt">
      <JsonNode name={null} value={value} depth={0} open={open} />
    </div>
  );
}

function brief(v: unknown): string {
  if (Array.isArray(v)) return `[${v.length}]`;
  if (v && typeof v === "object") {
    const keys = Object.keys(v);
    return `{${keys.slice(0, 4).join(", ")}${keys.length > 4 ? ", …" : ""}}`;
  }
  return "";
}

function JsonNode({ name, value, depth, open }: { name: string | number | null; value: unknown; depth: number; open: number }) {
  const [expanded, setExpanded] = useState(depth < open);
  const [all, setAll] = useState(false);
  const label = name === null ? null : <span className="jt-key">{typeof name === "number" ? name : JSON.stringify(name).slice(1, -1)}</span>;
  if (value === null || typeof value !== "object") {
    return (
      <div className="jt-leaf">
        {label}
        {label && <span className="jt-colon">: </span>}
        <Scalar value={value} />
      </div>
    );
  }
  const entries: [string | number, unknown][] = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
  if (!entries.length) {
    return (
      <div className="jt-leaf">
        {label}
        {label && <span className="jt-colon">: </span>}
        <span className="jt-null">{Array.isArray(value) ? "[]" : "{}"}</span>
      </div>
    );
  }
  const shown = all ? entries : entries.slice(0, MANY);
  return (
    <div className="jt-node">
      <button className="jt-toggle" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
        <ChevronRight size={12} className={clsx("jt-caret", expanded && "open")} />
        {label}
        <span className="jt-brief">{brief(value)}</span>
      </button>
      {expanded && (
        <div className="jt-children">
          {shown.map(([k, v]) => (
            <JsonNode key={k} name={k} value={v} depth={depth + 1} open={open} />
          ))}
          {shown.length < entries.length && (
            <button className="jt-more" onClick={() => setAll(true)}>
              {entries.length - shown.length} more…
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function Scalar({ value }: { value: unknown }) {
  const [all, setAll] = useState(false);
  if (typeof value !== "string") return <span className={value === null ? "jt-null" : "jt-num"}>{String(value)}</span>;
  if (value.length <= LONG || all) return <span className="jt-str">"{value}"</span>;
  return (
    <span className="jt-str">
      "{value.slice(0, LONG)}…"
      <button className="jt-more inline" onClick={() => setAll(true)}>
        all {value.length.toLocaleString()} chars
      </button>
    </span>
  );
}

/** Text that may be megabytes long: its start, and the rest on request. */
export function LongText({ text, limit = 20000, className }: { text: string; limit?: number; className?: string }) {
  const [all, setAll] = useState(false);
  const cut = !all && text.length > limit;
  return (
    <div className={clsx("long-text", className)}>
      {cut ? text.slice(0, limit) : text}
      {cut && (
        <button className="jt-more" onClick={() => setAll(true)}>
          Show all ({text.length.toLocaleString()} chars)
        </button>
      )}
    </div>
  );
}
