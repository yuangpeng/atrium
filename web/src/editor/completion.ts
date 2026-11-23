// Inline completion in the Files editor, as Cursor's Tab (atrium/complete.py): what comes next at the cursor, in grey as
// one types. Tab takes it, ⌘→ (Ctrl+→ off the Mac) a word of it, Esc puts it away.
//
// A model takes a second or two to answer, so this side hides what it can of that:
// - a suggestion typed out as it reads stays, its rest shown with no request (`shown`; Monaco keeps it as well);
// - while a suggestion shows, what comes after it is asked for ahead (`speculative`), so that Tab after Tab finds the
//   next one waiting; after Tab, or with a suggestion typed out, the next request goes at once;
// - else a request waits for a pause in the typing, and one the typing made stale is given up here but runs on at the
//   server: its answer may fit what has been typed by the time it comes;
// - a suggestion of several lines shows its first line as soon as the model has written it, the rest when it comes
//   (`follow`).
import { monaco } from "../monaco-setup";
import type * as Monaco from "monaco-editor";
import { api, EditEvent } from "../api";
import { useApp } from "../store";
import { useCompletionStatus } from "../editorStatus";
import { recentEdits } from "./nextEdit";
import { Related, relatedSnippets } from "./related";

const BEFORE_CHARS = 6000; // of the file before the cursor sent, in whole lines (as much as complete.py takes)
const AFTER_CHARS = 2000;
const PAUSE = 100; // ms without typing before asking, on top of Monaco's own 50
const SKIP = new Set(["plaintext", "markdown"]); // prose
const CLOSERS_ONLY = /^[\s)\]}>'"`;,.:]*$/; // what may follow the cursor on its line; else it is in the middle of the code
// files that hold secrets, never sent to the model (as complete.py's SECRET_FILES)
const SECRET_FILES = /(?:^\.env(?:rc|\..*)?|\.(?:pem|key|p12|pfx|keystore|jks)|^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|^\.(?:npmrc|pypirc|netrc|pgpass)|^credentials(?:\..*)?|secrets?\.(?:json|ya?ml|toml))$/i;
const ACCEPTED = "atrium.completion.accepted";

const served = new Set<Monaco.editor.ICodeEditor>(); // the Files editors: what each shows is a file of the project

interface Batch extends Monaco.languages.InlineCompletions {
  partial?: boolean; // the first line of a longer suggestion
  pid: string;
  path: string;
  language: string;
  before: string;
  after: string;
  events: EditEvent[];
  related: Related[];
}

/** The suggestion shown last: what was typed of it since tells what is left of it. */
type Shown = { uri: string; tail: string; after: string; text: string };
let shown: Shown | null = null;
let acceptedAt = 0; // Tab took a suggestion
let aheadKey = ""; // the place asked for ahead last

/** The text before the cursor (whole lines, up to BEFORE_CHARS) and after it, with \n for line ends. */
function around(model: Monaco.editor.ITextModel, position: Monaco.Position): { before: string; after: string } {
  const at = model.getOffsetAt(position);
  const end = model.getValueLength();
  const lf = monaco.editor.EndOfLinePreference.LF;
  const from = Math.max(0, at - BEFORE_CHARS);
  let before = model.getValueInRange(monaco.Range.fromPositions(model.getPositionAt(from), position), lf);
  if (from > 0) before = before.slice(before.indexOf("\n") + 1);
  const to = Math.min(end, at + AFTER_CHARS);
  let after = model.getValueInRange(monaco.Range.fromPositions(position, model.getPositionAt(to)), lf);
  if (to < end && after.includes("\n")) after = after.slice(0, after.lastIndexOf("\n") + 1);
  return { before, after };
}

/** The file's name for the model: its path in the checkout that holds it. */
function labelOf(abs: string): string {
  const p = useApp.getState().project;
  const roots = p ? [p.path, ...p.worktrees.map((w) => w.path)].sort((a, b) => b.length - a.length) : [];
  const root = roots.find((r) => abs.startsWith(r + "/"));
  return root ? abs.slice(root.length + 1) : abs;
}

/** What was typed of the suggestion shown last, if the cursor is still on its way through it (null: elsewhere). */
function typedOf(uri: string, before: string, after: string): string | null {
  if (!shown || shown.uri !== uri || shown.after !== after) return null;
  const at = before.lastIndexOf(shown.tail);
  if (at < 0 || before.length - at - shown.tail.length > 600) return null;
  const typed = before.slice(at + shown.tail.length);
  return shown.text.startsWith(typed) ? typed : null;
}

function wholeLines(before: string): string {
  return before.length > BEFORE_CHARS ? before.slice(before.indexOf("\n", before.length - BEFORE_CHARS) + 1) : before;
}

const provider: Monaco.languages.InlineCompletionsProvider<Batch> = {
  async provideInlineCompletions(model, position, context, token) {
    const editor = [...served].find((e) => e.getModel() === model);
    const settings = useApp.getState().meta?.settings;
    const pid = useApp.getState().project?.id;
    if (!editor || settings?.ui.completion === false || !settings?.completion?.model || !pid || context.selectedSuggestionInfo) return;
    if (editor.getOption(monaco.editor.EditorOption.readOnly) || !editor.getSelection()?.isEmpty()) return;
    const language = model.getLanguageId();
    if (SKIP.has(language)) return;
    const uri = model.uri.toString();
    const path = model.uri.path;
    if (SECRET_FILES.test(path.slice(path.lastIndexOf("/") + 1))) return;
    const { before, after } = around(model, position);
    if (!CLOSERS_ONLY.test(after.split("\n", 1)[0])) return;
    let events: EditEvent[] = [];
    let related: Related[] = [];
    const batch = (text: string, partial = false): Batch => ({
      items: [{ insertText: text, range: monaco.Range.fromPositions(position), command: { id: ACCEPTED, title: "" } }],
      partial,
      pid,
      path,
      language,
      before,
      after,
      events,
      related,
    });
    const typed = typedOf(uri, before, after);
    if (typed !== null && shown && typed.length < shown.text.length) return batch(shown.text.slice(typed.length));
    // just after Tab, or with the suggestion typed out: what comes next was asked for already, don't wait
    const next = Date.now() - acceptedAt < 1500 || typed !== null;
    if (!next && context.triggerKind === monaco.languages.InlineCompletionTriggerKind.Automatic) {
      await new Promise((r) => setTimeout(r, PAUSE));
      if (token.isCancellationRequested) return;
    }
    const ctl = new AbortController();
    const sub = token.onCancellationRequested(() => ctl.abort());
    useCompletionStatus.setState((s) => ({ busy: s.busy + 1 }));
    try {
      events = recentEdits(model);
      related = relatedSnippets(model, position.lineNumber);
      const body = { path, label: labelOf(path), language, before, after, events, related };
      const r = await api.complete(pid, body, ctl.signal);
      useCompletionStatus.setState({ error: null });
      if (!r?.text || token.isCancellationRequested) return;
      const record = (shown = { uri, tail: before.slice(-400), after, text: r.text });
      if (r.partial) follow(editor, pid, body, record);
      return batch(r.text, r.partial);
    } catch (e) {
      if (!ctl.signal.aborted) useCompletionStatus.setState({ error: e instanceof Error ? e.message : String(e) });
      return;
    } finally {
      sub.dispose();
      useCompletionStatus.setState((s) => ({ busy: s.busy - 1 }));
    }
  },
  handleItemDidShow(b, item) {
    // what comes after it, asked for while it shows: waiting after Tab (after the whole of it, once that is there)
    if (b.partial) return;
    const text = typeof item.insertText === "string" ? item.insertText : item.insertText.snippet;
    const before = wholeLines(b.before + text);
    const key = `${b.path}\0${before.slice(-400)}\0${b.after}`;
    if (key === aheadKey) return;
    aheadKey = key;
    api.complete(b.pid, { path: b.path, label: labelOf(b.path), language: b.language, before, after: b.after, events: b.events, related: b.related, speculative: true }).catch(() => undefined);
  },
  freeInlineCompletions() {},
};

/** The rest of a suggestion that came as its first line: asked for whole, and shown in its place when it comes (unless
 *  the suggestion was put away, or the typing went elsewhere: `shown` is another then). */
function follow(editor: Monaco.editor.ICodeEditor, pid: string, body: Parameters<typeof api.complete>[1], record: Shown) {
  api.complete(pid, { ...body, whole: true }).then(
    (r) => {
      if (!r?.text || shown !== record || r.text.length <= record.text.length || !r.text.startsWith(record.text)) return;
      record.text = r.text;
      const keys = (editor as unknown as { _contextKeyService?: { getContextKeyValue(k: string): unknown } })._contextKeyService;
      if (keys?.getContextKeyValue("inlineSuggestionVisible") !== false) editor.trigger("atrium", "editor.action.inlineSuggest.trigger", {});
    },
    () => undefined,
  );
}

monaco.languages.registerInlineCompletionsProvider("*", provider);
monaco.editor.registerCommand(ACCEPTED, () => void (acceptedAt = Date.now()));

/** The connection to the model's server, opened as an editor gets the focus: the first suggestion doesn't wait for it
 *  (the server skips it when it was asked lately). */
let warmedAt = 0;
function warm() {
  const { meta, project } = useApp.getState();
  if (!project || meta?.settings.ui.completion === false || !meta?.settings.completion?.model || Date.now() - warmedAt < 60_000) return;
  warmedAt = Date.now();
  api.completeWarm(project.id).catch(() => undefined);
}

/** Inline completion for a Files editor, while it is mounted. */
export function serveCompletion(editor: Monaco.editor.ICodeEditor): Monaco.IDisposable {
  served.add(editor);
  const focus = editor.onDidFocusEditorText(warm);
  const gone = editor.onDidDispose(() => served.delete(editor));
  return { dispose: () => (served.delete(editor), focus.dispose(), gone.dispose()) };
}
