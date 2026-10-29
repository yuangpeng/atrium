// Slash commands in the composer: "/" at the start of a message opens a menu of what the harness offers
// (its built-ins, custom commands and skills; "$" before a Codex skill opens it too). Enter runs the
// highlighted command, Tab completes it to add arguments. The harness runs what is sent; Atrium's own
// commands (useAtriumCommands) never leave the browser.
import type { ReactNode, RefObject } from "react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, SlashCommand } from "../api";
import { useApp } from "../store";
import { useDrafts } from "../drafts";
import { clsx } from "../util";
import { Spinner } from "../components/ui";
import { effortLabel } from "./Composer";

const TTL = 60_000;
const cache = new Map<string, { at: number; list: Promise<SlashCommand[]> }>();

/** The harness's commands for a session (`sid`) or a new agent (`harness` in `cwd`): fetched once `wanted`,
 *  then kept a minute. null while loading. */
export function useSlashCommands(pid: string, where: { sid?: string; harness?: string; cwd?: string }, wanted: boolean): SlashCommand[] | null {
  const key = JSON.stringify([pid, where.sid, where.harness, where.cwd]);
  const [got, setGot] = useState<{ key: string; list: SlashCommand[] } | null>(null);
  useEffect(() => {
    if (!wanted) return;
    let hit = cache.get(key);
    if (!hit || Date.now() - hit.at > TTL) {
      const list = api.commands(pid, where).catch(() => {
        cache.delete(key); // try again next time
        return [] as SlashCommand[];
      });
      cache.set(key, (hit = { at: Date.now(), list }));
    }
    let alive = true;
    void hit.list.then((list) => alive && setGot({ key, list }));
    return () => {
      alive = false;
    };
  }, [key, wanted]); // `where` is in `key`
  return got?.key === key ? got.list : null;
}

// /clear and its aliases switch Claude Code to another session behind Atrium's back: Atrium's /new stands in
const RESET = ["clear", "reset", "new"];

/** Atrium's commands first, then the harness's, minus the ones Atrium's replace. */
export function mergeCommands(atrium: SlashCommand[], harness: SlashCommand[] | null): SlashCommand[] | null {
  if (!harness) return null;
  const taken = new Set([...RESET, ...atrium.flatMap((c) => [c.name, ...c.aliases])]);
  return [...atrium, ...harness.filter((c) => c.prefix !== "/" || (!taken.has(c.name) && !c.aliases.some((a) => taken.has(a))))];
}

/** How well a command matches what was typed after its prefix (lower is better), null for not at all. */
function rank(c: SlashCommand, q: string): number | null {
  if (!q) return 0;
  const name = c.name.toLowerCase();
  if (name.startsWith(q)) return 0;
  if (c.aliases.some((a) => a.toLowerCase().startsWith(q))) return 1;
  if (name.includes(q)) return 2;
  if (c.description.toLowerCase().includes(q)) return 3;
  return null;
}

/** The word being typed at the caret, if it calls for the menu: a "/command" opening the message, or a "$skill". */
function tokenAt(text: string, caret: number, skills: boolean) {
  const before = text.slice(0, caret);
  const end = caret + (/^\S*/.exec(text.slice(caret))?.[0].length ?? 0);
  const slash = /^\/(\S*)$/.exec(before);
  if (slash) return { prefix: "/", query: slash[1]!.toLowerCase(), start: 0, end };
  const dollar = skills ? /(?:^|\s)\$([\w.:-]*)$/.exec(before) : null;
  if (dollar) return { prefix: "$", query: dollar[1]!.toLowerCase(), start: caret - dollar[1]!.length - 1, end };
  return null;
}

// "<model>" must be given; "[name]" and "<optional …>" may be
const needsArgs = (c: SlashCommand) => c.argument_hint.startsWith("<") && !/optional/i.test(c.argument_hint);

/** The menu for a composer's textarea: render `menu` inside the composer box, and give the textarea
 *  `onKeyDown` first (true = it took the key) and `onSelect`. `onRun` sends a command right away. */
export function useSlashMenu({ text, setText, input, commands, onRun, skills = false }: {
  text: string;
  setText: (t: string) => void;
  input: RefObject<HTMLTextAreaElement | null>;
  commands: SlashCommand[] | null;
  onRun: (text: string) => void;
  skills?: boolean; // "$name" mentions (Codex)
}) {
  const [caret, setCaret] = useState(0);
  const [dismissed, setDismissed] = useState<string | null>(null); // the word Escape closed the menu on
  const [sel, setSel] = useState(-1);
  const list = useRef<HTMLDivElement>(null);
  const placeCaret = useRef<number | null>(null); // where a completion leaves the caret, once the text is in
  const token = tokenAt(text, Math.min(caret, text.length), skills);
  const word = token ? text.slice(token.start, token.end) : null;
  const matches = useMemo(() => {
    if (!token || !commands) return [];
    return commands
      .filter((c) => token.prefix === "/" || c.prefix === "$")
      .map((c) => [c, rank(c, token.query)] as const)
      .filter((x): x is readonly [SlashCommand, number] => x[1] !== null)
      .sort((a, b) => a[1] - b[1] || (token.query ? a[0].name.localeCompare(b[0].name) : 0)) // a bare "/": the given order
      .map(([c]) => c);
  }, [commands, token?.prefix, token?.query]);
  const open = !!token && word !== dismissed && (commands === null || matches.length > 0);

  // the caret after each edit, typed or not (select events only follow keys and clicks)
  useLayoutEffect(() => {
    const el = input.current;
    if (el && placeCaret.current !== null) el.setSelectionRange(placeCaret.current, placeCaret.current);
    placeCaret.current = null;
    setCaret(el?.selectionStart ?? text.length);
  }, [text, input]);
  // nothing is highlighted on a bare "/" (Enter there shouldn't run the first command); typing picks the best match
  useEffect(() => setSel(token?.query ? 0 : -1), [token?.query, token?.prefix, commands === null]);
  useLayoutEffect(() => {
    if (sel >= 0) list.current?.children[sel]?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const complete = (c: SlashCommand) => {
    if (!token) return;
    const head = text.slice(0, token.start) + c.prefix + c.name + " ";
    const next = head + text.slice(token.end).replace(/^ /, "");
    input.current?.focus();
    if (next !== text) {
      placeCaret.current = head.length;
      setText(next);
    } else {
      input.current?.setSelectionRange(head.length, head.length);
      setCaret(head.length);
    }
  };
  const accept = (c: SlashCommand) => {
    if (!token) return;
    if (c.prefix !== "/" || needsArgs(c)) return complete(c);
    onRun(`/${c.name}${text.slice(token.end)}`.trim());
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!open || !matches.length || e.nativeEvent.isComposing) return false;
    const n = matches.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      setSel((s) => (e.key === "ArrowDown" ? (s + 1) % n : s <= 0 ? n - 1 : s - 1));
    } else if (e.key === "Tab" && !e.shiftKey) {
      complete(matches[Math.max(sel, 0)]!);
    } else if (e.key === "Enter" && !e.shiftKey) {
      if (sel >= 0) accept(matches[sel]!);
    } else if (e.key === "Escape") {
      setDismissed(word);
    } else {
      return false;
    }
    e.preventDefault();
    return true;
  };
  const onSelect = (e: React.SyntheticEvent<HTMLTextAreaElement>) => setCaret(e.currentTarget.selectionStart);

  const menu: ReactNode = open ? (
    <div className="slash-menu" role="listbox" aria-label="Commands" ref={list}>
      {commands === null ? (
        <div className="slash-empty">
          <Spinner /> Loading commands…
        </div>
      ) : (
        matches.map((c, i) => (
          <button
            key={c.prefix + c.name}
            role="option"
            aria-selected={i === sel}
            className={clsx("slash-item", i === sel && "on")}
            onMouseEnter={() => setSel(i)}
            onMouseDown={(e) => e.preventDefault()} // keep the caret in the textarea
            onClick={() => complete(c)}
          >
            <div className="slash-line">
              <span className="slash-name">
                {c.prefix}
                {c.name}
              </span>
              {c.argument_hint && <span className="slash-hint">{c.argument_hint}</span>}
              {c.source && <span className="slash-src">{c.source}</span>}
            </div>
            {c.description && <div className="slash-desc">{c.description}</div>}
          </button>
        ))
      )}
    </div>
  ) : null;
  return { menu, onKeyDown, onSelect };
}

const atrium = (name: string, description: string, argument_hint = "", aliases: string[] = []): SlashCommand => ({
  name, description, argument_hint, aliases, source: "Atrium", prefix: "/",
});

/** Atrium's own commands in an agent's chat. They act on Atrium (its new-agent page, name, fork dialog,
 *  effort setting), which the harness's commands of the same name would bypass. `run` is true when it took the text. */
export function useAtriumCommands({ pid, sid, harness, cwd, running, efforts, effort, setEffort }: {
  pid: string;
  sid: string;
  harness: string | undefined;
  cwd: string;
  running: boolean;
  efforts: string[];
  effort: string; // what applies now
  setEffort: (e: string) => Promise<boolean>; // false: it failed (and said so)
}) {
  const project = useApp((s) => s.project);
  const toast = useApp((s) => s.toast);
  const selectSession = useApp((s) => s.selectSession);
  const setForkRequest = useApp((s) => s.setForkRequest);
  const refreshSessions = useApp((s) => s.refreshSessions);
  const updateDraft = useDrafts((s) => s.update);
  const levels = [...efforts, "auto"].join(", ");
  const commands = useMemo(
    () => [
      atrium("new", "Start a new agent, here and with this harness", "", ["clear", "reset"]),
      atrium("rename", "Rename this agent", "<name>", ["name"]),
      atrium("fork", "Fork this conversation into a new agent"),
      atrium("effort", `Set the reasoning effort: ${levels}`, "[level]"),
    ],
    [levels],
  );

  const run = (text: string): boolean => {
    const m = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text.trim());
    const c = m && commands.find((x) => x.name === m[1] || x.aliases.includes(m[1]!));
    if (!c) return false;
    const args = (m![2] ?? "").trim();
    if (c.name === "new") {
      const wt = project?.worktrees.find((w) => !w.main && w.path === cwd);
      updateDraft(pid, (d) => ({
        ...d,
        ...(harness && d.harness !== harness ? { harness, mode: undefined, model: undefined, effort: undefined } : {}),
        ws: wt ? { kind: "worktree", path: wt.path, branch: wt.branch } : { kind: "root" },
      }));
      selectSession(null);
    } else if (c.name === "rename") {
      if (!args) toast("Usage: /rename <name>", "error");
      else
        void api
          .patchSession(pid, sid, { name: args })
          .then(() => (void refreshSessions(), toast(`Renamed to “${args}”`, "ok")))
          .catch((e) => toast(e.message || String(e), "error"));
    } else if (c.name === "fork") {
      if (running) toast("Fork once the turn ends (or stop it); or fork from an earlier message", "error");
      else setForkRequest({ sid, at: null });
    } else {
      const level = args.toLowerCase();
      if (!level) toast(`Reasoning effort: ${effort ? effortLabel(effort) : "default"}`);
      else if (level === "auto" || level === "default") void setEffort("").then((ok) => ok && toast("Reasoning effort: from config", "ok"));
      else if (efforts.includes(level)) void setEffort(level).then((ok) => ok && toast(`Reasoning effort: ${effortLabel(level)}`, "ok"));
      else toast(`Effort levels: ${levels}`, "error");
    }
    return true;
  };
  return { commands, run };
}
