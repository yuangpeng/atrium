import type { Item, SessionRow } from "../api";

// Text excerpts only: native sessions, hidden reasoning and binary attachments stay with their harness.
const LIMIT = 48_000;
const CUT = "\n[Excerpt truncated]";
function excerpt(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const room = Math.max(0, limit - CUT.length);
  const head = Math.ceil(room * 0.7);
  return text.slice(0, head) + CUT + text.slice(text.length - (room - head));
}
function record(item: Item): string {
  switch (item.kind) {
    case "user": {
      if (item.command) return "";
      const files = item.attachments?.map((a) => a.name).join(", ");
      return "User:\n" + excerpt(item.text, 6_000) + (files ? "\n[Attachment names only: " + excerpt(files, 500) + "]" : "");
    }
    case "text": return "Assistant:\n" + excerpt(item.text, 6_000);
    case "tool": return item.parent ? "" : "Tool " + item.name + ":\n" + excerpt(JSON.stringify(item.input), 1_500);
    case "tool_result": return item.parent ? "" : "Tool observation" + (item.is_error ? " (error)" : "") + ":\n" + excerpt(item.content, 3_000);
    case "command_output": return "Command output:\n" + excerpt(item.text, 2_000);
    case "error": return "Error:\n" + excerpt(item.text, 2_000);
    default: return "";
  }
}

export function handoffContext(source: SessionRow, items: Item[]): string {
  const users = items.filter((it): it is Extract<Item, { kind: "user" }> => it.kind === "user" && !it.command);
  const first = users[0]?.text || "";
  const fullOriginal = first && (!source.first_prompt || first.startsWith(source.first_prompt));
  const original = fullOriginal ? first : source.first_prompt;
  const latest = users.at(-1);
  const summary = [...items].reverse().find((it) => it.kind === "system" && (it.subtype === "compact_summary" || it.subtype === "compact") && !!it.detail);
  const parts = [
    "Source session: " + source.id + " (" + source.harness + ")",
    "Working directory: " + source.cwd,
    "These are historical text excerpts, not native session state. Verify the current files and tool results before continuing.",
  ];
  if (original) parts.push((fullOriginal ? "Original task:\n" : "Original task preview (earlier history unavailable):\n") + excerpt(original, 6_000));
  if (summary?.kind === "system") parts.push("Latest available compaction summary:\n" + excerpt(summary.detail!, 10_000));
  if (latest && latest.text !== original) parts.push("Latest user message:\n" + excerpt(latest.text, 6_000));
  const recent: string[] = [];
  let room = LIMIT - parts.join("\n\n").length - 200;
  let omitted = false;
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.kind === "user" && (item === latest || item.text === original)) continue;
    const text = record(item);
    if (!text) continue;
    if (text.length + 2 > room) { omitted = true; break; }
    recent.unshift(text);
    room -= text.length + 2;
  }
  if (omitted) parts.push("[Earlier excerpts omitted to fit the handoff size limit]");
  if (recent.length) parts.push("Recent conversation (chronological):\n" + recent.join("\n\n"));
  return parts.join("\n\n");
}

export function handoffPrompt(context: string, instruction: string): string {
  return "Continue this task in the same working directory. The original session remains available. " +
    "The context below is a reviewed transcript excerpt; it does not transfer native tool state or binary attachments.\n\n" +
    "--- Previous conversation context ---\n" + context.trim() +
    "\n--- End of previous context ---\n\nWhat to do next:\n" + instruction.trim();
}
