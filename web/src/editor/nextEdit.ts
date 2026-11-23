// Next edit, as Cursor's Tab after Tab (atrium/next_edit.py): after an edit, the next one the model expects (the same
// rename further down, the other calls of a function whose signature changed), marked where it goes: the lines it
// changes struck through, what they become below them. Tab goes there, Tab again makes it, and the next one shows;
// Esc puts them away. On a phone, the buttons of its hint do the same.
//
// The page keeps the human's recent edits per file (`recentEdits`): typing in one place is one edit (it ends after
// BURST_GAP without typing, or when the cursor goes elsewhere), its changed lines from before to after. They go with
// each request, the inline completion's too (completion.ts).
import { monaco } from "../monaco-setup";
import type * as Monaco from "monaco-editor";
import { api, EditEvent } from "../api";
import { useApp } from "../store";
import { useCompletionStatus } from "../editorStatus";

const PAUSE = 700; // ms after an edit before asking
const AFTER_ACCEPT = 250; // ms after a next edit is made before asking for the one after it
const BURST_GAP = 3000; // ms without typing that end an edit
const MAX_EVENTS = 6;
const EVENT_LINES = 20; // of an edit's lines kept, before and after
const REGION_CHARS = 6000; // of the code around the cursor the model sees (the whole file, if it is no longer)
const MAX_PROBLEMS = 8;
const SKIP = new Set(["plaintext", "markdown"]);
export const SECRET_FILES = /(?:^\.env(?:rc|\..*)?|\.(?:pem|key|p12|pfx|keystore|jks)|^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|^\.(?:npmrc|pypirc|netrc|pgpass)|^credentials(?:\..*)?|secrets?\.(?:json|ya?ml|toml))$/i;

interface History {
  events: EditEvent[]; // finished, oldest first
  base: string[]; // the file's lines when the edit in progress began (or the last one ended)
  at: number; // the line the edit in progress is around (0: none in progress)
  timer: number;
  length: number; // the text's length after the last change (a change replacing all of it is a reload)
}
const histories = new WeakMap<Monaco.editor.ITextModel, History>();

/** The lines that differ between two versions of a file: one edit, from its first changed line to its last. */
function diffLines(a: string[], b: string[]): EditEvent | null {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let q = 0;
  while (q < a.length - p && q < b.length - p && a[a.length - 1 - q] === b[b.length - 1 - q]) q++;
  const old = a.slice(p, a.length - q);
  const neu = b.slice(p, b.length - q);
  if (!old.length && !neu.length) return null;
  return { line: p + 1, old: old.slice(0, EVENT_LINES).join("\n"), new: neu.slice(0, EVENT_LINES).join("\n") };
}

function historyOf(model: Monaco.editor.ITextModel): History {
  let h = histories.get(model);
  if (!h) histories.set(model, (h = { events: [], base: model.getLinesContent(), at: 0, timer: 0, length: model.getValueLength() }));
  return h;
}

/** The edit in progress ends: it joins the finished ones, and the file as it is now is the next one's start. */
function endBurst(model: Monaco.editor.ITextModel, h: History) {
  window.clearTimeout(h.timer);
  if (!h.at || model.isDisposed()) return;
  const now = model.getLinesContent();
  const ev = diffLines(h.base, now);
  if (ev) h.events = [...h.events, ev].slice(-MAX_EVENTS);
  h.base = now;
  h.at = 0;
}

/** The human's recent edits in this file, oldest first, the one in progress last: for the model. */
export function recentEdits(model: Monaco.editor.ITextModel): EditEvent[] {
  const h = histories.get(model);
  if (!h) return [];
  const current = h.at ? diffLines(h.base, model.getLinesContent()) : null;
  return current ? [...h.events, current].slice(-MAX_EVENTS) : h.events;
}

// ---- what is shown -----------------------------------------------------------------------------------------------
interface Pending {
  deco: string; // tracks its lines as the file changes
  old: string;
  text: string;
  insert: boolean; // nothing replaced: inserted before the line
}

/** One editor's next edits: those still to make, the first shown. */
class NextEdits {
  pending: Pending[] = [];
  decos: string[] = []; // of the one shown
  zone: string | null = null;
  widget: Monaco.editor.IContentWidget | null = null; // the hint, one for as long as there are edits to show
  hintAt: Monaco.IPosition = { lineNumber: 1, column: 1 };
  key: Monaco.editor.IContextKey<boolean>;
  timer = 0;
  ask: AbortController | null = null;
  applying = false;

  constructor(private editor: Monaco.editor.IStandaloneCodeEditor) {
    this.key = editor.createContextKey<boolean>("atriumNextEdit", false);
  }

  /** The range a pending edit has now (its decoration follows the file's changes). */
  range(p: Pending): Monaco.Range | null {
    return this.editor.getModel()?.getDecorationRange(p.deco) ?? null;
  }

  clear() {
    this.ask?.abort();
    this.ask = null;
    window.clearTimeout(this.timer);
    this.editor.removeDecorations([...this.pending.map((p) => p.deco), ...this.decos]);
    this.pending = [];
    this.decos = [];
    if (this.zone) {
      const z = this.zone;
      this.editor.changeViewZones((a) => a.removeZone(z));
      this.zone = null;
    }
    if (this.widget) this.editor.removeContentWidget(this.widget);
    this.widget = null;
    this.key.set(false);
  }

  show(edits: { start: number; end: number; old: string; text: string }[]) {
    this.clear();
    const model = this.editor.getModel();
    if (!model || !edits.length) return;
    const pending: Pending[] = [];
    for (const e of edits) {
      const at = locate(model, e);
      if (at == null) continue;
      const insert = e.start === e.end;
      const range = insert ? new monaco.Range(at, 1, at, 1) : new monaco.Range(at, 1, at + (e.end - e.start) - 1, model.getLineMaxColumn(at + (e.end - e.start) - 1));
      const [deco] = this.editor.deltaDecorations([], [{ range, options: { stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges } }]);
      pending.push({ deco, old: e.old, text: e.text, insert });
    }
    this.pending = pending;
    this.render();
  }

  /** The first pending edit, marked; the hint at the cursor (go there) or at the edit (make it). */
  render() {
    const model = this.editor.getModel();
    this.decos = this.editor.deltaDecorations(this.decos, []);
    if (this.zone) {
      const z = this.zone;
      this.editor.changeViewZones((a) => a.removeZone(z));
      this.zone = null;
    }
    const p = this.pending[0];
    const r = p && this.range(p);
    if (!model || !p || !r) return this.clear();
    const marks: Monaco.editor.IModelDeltaDecoration[] = [
      {
        range: r,
        options: {
          isWholeLine: !p.insert,
          className: p.insert ? undefined : "atrium-nes-old",
          inlineClassName: p.insert || !p.old.trim() ? undefined : "atrium-nes-strike",
          overviewRuler: { color: "#3f7d58", position: monaco.editor.OverviewRulerLane.Full },
          minimap: { color: "#3f7d58", position: monaco.editor.MinimapPosition.Inline },
        },
      },
    ];
    this.decos = this.editor.deltaDecorations([], marks);
    if (p.text) {
      const lines = p.text.split("\n");
      const font = this.editor.getOption(monaco.editor.EditorOption.fontInfo);
      const node = document.createElement("div");
      node.className = "atrium-nes-new";
      node.style.fontFamily = font.fontFamily;
      node.style.fontSize = `${font.fontSize}px`;
      node.style.lineHeight = `${this.editor.getOption(monaco.editor.EditorOption.lineHeight)}px`;
      node.textContent = lines.join("\n");
      const after = p.insert ? r.startLineNumber - 1 : r.endLineNumber;
      this.editor.changeViewZones((a) => {
        this.zone = a.addZone({ afterLineNumber: after, heightInLines: lines.length, domNode: node });
      });
    }
    this.key.set(true);
    this.hint();
  }

  /** At the edit: Tab makes it. Elsewhere: Tab goes there. */
  atEdit(): boolean {
    const p = this.pending[0];
    const r = p && this.range(p);
    const pos = this.editor.getPosition();
    return !!r && !!pos && pos.lineNumber >= r.startLineNumber && pos.lineNumber <= Math.max(r.endLineNumber, r.startLineNumber);
  }

  hint() {
    const p = this.pending[0];
    const r = p && this.range(p);
    const pos = this.editor.getPosition();
    const model = this.editor.getModel();
    if (!r || !pos || !model) return;
    const here = this.atEdit();
    const line = here ? r.startLineNumber : pos.lineNumber;
    this.hintAt = { lineNumber: line, column: model.getLineMaxColumn(line) };
    if (!this.widget) {
      const node = document.createElement("div");
      node.className = "atrium-nes-hint";
      const label = document.createElement("span");
      const go = document.createElement("button");
      go.onmousedown = (e) => (e.preventDefault(), this.tab());
      const no = document.createElement("button");
      no.textContent = "×";
      no.setAttribute("aria-label", "Put the next edits away (Esc)");
      no.onmousedown = (e) => (e.preventDefault(), this.clear());
      node.append(label, go, no);
      this.widget = {
        getId: () => "atrium.nextEdit.hint",
        getDomNode: () => node,
        getPosition: () => ({ position: this.hintAt, preference: [monaco.editor.ContentWidgetPositionPreference.EXACT, monaco.editor.ContentWidgetPositionPreference.BELOW] }),
      };
      this.editor.addContentWidget(this.widget);
    }
    const [label, go] = Array.from(this.widget.getDomNode().children) as HTMLElement[];
    label.innerHTML = here ? "<kbd>Tab</kbd> make this edit" : `<kbd>Tab</kbd> next edit, line ${r.startLineNumber}`;
    go.textContent = here ? "Accept" : "Go";
    go.setAttribute("aria-label", here ? "Make this edit" : "Go to the next edit");
    this.editor.layoutContentWidget(this.widget);
  }

  /** Tab: to the edit, or, there, make it. */
  tab() {
    const p = this.pending[0];
    const r = p && this.range(p);
    const model = this.editor.getModel();
    if (!p || !r || !model) return this.clear();
    if (!this.atEdit()) {
      this.editor.setPosition({ lineNumber: r.startLineNumber, column: model.getLineFirstNonWhitespaceColumn(r.startLineNumber) || 1 });
      this.editor.revealLineInCenterIfOutsideViewport(r.startLineNumber);
      this.editor.focus();
      return; // the cursor's move moved the hint
    }
    const { range, text } = editOf(model, r, p);
    this.applying = true;
    this.editor.pushUndoStop();
    this.editor.executeEdits("atrium.nextEdit", [{ range, text, forceMoveMarkers: true }]);
    this.editor.pushUndoStop();
    this.applying = false;
    const lines = text.split("\n");
    const endLine = range.startLineNumber + lines.length - 1 - (text.endsWith("\n") ? 1 : 0);
    this.editor.setPosition({ lineNumber: endLine, column: model.getLineMaxColumn(endLine) });
    this.editor.removeDecorations([p.deco]);
    this.pending = this.pending.slice(1);
    if (this.pending.length) this.render();
    else {
      this.clear();
      this.timer = window.setTimeout(() => void this.request(), AFTER_ACCEPT); // the one after it
    }
    this.editor.focus();
  }

  /** Asks the model for the next edits after the recent ones. */
  async request() {
    const editor = this.editor;
    const model = editor.getModel();
    const pos = editor.getPosition();
    const { meta, project } = useApp.getState();
    const settings = meta?.settings;
    if (!model || !pos || !project || settings?.ui.completion === false || !settings?.completion?.model || settings.completion.next_edit === false) return;
    if (editor.getOption(monaco.editor.EditorOption.readOnly) || !editor.hasTextFocus() || inlineShown(editor)) return;
    const language = model.getLanguageId();
    const path = model.uri.path;
    if (SKIP.has(language) || SECRET_FILES.test(path.slice(path.lastIndexOf("/") + 1))) return;
    if (!recentEdits(model).length) return;
    const version = model.getAlternativeVersionId();
    this.ask?.abort();
    const ctl = (this.ask = new AbortController());
    // the suggestion at the cursor goes first: one on its way is waited for, and where one shows, nothing is asked
    if (useCompletionStatus.getState().busy > 0) {
      for (const t0 = Date.now(); useCompletionStatus.getState().busy > 0 && Date.now() - t0 < 5000; ) await sleep(100);
      await sleep(50); // shown a moment after it came
    }
    const now = editor.getPosition();
    if (ctl.signal.aborted || !now || editor.getModel() !== model || model.getAlternativeVersionId() !== version || inlineShown(editor) || !editor.hasTextFocus()) return;
    const [start, end] = regionOf(model, now.lineNumber);
    const problems = problemsIn(model, start, end);
    const lines = model.getLinesContent().slice(start - 1, end);
    const i = now.lineNumber - start;
    lines[i] = lines[i].slice(0, now.column - 1) + "<|cursor|>" + lines[i].slice(now.column - 1);
    const events = recentEdits(model);
    try {
      const r = await api.completeNext(project.id, { path, label: labelOf(path), language, region: lines.join("\n"), start, cursor: now.lineNumber, events, problems }, ctl.signal);
      if (ctl.signal.aborted || !r?.edits.length) return;
      // the suggestions list open (the language server's, as one types a name): Tab is its; shown once it closes
      while (shownKey(editor, "suggestWidgetVisible") && !ctl.signal.aborted && model.getAlternativeVersionId() === version) await sleep(100);
      if (ctl.signal.aborted || model.getAlternativeVersionId() !== version || inlineShown(editor)) return;
      this.show(r.edits);
    } catch {
      // nothing to show: the inline completion's switch says what failed (the same model and server)
    } finally {
      if (this.ask === ctl) this.ask = null;
    }
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The lines the model sees: the whole file when it is short, else as many around the cursor as REGION_CHARS holds, two
 *  below it for each one above (the edits that follow one are mostly further down). */
function regionOf(model: Monaco.editor.ITextModel, line: number): [number, number] {
  const count = model.getLineCount();
  if (model.getValueLength() <= REGION_CHARS) return [1, count];
  let start = line;
  let end = line;
  let size = model.getLineLength(line) + 1;
  for (let i = 0; start > 1 || end < count; i++) {
    const up = (i % 3 === 0 && start > 1) || end >= count;
    const n = model.getLineLength(up ? start - 1 : end + 1) + 1;
    if (size + n > REGION_CHARS) break;
    size += n;
    if (up) start--;
    else end++;
  }
  return [start, end];
}

/** The errors the language server reports in lines `start` to `end`, each after the code of its line: where the change
 *  still needs following up. As it last reported them: a server says nothing when they stay as they were (so there is
 *  no telling news yet to come from none), and the pause before asking has mostly given it the time to look. */
function problemsIn(model: Monaco.editor.ITextModel, start: number, end: number): string[] {
  return monaco.editor
    .getModelMarkers({ resource: model.uri })
    .filter((m) => m.severity === monaco.MarkerSeverity.Error && m.startLineNumber >= start && m.startLineNumber <= end)
    .sort((a, b) => a.startLineNumber - b.startLineNumber)
    .slice(0, MAX_PROBLEMS)
    .map((m) => `${model.getLineContent(m.startLineNumber).trim().slice(0, 160)}  <- ${m.message.split("\n")[0].slice(0, 200)}`);
}

/** An edit's place in the file now: where its old lines are (as sent, or within a few lines of it). */
function locate(model: Monaco.editor.ITextModel, e: { start: number; end: number; old: string }): number | null {
  const n = e.end - e.start;
  const count = model.getLineCount();
  const fits = (at: number) => at >= 1 && (n === 0 ? at <= count + 1 : at + n - 1 <= count && model.getValueInRange(new monaco.Range(at, 1, at + n - 1, model.getLineMaxColumn(at + n - 1))) === e.old);
  if (n === 0) return e.start <= count + 1 ? e.start : null;
  for (let d = 0; d <= 20; d++) for (const at of d ? [e.start + d, e.start - d] : [e.start]) if (fits(at)) return at;
  return null;
}

/** The text edit that makes a pending edit: its lines replaced, or the new ones inserted before its line. */
function editOf(model: Monaco.editor.ITextModel, r: Monaco.Range, p: Pending): { range: Monaco.Range; text: string } {
  const count = model.getLineCount();
  if (p.insert) {
    if (r.startLineNumber <= count) return { range: new monaco.Range(r.startLineNumber, 1, r.startLineNumber, 1), text: p.text + "\n" };
    return { range: new monaco.Range(count, model.getLineMaxColumn(count), count, model.getLineMaxColumn(count)), text: "\n" + p.text };
  }
  const last = r.endLineNumber;
  if (last < count) return { range: new monaco.Range(r.startLineNumber, 1, last + 1, 1), text: p.text ? p.text + "\n" : "" };
  return { range: new monaco.Range(r.startLineNumber, 1, last, model.getLineMaxColumn(last)), text: p.text };
}

/** Whether an inline suggestion shows: it goes first. */
export function inlineShown(editor: Monaco.editor.ICodeEditor): boolean {
  return shownKey(editor, "inlineSuggestionVisible");
}

function shownKey(editor: Monaco.editor.ICodeEditor, key: string): boolean {
  const keys = (editor as unknown as { _contextKeyService?: { getContextKeyValue(k: string): unknown } })._contextKeyService;
  return keys?.getContextKeyValue(key) === true;
}

export function labelOf(abs: string): string {
  const p = useApp.getState().project;
  const roots = p ? [p.path, ...p.worktrees.map((w) => w.path)].sort((a, b) => b.length - a.length) : [];
  const root = roots.find((r) => abs.startsWith(r + "/"));
  return root ? abs.slice(root.length + 1) : abs;
}

/** Next edits for a Files editor, while it is mounted. */
export function serveNextEdit(editor: Monaco.editor.IStandaloneCodeEditor): Monaco.IDisposable {
  const next = new NextEdits(editor);
  const start = () => {
    const m = editor.getModel();
    if (m) historyOf(m); // the file as opened: the first edit's start
  };
  start();
  const when = "atriumNextEdit && !inlineSuggestionVisible && !suggestWidgetVisible";
  const actions = [
    editor.addAction({ id: "atrium.nextEdit.tab", label: "Go to / make the next edit", keybindings: [monaco.KeyCode.Tab], precondition: `${when} && editorTextFocus`, run: () => next.tab() }),
    editor.addAction({ id: "atrium.nextEdit.hide", label: "Put the next edits away", keybindings: [monaco.KeyCode.Escape], precondition: when, run: () => next.clear() }),
  ];
  const subs = [
    editor.onDidChangeModelContent((e) => {
      const m = editor.getModel();
      if (!m) return;
      const h = historyOf(m);
      const reload = e.isFlush || (e.changes.length === 1 && e.changes[0].rangeOffset === 0 && e.changes[0].rangeLength === h.length);
      h.length = m.getValueLength();
      if (reload) {
        // the file read again (from disk): no edit of the human's, and what was pending may be gone
        h.base = m.getLinesContent();
        h.at = 0;
        return next.clear();
      }
      if (!next.applying) next.clear(); // typing puts the next edits away; they are asked for again after it
      const line = e.changes[0].range.startLineNumber;
      if (h.at && Math.abs(line - h.at) > 8) endBurst(m, h);
      h.at = line;
      window.clearTimeout(h.timer);
      h.timer = window.setTimeout(() => endBurst(m, h), BURST_GAP);
      if (!next.applying) {
        window.clearTimeout(next.timer);
        next.timer = window.setTimeout(() => void next.request(), PAUSE);
      }
    }),
    editor.onDidChangeCursorPosition((e) => {
      const m = editor.getModel();
      const h = m && histories.get(m);
      if (m && h?.at && Math.abs(e.position.lineNumber - h.at) > 8) endBurst(m, h); // gone elsewhere: that edit is done
      if (next.pending.length) next.hint();
    }),
    editor.onDidChangeModel(() => (next.clear(), start())),
  ];
  return {
    dispose: () => {
      next.clear();
      actions.forEach((a) => a.dispose());
      subs.forEach((s) => s.dispose());
    },
  };
}
