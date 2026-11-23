// Find in Files (⌘⇧F): the Files panel's side shows this instead of the tree. Searches as the query is typed
// (search.py: ripgrep, git grep or Python on the server); a result opens its file at the match.
import { useEffect, useMemo, useRef, useState } from "react";
import { CaseSensitive, ChevronDown, ChevronRight, EyeOff, File, ListFilter, Regex, WholeWord, X } from "lucide-react";
import { api, SearchResults } from "../api";
import { clsx } from "../util";
import { Spinner } from "./ui";

const DEBOUNCE_MS = 300;
// one id per page: the server stops this page's last search when its next one starts
const PAGE = Math.random().toString(36).slice(2);

export interface SearchOptions {
  query: string;
  case: boolean;
  word: boolean;
  regex: boolean;
  include: string;
  exclude: string;
  ignored: boolean;
}

export const NO_SEARCH: SearchOptions = { query: "", case: false, word: false, regex: false, include: "", exclude: "", ignored: false };

/** A line with its matches marked. */
function Line({ text, ranges }: { text: string; ranges: number[][] }) {
  const out: React.ReactNode[] = [];
  let at = 0;
  for (const [s, e] of ranges) {
    if (s < at) continue;
    if (s > at) out.push(text.slice(at, s));
    out.push(<mark key={s}>{text.slice(s, e)}</mark>);
    at = e;
  }
  out.push(text.slice(at));
  return <>{out}</>;
}

export function SearchView({
  pid,
  root,
  opts,
  setOpts,
  focus,
  onOpen,
}: {
  pid: string;
  root: string;
  opts: SearchOptions;
  setOpts: (o: SearchOptions) => void;
  focus: number; // changes when ⌘⇧F asks for the field
  onOpen: (path: string, line: number, column: number, endColumn: number) => void;
}) {
  const [res, setRes] = useState<SearchResults | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [filters, setFilters] = useState(!!(opts.include || opts.exclude));
  const input = useRef<HTMLInputElement>(null);
  const seq = useRef(0);
  const set = (p: Partial<SearchOptions>) => setOpts({ ...opts, ...p });

  useEffect(() => {
    if (!focus) return;
    input.current?.focus();
    input.current?.select();
  }, [focus]);

  // as it is typed, once it rests; every option change searches again
  const ask = useMemo(() => JSON.stringify({ ...opts, root }), [opts, root]);
  useEffect(() => {
    const n = ++seq.current;
    if (!opts.query) {
      setRes(null);
      setBusy(false);
      setFailed(null);
      return;
    }
    setBusy(true);
    const t = window.setTimeout(() => {
      api.search(pid, { ...opts, root, page: PAGE, limit: 2000 }).then(
        (r) => {
          if (n !== seq.current) return;
          setRes(r);
          setFailed(null);
          setFolded(new Set());
          setBusy(false);
        },
        (e: Error) => {
          if (n !== seq.current) return;
          setFailed(e.message);
          setBusy(false);
        },
      );
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ask, pid]);

  const files = res?.files ?? [];
  const toggle = (path: string) =>
    setFolded((f) => {
      const n = new Set(f);
      if (n.has(path)) n.delete(path);
      else n.add(path);
      return n;
    });
  const summary = failed
    ? failed
    : res?.error
      ? res.error
      : res && opts.query
        ? res.total === 0
          ? "No results"
          : `${(res.total + res.more).toLocaleString()} result${res.total + res.more === 1 ? "" : "s"} in ${files.length.toLocaleString()} file${files.length === 1 ? "" : "s"}` +
            (res.truncated ? ` — the first ${res.total.toLocaleString()} shown` : "")
        : null;

  return (
    <div className="search-view">
      <div className="search-box">
        <div className="search-field">
          <input
            ref={input}
            className="input"
            value={opts.query}
            placeholder="Search"
            aria-label="Search in files"
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            onChange={(e) => set({ query: e.target.value })}
            onKeyDown={(e) => e.key === "Escape" && opts.query && (e.stopPropagation(), set({ query: "" }))}
          />
          {opts.query && (
            <button className="icon-btn sm search-clear" aria-label="Clear" data-tip="Clear" onClick={() => (set({ query: "" }), input.current?.focus())}>
              <X size={12} />
            </button>
          )}
        </div>
        <div className="search-opts">
          <button className={clsx("icon-btn sm", opts.case && "active")} aria-pressed={opts.case} aria-label="Match case" data-tip="Match case" onClick={() => set({ case: !opts.case })}>
            <CaseSensitive size={15} />
          </button>
          <button className={clsx("icon-btn sm", opts.word && "active")} aria-pressed={opts.word} aria-label="Whole word" data-tip="Whole word" onClick={() => set({ word: !opts.word })}>
            <WholeWord size={15} />
          </button>
          <button className={clsx("icon-btn sm", opts.regex && "active")} aria-pressed={opts.regex} aria-label="Regular expression" data-tip="Regular expression" onClick={() => set({ regex: !opts.regex })}>
            <Regex size={14} />
          </button>
          <button
            className={clsx("icon-btn sm", opts.ignored && "active")}
            aria-pressed={opts.ignored}
            aria-label="Include ignored files"
            data-tip={opts.ignored ? "Searching what .gitignore leaves out too (node_modules, build output…)" : "Include what .gitignore leaves out (node_modules, build output…)"}
            onClick={() => set({ ignored: !opts.ignored })}
          >
            <EyeOff size={13} />
          </button>
          <span className="grow" />
          <button className={clsx("icon-btn sm", (filters || opts.include || opts.exclude) && "active")} aria-pressed={filters} aria-label="Files to include or exclude" data-tip="Files to include / exclude" onClick={() => setFilters((v) => !v)}>
            <ListFilter size={13} />
          </button>
        </div>
        {filters && (
          <>
            <input className="input" value={opts.include} placeholder="Files to include (e.g. src, *.py)" aria-label="Files to include" spellCheck={false} autoCapitalize="off" onChange={(e) => set({ include: e.target.value })} />
            <input className="input" value={opts.exclude} placeholder="Files to exclude (e.g. tests, *.min.js)" aria-label="Files to exclude" spellCheck={false} autoCapitalize="off" onChange={(e) => set({ exclude: e.target.value })} />
          </>
        )}
        {(summary || busy) && (
          <div className={clsx("search-summary", (failed || res?.error) && "err")} title={res ? `${res.engine} · ${res.elapsed}s` : undefined}>
            {busy && <Spinner />}
            <span className="ellipsis">{summary ?? "Searching…"}</span>
          </div>
        )}
      </div>
      <div className="search-results">
        {files.map((f) => {
          const shut = folded.has(f.path);
          const slash = f.path.lastIndexOf("/");
          return (
            <div key={f.path} className="search-file">
              <div className="search-file-head" onClick={() => toggle(f.path)} title={f.path}>
                {shut ? <ChevronRight size={13} className="faint" /> : <ChevronDown size={13} className="faint" />}
                <File size={13} className="faint" />
                <span className="nm">{f.path.slice(slash + 1)}</span>
                {slash > 0 && <span className="dir">{f.path.slice(0, slash)}</span>}
                <span className="n">{f.matches.length + f.more}</span>
              </div>
              {!shut &&
                f.matches.map((m, i) => (
                  <div
                    key={i}
                    className="search-hit"
                    onClick={() => {
                      const r = m.ranges[0];
                      onOpen(f.path, m.line, m.col, m.col + (r ? r[1] - r[0] : 0));
                    }}
                  >
                    <span className="ln">{m.line}</span>
                    <span className="tx">
                      <Line text={m.text} ranges={m.ranges} />
                    </span>
                  </div>
                ))}
              {!shut && f.more > 0 && <div className="search-more">+{f.more.toLocaleString()} more in this file</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
