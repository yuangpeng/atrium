// Monaco, bundled locally (no CDN) with its web workers and Atrium themes.
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import CssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import HtmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import TsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";
import { loader } from "@monaco-editor/react";
import type * as Monaco from "monaco-editor";
import { pythonLanguage } from "./editor/python";

(self as any).MonacoEnvironment = {
  getWorker(_: string, label: string) {
    if (label === "json") return new JsonWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    if (label === "typescript" || label === "javascript") return new TsWorker();
    return new EditorWorker();
  },
};

// Token colours follow VS Code's Light+ / Dark+ (what Pylance users are used to), on Atrium's
// warm backgrounds. One table drives both the tokenizer scopes and the language server's
// semantic token types ("type.modifier…", matched by prefix in legend order).
type Palette = Record<"fg" | "comment" | "keyword" | "control" | "fn" | "type" | "variable" | "constant" | "string" | "escape" | "number" | "regexp" | "operator", string>;
const DARK: Palette = {
  fg: "D4D4D4", comment: "6A9955", keyword: "569CD6", control: "C586C0", fn: "DCDCAA", type: "4EC9B0", variable: "9CDCFE",
  constant: "4FC1FF", string: "CE9178", escape: "D7BA7D", number: "B5CEA8", regexp: "D16969", operator: "D4D4D4",
};
const LIGHT: Palette = {
  fg: "1F1E1D", comment: "008000", keyword: "0000FF", control: "AF00DB", fn: "795E26", type: "267F99", variable: "001080",
  constant: "0070C1", string: "A31515", escape: "EE0000", number: "098658", regexp: "811F3F", operator: "1F1E1D",
};

function tokenRules(c: Palette): Monaco.editor.ITokenThemeRule[] {
  const r = (token: string, foreground: string, fontStyle?: string) => ({ token, foreground, ...(fontStyle ? { fontStyle } : {}) });
  const readonly = (t: string) => ["", "declaration.", "definition.", "declaration.definition."].map((m) => r(`${t}.${m}readonly`, c.constant));
  return [
    // tokenizer scopes
    r("", c.fg),
    r("identifier", c.fg),
    r("comment", c.comment),
    r("keyword", c.keyword),
    r("keyword.control", c.control),
    r("storage.string", c.keyword),
    r("constant.language", c.keyword),
    r("variable.language", c.keyword),
    r("constant", c.constant),
    r("string", c.string),
    r("string.escape", c.escape),
    r("string.format", c.keyword),
    r("delimiter.fstring", c.keyword),
    r("regexp", c.regexp),
    r("number", c.number),
    r("operator", c.operator),
    r("delimiter", c.fg),
    r("entity.name.function", c.fn),
    r("function.call", c.fn),
    r("support.function", c.fn),
    r("decorator", c.fn),
    r("entity.name.class", c.type),
    r("entity.name.type", c.type),
    r("support.type", c.type),
    r("type", c.type),
    r("type.identifier", c.type),
    // semantic token types (basedpyright / pyright, clangd, gopls, rust-analyzer)
    r("namespace", c.type),
    r("class", c.type),
    r("enum", c.type),
    r("interface", c.type),
    r("struct", c.type),
    r("typeParameter", c.type),
    r("concept", c.type),
    r("function", c.fn),
    r("method", c.fn),
    r("macro", c.keyword),
    r("parameter", c.variable),
    r("variable", c.variable),
    r("property", c.variable),
    r("selfParameter", c.keyword),
    r("clsParameter", c.keyword),
    r("enumMember", c.constant),
    r("modifier", c.keyword),
    r("label", c.fg),
    r("unknown", c.fg),
    r("bracket", c.fg),
    ...readonly("variable"),
    ...readonly("property"),
  ];
}

monaco.editor.defineTheme("atrium-light", {
  base: "vs",
  inherit: true,
  rules: tokenRules(LIGHT),
  colors: {
    "editor.background": "#FAF9F5",
    "editor.foreground": "#1F1E1D",
    "editor.lineHighlightBackground": "#F0EEE680",
    "editorLineNumber.foreground": "#B8B5AA",
    "editorLineNumber.activeForeground": "#73726C",
    "editorGutter.background": "#FAF9F5",
    "editor.selectionBackground": "#D9775733",
    "editor.wordHighlightBackground": "#57606A1F",
    "editor.wordHighlightStrongBackground": "#0E639C24",
    "editorCursor.foreground": "#C6613F",
    "diffEditor.insertedTextBackground": "#3F7D5826",
    "diffEditor.removedTextBackground": "#B5443A26",
    "diffEditor.insertedLineBackground": "#3F7D5814",
    "diffEditor.removedLineBackground": "#B5443A14",
    "editorWidget.background": "#FFFFFF",
    "editorHoverWidget.background": "#FFFFFF",
    "editorHoverWidget.border": "#E6E3D8",
    "editorLink.activeForeground": "#3B6FA8",
    "scrollbarSlider.background": "#73726C30",
  },
});

monaco.editor.defineTheme("atrium-dark", {
  base: "vs-dark",
  inherit: true,
  rules: tokenRules(DARK),
  colors: {
    "editor.background": "#262624",
    "editor.foreground": "#D4D4D4",
    "editor.lineHighlightBackground": "#30302E",
    "editorLineNumber.foreground": "#5C5B56",
    "editorLineNumber.activeForeground": "#A6A39A",
    "editorGutter.background": "#262624",
    "editor.selectionBackground": "#D9775744",
    "editor.wordHighlightBackground": "#575757B8",
    "editor.wordHighlightStrongBackground": "#004972B8",
    "editorCursor.foreground": "#E08A6D",
    "diffEditor.insertedTextBackground": "#7DBB9133",
    "diffEditor.removedTextBackground": "#E8857A33",
    "diffEditor.insertedLineBackground": "#7DBB9118",
    "diffEditor.removedLineBackground": "#E8857A18",
    "editorWidget.background": "#30302E",
    "editorHoverWidget.background": "#30302E",
    "editorHoverWidget.border": "#4C4B47",
    "editorLink.activeForeground": "#86AEDD",
  },
});

// Replace the built-in Python tokenizer: register ours directly (wins over the lazily resolved
// built-in) and take over the built-in's factory so a late resolution can't put it back.
monaco.languages.register({ id: "python", extensions: [".pyi"] });
monaco.languages.setMonarchTokensProvider("python", pythonLanguage);
monaco.languages.registerTokensProviderFactory("python", { create: () => pythonLanguage });

(globalThis as any).__atriumMonaco = monaco; // handy from devtools

// Monaco rejects in-flight work with a "Canceled" error when the user moves on (e.g. ⌘-click opens
// another tab mid-request); that's expected, not worth an "Uncaught (in promise)" in the console.
window.addEventListener("unhandledrejection", (e) => {
  if (e.reason?.name === "Canceled" || e.reason?.message === "Canceled") e.preventDefault();
});

loader.config({ monaco });

export { monaco };
