import { monaco } from "../monaco-setup";
import { useEffect, useRef } from "react";
import Editor, { DiffEditor as MonacoDiff, OnMount, DiffOnMount } from "@monaco-editor/react";
import type * as Monaco from "monaco-editor";
import { useApp } from "../store";
import { trackEditor } from "../lsp/client";

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
    editor.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.KeyS, () => saveRef.current?.());
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

export function DiffEditor({
  id,
  original,
  modified,
  language,
  editable,
  sideBySide,
  onChange,
  onSave,
}: {
  id: string;
  original: string;
  modified: string;
  language: string;
  editable?: boolean;
  sideBySide?: boolean;
  onChange?: (v: string) => void;
  onSave?: () => void;
}) {
  const theme = useMonacoTheme();
  const saveRef = useRef(onSave);
  saveRef.current = onSave;
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  const onMount: DiffOnMount = (editor, monaco) => {
    const mod = editor.getModifiedEditor();
    mod.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current?.());
    mod.onDidChangeModelContent(() => changeRef.current?.(mod.getValue()));
  };
  return (
    <MonacoDiff
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
        renderSideBySide: sideBySide ?? true,
        useInlineViewWhenSpaceIsLimited: true,
        hideUnchangedRegions: { enabled: true, contextLineCount: 3, minimumLineCount: 5 },
        renderOverviewRuler: true,
        ignoreTrimWhitespace: false,
      }}
    />
  );
}
