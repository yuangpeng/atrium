// LSP client for the Files editor. Atrium runs the language servers (atrium/lsp.py) and relays
// LSP over one websocket; this maps Monaco's providers onto LSP requests and keeps the server's
// copy of each open document in sync with its Monaco model.
import type * as Monaco from "monaco-editor";
import { monaco } from "../monaco-setup";
import { useApp } from "../store";
import { editorBridge, useEditorStatus } from "../editorStatus";

type Json = any; // LSP payloads, read defensively

interface Server {
  key: string;
  language: string;
  root: string;
  name: string;
  caps: Json;
}

interface Doc {
  uri: string;
  model: Monaco.editor.ITextModel;
  server: Server;
  dirty: boolean;
  timer: number | null;
  listeners: Monaco.IDisposable[];
  semanticRetries: number;
}

const CHANGE_DEBOUNCE_MS = 120;
const MAX_PREVIEW_FILES = 100;

// ---------------------------------------------------------------------------- connection
class Connection {
  private ws: WebSocket | null = null;
  private opening: Promise<void> | null = null;
  private retryAt = 0;
  private nextId = 1;
  private pending = new Map<number, (v: Json) => void>();
  private reads = new Map<number, (v: { text?: string; error?: string }) => void>();
  private attaching = new Map<string, Promise<Server | string>>(); // "language:root" -> server | error
  private attachWaiters = new Map<string, (v: Server | string) => void>();
  onMessage: (key: string, msg: Json) => void = () => {};
  onReset: () => void = () => {};
  onApplyEdit: (id: number, edit: Json) => void = () => {};

  private connect(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) return Promise.resolve();
    if (this.opening) return this.opening;
    if (Date.now() < this.retryAt) return Promise.reject(new Error("language servers unreachable"));
    this.opening = new Promise((resolve, reject) => {
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/lsp`);
      ws.onopen = () => {
        this.ws = ws;
        this.opening = null;
        resolve();
      };
      ws.onmessage = (ev) => this.receive(JSON.parse(ev.data));
      ws.onclose = () => {
        const wasOpen = this.ws === ws;
        this.ws = null;
        this.opening = null;
        if (!wasOpen) {
          this.retryAt = Date.now() + 3000;
          reject(new Error("language servers unreachable"));
          return;
        }
        this.pending.forEach((r) => r(null));
        this.reads.forEach((r) => r({ error: "disconnected" }));
        this.attachWaiters.forEach((r) => r("disconnected"));
        this.pending.clear();
        this.reads.clear();
        this.attachWaiters.clear();
        this.attaching.clear();
        this.onReset();
      };
    });
    return this.opening;
  }

  private send(msg: Json): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(msg));
    return true;
  }

  private receive(msg: Json) {
    switch (msg.op) {
      case "attached": {
        const k = `${msg.language}:${msg.root}`;
        const done = this.attachWaiters.get(k);
        this.attachWaiters.delete(k);
        done?.(msg.error ? String(msg.error) : { key: msg.key, language: msg.language, root: msg.root, name: msg.server, caps: msg.capabilities || {} });
        break;
      }
      case "rpc": {
        const m = msg.msg || {};
        if (m.id != null && !m.method) {
          const done = this.pending.get(m.id);
          this.pending.delete(m.id);
          if (m.error && m.error.code !== -32800) console.debug("lsp:", m.error.message);
          done?.(m.error ? null : m.result);
        } else if (m.method) this.onMessage(msg.key, m);
        break;
      }
      case "exit":
        for (const [k, p] of this.attaching) void p.then((s) => typeof s !== "string" && s.key === msg.key && this.attaching.delete(k));
        this.onMessage(msg.key, { method: "atrium/exit" });
        break;
      case "refresh":
        this.onMessage(msg.key, { method: "workspace/semanticTokens/refresh" });
        break;
      case "read": {
        const done = this.reads.get(msg.id);
        this.reads.delete(msg.id);
        done?.(msg);
        break;
      }
      case "applyEdit": // the server wants files changed (a command's result): the page makes the change
        this.onApplyEdit(msg.id, msg.edit);
        break;
    }
  }

  answerEdit(id: number, result: { applied: boolean; failureReason?: string }) {
    this.send({ op: "applied", id, ...result });
  }

  attach(language: string, root: string): Promise<Server | string> {
    const k = `${language}:${root}`;
    let p = this.attaching.get(k);
    if (!p) {
      p = this.connect().then(
        () =>
          new Promise<Server | string>((resolve) => {
            this.attachWaiters.set(k, resolve);
            this.send({ op: "attach", language, root });
          }),
        (e: Error) => e.message,
      );
      this.attaching.set(k, p);
      // a failed attach is retried on the next request after a pause
      void p.then((r) => typeof r === "string" && setTimeout(() => this.attaching.get(k) === p && this.attaching.delete(k), 10_000));
    }
    return p;
  }

  request(server: Server, method: string, params: Json, token?: Monaco.CancellationToken): Promise<Json> {
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.pending.set(id, resolve);
      if (!this.send({ op: "rpc", key: server.key, msg: { jsonrpc: "2.0", id, method, params } })) {
        this.pending.delete(id);
        resolve(null);
        return;
      }
      token?.onCancellationRequested(() => {
        if (!this.pending.delete(id)) return;
        this.notify(server, "$/cancelRequest", { id });
        resolve(null);
      });
    });
  }

  notify(server: Server, method: string, params: Json) {
    this.send({ op: "rpc", key: server.key, msg: { jsonrpc: "2.0", method, params } });
  }

  read(uri: string): Promise<{ text?: string; error?: string }> {
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.reads.set(id, resolve);
      if (!this.send({ op: "read", id, uri })) {
        this.reads.delete(id);
        resolve({ error: "disconnected" });
      }
    });
  }
}

const conn = new Connection();
const docs = new Map<string, Doc>(); // uri -> document open on a server
const servers = new Map<string, Server>(); // key -> a server attached (for the commands its code actions run)
const fileEditors = new Set<Monaco.editor.ICodeEditor>(); // the Files panel's editor(s): what they show is the file
const tracked = new Set<string>(); // uris shown in a Files editor (get diagnostics + semantic colours)
const diagnostics = new Map<string, Json[]>();
const externalRoot = new Map<string, string>(); // library file -> root of the server that pointed there
const semanticChanged = new Map<string, Monaco.Emitter<void>>();
const registered = new Set<string>();
const status = () => useEditorStatus.getState();

// ---------------------------------------------------------------------------- helpers
const toPos = (p: Monaco.IPosition) => ({ line: p.lineNumber - 1, character: p.column - 1 });
const toRange = (r: Json) => new monaco.Range(r.start.line + 1, r.start.character + 1, r.end.line + 1, r.end.character + 1);
const fromRange = (r: Monaco.IRange) => ({ start: { line: r.startLineNumber - 1, character: r.startColumn - 1 }, end: { line: r.endLineNumber - 1, character: r.endColumn - 1 } });
const docPos = (doc: Doc, p: Monaco.IPosition) => ({ textDocument: { uri: doc.uri }, position: toPos(p) });

function projectRoots(): string[] {
  const p = useApp.getState().project;
  if (!p) return [];
  return p.worktrees.length ? p.worktrees.map((w) => w.path) : [p.path];
}

function ownerRoot(path: string): string | null {
  let best: string | null = null;
  for (const r of projectRoots()) if ((path === r || path.startsWith(r + "/")) && (!best || r.length > best.length)) best = r;
  return best;
}

function markdown(c: Json): Monaco.IMarkdownString[] {
  if (!c) return [];
  if (Array.isArray(c)) return c.flatMap(markdown);
  if (typeof c === "string") return c ? [{ value: c }] : [];
  if (c.kind) return c.value ? [{ value: c.kind === "markdown" ? c.value : "```\n" + c.value + "\n```" }] : [];
  if (c.language) return [{ value: "```" + c.language + "\n" + c.value + "\n```" }];
  return [];
}

function documentation(d: Json): string | Monaco.IMarkdownString | undefined {
  if (!d) return undefined;
  if (typeof d === "string") return d;
  return d.kind === "markdown" ? { value: d.value } : d.value;
}

// ---------------------------------------------------------------------------- documents
async function ensureDoc(model: Monaco.editor.ITextModel): Promise<Doc | null> {
  if (model.isDisposed() || model.uri.scheme !== "file") return null;
  const uri = model.uri.toString();
  const open = docs.get(uri);
  if (open) {
    flush(open);
    return open;
  }
  const language = model.getLanguageId();
  const info = useApp.getState().meta?.lsp?.[language];
  if (!info) return null;
  const path = model.uri.path;
  const root = ownerRoot(path) ?? externalRoot.get(uri) ?? useApp.getState().project?.path;
  if (!root) return null;
  const isTracked = tracked.has(uri);
  if (isTracked && !status().files[path]) status().patch(path, { state: "connecting", server: info.server });
  const server = await conn.attach(language, root);
  if (typeof server === "string") {
    if (isTracked) status().patch(path, { state: "unavailable", server: info.server, detail: server });
    return null;
  }
  if (model.isDisposed()) return null;
  const raced = docs.get(uri);
  if (raced) return raced;
  servers.set(server.key, server);
  const doc: Doc = { uri, model, server, dirty: false, timer: null, listeners: [], semanticRetries: 0 };
  docs.set(uri, doc);
  const languageId = /\.tsx$/i.test(path) ? "typescriptreact" : /\.jsx$/i.test(path) ? "javascriptreact" : language; // JSX by its name
  conn.notify(server, "textDocument/didOpen", { textDocument: { uri, languageId, version: model.getVersionId(), text: model.getValue() } });
  doc.listeners.push(
    model.onDidChangeContent(() => {
      doc.dirty = true;
      if (doc.timer === null) doc.timer = window.setTimeout(() => flush(doc), CHANGE_DEBOUNCE_MS);
    }),
    model.onWillDispose(() => closeDoc(uri)),
  );
  registerProviders(language, server);
  if (tracked.has(uri)) status().patch(path, { state: "ready", server: server.name, detail: undefined });
  return doc;
}

function flush(doc: Doc) {
  if (doc.timer !== null) {
    clearTimeout(doc.timer);
    doc.timer = null;
  }
  if (!doc.dirty || doc.model.isDisposed()) return;
  doc.dirty = false;
  conn.notify(doc.server, "textDocument/didChange", {
    textDocument: { uri: doc.uri, version: doc.model.getVersionId() },
    contentChanges: [{ text: doc.model.getValue() }],
  });
}

function forget(doc: Doc) {
  if (doc.timer !== null) clearTimeout(doc.timer);
  doc.listeners.forEach((d) => d.dispose());
  docs.delete(doc.uri);
}

function closeDoc(uri: string) {
  const doc = docs.get(uri);
  if (doc) {
    forget(doc);
    conn.notify(doc.server, "textDocument/didClose", { textDocument: { uri } });
  }
  diagnostics.delete(uri);
  if (tracked.delete(uri)) status().drop(monaco.Uri.parse(uri).path);
}

const SEVERITY: Record<number, Monaco.MarkerSeverity> = {
  1: monaco.MarkerSeverity.Error,
  2: monaco.MarkerSeverity.Warning,
  3: monaco.MarkerSeverity.Info,
  4: monaco.MarkerSeverity.Hint,
};

function applyDiagnostics(uri: string) {
  const model = monaco.editor.getModel(monaco.Uri.parse(uri));
  if (!model || model.isDisposed()) return;
  const list = diagnostics.get(uri) ?? [];
  monaco.editor.setModelMarkers(
    model,
    "lsp",
    list.map((d) => ({
      severity: SEVERITY[d.severity ?? 1] ?? monaco.MarkerSeverity.Error,
      message: d.message,
      source: d.source,
      code: d.code == null ? undefined : d.codeDescription?.href ? { value: String(d.code), target: monaco.Uri.parse(d.codeDescription.href) } : String(d.code),
      ...{ startLineNumber: d.range.start.line + 1, startColumn: d.range.start.character + 1, endLineNumber: d.range.end.line + 1, endColumn: d.range.end.character + 1 },
      tags: (d.tags as number[] | undefined)?.map((t) => (t === 1 ? monaco.MarkerTag.Unnecessary : monaco.MarkerTag.Deprecated)),
    })),
  );
  if (tracked.has(uri))
    status().patch(model.uri.path, { errors: list.filter((d) => (d.severity ?? 1) === 1).length, warnings: list.filter((d) => d.severity === 2).length });
}

conn.onMessage = (key, m) => {
  if (m.method === "textDocument/publishDiagnostics") {
    const uri = monaco.Uri.parse(m.params.uri).toString();
    diagnostics.set(uri, m.params.diagnostics || []);
    applyDiagnostics(uri);
  } else if (m.method === "workspace/semanticTokens/refresh") {
    const lang = [...docs.values()].find((d) => d.server.key === key)?.server.language;
    if (lang) semanticChanged.get(lang)?.fire();
  } else if (m.method === "atrium/exit") {
    for (const doc of [...docs.values()]) {
      if (doc.server.key !== key) continue;
      forget(doc);
      if (tracked.has(doc.uri)) status().patch(doc.model.uri.path, { state: "unavailable", detail: "the language server exited — edit to restart it" });
    }
  }
};

conn.onReset = () => {
  for (const doc of [...docs.values()]) forget(doc);
  setTimeout(() => {
    for (const uri of tracked) {
      const model = monaco.editor.getModel(monaco.Uri.parse(uri));
      if (model) void ensureDoc(model);
    }
    semanticChanged.forEach((e) => e.fire());
  }, 1500);
};

/** Load a file the server pointed at as a (hidden) model, so peek / ⌘-hover previews work. */
async function ensureModel(uri: Monaco.Uri, fromRoot: string) {
  if (uri.scheme !== "file" || monaco.editor.getModel(uri)) return;
  if (!ownerRoot(uri.path)) externalRoot.set(uri.toString(), fromRoot);
  const res = await conn.read(uri.toString());
  if (res.text != null && !monaco.editor.getModel(uri)) monaco.editor.createModel(res.text, undefined, uri);
}

// ---------------------------------------------------------------------------- edits across files
type FileEdits = Map<string, Json[]>; // uri (as Monaco writes it) -> LSP TextEdits

/** A WorkspaceEdit's text edits by file. Creating, renaming or deleting files is refused (Atrium makes text edits). */
function editsOf(edit: Json): FileEdits {
  const out: FileEdits = new Map();
  const add = (uri: string, es: Json[]) => {
    const key = monaco.Uri.parse(uri).toString();
    out.set(key, [...(out.get(key) ?? []), ...es]);
  };
  for (const c of edit?.documentChanges ?? []) {
    if (c.kind) throw new Error(`it would ${c.kind} a file, which Atrium doesn't do`);
    add(c.textDocument.uri, c.edits ?? []);
  }
  for (const [uri, es] of Object.entries(edit?.changes ?? {})) add(uri, es as Json[]);
  return out;
}

/** The model the Files editor shows for this file, if it shows it: editing that is editing the file (its tab). */
const shownModel = (uri: string) =>
  [...fileEditors].map((e) => e.getModel()).find((m): m is Monaco.editor.ITextModel => !!m && !m.isDisposed() && m.uri.toString() === uri) ?? null;

/** Makes a WorkspaceEdit: where the Files editor shows the file, in the editor (undo takes it back); else in the file
 *  itself, through the Files panel (an open tab's text, or the file on disk), and in any hidden copy of it (a peek,
 *  what the language server was told). `skip`: the file whose edits Monaco makes itself (the one a rename or a quick
 *  fix was asked in). `files`: the files changed. */
async function applyEdit(edit: Json, skip?: string): Promise<{ applied: boolean; failureReason?: string; files: number }> {
  let byFile: FileEdits;
  try {
    byFile = editsOf(edit);
  } catch (e: any) {
    return { applied: false, failureReason: e.message, files: 0 };
  }
  let files = 0;
  for (const [uri, es] of byFile) {
    if (uri === skip || !es.length) continue;
    const u = monaco.Uri.parse(uri);
    try {
      const shown = shownModel(uri);
      if (shown) shown.pushEditOperations([], es.map((e) => ({ range: toRange(e.range), text: e.newText, forceMoveMarkers: true })), () => null);
      else {
        if (!editorBridge.editFile) throw new Error("the Files panel isn't open");
        const text = await editorBridge.editFile(u.path, es);
        const hidden = monaco.editor.getModel(u);
        if (hidden && !hidden.isDisposed() && hidden.getValue() !== text) hidden.pushEditOperations([], [{ range: hidden.getFullModelRange(), text }], () => null);
      }
      files++;
    } catch (e: any) {
      return { applied: false, failureReason: `${u.path}: ${e.message}`, files };
    }
  }
  return { applied: true, files };
}

conn.onApplyEdit = (id, edit) => void applyEdit(edit).then(({ applied, failureReason }) => conn.answerEdit(id, { applied, failureReason }));

// A code action whose edit reaches other files, or that runs a server command: Monaco makes the edit in the file it was
// asked in, then runs this with the rest.
const RUN = "atrium.lsp.run";
monaco.editor.registerCommand(RUN, async (_accessor, key: string, rest: { skip: string; edit: Json } | null, command: Json | null) => {
  if (rest) {
    const done = await applyEdit(rest.edit, rest.skip);
    if (!done.applied) return useApp.getState().toast(`Not applied: ${done.failureReason}`, "error");
  }
  const server = servers.get(key);
  if (command && server) await conn.request(server, "workspace/executeCommand", { command: command.command, arguments: command.arguments });
});

// ---------------------------------------------------------------------------- providers
const isStub = (u: Monaco.Uri) => u.path.endsWith(".pyi");

type LspAction = Monaco.languages.CodeAction & { _lsp?: Json; _server?: Server; _uri?: string };

function toAction(a: Json, model: Monaco.editor.ITextModel, server: Server): LspAction {
  if (typeof a.command === "string") return { title: a.title, command: { id: RUN, title: a.title, arguments: [server.key, null, a] } }; // a bare Command
  const uri = model.uri.toString();
  let own: Json[] = [];
  let rest: Json = null;
  let disabled = a.disabled?.reason as string | undefined;
  if (a.edit) {
    try {
      const byFile = editsOf(a.edit);
      own = byFile.get(uri) ?? [];
      if ([...byFile.keys()].some((k) => k !== uri)) rest = { skip: uri, edit: a.edit };
    } catch (e: any) {
      disabled = e.message;
    }
  }
  return {
    title: a.title,
    kind: a.kind,
    isPreferred: a.isPreferred,
    disabled,
    edit: own.length ? { edits: own.map((e) => ({ resource: model.uri, versionId: undefined, textEdit: { range: toRange(e.range), text: e.newText } })) } : undefined,
    command: rest || a.command ? { id: RUN, title: a.title, arguments: [server.key, rest, a.command ?? null] } : undefined,
    _lsp: a,
    _server: server,
    _uri: uri,
  };
}

function toSymbol(s: Json): Monaco.languages.DocumentSymbol {
  const range = toRange(s.location?.range ?? s.range);
  return {
    name: s.name,
    detail: s.detail ?? s.containerName ?? "",
    kind: Math.max(0, (s.kind ?? 13) - 1), // LSP counts SymbolKind from 1, Monaco from 0
    tags: s.tags ?? (s.deprecated ? [monaco.languages.SymbolTag.Deprecated] : []),
    range,
    selectionRange: s.selectionRange ? toRange(s.selectionRange) : range,
    children: s.children?.map(toSymbol),
  };
}

async function locations(model: Monaco.editor.ITextModel, position: Monaco.Position, token: Monaco.CancellationToken, method: string, extra: Json = {}) {
  const doc = await ensureDoc(model);
  if (!doc) return null;
  const r = await conn.request(doc.server, method, { ...docPos(doc, position), ...extra }, token);
  if (!r) return null;
  const list: Monaco.languages.LocationLink[] = (Array.isArray(r) ? r : [r]).map((l: Json) =>
    l.targetUri
      ? {
          uri: monaco.Uri.parse(l.targetUri),
          range: toRange(l.targetRange),
          targetSelectionRange: toRange(l.targetSelectionRange ?? l.targetRange),
          originSelectionRange: l.originSelectionRange ? toRange(l.originSelectionRange) : undefined,
        }
      : { uri: monaco.Uri.parse(l.uri), range: toRange(l.range) },
  );
  // a stub next to its implementation: go to the source, like Pylance's Go to Definition
  if (method !== "textDocument/references") list.sort((a, b) => Number(isStub(a.uri)) - Number(isStub(b.uri)));
  const uris = [...new Map(list.map((l) => [l.uri.toString(), l.uri])).values()].slice(0, MAX_PREVIEW_FILES);
  await Promise.all(uris.map((u) => ensureModel(u, doc.server.root)));
  return list;
}

const LSP_KIND = (() => {
  const K = monaco.languages.CompletionItemKind;
  return [K.Text, K.Text, K.Method, K.Function, K.Constructor, K.Field, K.Variable, K.Class, K.Interface, K.Module, K.Property, K.Unit, K.Value, K.Enum, K.Keyword, K.Snippet, K.Color, K.File, K.Reference, K.Folder, K.EnumMember, K.Constant, K.Struct, K.Event, K.Operator, K.TypeParameter];
})();

type LspCompletion = Monaco.languages.CompletionItem & { _lsp?: Json; _server?: Server };

function toCompletion(it: Json, fallback: Monaco.IRange, defaults: Json, server: Server): LspCompletion {
  const editRange = defaults?.editRange;
  const edit = it.textEdit ?? (editRange ? { ...("insert" in editRange ? editRange : { range: editRange }), newText: it.textEditText ?? it.label } : null);
  const range = edit ? ("range" in edit ? toRange(edit.range) : { insert: toRange(edit.insert), replace: toRange(edit.replace) }) : fallback;
  return {
    label: it.labelDetails ? { label: it.label, detail: it.labelDetails.detail, description: it.labelDetails.description } : it.label,
    kind: LSP_KIND[it.kind] ?? monaco.languages.CompletionItemKind.Text,
    detail: it.detail,
    documentation: documentation(it.documentation),
    insertText: edit?.newText ?? it.insertText ?? it.label,
    insertTextRules: (it.insertTextFormat ?? defaults?.insertTextFormat) === 2 ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
    range,
    sortText: it.sortText,
    filterText: it.filterText,
    preselect: it.preselect,
    commitCharacters: it.commitCharacters ?? defaults?.commitCharacters,
    additionalTextEdits: it.additionalTextEdits?.map((e: Json) => ({ range: toRange(e.range), text: e.newText })),
    tags: it.deprecated || it.tags?.includes(1) ? [monaco.languages.CompletionItemTag.Deprecated] : undefined,
    _lsp: it,
    _server: server,
  };
}

function registerProviders(language: string, first: Server) {
  if (registered.has(language)) return;
  registered.add(language);
  const caps = first.caps;
  const L = monaco.languages;
  // TypeScript / JavaScript: the server reads the project (tsconfig, node_modules); Monaco's own worker sees one file,
  // so its completions, hovers and errors would come twice and contradict the server's. Its colouring stays.
  if (language === "typescript" || language === "javascript") {
    const defaults = language === "typescript" ? L.typescript.typescriptDefaults : L.typescript.javascriptDefaults;
    defaults.setModeConfiguration({
      completionItems: false, hovers: false, documentSymbols: false, definitions: false, references: false, documentHighlights: false, rename: false,
      diagnostics: false, documentRangeFormattingEdits: false, signatureHelp: false, onTypeFormattingEdits: false, codeActions: false, inlayHints: false,
    });
  }

  if (caps.hoverProvider)
    L.registerHoverProvider(language, {
      provideHover: async (model, position, token) => {
        const doc = await ensureDoc(model);
        if (!doc) return null;
        const r = await conn.request(doc.server, "textDocument/hover", docPos(doc, position), token);
        const contents = markdown(r?.contents);
        return contents.length ? { contents, range: r.range ? toRange(r.range) : undefined } : null;
      },
    });
  if (caps.definitionProvider)
    L.registerDefinitionProvider(language, { provideDefinition: (m, p, t) => locations(m, p, t, "textDocument/definition") });
  if (caps.declarationProvider)
    L.registerDeclarationProvider(language, { provideDeclaration: (m, p, t) => locations(m, p, t, "textDocument/declaration") });
  if (caps.typeDefinitionProvider)
    L.registerTypeDefinitionProvider(language, { provideTypeDefinition: (m, p, t) => locations(m, p, t, "textDocument/typeDefinition") });
  if (caps.implementationProvider)
    L.registerImplementationProvider(language, { provideImplementation: (m, p, t) => locations(m, p, t, "textDocument/implementation") });
  if (caps.referencesProvider)
    L.registerReferenceProvider(language, {
      provideReferences: (m, p, ctx, t) => locations(m, p, t, "textDocument/references", { context: { includeDeclaration: ctx.includeDeclaration } }),
    });
  if (caps.documentHighlightProvider)
    L.registerDocumentHighlightProvider(language, {
      provideDocumentHighlights: async (model, position, token) => {
        const doc = await ensureDoc(model);
        if (!doc) return null;
        const r = await conn.request(doc.server, "textDocument/documentHighlight", docPos(doc, position), token);
        return (r || []).map((h: Json) => ({ range: toRange(h.range), kind: (h.kind ?? 1) - 1 }));
      },
    });
  if (caps.completionProvider)
    L.registerCompletionItemProvider(language, {
      triggerCharacters: caps.completionProvider.triggerCharacters ?? [],
      provideCompletionItems: async (model, position, context, token) => {
        const doc = await ensureDoc(model);
        if (!doc) return null;
        const r = await conn.request(
          doc.server,
          "textDocument/completion",
          { ...docPos(doc, position), context: { triggerKind: context.triggerKind + 1, triggerCharacter: context.triggerCharacter } },
          token,
        );
        if (!r) return null;
        const items: Json[] = Array.isArray(r) ? r : r.items ?? [];
        const word = model.getWordUntilPosition(position);
        const fallback = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
        return { incomplete: !Array.isArray(r) && !!r.isIncomplete, suggestions: items.map((it) => toCompletion(it, fallback, Array.isArray(r) ? null : r.itemDefaults, doc.server)) };
      },
      resolveCompletionItem: caps.completionProvider.resolveProvider
        ? async (item: LspCompletion, token) => {
            if (!item._lsp || !item._server) return item;
            const r = await conn.request(item._server, "completionItem/resolve", item._lsp, token);
            if (!r) return item;
            return {
              ...item,
              detail: r.detail ?? item.detail,
              documentation: documentation(r.documentation) ?? item.documentation,
              additionalTextEdits: r.additionalTextEdits?.map((e: Json) => ({ range: toRange(e.range), text: e.newText })) ?? item.additionalTextEdits,
            };
          }
        : undefined,
    });
  if (caps.signatureHelpProvider)
    L.registerSignatureHelpProvider(language, {
      signatureHelpTriggerCharacters: caps.signatureHelpProvider.triggerCharacters ?? [],
      signatureHelpRetriggerCharacters: caps.signatureHelpProvider.retriggerCharacters ?? [],
      provideSignatureHelp: async (model, position, token, context) => {
        const doc = await ensureDoc(model);
        if (!doc) return null;
        const r = await conn.request(
          doc.server,
          "textDocument/signatureHelp",
          { ...docPos(doc, position), context: { triggerKind: context.triggerKind, triggerCharacter: context.triggerCharacter, isRetrigger: context.isRetrigger } },
          token,
        );
        if (!r?.signatures?.length) return null;
        return {
          value: {
            signatures: r.signatures.map((s: Json) => ({
              label: s.label,
              documentation: documentation(s.documentation),
              parameters: (s.parameters || []).map((p: Json) => ({ label: p.label, documentation: documentation(p.documentation) })),
              activeParameter: s.activeParameter,
            })),
            activeSignature: r.activeSignature ?? 0,
            activeParameter: r.activeParameter ?? 0,
          },
          dispose() {},
        };
      },
    });
  if (caps.renameProvider)
    L.registerRenameProvider(language, {
      provideRenameEdits: async (model, position, newName, token) => {
        const doc = await ensureDoc(model);
        if (!doc) return { edits: [], rejectReason: "No language server for this file" };
        const r = await conn.request(doc.server, "textDocument/rename", { ...docPos(doc, position), newName }, token);
        if (!r) return { edits: [], rejectReason: "Nothing to rename here" };
        // the other files first (their tabs, or the files themselves); Monaco makes this one's edits, so undo has them
        const uri = model.uri.toString();
        const done = await applyEdit(r, uri);
        if (!done.applied) {
          useApp.getState().toast(`Rename stopped: ${done.failureReason}`, "error");
          return { edits: [], rejectReason: done.failureReason };
        }
        if (done.files) useApp.getState().toast(`Renamed in ${done.files + 1} files`, "ok");
        let own: Json[] = [];
        try {
          own = editsOf(r).get(uri) ?? [];
        } catch {
          /* told above */
        }
        return { edits: own.map((e) => ({ resource: model.uri, versionId: undefined, textEdit: { range: toRange(e.range), text: e.newText } })) };
      },
      resolveRenameLocation: caps.renameProvider.prepareProvider
        ? async (model, position, token) => {
            const doc = await ensureDoc(model);
            if (!doc) return null;
            const r = await conn.request(doc.server, "textDocument/prepareRename", docPos(doc, position), token);
            if (!r) return { range: new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column), text: "", rejectReason: "This can't be renamed" };
            if (r.defaultBehavior) return null; // the word at the cursor
            const range = toRange(r.range ?? r);
            return { range, text: r.placeholder ?? model.getValueInRange(range) };
          }
        : undefined,
    });
  if (caps.documentFormattingProvider)
    L.registerDocumentFormattingEditProvider(language, {
      provideDocumentFormattingEdits: async (model, options, token) => {
        const doc = await ensureDoc(model);
        if (!doc) return [];
        const r = await conn.request(doc.server, "textDocument/formatting", { textDocument: { uri: doc.uri }, options: { tabSize: options.tabSize, insertSpaces: options.insertSpaces } }, token);
        return (r || []).map((e: Json) => ({ range: toRange(e.range), text: e.newText }));
      },
    });
  if (caps.documentRangeFormattingProvider)
    L.registerDocumentRangeFormattingEditProvider(language, {
      provideDocumentRangeFormattingEdits: async (model, range, options, token) => {
        const doc = await ensureDoc(model);
        if (!doc) return [];
        const r = await conn.request(
          doc.server,
          "textDocument/rangeFormatting",
          { textDocument: { uri: doc.uri }, range: fromRange(range), options: { tabSize: options.tabSize, insertSpaces: options.insertSpaces } },
          token,
        );
        return (r || []).map((e: Json) => ({ range: toRange(e.range), text: e.newText }));
      },
    });
  if (caps.documentSymbolProvider)
    L.registerDocumentSymbolProvider(language, {
      provideDocumentSymbols: async (model, token) => {
        const doc = await ensureDoc(model);
        if (!doc) return [];
        const r = await conn.request(doc.server, "textDocument/documentSymbol", { textDocument: { uri: doc.uri } }, token);
        return (r || []).map(toSymbol);
      },
    });
  if (caps.codeActionProvider) {
    const kinds = caps.codeActionProvider.codeActionKinds;
    L.registerCodeActionProvider(
      language,
      {
        provideCodeActions: async (model, range, context, token) => {
          const none = { actions: [], dispose() {} };
          const doc = await ensureDoc(model);
          if (!doc) return none;
          // what the server said about here: its own diagnostics, as it sent them (some carry data it wants back)
          const diags = (diagnostics.get(doc.uri) ?? []).filter((d: Json) => monaco.Range.areIntersectingOrTouching(toRange(d.range), range));
          const r = await conn.request(
            doc.server,
            "textDocument/codeAction",
            {
              textDocument: { uri: doc.uri },
              range: fromRange(range),
              context: { diagnostics: diags, only: context.only ? [context.only] : undefined, triggerKind: context.trigger === monaco.languages.CodeActionTriggerType.Invoke ? 1 : 2 },
            },
            token,
          );
          if (!Array.isArray(r)) return none;
          return { actions: r.map((a: Json) => toAction(a, model, doc.server)), dispose() {} };
        },
        resolveCodeAction: caps.codeActionProvider.resolveProvider
          ? async (action: LspAction, token) => {
              if (!action._lsp || !action._server || action._lsp.edit || typeof action._lsp.command === "string") return action;
              const r = await conn.request(action._server, "codeAction/resolve", action._lsp, token);
              const model = action._uri ? monaco.editor.getModel(monaco.Uri.parse(action._uri)) : null;
              return r && model ? toAction(r, model, action._server) : action;
            }
          : undefined,
      },
      Array.isArray(kinds) ? { providedCodeActionKinds: kinds } : undefined,
    );
  }
  const st = caps.semanticTokensProvider;
  if (st?.legend && st.full) {
    const changed = new monaco.Emitter<void>();
    semanticChanged.set(language, changed);
    L.registerDocumentSemanticTokensProvider(language, {
      onDidChange: changed.event,
      getLegend: () => st.legend,
      provideDocumentSemanticTokens: async (model, _lastResultId, token) => {
        if (!tracked.has(model.uri.toString())) return null;
        const doc = await ensureDoc(model);
        if (!doc) return null;
        const r = await conn.request(doc.server, "textDocument/semanticTokens/full", { textDocument: { uri: doc.uri } }, token);
        if (r?.data) {
          doc.semanticRetries = 0;
          return { data: new Uint32Array(r.data), resultId: r.resultId };
        }
        // pyright may fail a request that races the didOpen it follows: ask again shortly
        if (!token.isCancellationRequested && ++doc.semanticRetries <= 3) setTimeout(() => changed.fire(), 400 * doc.semanticRetries);
        return null;
      },
      releaseDocumentSemanticTokens: () => {},
    });
  }
}

// ---------------------------------------------------------------------------- navigation
async function openLocation(uri: Monaco.Uri, target: { line?: number; column?: number }, fromRoot?: string) {
  const app = useApp.getState();
  const path = uri.path;
  if (ownerRoot(path)) return app.openFile(path, undefined, target);
  if (fromRoot) externalRoot.set(uri.toString(), fromRoot);
  const content = monaco.editor.getModel(uri)?.getValue() ?? (await conn.read(uri.toString())).text;
  if (content == null) return app.toast(`Can't open ${path}`, "error");
  app.openFile(path, undefined, { ...target, external: true, content });
}

// ⌘-click / F12 into another file opens it in the Files panel (read-only when outside the project)
monaco.editor.registerEditorOpener({
  openCodeEditor(source, resource, selectionOrPosition) {
    if (resource.scheme !== "file") return false;
    const s = selectionOrPosition as Partial<Monaco.IRange & Monaco.IPosition> | undefined;
    const target = s ? { line: s.startLineNumber ?? s.lineNumber, column: s.startColumn ?? s.column } : {};
    void openLocation(resource, target, docs.get(source.getModel()?.uri.toString() ?? "")?.server.root);
    return true;
  },
});

editorBridge.closeFile = (path) => {
  const uri = monaco.Uri.file(path);
  setTimeout(() => {
    const model = monaco.editor.getModel(uri);
    if (!model) return closeDoc(uri.toString());
    if (!monaco.editor.getEditors().some((e) => e.getModel() === model)) model.dispose(); // → closeDoc
  }, 50);
};

/** Start language-server support for whatever file the editor shows (now and after model switches). */
export function trackEditor(editor: Monaco.editor.ICodeEditor): Monaco.IDisposable {
  fileEditors.add(editor);
  editor.onDidDispose(() => fileEditors.delete(editor));
  const track = () => {
    const model = editor.getModel();
    if (!model || model.uri.scheme !== "file") return;
    const uri = model.uri.toString();
    tracked.add(uri);
    void ensureDoc(model).then((doc) => {
      if (!doc) return;
      applyDiagnostics(uri);
      semanticChanged.get(doc.server.language)?.fire();
    });
  };
  track();
  return editor.onDidChangeModel(track);
}
