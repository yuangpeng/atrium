// Small shared helpers.

export function timeAgo(ts?: number | null): string {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function fmtDuration(ms?: number | null): string {
  if (!ms) return "";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

export function basename(p: string): string {
  const parts = p.split("/").filter(Boolean);
  return parts[parts.length - 1] || p;
}

export function relTo(root: string, p: string): string {
  if (p === root) return ".";
  return p.startsWith(root + "/") ? p.slice(root.length + 1) : p;
}

export function shortPath(p: string): string {
  const home = p.match(/^\/Users\/[^/]+/)?.[0];
  return home ? "~" + p.slice(home.length) : p;
}

// Stable warm accent per agent, derived from its id.
const AGENT_HUES = [18, 32, 145, 200, 265, 330, 50, 175];
export function agentColor(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return `hsl(${AGENT_HUES[h % AGENT_HUES.length]} 45% 52%)`;
}

export function displayName(s: { name: string | null; title: string }): string {
  return s.name || s.title || "Untitled";
}

const EXT_LANG: Record<string, string> = {
  ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  py: "python", rs: "rust", go: "go", java: "java", kt: "kotlin", c: "c", h: "c", cc: "cpp", cpp: "cpp", hpp: "cpp",
  cs: "csharp", rb: "ruby", php: "php", swift: "swift", scala: "scala", sh: "shell", bash: "shell", zsh: "shell",
  json: "json", jsonc: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", md: "markdown", markdown: "markdown",
  html: "html", htm: "html", css: "css", scss: "scss", less: "less", sql: "sql", xml: "xml", svg: "xml",
  dockerfile: "dockerfile", lua: "lua", r: "r", dart: "dart", vue: "html", proto: "protobuf", graphql: "graphql",
};

export function languageFor(path: string): string {
  const name = basename(path).toLowerCase();
  if (name === "dockerfile") return "dockerfile";
  if (name === "makefile") return "makefile";
  const ext = name.includes(".") ? name.split(".").pop()! : "";
  return EXT_LANG[ext] ?? "plaintext";
}

export function clsx(...xs: (string | false | null | undefined)[]): string {
  return xs.filter(Boolean).join(" ");
}

export function throttle<T extends (...a: any[]) => void>(fn: T, ms: number): T {
  let last = 0;
  let timer: number | null = null;
  return ((...args: any[]) => {
    const now = Date.now();
    const run = () => {
      last = Date.now();
      timer = null;
      fn(...args);
    };
    if (now - last >= ms) run();
    else if (timer === null) timer = window.setTimeout(run, ms - (now - last));
  }) as T;
}
