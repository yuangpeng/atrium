// LSP text edits applied to a string: what a rename or a quick fix changes in a file that isn't shown in the editor (an
// open tab behind another, or a file not open at all). Positions are lines and UTF-16 code units, which is what a
// JavaScript string counts: no conversion. No Monaco here (the Files panel, in the main bundle, uses it).

export interface LspPosition {
  line: number;
  character: number;
}
export interface LspTextEdit {
  range: { start: LspPosition; end: LspPosition };
  newText: string;
}

export function applyTextEdits(text: string, edits: LspTextEdit[]): string {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text[i] === "\n") starts.push(i + 1);
  // a character past the end of its line means the end of the line (the spec's rule); a line past the end, the end
  const at = (p: LspPosition) => {
    if (p.line >= starts.length) return text.length;
    let end = p.line + 1 < starts.length ? starts[p.line + 1] - 1 : text.length;
    if (end > starts[p.line] && text[end - 1] === "\r") end--;
    return Math.min(starts[p.line] + Math.max(0, p.character), end);
  };
  // from the end, so each edit's positions still hold; edits at one place keep their order
  const items = edits.map((e, i) => ({ from: at(e.range.start), to: at(e.range.end), text: e.newText, i }));
  items.sort((a, b) => b.from - a.from || b.i - a.i);
  let out = text;
  for (const it of items) out = out.slice(0, it.from) + it.text + out.slice(Math.max(it.from, it.to));
  return out;
}
