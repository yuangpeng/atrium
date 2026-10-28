import "../monaco-setup";
import { useEffect, useRef } from "react";
import Editor, { DiffEditor as MonacoDiff, OnMount, DiffOnMount } from "@monaco-editor/react";
import type * as Monaco from "monaco-editor";
import { useApp } from "../store";

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
};

export function CodeEditor({
  path,
  value,
  language,
  readOnly,
  onChange,
  onSave,
  line,
}: {
  path: string;
  value: string;
  language: string;
  readOnly?: boolean;
  onChange?: (v: string) => void;
  onSave?: () => void;
  line?: number;
}) {
  const theme = useMonacoTheme();
  const saveRef = useRef(onSave);
  saveRef.current = onSave;
  const edRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const onMount: OnMount = (editor, monaco) => {
    edRef.current = editor;
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current?.());
    if (line) {
      editor.revealLineInCenter(line);
      editor.setPosition({ lineNumber: line, column: 1 });
    }
  };
  useEffect(() => {
    if (line && edRef.current) {
      edRef.current.revealLineInCenter(line);
      edRef.current.setPosition({ lineNumber: line, column: 1 });
    }
  }, [line]);
  return (
    <Editor
      path={path}
      value={value}
      language={language}
      theme={theme}
      onChange={(v) => onChange?.(v ?? "")}
      onMount={onMount}
      options={{ ...baseOptions, readOnly }}
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
