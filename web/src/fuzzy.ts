// Fuzzy matching for quick open (⌘P), as in VS Code: the letters typed, in order, anywhere in the path, the file's own
// name first. No Monaco or React here: it runs on every keystroke over up to LIST_MAX paths (search.py).

export interface Match {
  score: number;
  hits: number[]; // indices into the path of the letters matched, ascending
}

const SEP = new Set(["/", "\\", "_", "-", ".", " "]);

/** Whether the letter at `i` starts a word: after a separator, or a capital after a small letter (camelCase). */
function boundary(path: string, i: number): boolean {
  if (i === 0) return true;
  const prev = path[i - 1];
  if (SEP.has(prev)) return true;
  const c = path[i];
  return c !== c.toLowerCase() && prev === prev.toLowerCase() && prev !== prev.toUpperCase();
}

/** The letters of `q` (lower case) in `lower` from `from` on, as tight as they come: the first place they all fit, then
 *  walked back from its end, so `ab` in `a/x/ab` takes the second `a` (fzf's way). null: they don't. */
function align(q: string, lower: string, from: number): number[] | null {
  let i = 0;
  let end = -1;
  for (let j = from; j < lower.length; j++) {
    if (lower[j] === q[i] && ++i === q.length) {
      end = j;
      break;
    }
  }
  if (end < 0) return null;
  let start = end;
  for (let k = q.length - 1, j = end; k >= 0; j--) {
    if (lower[j] === q[k]) {
      start = j;
      k--;
    }
  }
  const hits: number[] = [];
  for (let j = start, k = 0; k < q.length; j++) if (lower[j] === q[k]) hits.push(j), k++;
  return hits;
}

/** How well one word of the query matches the path; null: it doesn't. */
function term(q: string, path: string, lower: string, name: number): Match | null {
  // within the file's own name first: `app` finds src/app.py before src/apple/x.py
  const hits = align(q, lower, name) ?? align(q, lower, 0);
  if (!hits) return null;
  let score = 0;
  let prev = -2;
  for (const h of hits) {
    score += 1;
    if (h === prev + 1) score += 5; // in a row
    if (boundary(path, h)) score += 4; // a word's first letter
    if (h >= name) score += 2; // in the file's name
    prev = h;
  }
  if (lower.startsWith(q, name)) score += 12; // the name starts with it
  if (hits[0] >= name && lower.slice(name).replace(/\.[^.]*$/, "") === q) score += 20; // the whole name (but its extension)
  return { score, hits };
}

/** The query (words separated by spaces, each fuzzy) against a path. Shorter paths win ties. */
export function fuzzy(query: string, path: string): Match | null {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return { score: 0, hits: [] };
  const lower = path.toLowerCase();
  const name = Math.max(lower.lastIndexOf("/"), lower.lastIndexOf("\\")) + 1;
  let score = 0;
  const hits = new Set<number>();
  for (const w of words) {
    const m = term(w, path, lower, name);
    if (!m) return null;
    score += m.score;
    m.hits.forEach((h) => hits.add(h));
  }
  return { score: score - path.length * 0.05, hits: [...hits].sort((a, b) => a - b) };
}

/** The best `limit` of `paths` for `query`, best first; `bonus`: extra score per path (recently opened ones). */
export function rank(query: string, paths: string[], limit: number, bonus?: (p: string) => number): { path: string; match: Match }[] {
  const out: { path: string; match: Match }[] = [];
  for (const p of paths) {
    const m = fuzzy(query, p);
    if (!m) continue;
    if (bonus) m.score += bonus(p);
    out.push({ path: p, match: m });
  }
  out.sort((a, b) => b.match.score - a.match.score || a.path.length - b.path.length || (a.path < b.path ? -1 : 1));
  return out.slice(0, limit);
}

/** `name:12` / `name:12:5` → the query and where to go in the file; `:12` alone means the file shown. */
export function splitLine(query: string): { query: string; line?: number; column?: number } {
  const m = /^(.*?):(\d+)(?::(\d+))?\s*$/.exec(query.trim());
  if (!m) return { query: query.trim() };
  return { query: m[1].trim(), line: Number(m[2]), column: m[3] ? Number(m[3]) : undefined };
}
