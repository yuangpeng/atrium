// What a model API request and its answer hold, in one shape for the three APIs the harnesses speak: Anthropic
// Messages (Claude Code), OpenAI Responses (Codex; Chat Completions roughly) and Gemini generateContent (Antigravity).
// Pure functions over the bodies capture.py recorded; the Requests panel renders what they return.

export type PartKind = "text" | "thinking" | "call" | "result" | "media" | "other";

/** One piece of a message: text, thinking, a tool call (its input in `data`), a tool result, an image… */
export interface Part {
  kind: PartKind;
  title?: string; // a call's tool, a result's call id, a media type, an unknown block's type
  text?: string;
  data?: unknown; // shown as a JSON tree: a call's input, a result that isn't text, a block not known here
  cached?: boolean; // an Anthropic cache breakpoint (cache_control) is set on it
  error?: boolean;
}

export interface Message {
  role: string;
  parts: Part[];
  raw: unknown; // the message as sent, for comparing requests
}

export interface ToolDef {
  name: string;
  description?: string;
  schema?: unknown;
}

export interface Request {
  params: [string, unknown][]; // everything but the system prompt, the tools and the messages
  system: Part[];
  tools: ToolDef[];
  messages: Message[];
}

export interface StreamEvent {
  name: string;
  data: unknown;
  at?: number; // ms after the request was sent, when the bytes it ended with came
}

export interface Answer {
  parts: Part[];
  events: StreamEvent[] | null; // null: not a stream
}

const obj = (v: unknown): Record<string, any> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, any>) : {});
const arr = (v: unknown): any[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v, null, 2));

function json(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** Every event of a text/event-stream body: its name, its data (parsed when JSON) and the character offset it ends at. */
export function sseEvents(text: string): { name: string; data: unknown; end: number }[] {
  const out: { name: string; data: unknown; end: number }[] = [];
  let name = "";
  let data: string[] = [];
  const re = /([^\r\n]*)(\r\n|\r|\n|$)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    const line = m[1];
    const end = re.lastIndex;
    if (line === "") {
      if (data.length) out.push({ name, data: json(data.join("\n")), end });
      name = "";
      data = [];
      if (m[2] === "") break;
      continue;
    }
    if (line.startsWith("data:")) data.push(line.slice(line.startsWith("data: ") ? 6 : 5));
    else if (line.startsWith("event:")) name = line.slice(6).trim();
    if (m[2] === "") {
      if (data.length) out.push({ name, data: json(data.join("\n")), end });
      break;
    }
  }
  return out;
}

// ---------------------------------------------------------------- Anthropic Messages

function anthropicParts(content: unknown): Part[] {
  if (typeof content === "string") return [{ kind: "text", text: content }];
  return arr(content).map((b): Part => {
    const cached = !!b?.cache_control;
    switch (b?.type) {
      case "text":
        return { kind: "text", text: b.text, cached };
      case "thinking":
        return { kind: "thinking", text: b.thinking, cached };
      case "redacted_thinking":
        return { kind: "thinking", title: "redacted", text: "", cached };
      case "tool_use":
      case "server_tool_use":
      case "mcp_tool_use":
        return { kind: "call", title: b.name, data: b.input, cached };
      case "tool_result":
      case "mcp_tool_result":
      case "web_search_tool_result": {
        const c = b.content;
        const texts = typeof c === "string" ? c : arr(c).every((x) => x?.type === "text") ? arr(c).map((x) => x.text).join("\n") : null;
        return { kind: "result", title: b.tool_use_id, text: texts ?? undefined, data: texts == null ? c : undefined, error: !!b.is_error, cached };
      }
      case "image":
      case "document": {
        const src = obj(b.source);
        const size = typeof src.data === "string" ? ` · ${fmtBytes((src.data.length * 3) / 4)}` : src.url ? ` · ${src.url}` : "";
        return { kind: "media", title: b.type, text: `${src.media_type || src.type || ""}${size}`, cached };
      }
      default:
        return { kind: "other", title: b?.type ?? typeof b, data: b, cached };
    }
  });
}

function anthropicRequest(body: Record<string, any>): Request {
  const system = typeof body.system === "string" ? [{ kind: "text" as const, text: body.system }] : anthropicParts(body.system);
  return {
    params: Object.entries(body).filter(([k]) => !["system", "tools", "messages"].includes(k)),
    system,
    tools: arr(body.tools).map((t) => ({ name: t.name ?? t.type, description: t.description, schema: t.input_schema ?? t })),
    messages: arr(body.messages).map((m) => ({ role: m.role, parts: anthropicParts(m.content), raw: m })),
  };
}

function anthropicAnswer(events: StreamEvent[]): Part[] {
  const blocks: Record<string, any>[] = [];
  for (const { data } of events) {
    const e = obj(data);
    if (e.type === "message") return anthropicParts(e.content); // not streamed
    if (e.type === "content_block_start") blocks[e.index] = { ...e.content_block, _json: "" };
    else if (e.type === "content_block_delta") {
      const b = blocks[e.index];
      const d = obj(e.delta);
      if (!b) continue;
      if (d.type === "text_delta") b.text = (b.text ?? "") + d.text;
      else if (d.type === "thinking_delta") b.thinking = (b.thinking ?? "") + d.thinking;
      else if (d.type === "input_json_delta") b._json += d.partial_json ?? "";
    }
  }
  return anthropicParts(
    blocks.filter(Boolean).map(({ _json, ...b }) => (_json ? { ...b, input: json(_json) } : b)),
  );
}

// ---------------------------------------------------------------- OpenAI Responses (and Chat Completions)

function responsesItem(item: Record<string, any>): Message {
  const t = item.type ?? (item.role ? "message" : "");
  let role = item.role ?? "assistant";
  let parts: Part[];
  switch (t) {
    case "message":
      parts =
        typeof item.content === "string"
          ? [{ kind: "text", text: item.content }]
          : arr(item.content).map((c): Part => {
              if (["input_text", "output_text", "text", "summary_text"].includes(c?.type)) return { kind: "text", text: c.text };
              if (c?.type === "input_image" || c?.type === "input_file")
                return { kind: "media", title: c.type, text: str(c.image_url ?? c.file_url ?? c.filename ?? "").slice(0, 200) };
              return { kind: "other", title: c?.type, data: c };
            });
      break;
    case "reasoning": {
      const summary = arr(item.summary).map((s) => s?.text ?? "").join("\n\n");
      const content = arr(item.content).map((s) => s?.text ?? "").join("\n\n");
      parts = [{ kind: "thinking", title: item.encrypted_content ? "encrypted" : undefined, text: summary || content }];
      break;
    }
    case "function_call":
      parts = [{ kind: "call", title: item.name, data: json(item.arguments ?? "") }];
      break;
    case "custom_tool_call":
      parts = [{ kind: "call", title: item.name, text: str(item.input) }];
      break;
    case "function_call_output":
    case "custom_tool_call_output":
    case "local_shell_call_output": {
      role = "tool";
      const out = item.output;
      parts = [{ kind: "result", title: item.call_id, text: typeof out === "string" ? out : undefined, data: typeof out === "string" ? undefined : out }];
      break;
    }
    default:
      parts = [{ kind: "other", title: t || "item", data: item }];
  }
  return { role, parts, raw: item };
}

function responsesRequest(body: Record<string, any>): Request {
  if (Array.isArray(body.messages)) return chatRequest(body);
  const input = typeof body.input === "string" ? [{ type: "message", role: "user", content: body.input }] : arr(body.input);
  return {
    params: Object.entries(body).filter(([k]) => !["instructions", "input", "tools"].includes(k)),
    system: body.instructions ? [{ kind: "text", text: str(body.instructions) }] : [],
    tools: arr(body.tools).map((t) => ({ name: t.name ?? t.type, description: t.description, schema: t.parameters ?? t.format ?? t })),
    messages: input.map((i) => responsesItem(obj(i))),
  };
}

function chatRequest(body: Record<string, any>): Request {
  const msgs = arr(body.messages);
  const system = msgs.filter((m) => m?.role === "system" || m?.role === "developer");
  return {
    params: Object.entries(body).filter(([k]) => !["messages", "tools"].includes(k)),
    system: system.map((m) => ({ kind: "text", text: str(m.content) })),
    tools: arr(body.tools).map((t) => ({ name: t.function?.name ?? t.type, description: t.function?.description, schema: t.function?.parameters ?? t })),
    messages: msgs
      .filter((m) => !system.includes(m))
      .map((m) => ({
        role: m.role,
        raw: m,
        parts: [
          ...(m.content ? [{ kind: m.role === "tool" ? "result" : "text", title: m.tool_call_id, text: str(m.content) } as Part] : []),
          ...arr(m.tool_calls).map((c): Part => ({ kind: "call", title: c.function?.name, data: json(c.function?.arguments ?? "") })),
        ],
      })),
  };
}

function responsesAnswer(events: StreamEvent[]): Part[] {
  let final: Record<string, any> | null = null;
  const done: Record<string, any>[] = [];
  let text = "";
  const chat: Part[] = [];
  for (const { data } of events) {
    const e = obj(data);
    const r = String(e.type ?? "").startsWith("response.") ? obj(e.response) : e.object === "response" ? e : null;
    if (r && r.output && !["in_progress", "queued", undefined].includes(r.status)) final = r;
    if (e.type === "response.output_item.done") done.push(obj(e.item));
    if (e.type === "response.output_text.delta") text += e.delta ?? "";
    for (const c of arr(e.choices)) {
      const d = obj(c.delta ?? c.message);
      if (d.content) chat.push({ kind: "text", text: d.content });
    }
  }
  const items = arr(final?.output).length ? arr(final!.output) : done;
  if (items.length) return items.flatMap((i) => responsesItem(obj(i)).parts);
  if (chat.length) return [{ kind: "text", text: chat.map((p) => p.text).join("") }];
  return text ? [{ kind: "text", text }] : [];
}

// ---------------------------------------------------------------- Gemini generateContent

function geminiParts(parts: unknown): Part[] {
  return arr(parts).flatMap((p): Part[] => {
    if (typeof p?.text === "string") return [{ kind: p.thought ? "thinking" : "text", text: p.text }];
    if (p?.functionCall) return [{ kind: "call", title: p.functionCall.name, data: p.functionCall.args }];
    if (p?.functionResponse) return [{ kind: "result", title: p.functionResponse.name, data: p.functionResponse.response }];
    if (p?.inlineData || p?.fileData) {
      const d = obj(p.inlineData ?? p.fileData);
      const size = typeof d.data === "string" ? ` · ${fmtBytes((d.data.length * 3) / 4)}` : d.fileUri ? ` · ${d.fileUri}` : "";
      return [{ kind: "media", title: p.inlineData ? "inlineData" : "fileData", text: `${d.mimeType ?? ""}${size}` }];
    }
    if (p && Object.keys(p).length === 1 && p.thoughtSignature) return []; // an opaque token the model gets back
    return [{ kind: "other", title: Object.keys(obj(p))[0], data: p }];
  });
}

function geminiRequest(body: Record<string, any>): Request {
  const sys = obj(body.systemInstruction ?? body.system_instruction);
  return {
    params: Object.entries(body).filter(([k]) => !["contents", "systemInstruction", "system_instruction", "tools"].includes(k)),
    system: geminiParts(sys.parts),
    tools: arr(body.tools).flatMap((t): ToolDef[] => {
      const decls = arr(t.functionDeclarations ?? t.function_declarations);
      if (decls.length) return decls.map((d) => ({ name: d.name, description: d.description, schema: d.parameters ?? d.parametersJsonSchema ?? d }));
      return Object.keys(obj(t)).map((k) => ({ name: k, schema: t[k] }));
    }),
    messages: arr(body.contents).map((c) => ({ role: c.role ?? "user", parts: geminiParts(c.parts), raw: c })),
  };
}

function geminiAnswer(events: StreamEvent[]): Part[] {
  const out: Part[] = [];
  for (const { data } of events)
    for (const c of arr(obj(data).candidates).slice(0, 1))
      for (const p of geminiParts(obj(c.content).parts)) {
        const last = out[out.length - 1];
        if (last && (p.kind === "text" || p.kind === "thinking") && last.kind === p.kind) last.text = (last.text ?? "") + (p.text ?? "");
        else out.push({ ...p });
      }
  return out;
}

// ---------------------------------------------------------------- the three together

export function parseRequest(api: string, body: unknown): Request | null {
  const b = obj(body);
  if (!Object.keys(b).length) return null;
  if (api === "anthropic") return anthropicRequest(b);
  if (api === "openai") return responsesRequest(b);
  if (api === "gemini") return geminiRequest(b);
  return null;
}

/** The answer as the parts of one message, and its stream's events, each placed in time by `marks` ([ms, bytes so far]). */
export function parseAnswer(api: string, text: string, contentType: string, marks: [number, number][]): Answer {
  let events: StreamEvent[];
  let stream = false;
  if (/event-stream/.test(contentType)) {
    stream = true;
    const enc = new TextEncoder();
    let chars = 0;
    let bytes = 0;
    let mi = 0;
    events = sseEvents(text).map(({ name, data, end }) => {
      bytes += enc.encode(text.slice(chars, end)).length;
      chars = end;
      while (mi < marks.length - 1 && marks[mi][1] < bytes) mi++;
      return { name, data, at: marks.length ? marks[mi][0] : undefined };
    });
  } else {
    const d = json(text);
    events = (Array.isArray(d) ? d : [d]).map((x) => ({ name: "", data: x }));
  }
  const parts = api === "anthropic" ? anthropicAnswer(events) : api === "openai" ? responsesAnswer(events) : api === "gemini" ? geminiAnswer(events) : [];
  return { parts, events: stream ? events : null };
}

/** How many of `cur`'s messages open the same as `prev`'s: what the request before it already sent. Cache
 *  breakpoints aside, which harnesses move to the newest message on every call. */
export function samePrefix(prev: Message[], cur: Message[]): number {
  const key = (m: Message) => JSON.stringify(m.raw, (k, v) => (k === "cache_control" ? undefined : v));
  let i = 0;
  while (i < prev.length && i < cur.length && key(prev[i]) === key(cur[i])) i++;
  return i;
}

// ---------------------------------------------------------------- numbers

export function fmtBytes(n?: number | null): string {
  if (n == null) return "";
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtTokens(n?: number | null): string {
  if (n == null) return "–";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export function fmtSeconds(s?: number | null): string {
  if (s == null) return "";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}
