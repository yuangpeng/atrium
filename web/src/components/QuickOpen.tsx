// Quick open (⌘P): a file of the checkout by a few letters of its path, as in VS Code — `name:12` opens it at a line,
// `:12` goes to a line of the file shown. Over the whole app (a portal), from the Files panel, which is mounted
// whenever a project is open, so ⌘P works from the chat too.
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CornerDownLeft, File, FileSearch } from "lucide-react";
import { api } from "../api";
import { rank, splitLine } from "../fuzzy";
import { basename, clsx } from "../util";
import { Spinner, useFullscreenElement } from "./ui";

const SHOWN = 60; // results listed
// a checkout's file list, kept while nothing in the project changes (fsVersion): ⌘P again is instant
const lists = new Map<string, { version: number; files: string[]; truncated: boolean }>();

export interface QuickOpenProps {
  pid: string;
  root: string;
  rootLabel: string;
  fsVersion: number;
  recent: string[]; // paths (relative to root) opened lately, the latest first
  current: string | null; // the file shown, for `:12`
  onPick: (path: string, line?: number, column?: number) => void;
  onClose: () => void;
}

/** The path with the matched letters marked. */
function Marked({ text, hits, offset }: { text: string; hits: Set<number>; offset: number }) {
  const out: React.ReactNode[] = [];
  let run = "";
  let on = false;
  const push = () => run && out.push(on ? <b key={out.length}>{run}</b> : run);
  for (let i = 0; i < text.length; i++) {
    const hit = hits.has(i + offset);
    if (hit !== on) {
      push();
      run = "";
      on = hit;
    }
    run += text[i];
  }
  push();
  return <>{out}</>;
}

export function QuickOpen({ pid, root, rootLabel, fsVersion, recent, current, onPick, onClose }: QuickOpenProps) {
  const key = `${pid}::${root}`;
  const cached = lists.get(key);
  const [files, setFiles] = useState<string[] | null>(cached?.files ?? null);
  const [truncated, setTruncated] = useState(!!cached?.truncated);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [sel, setSel] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const fullscreen = useFullscreenElement();

  useEffect(() => {
    if (cached && cached.version === fsVersion) return;
    let alive = true;
    api.allFiles(pid, root).then(
      (r) => {
        lists.set(key, { version: fsVersion, files: r.files, truncated: r.truncated });
        if (!alive) return;
        setFiles(r.files);
        setTruncated(r.truncated);
      },
      (e: Error) => alive && setError(e.message),
    );
    return () => void (alive = false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, fsVersion]);

  // ranked a beat behind the typing: on a phone, a big repository's list takes longer than a keystroke
  const typed = useDeferredValue(text);
  const rankFor = (t: string) => {
    const { query } = splitLine(t);
    if (!query) return recent.map((path) => ({ path, match: { score: 0, hits: [] as number[] } }));
    // recently opened files come first among those that match about as well
    const order = new Map(recent.map((p, i) => [p, i]));
    return rank(query, files ?? recent, SHOWN, (p) => (order.has(p) ? 6 - Math.min(5, order.get(p)!) : 0));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const shown = useMemo(() => rankFor(typed), [typed, files, recent]);
  const { query, line, column } = splitLine(text);
  const goLine = !query && line != null && current; // `:12`: a line of the file shown
  const results = goLine ? [] : shown;

  useEffect(() => setSel(0), [text]);
  // (braces: scrollIntoView returns a promise in newer browsers, which React would take for the effect's cleanup)
  useEffect(() => {
    listRef.current?.querySelector(".qo-row.on")?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const pick = (i: number) => {
    if (goLine) return onPick(current, line, column);
    // Enter right after a keystroke: the list may be a beat behind, so the text as it is now decides
    const r = (typed === text ? results : rankFor(text))[i];
    if (r) onPick(r.path, line, column);
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) {
      e.preventDefault();
      setSel((s) => (results.length ? (s + 1) % results.length : 0));
    } else if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) {
      e.preventDefault();
      setSel((s) => (results.length ? (s - 1 + results.length) % results.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      pick(sel);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation(); // not the full screen's Esc
      onClose();
    }
  };

  const note = error
    ? error
    : goLine
      ? null
      : !query
        ? recent.length ? null : "Type part of a file's name or path · name:12 opens it at line 12"
        : files === null
          ? null
          : !results.length
            ? "No file matches"
            : truncated
              ? `Only the first ${files.length.toLocaleString()} files are looked at`
              : null;

  return createPortal(
    <div className="qo-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="qo-box" role="dialog" aria-label="Go to file">
        <div className="qo-input">
          <FileSearch size={15} className="faint" />
          <input
            autoFocus
            value={text}
            placeholder={`Go to file in ${rootLabel}…`}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            aria-label="File name"
          />
          {files === null && !error && <Spinner />}
        </div>
        <div className="qo-list" ref={listRef}>
          {goLine ? (
            <div className="qo-row on" onClick={() => pick(0)}>
              <CornerDownLeft size={13} className="faint" />
              <span className="qo-name">
                Go to line {line}
                {column ? `, column ${column}` : ""}
              </span>
              <span className="qo-dir">{basename(current)}</span>
            </div>
          ) : (
            results.map((r, i) => {
              const name = r.path.lastIndexOf("/") + 1;
              const hits = new Set(r.match.hits);
              return (
                <div key={r.path} className={clsx("qo-row", i === sel && "on")} onClick={() => pick(i)} onMouseMove={() => i !== sel && setSel(i)} title={r.path}>
                  <File size={13} className="faint" />
                  <span className="qo-name">
                    <Marked text={r.path.slice(name)} hits={hits} offset={name} />
                  </span>
                  {name > 0 && (
                    <span className="qo-dir">
                      <Marked text={r.path.slice(0, name - 1)} hits={hits} offset={0} />
                    </span>
                  )}
                  {!query && <span className="qo-tag">recent</span>}
                </div>
              );
            })
          )}
          {note && <div className="qo-note">{note}</div>}
        </div>
      </div>
    </div>,
    fullscreen ?? document.body,
  );
}
