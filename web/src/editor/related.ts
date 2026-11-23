// Code from the other files open in the editor that looks like the code at the cursor, for the inline completion's model
// (as Copilot's "neighboring tabs"): what the human has open is often what they are using, its names and its patterns.
// Each file's window of lines that shares the most names with the lines before the cursor (the Jaccard similarity of
// their sets of identifiers), the best two, if they share enough. Files of the project only, never one with secrets.
import { monaco } from "../monaco-setup";
import type * as Monaco from "monaco-editor";
import { labelOf, SECRET_FILES } from "./nextEdit";

const QUERY_LINES = 20; // before the cursor, compared
const WINDOW = 20; // lines of a snippet
const STEP = 10;
const MIN_SCORE = 0.15;
const MAX_SNIPPETS = 2;
const MAX_CHARS = 2000; // of all of them
const MAX_LINES = 5000; // a longer file isn't looked through
const IDENT = /[A-Za-z_$][\w$]{2,}/g;
// names every file has, which say nothing about what it is about
const COMMON = new Set(
  "const let var function return else for while import from export default class new this self def None True False async await type interface public private protected static void int string number boolean null undefined true false struct impl pub use mut match package func defer nil err elif pass with not and lambda yield try catch except finally throw raise break continue switch case extends implements readonly enum".split(" "),
);
const FAMILY: Record<string, string> = { javascript: "ts", typescript: "ts", javascriptreact: "ts", typescriptreact: "ts", c: "c", cpp: "c" };

export interface Related {
  label: string;
  text: string;
}

const names = new WeakMap<Monaco.editor.ITextModel, { version: number; lines: string[][] }>();

/** Each line's names, kept until the file changes. */
function namesOf(model: Monaco.editor.ITextModel): string[][] {
  const known = names.get(model);
  if (known && known.version === model.getVersionId()) return known.lines;
  const lines = model.getLinesContent().map((l) => (l.match(IDENT) ?? []).filter((t) => !COMMON.has(t)));
  names.set(model, { version: model.getVersionId(), lines });
  return lines;
}

const family = (language: string) => FAMILY[language] ?? language;

export function relatedSnippets(model: Monaco.editor.ITextModel, line: number): Related[] {
  const own = labelOf(model.uri.path);
  const query = new Set<string>();
  for (let l = Math.max(1, line - QUERY_LINES); l <= line; l++) for (const t of model.getLineContent(l).match(IDENT) ?? []) if (!COMMON.has(t)) query.add(t);
  if (query.size < 3) return [];
  const lang = family(model.getLanguageId());
  const found: { score: number; model: Monaco.editor.ITextModel; label: string; start: number }[] = [];
  for (const m of monaco.editor.getModels()) {
    if (m === model || m.isDisposed() || m.uri.scheme !== "file" || family(m.getLanguageId()) !== lang || m.getLineCount() > MAX_LINES) continue;
    const path = m.uri.path;
    const label = labelOf(path);
    // outside the project, the same file in another checkout, or secret
    if (label === path || label === own || SECRET_FILES.test(path.slice(path.lastIndexOf("/") + 1))) continue;
    const lines = namesOf(m);
    let best = { score: 0, start: 0 };
    for (let s = 0; s < lines.length; s += STEP) {
      const win = new Set(lines.slice(s, s + WINDOW).flat());
      let both = 0;
      for (const t of win) if (query.has(t)) both++;
      const score = both / (query.size + win.size - both);
      if (score > best.score) best = { score, start: s };
      if (s + WINDOW >= lines.length) break;
    }
    if (best.score >= MIN_SCORE) found.push({ ...best, model: m, label });
  }
  found.sort((a, b) => b.score - a.score);
  const out: Related[] = [];
  let chars = 0;
  for (const f of found.slice(0, MAX_SNIPPETS)) {
    const text = f.model.getLinesContent().slice(f.start, f.start + WINDOW).join("\n").slice(0, MAX_CHARS - chars);
    chars += text.length;
    if (text.trim()) out.push({ label: f.label, text });
    if (chars >= MAX_CHARS) break;
  }
  return out;
}
