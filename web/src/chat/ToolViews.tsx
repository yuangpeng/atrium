import type { JSX } from "react";
import { useMemo, useState } from "react";
import {
  Bot, Check, ChevronDown, ChevronRight, ClipboardCheck, ExternalLink, FileDiff, FilePlus2, FileText, GitBranch, Globe, Image, LayoutDashboard,
  Link2, ListTodo, Pencil, Plug, Search, ShieldAlert, Terminal, X,
} from "lucide-react";
import { Item } from "../api";
import { useApp } from "../store";
import { relTo } from "../util";
import { Markdown, Spinner } from "../components/ui";

type ToolItem = Extract<Item, { kind: "tool" }>;
type ResultItem = Extract<Item, { kind: "tool_result" }>;
type PermItem = Extract<Item, { kind: "permission" }>;

// ------------------------------------------------------------------ line diff
type DiffLine = { t: "ctx" | "add" | "del"; s: string };

export function lineDiff(a: string, b: string): DiffLine[] {
  const A = a.split("\n");
  const B = b.split("\n");
  if (A.length * B.length > 250_000) return [...A.map((s) => ({ t: "del" as const, s })), ...B.map((s) => ({ t: "add" as const, s }))];
  const n = A.length, m = B.length;
  const dp: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) (out.push({ t: "ctx", s: A[i] }), i++, j++);
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ t: "del", s: A[i++] });
    else out.push({ t: "add", s: B[j++] });
  }
  while (i < n) out.push({ t: "del", s: A[i++] });
  while (j < m) out.push({ t: "add", s: B[j++] });
  return out;
}

export function MiniDiff({ before, after, context = 3 }: { before: string; after: string; context?: number }) {
  const lines = useMemo(() => {
    const d = lineDiff(before, after);
    // collapse long unchanged stretches
    const keep = d.map((l, i) => l.t !== "ctx" || d.slice(Math.max(0, i - context), i + context + 1).some((x) => x.t !== "ctx"));
    const out: (DiffLine | { t: "hunk"; s: string })[] = [];
    let skipped = 0;
    d.forEach((l, i) => {
      if (keep[i]) {
        if (skipped) out.push({ t: "hunk", s: `… ${skipped} unchanged lines` });
        skipped = 0;
        out.push(l);
      } else skipped++;
    });
    if (skipped) out.push({ t: "hunk", s: `… ${skipped} unchanged lines` });
    return out;
  }, [before, after, context]);
  return (
    <pre className="mini-diff">
      {lines.map((l, i) => (
        <div key={i} className={`l ${l.t}`}>
          {l.t === "add" ? "+ " : l.t === "del" ? "- " : l.t === "hunk" ? "" : "  "}
          {l.s}
        </div>
      ))}
    </pre>
  );
}

/** Unified-diff hunks as recorded by Codex patches; whole-file adds/deletes carry the raw content. */
export function PatchDiff({ diff, kind = "update" }: { diff: string; kind?: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  if (kind === "add" || kind === "delete")
    return (
      <pre className="mini-diff">
        {lines.map((l, i) => (
          <div key={i} className={`l ${kind === "add" ? "add" : "del"}`}>
            {kind === "add" ? "+ " : "- "}
            {l}
          </div>
        ))}
      </pre>
    );
  return (
    <pre className="mini-diff">
      {lines.map((l, i) => {
        const t = l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "ctx";
        return (
          <div key={i} className={`l ${t}`}>
            {t === "add" ? "+ " : t === "del" ? "- " : t === "hunk" ? "" : "  "}
            {t === "hunk" ? l : l.slice(1)}
          </div>
        );
      })}
    </pre>
  );
}

type PatchChange = { path: string; kind: string; move_path?: string | null; diff: string };

function patchStats(changes: PatchChange[]): [number, number] {
  let a = 0, r = 0;
  for (const c of changes) {
    const n = (c.diff || "").replace(/\n$/, "").split("\n").length;
    if (c.kind === "add") (a += c.diff ? n : 0);
    else if (c.kind === "delete") (r += c.diff ? n : 0);
    else for (const l of (c.diff || "").split("\n")) {
      if (l.startsWith("+") && !l.startsWith("+++")) a++;
      else if (l.startsWith("-") && !l.startsWith("---")) r++;
    }
  }
  return [a, r];
}

function PatchView({ changes, cwd }: { changes: PatchChange[]; cwd: string }) {
  const openFile = useApp((s) => s.openFile);
  return (
    <>
      {changes.map((c, i) => (
        <div key={i} className="patch-file">
          <div className="row" style={{ gap: 6, fontSize: 12.5 }}>
            <span className={`pill ${c.kind === "add" ? "ok" : c.kind === "delete" ? "err" : ""}`}>{c.kind}</span>
            <button className="btn btn-sm btn-ghost mono" onClick={() => openFile(c.path, cwd)} title="Open file">
              {relTo(cwd, c.path)}
              {c.move_path ? ` → ${relTo(cwd, c.move_path)}` : ""}
            </button>
          </div>
          {c.diff ? <PatchDiff diff={c.diff} kind={c.kind} /> : c.kind === "delete" ? <div className="muted" style={{ fontSize: 12.5 }}>File deleted</div> : null}
        </div>
      ))}
    </>
  );
}

export const isBoardTool = (name: string) => name.startsWith("mcp__board__") || name.startsWith("mcp__atrium_board__");

export function diffStats(before: string, after: string): [number, number] {
  let a = 0, r = 0;
  for (const l of lineDiff(before, after)) l.t === "add" ? a++ : l.t === "del" ? r++ : 0;
  return [a, r];
}

// ------------------------------------------------------------------ tool meta
export function toolMeta(name: string, input: Record<string, any>, cwd: string): { icon: JSX.Element; label: string; brief: string } {
  const p = (x?: string) => (x ? relTo(cwd, x) : "");
  const s = 14;
  switch (name) {
    case "Read":
      return { icon: <FileText size={s} />, label: "Read", brief: p(input.file_path) + (input.offset ? `:${input.offset}` : "") };
    case "Edit":
    case "MultiEdit": {
      const [a, r] = input.old_string !== undefined ? diffStats(input.old_string ?? "", input.new_string ?? "") : [0, 0];
      return { icon: <Pencil size={s} />, label: "Edit", brief: `${p(input.file_path)}  +${a} −${r}` };
    }
    case "Write":
      return { icon: <FilePlus2 size={s} />, label: "Write", brief: `${p(input.file_path)}  ${String(input.content ?? "").split("\n").length} lines` };
    case "NotebookEdit":
      return { icon: <Pencil size={s} />, label: "Notebook", brief: p(input.notebook_path) };
    case "Bash":
      return { icon: <Terminal size={s} />, label: "Bash", brief: input.description || input.command };
    case "Grep":
      return { icon: <Search size={s} />, label: "Grep", brief: `${input.pattern}${input.path ? "  in " + p(input.path) : ""}` };
    case "Glob":
      return { icon: <Search size={s} />, label: "Glob", brief: input.pattern };
    case "Patch": {
      const changes: PatchChange[] = input.changes || [];
      const [a, r] = patchStats(changes);
      const files = changes.length === 1 ? p(changes[0].path) : `${changes.length} files`;
      return { icon: <FileDiff size={s} />, label: changes.every((c) => c.kind === "add") ? "Create" : "Patch", brief: `${files}  +${a} −${r}` };
    }
    case "WebSearch":
    case "mcp__web__search":
    case "mcp__web__web_search":
      return { icon: <Globe size={s} />, label: "Search", brief: input.query || input.url || "" };
    case "WebFetch":
    case "mcp__web__fetch":
    case "mcp__web__web_fetch":
      return { icon: <Link2 size={s} />, label: "Fetch", brief: input.url };
    case "ViewImage":
      return { icon: <Image size={s} />, label: "View image", brief: p(input.file_path) };
    case "ImageGen":
      return { icon: <Image size={s} />, label: "Generate image", brief: input.prompt || "" };
    case "Permissions":
      return { icon: <ShieldAlert size={s} />, label: "Permissions", brief: JSON.stringify(input).slice(0, 160) };
    case "mcp__graph__set_view":
    case "mcp__atrium_git__git_view":
      return { icon: <GitBranch size={s} />, label: "Graph view", brief: (input.branches || []).join(", ") || "all refs" };
    case "Task":
    case "Agent":
      return { icon: <Bot size={s} />, label: input.subagent_type || "Subagent", brief: input.description || "" };
    case "TodoWrite":
      return { icon: <ListTodo size={s} />, label: "Todos", brief: `${(input.todos || []).filter((t: any) => t.status === "completed").length}/${(input.todos || []).length} done` };
    case "ExitPlanMode":
      return { icon: <ClipboardCheck size={s} />, label: "Plan", brief: "Ready for review" };
    default:
      if (name.startsWith("mcp__")) {
        const [, server, tool] = name.split("__");
        return { icon: <Plug size={s} />, label: `${server}.${tool}`, brief: JSON.stringify(input).slice(0, 160) };
      }
      return { icon: <Plug size={s} />, label: name, brief: JSON.stringify(input).slice(0, 160) };
  }
}

function Todos({ todos }: { todos: { content: string; status: string; activeForm?: string }[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 13 }}>
      {todos.map((t, i) => (
        <div key={i} className="row" style={{ alignItems: "flex-start", gap: 7, color: t.status === "completed" ? "var(--faint)" : undefined }}>
          <span style={{ marginTop: 2, color: t.status === "completed" ? "var(--ok)" : t.status === "in_progress" ? "var(--accent)" : "var(--faint)" }}>
            {t.status === "completed" ? <Check size={13} /> : t.status === "in_progress" ? "◐" : "○"}
          </span>
          <span style={{ textDecoration: t.status === "completed" ? "line-through" : undefined, fontWeight: t.status === "in_progress" ? 600 : 400 }}>
            {t.status === "in_progress" && t.activeForm ? t.activeForm : t.content}
          </span>
        </div>
      ))}
    </div>
  );
}

function ToolDetail({ tool, result, cwd }: { tool: ToolItem; result?: ResultItem; cwd: string }) {
  const openFile = useApp((s) => s.openFile);
  const inp = tool.input || {};
  const fileLink = inp.file_path ? (
    <button className="btn btn-sm btn-ghost" style={{ alignSelf: "flex-start" }} onClick={() => openFile(inp.file_path, cwd)}>
      <ExternalLink size={12} /> Open {relTo(cwd, inp.file_path)}
    </button>
  ) : null;
  const out = result ? <pre className={`code-block wrap ${result.is_error ? "err" : ""}`}>{result.content || "(no output)"}</pre> : null;
  switch (tool.name) {
    case "Edit":
      return (
        <>
          <MiniDiff before={inp.old_string ?? ""} after={inp.new_string ?? ""} />
          {fileLink}
          {result?.is_error && out}
        </>
      );
    case "MultiEdit":
      return (
        <>
          {(inp.edits || []).map((e: any, i: number) => (
            <MiniDiff key={i} before={e.old_string ?? ""} after={e.new_string ?? ""} />
          ))}
          {fileLink}
        </>
      );
    case "Write":
      return (
        <>
          <pre className="code-block">{String(inp.content ?? "").split("\n").slice(0, 300).join("\n")}</pre>
          {fileLink}
          {result?.is_error && out}
        </>
      );
    case "Bash":
      return (
        <>
          <pre className="code-block wrap">$ {inp.command}</pre>
          {out}
        </>
      );
    case "Patch":
      return (
        <>
          <PatchView changes={inp.changes || []} cwd={cwd} />
          {result?.is_error && out}
        </>
      );
    case "TodoWrite":
      return <Todos todos={inp.todos || []} />;
    case "Read":
      return (
        <>
          {fileLink}
          {result?.is_error && out}
        </>
      );
    default:
      return (
        <>
          <div className="lbl">Input</div>
          <pre className="code-block wrap">{JSON.stringify(inp, null, 2)}</pre>
          {result && <div className="lbl">Output</div>}
          {out}
        </>
      );
  }
}

export function ToolRow({ tool, result, cwd, running }: { tool: ToolItem; result?: ResultItem; cwd: string; running: boolean }) {
  const [open, setOpen] = useState(tool.name === "TodoWrite");
  const meta = toolMeta(tool.name, tool.input || {}, cwd);
  return (
    <div className="tool">
      <div className="tool-row" onClick={() => setOpen((o) => !o)}>
        <span className="ico">{meta.icon}</span>
        <span className="label">{meta.label}</span>
        <span className="brief">{meta.brief}</span>
        <span className={`st ${result ? (result.is_error ? "err" : "ok") : ""}`}>
          {result ? result.is_error ? <X size={14} /> : <Check size={14} /> : running ? <Spinner /> : null}
        </span>
        <span className="ico">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
      </div>
      {open && (
        <div className="tool-body">
          <ToolDetail tool={tool} result={result} cwd={cwd} />
        </div>
      )}
    </div>
  );
}

export function BoardToolRow({ tool }: { tool: ToolItem }) {
  const setPanel = useApp((s) => s.setPanel);
  const inp = tool.input || {};
  // mcp__board__status (current) or mcp__atrium_board__board_status (older transcripts)
  const short = tool.name.replace(/^mcp__(atrium_)?board__(board_)?/, "");
  let text = "";
  if (short === "status") text = `Status → ${inp.status} · ${inp.progress ?? 0}% · ${inp.doing ?? ""}`;
  else if (short === "publish") text = `Published card “${inp.title}”`;
  else if (short === "note") text = `Timeline: ${inp.text}`;
  else if (short === "read") text = "Checked the board";
  return (
    <div className="board-tool">
      <span className="ico">
        <LayoutDashboard size={14} />
      </span>
      <span className="grow ellipsis">{text}</span>
      {short === "publish" && (
        <button className="btn btn-sm btn-ghost" onClick={() => setPanel("board")}>
          View on board
        </button>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ permission
export function PermissionCard({ perm, cwd, onDecide }: { perm: PermItem; cwd: string; onDecide: (d: Record<string, unknown>) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [denyOpen, setDenyOpen] = useState(false);
  const [msg, setMsg] = useState("");
  const inp = perm.input || {};
  const decide = async (d: Record<string, unknown>) => {
    setBusy(true);
    try {
      await onDecide(d);
    } finally {
      setBusy(false);
    }
  };

  if (perm.tool === "AskUserQuestion") return <AskCard perm={perm} onDecide={decide} busy={busy} />;

  const isPlan = perm.tool === "ExitPlanMode";
  const meta = toolMeta(perm.tool, inp, cwd);
  let preview: JSX.Element;
  if (isPlan) preview = <div className="msg-text" style={{ fontSize: 14.5 }}><Markdown text={inp.plan || ""} /></div>;
  else if (perm.tool === "Edit") preview = <MiniDiff before={inp.old_string ?? ""} after={inp.new_string ?? ""} />;
  else if (perm.tool === "Write") preview = <pre className="code-block">{String(inp.content ?? "").slice(0, 6000)}</pre>;
  else if (perm.tool === "Patch") preview = <PatchView changes={inp.changes || []} cwd={cwd} />;
  else if (perm.tool === "Bash")
    preview = (
      <>
        {inp.description && <div className="muted">{inp.description}</div>}
        <pre className="code-block wrap">$ {inp.command}</pre>
        {inp.cwd && inp.cwd !== cwd && <div className="muted mono" style={{ fontSize: 12 }}>in {inp.cwd}</div>}
      </>
    );
  else preview = <pre className="code-block wrap">{JSON.stringify(inp, null, 2)}</pre>;

  return (
    <div className="perm-card">
      <div className="head">
        <ShieldAlert size={15} />
        <span className="grow">{isPlan ? "Plan ready for your review" : perm.title || `Allow ${meta.label}?`}</span>
      </div>
      <div className="content">
        {!isPlan && (
          <div className="row" style={{ fontSize: 13 }}>
            {meta.icon}
            <b>{perm.display_name || meta.label}</b>
            <span className="muted mono ellipsis" style={{ fontSize: 12 }}>
              {meta.brief}
            </span>
          </div>
        )}
        {preview}
        {perm.reason && <div className="muted" style={{ fontSize: 12.5 }}>Reason: {perm.reason}</div>}
        {denyOpen && (
          <textarea
            className="textarea"
            rows={2}
            autoFocus
            placeholder={isPlan ? "What should change in the plan?" : "Tell the agent what to do instead (optional)"}
            value={msg}
            onChange={(e) => setMsg(e.target.value)}
          />
        )}
      </div>
      <div className="actions">
        {denyOpen ? (
          <>
            <button className="btn btn-primary" disabled={busy} onClick={() => decide({ behavior: "deny", message: msg || undefined })}>
              {isPlan ? "Send feedback" : "Deny"}
            </button>
            <button className="btn" disabled={busy} onClick={() => decide({ behavior: "deny", message: msg || undefined, interrupt: true })}>
              Deny & stop
            </button>
            <button className="btn btn-ghost" onClick={() => setDenyOpen(false)}>
              Back
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-primary" disabled={busy} onClick={() => decide({ behavior: "allow" })}>
              {isPlan ? "Approve plan" : "Allow"}
            </button>
            {perm.can_remember && !isPlan && (
              <button className="btn" disabled={busy} onClick={() => decide({ behavior: "allow", remember: true })}>
                Always allow
              </button>
            )}
            {isPlan && (
              <button className="btn" disabled={busy} onClick={() => decide({ behavior: "allow", then_mode: "acceptEdits" })}>
                Approve & auto-accept edits
              </button>
            )}
            <button className="btn btn-ghost" disabled={busy} onClick={() => setDenyOpen(true)}>
              {isPlan ? "Keep planning…" : "Deny…"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function AskCard({ perm, onDecide, busy }: { perm: PermItem; onDecide: (d: Record<string, unknown>) => Promise<void>; busy: boolean }) {
  const questions: { question: string; header?: string; options: { label: string; description?: string }[]; multiSelect?: boolean }[] =
    perm.input?.questions || [];
  const [sel, setSel] = useState<Record<string, string[]>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const toggle = (q: string, label: string, multi?: boolean) =>
    setSel((s) => {
      const cur = s[q] || [];
      return { ...s, [q]: multi ? (cur.includes(label) ? cur.filter((x) => x !== label) : [...cur, label]) : [label] };
    });
  const answers = Object.fromEntries(
    questions.map((q) => [q.question, [...(sel[q.question] || []), ...(other[q.question] ? [other[q.question]] : [])].join(", ")]),
  );
  const complete = questions.every((q) => answers[q.question]);
  return (
    <div className="perm-card">
      <div className="head">
        <ShieldAlert size={15} /> The agent has {questions.length > 1 ? "some questions" : "a question"}
      </div>
      <div className="content">
        {questions.map((q) => (
          <div key={q.question}>
            <div className="q">{q.question}</div>
            {q.options.map((o) => (
              <div key={o.label} className={`opt ${(sel[q.question] || []).includes(o.label) ? "on" : ""}`} onClick={() => toggle(q.question, o.label, q.multiSelect)}>
                <div>
                  <div>{o.label}</div>
                  {o.description && <div className="d">{o.description}</div>}
                </div>
              </div>
            ))}
            <input className="input" placeholder="Other…" value={other[q.question] || ""} onChange={(e) => setOther((s) => ({ ...s, [q.question]: e.target.value }))} />
          </div>
        ))}
      </div>
      <div className="actions">
        <button className="btn btn-primary" disabled={busy || !complete} onClick={() => onDecide({ behavior: "allow", answers })}>
          Submit answers
        </button>
        <button className="btn btn-ghost" disabled={busy} onClick={() => onDecide({ behavior: "deny", message: "The user skipped the question." })}>
          Skip
        </button>
      </div>
    </div>
  );
}
