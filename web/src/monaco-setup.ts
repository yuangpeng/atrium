// Monaco, bundled locally (no CDN) with its web workers and Atrium themes.
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import CssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import HtmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import TsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";
import { loader } from "@monaco-editor/react";

(self as any).MonacoEnvironment = {
  getWorker(_: string, label: string) {
    if (label === "json") return new JsonWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    if (label === "typescript" || label === "javascript") return new TsWorker();
    return new EditorWorker();
  },
};

monaco.editor.defineTheme("atrium-light", {
  base: "vs",
  inherit: true,
  rules: [
    { token: "comment", foreground: "A09E96", fontStyle: "italic" },
    { token: "keyword", foreground: "A0522D" },
    { token: "string", foreground: "3F7D58" },
    { token: "number", foreground: "3B6FA8" },
    { token: "type", foreground: "8B5CB8" },
  ],
  colors: {
    "editor.background": "#FAF9F5",
    "editor.lineHighlightBackground": "#F0EEE680",
    "editorLineNumber.foreground": "#B8B5AA",
    "editorLineNumber.activeForeground": "#73726C",
    "editorGutter.background": "#FAF9F5",
    "editor.selectionBackground": "#D9775733",
    "editorCursor.foreground": "#C6613F",
    "diffEditor.insertedTextBackground": "#3F7D5826",
    "diffEditor.removedTextBackground": "#B5443A26",
    "diffEditor.insertedLineBackground": "#3F7D5814",
    "diffEditor.removedLineBackground": "#B5443A14",
    "editorWidget.background": "#FFFFFF",
    "scrollbarSlider.background": "#73726C30",
  },
});

monaco.editor.defineTheme("atrium-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "comment", foreground: "75736C", fontStyle: "italic" },
    { token: "keyword", foreground: "E8A07A" },
    { token: "string", foreground: "9CCF9F" },
    { token: "number", foreground: "8FB6E6" },
    { token: "type", foreground: "C9A5E8" },
  ],
  colors: {
    "editor.background": "#262624",
    "editor.lineHighlightBackground": "#30302E",
    "editorLineNumber.foreground": "#5C5B56",
    "editorLineNumber.activeForeground": "#A6A39A",
    "editorGutter.background": "#262624",
    "editor.selectionBackground": "#D9775744",
    "editorCursor.foreground": "#E08A6D",
    "diffEditor.insertedTextBackground": "#7DBB9133",
    "diffEditor.removedTextBackground": "#E8857A33",
    "diffEditor.insertedLineBackground": "#7DBB9118",
    "diffEditor.removedLineBackground": "#E8857A18",
    "editorWidget.background": "#30302E",
  },
});

loader.config({ monaco });

export { monaco };
