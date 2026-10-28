// Changed files as a directory tree (Changes, Git): folders first, chains of single-child folders
// compacted into one row ("web/src/panels"), each folder knowing every file beneath it so callers
// can show counts and offer folder-wide actions.
import type { JSX, KeyboardEvent, ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, Folder, FolderOpen } from "lucide-react";
import { clsx } from "../util";

export interface TreeFile<T> {
  kind: "file";
  path: string;
  name: string;
  item: T;
}

export interface TreeDir<T> {
  kind: "dir";
  path: string; // of the deepest folder when compacted
  name: string; // "src", or "web/src" when compacted
  children: TreeNode<T>[];
  files: T[]; // everything beneath, in display order
}

export type TreeNode<T> = TreeDir<T> | TreeFile<T>;

const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });

export function buildTree<T>(items: T[], pathOf: (t: T) => string): TreeNode<T>[] {
  interface Dir {
    path: string;
    dirs: Map<string, Dir>;
    files: TreeFile<T>[];
  }
  const root: Dir = { path: "", dirs: new Map(), files: [] };
  for (const item of items) {
    const p = pathOf(item);
    const parts = p.split("/").filter(Boolean);
    if (p.startsWith("/") && parts.length) parts[0] = "/" + parts[0]; // outside the root: keep it absolute
    let d = root;
    for (const seg of parts.slice(0, -1)) {
      let next = d.dirs.get(seg);
      if (!next) d.dirs.set(seg, (next = { path: d.path ? `${d.path}/${seg}` : seg, dirs: new Map(), files: [] }));
      d = next;
    }
    d.files.push({ kind: "file", path: p, name: parts[parts.length - 1] ?? p, item });
  }
  const convert = (d: Dir): TreeNode<T>[] => {
    const dirs = [...d.dirs.entries()].map(([name, sub]) => {
      let node: TreeDir<T> = { kind: "dir", path: sub.path, name, children: convert(sub), files: [] };
      while (node.children.length === 1 && node.children[0].kind === "dir") {
        const only: TreeDir<T> = node.children[0];
        node = { ...only, name: `${node.name}/${only.name}` };
      }
      node.files = node.children.flatMap((c) => (c.kind === "dir" ? c.files : [c.item]));
      return node;
    });
    return [...dirs.sort(byName), ...d.files.sort(byName)];
  };
  return convert(root);
}

function fileNodes<T>(nodes: TreeNode<T>[]): TreeFile<T>[] {
  return nodes.flatMap((n) => (n.kind === "dir" ? fileNodes(n.children) : [n]));
}

/** The tree for `items` plus its files in display order (for previous / next navigation). */
export function useFileTree<T>(items: T[] | null | undefined, pathOf: (t: T) => string): { nodes: TreeNode<T>[]; order: T[] } {
  return useMemo(() => {
    const nodes = buildTree(items ?? [], pathOf);
    return { nodes, order: fileNodes(nodes).map((f) => f.item) };
    // pathOf is a pure accessor; rebuilding when the items change is enough
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
}

export function FileTree<T>({
  nodes,
  selected,
  onSelect,
  renderFile,
  renderDir,
  rowClass,
}: {
  nodes: TreeNode<T>[];
  /** path (as given to the tree) of the selected file; its folders are kept open */
  selected?: string | null;
  onSelect: (item: T) => void;
  /** the row after the indent: status, name, counts, actions */
  renderFile: (item: T, name: string) => ReactNode;
  /** extra content after a folder's name */
  renderDir?: (dir: TreeDir<T>) => ReactNode;
  rowClass?: (item: T) => string | false | null | undefined;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!selected) return;
    setCollapsed((c) => {
      const hiding = [...c].filter((d) => selected.startsWith(d + "/"));
      if (!hiding.length) return c;
      const n = new Set(c);
      hiding.forEach((d) => n.delete(d));
      return n;
    });
  }, [selected]);
  const toggle = (path: string) =>
    setCollapsed((c) => {
      const n = new Set(c);
      if (n.has(path)) n.delete(path);
      else n.add(path);
      return n;
    });

  // ↑ / ↓ move through the files (collapsed folders open as the selection reaches them)
  const files = useMemo(() => fileNodes(nodes), [nodes]);
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const i = files.findIndex((f) => f.path === selected);
    const next = e.key === "ArrowDown" ? files[i + 1] : files[i - 1];
    if (next) onSelect(next.item);
  };
  // scroll a newly selected file into view, once its folders have opened
  const box = useRef<HTMLDivElement>(null);
  const revealed = useRef<string | null>(null);
  useEffect(() => {
    if (!selected) revealed.current = null;
    if (!selected || revealed.current === selected) return;
    const row = box.current?.querySelector(`[data-path="${CSS.escape(selected)}"]`);
    if (row) {
      row.scrollIntoView({ block: "nearest" });
      revealed.current = selected;
    }
  });

  const render = (n: TreeNode<T>, depth: number): JSX.Element[] => {
    const indent = { paddingLeft: 4 + depth * 12 };
    if (n.kind === "file")
      return [
        <div key={n.path} data-path={n.path} className={clsx("tree-row", n.path === selected && "on", rowClass?.(n.item))} style={indent} onClick={() => onSelect(n.item)} title={n.path}>
          <span className="chev" />
          {renderFile(n.item, n.name)}
        </div>,
      ];
    const open = !collapsed.has(n.path);
    return [
      <div key={n.path + "/"} className="tree-row dir" style={indent} onClick={() => toggle(n.path)} title={n.path}>
        <span className="chev">{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
        <span className="fico">{open ? <FolderOpen size={14} /> : <Folder size={14} />}</span>
        <span className="nm">{n.name}</span>
        {renderDir?.(n)}
      </div>,
      ...(open ? n.children.flatMap((c) => render(c, depth + 1)) : []),
    ];
  };
  return (
    <div ref={box} className="ftree" tabIndex={0} onKeyDown={onKeyDown}>
      {nodes.flatMap((n) => render(n, 0))}
    </div>
  );
}

/** ‹ 3 / 7 › — previous / next file. */
export function FileNav({ index, count, onGo }: { index: number; count: number; onGo: (i: number) => void }) {
  if (index < 0 || count < 1) return null;
  return (
    <span className="file-nav">
      <button className="icon-btn sm" title="Previous file" disabled={index <= 0} onClick={() => onGo(index - 1)}>
        <ChevronLeft size={14} />
      </button>
      <span>
        {index + 1} / {count}
      </span>
      <button className="icon-btn sm" title="Next file" disabled={index >= count - 1} onClick={() => onGo(index + 1)}>
        <ChevronRight size={14} />
      </button>
    </span>
  );
}
