import { monaco } from "../monaco-setup";
import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import Editor, { DiffEditor as MonacoDiff, DiffEditorProps, OnMount, DiffOnMount } from "@monaco-editor/react";
import type * as Monaco from "monaco-editor";
import { ArrowDown, ArrowUp } from "lucide-react";
import { useApp } from "../store";
import { trackEditor } from "../lsp/client";
import { useDiffPrefs } from "./DiffOptions";

function useMonacoTheme(): string {
  useApp((s) => s.theme); // re-render on theme changes
  return document.documentElement.dataset.theme === "dark" ? "atrium-dark" : "atrium-light";
}

const baseOptions: Monaco.editor.IStandaloneEditorConstructionOptions = {
  fontFamily: 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace',
  fontSize: 12.5,
  lineHeight: 19,
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  smoothScrolling: true,
  renderLineHighlight: "line",
  padding: { top: 8 },
  automaticLayout: true,
  tabSize: 4,
  scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
  "semanticHighlighting.enabled": true,
  fixedOverflowWidgets: true, // hovers / suggestions may extend past the panel
};

// files on disk: ⌘-click jumps straight to the definition (peek for several references)
const fileOptions: Monaco.editor.IStandaloneEditorConstructionOptions = {
  gotoLocation: { multipleDefinitions: "goto", multipleTypeDefinitions: "goto", multipleDeclarations: "goto", multipleImplementations: "peek", multipleReferences: "peek" },
  definitionLinkOpensInPeek: false,
  hover: { delay: 350 },
};

export interface Reveal {
  line: number;
  column?: number;
  nonce: number;
}

// ⌘S as an editor action, not addCommand: addCommand keybindings are global (the editor that
// registered last wins, even after it is gone); actions belong to their editor.
function addSave(editor: Monaco.editor.IStandaloneCodeEditor, m: typeof Monaco, save: { current?: () => void }) {
  editor.addAction({ id: "atrium.save", label: "Save", keybindings: [m.KeyMod.CtrlCmd | m.KeyCode.KeyS], run: () => save.current?.() });
}

function revealIn(editor: Monaco.editor.IStandaloneCodeEditor, r: Reveal) {
  const pos = { lineNumber: r.line, column: r.column ?? 1 };
  editor.setPosition(pos);
  editor.revealPositionInCenter(pos);
  editor.focus();
}

export function CodeEditor({
  path,
  filePath,
  value,
  language,
  readOnly,
  onChange,
  onSave,
  reveal,
}: {
  path?: string;
  /** absolute path of a file on disk: becomes the model's file:// URI and enables language servers */
  filePath?: string;
  value: string;
  language: string;
  readOnly?: boolean;
  onChange?: (v: string) => void;
  onSave?: () => void;
  reveal?: Reveal;
}) {
  const theme = useMonacoTheme();
  const saveRef = useRef(onSave);
  saveRef.current = onSave;
  const edRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const tracking = useRef<Monaco.IDisposable | null>(null);
  const onMount: OnMount = (editor, m) => {
    edRef.current = editor;
    addSave(editor, m, saveRef);
    if (filePath) tracking.current = trackEditor(editor);
    if (reveal) revealIn(editor, reveal);
  };
  useEffect(() => () => tracking.current?.dispose(), []);
  useEffect(() => {
    if (reveal && edRef.current) revealIn(edRef.current, reveal);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal?.nonce, filePath]);
  return (
    <Editor
      path={filePath ? monaco.Uri.file(filePath).toString() : path}
      value={value}
      language={language}
      theme={theme}
      onChange={(v) => onChange?.(v ?? "")}
      onMount={onMount}
      options={{ ...baseOptions, ...(filePath ? fileOptions : {}), readOnly }}
    />
  );
}

// One diff editor per file. On unmount it detaches and disposes its models itself, before
// @monaco-editor/react's cleanup runs (deletion effects run parent-first): that one disposes the
// models while still attached, which Monaco reports as "TextModel got disposed before
// DiffEditorWidget model got reset".
function DiffHost({ onMount, ...props }: DiffEditorProps) {
  const ed = useRef<Monaco.editor.IStandaloneDiffEditor | null>(null);
  useEffect(
    () => () => {
      const models = ed.current?.getModel();
      ed.current?.setModel(null);
      models?.original.dispose();
      models?.modified.dispose();
    },
    [],
  );
  return <MonacoDiff {...props} onMount={(e, m) => ((ed.current = e), onMount?.(e, m))} />;
}

export function DiffEditor({
  id,
  original,
  modified,
  language,
  editable,
  labels,
  onChange,
  onSave,
  reveal,
}: {
  id: string;
  original: string;
  modified: string;
  language: string;
  editable?: boolean;
  /** column headers: what the left (before) and right (after) side show */
  labels?: { original: ReactNode; modified: ReactNode };
  onChange?: (v: string) => void;
  onSave?: () => void;
  /** a line of the right (after) side to go to */
  reveal?: Reveal;
}) {
  const theme = useMonacoTheme();
  const prefs = useDiffPrefs();
  const saveRef = useRef(onSave);
  saveRef.current = onSave;
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  const revealRef = useRef(reveal);
  revealRef.current = reveal;
  const edRef = useRef<Monaco.editor.IStandaloneDiffEditor | null>(null);
  const [split, setSplit] = useState<number | null>(null); // left editor's width: the headers follow the sash
  const [changes, setChanges] = useState<{ n: number; at: number } | null>(null);
  useEffect(() => setChanges(null), [id]);

  const onMount: DiffOnMount = (editor, m) => {
    edRef.current = editor;
    const orig = editor.getOriginalEditor();
    const mod = editor.getModifiedEditor();
    for (const ed of [orig, mod]) {
      addSave(ed, m, saveRef);
      ed.addAction({ id: "atrium.nextChange", label: "Next change", keybindings: [m.KeyMod.Alt | m.KeyCode.F5], run: () => editor.goToDiff("next") });
      ed.addAction({ id: "atrium.prevChange", label: "Previous change", keybindings: [m.KeyMod.Shift | m.KeyMod.Alt | m.KeyCode.F5], run: () => editor.goToDiff("previous") });
    }
    mod.onDidChangeModelContent(() => changeRef.current?.(mod.getValue()));
    // "2 / 5 changes": how many changes start at or above the cursor (goToDiff moves it)
    const count = () => {
      const all = editor.getLineChanges() ?? [];
      const line = mod.getPosition()?.lineNumber ?? 0;
      setChanges({ n: all.length, at: all.filter((c) => Math.max(1, c.modifiedStartLineNumber) <= line).length });
    };
    editor.onDidUpdateDiff(count);
    mod.onDidChangeCursorPosition(count);
    orig.onDidLayoutChange((l) => setSplit(l.width));
    setSplit(orig.getLayoutInfo().width);
    // a line asked for with the file: go there once the diff is computed (hiding unchanged regions scrolls)
    const first = editor.onDidUpdateDiff(() => {
      first.dispose();
      if (revealRef.current) revealIn(mod, revealRef.current);
    });
  };
  useEffect(() => {
    if (reveal && edRef.current) revealIn(edRef.current.getModifiedEditor(), reveal);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal?.nonce]);
  const go = (target: "next" | "previous") => {
    edRef.current?.goToDiff(target);
    edRef.current?.getModifiedEditor().focus();
  };

  const nav = (
    <span className="diff-nav">
      {changes && <span className="n">{changes.n === 0 ? "no changes" : `${changes.at ? `${changes.at} / ` : ""}${changes.n} change${changes.n === 1 ? "" : "s"}`}</span>}
      <button className="icon-btn sm" title="Previous change (⇧⌥F5)" disabled={!changes?.n} onClick={() => go("previous")}>
        <ArrowUp size={13} />
      </button>
      <button className="icon-btn sm" title="Next change (⌥F5)" disabled={!changes?.n} onClick={() => go("next")}>
        <ArrowDown size={13} />
      </button>
    </span>
  );
  return (
    <div className="diff-wrap">
      <div className="diff-cols">
        {prefs.sideBySide ? (
          <>
            <div className="col" style={{ width: split ?? "50%" }}>
              {labels?.original}
            </div>
            <div className="col grow">
              {labels?.modified}
              {nav}
            </div>
          </>
        ) : (
          <div className="col grow">
            {labels && (
              <>
                {labels.original}
                <span className="faint">→</span>
                {labels.modified}
              </>
            )}
            {nav}
          </div>
        )}
      </div>
      <div className="diff-body">
        <DiffHost
          key={id}
          original={original}
          modified={modified}
          language={language}
          theme={theme}
          onMount={onMount}
          originalModelPath={`atrium-diff://original/${id}`}
          modifiedModelPath={`atrium-diff://modified/${id}`}
          options={{
            ...baseOptions,
            readOnly: !editable,
            originalEditable: false,
            renderSideBySide: prefs.sideBySide,
            useInlineViewWhenSpaceIsLimited: false, // side by side means side by side, even in a narrow panel
            hideUnchangedRegions: { enabled: prefs.hideUnchanged, contextLineCount: 3, minimumLineCount: 5 },
            ignoreTrimWhitespace: prefs.ignoreWhitespace,
            wordWrap: prefs.wrap ? "on" : "off",
            renderOverviewRuler: true,
          }}
        />
      </div>
    </div>
  );
}
