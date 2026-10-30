import { ReactNode, RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { X } from "lucide-react";
import { realHover } from "../layout";
import { clsx } from "../util";

export function Modal({ title, onClose, children, footer, wide, xl, bodyClass }: { title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean; xl?: boolean; bodyClass?: string }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={clsx("modal", wide && "wide", xl && "xl")}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} title="Close">
            <X size={16} />
          </button>
        </div>
        <div className={clsx("modal-body", bodyClass)}>{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/** Anchored popover menu; closes on outside click / Escape. `within` (an ancestor selector) keeps the menu inside that box. */
export function Popover({
  trigger,
  children,
  align = "left",
  up = false,
  width,
  within,
}: {
  trigger: (open: boolean, toggle: () => void) => ReactNode;
  children: (close: () => void) => ReactNode;
  align?: "left" | "right";
  up?: boolean;
  width?: number | "max-content";
  within?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  // narrow and slide the menu sideways until it fits inside `within` and the window
  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!open || !menu || !within) return;
    const box = ref.current?.closest(within)?.getBoundingClientRect();
    const lo = Math.max(8, box?.left ?? 0);
    const hi = Math.min(window.innerWidth - 8, box?.right ?? Infinity);
    if (menu.offsetWidth > hi - lo) menu.style.width = `${hi - lo}px`;
    const r = menu.getBoundingClientRect();
    const dx = r.right > hi ? hi - r.right : r.left < lo ? lo - r.left : 0;
    menu.style.transform = dx ? `translateX(${dx}px)` : "";
  }, [open, within]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div ref={ref} style={{ position: "relative", display: "inline-flex" }}>
      {trigger(open, () => setOpen((o) => !o))}
      {open && (
        <div
          ref={menuRef}
          className="menu"
          style={{
            [align]: 0,
            ...(up ? { bottom: "calc(100% + 6px)" } : { top: "calc(100% + 6px)" }),
            ...(width ? { width } : {}),
          }}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{ a: ({ node: _n, ...props }) => <a {...props} target="_blank" rel="noreferrer" /> }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" />;
}

type TipProps = { onPointerEnter: (e: React.PointerEvent<HTMLElement>) => void; onPointerLeave: () => void; onPointerDown: () => void };

/** Hover tooltip, a dark bubble over the element (after Codex's): spread `props` on the element, render `tip`.
 *  Not for fingers (see realHover). */
export function useTip(content: ReactNode): [TipProps, ReactNode] {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const hide = () => {
    window.clearTimeout(timer.current);
    setAnchor(null);
  };
  const props: TipProps = {
    onPointerEnter: (e) => {
      if (!realHover(e)) return;
      const el = e.currentTarget;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setAnchor(el), 250);
    },
    onPointerLeave: hide,
    onPointerDown: hide, // a click opens the element's menu instead
  };
  return [props, anchor && content ? createPortal(<TipBubble anchor={anchor}>{content}</TipBubble>, document.body) : null];
}

function TipBubble({ anchor, children }: { anchor: HTMLElement; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = anchor.getBoundingClientRect();
    const above = r.top - el.offsetHeight - 8;
    el.style.left = `${Math.max(8, Math.min(window.innerWidth - el.offsetWidth - 8, r.left + r.width / 2 - el.offsetWidth / 2))}px`;
    el.style.top = `${above >= 8 ? above : r.bottom + 8}px`;
    el.style.opacity = "1";
  });
  return (
    <div ref={ref} className="hover-tip" role="tooltip">
      {children}
    </div>
  );
}

/** A horizontal splitter (`.hresizer`): drag it to resize the box it sits on, as a fraction of the
 *  parent's height. Pointer events, not mouse ones, so a finger can drag it too — iOS makes up no
 *  mousemove while a finger moves. Capture keeps the gesture on the handle when the pointer leaves
 *  it (or crosses an iframe); the moves are taken from the window, so a refused capture still drags. */
export function startRowResize(e: React.PointerEvent<HTMLElement>, ref: RefObject<HTMLElement | null>, set: (fraction: number) => void) {
  if (e.button !== 0) return;
  e.preventDefault();
  const el = ref.current;
  const box = el?.parentElement?.getBoundingClientRect();
  if (!el || !box) return;
  const bottom = el.getBoundingClientRect().bottom;
  const handle = e.currentTarget;
  try {
    handle.setPointerCapture(e.pointerId);
  } catch {
    /* the pointer is already gone: the drag below still works */
  }
  const move = (ev: PointerEvent) => set(Math.min(0.85, Math.max(0.2, (bottom - ev.clientY) / box.height)));
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
  document.body.style.cursor = "row-resize";
  document.body.style.userSelect = "none";
}

export function useInterval(fn: () => void, ms: number | null) {  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (ms === null) return;
    const id = setInterval(() => saved.current(), ms);
    return () => clearInterval(id);
  }, [ms]);
}

/** Re-render every `ms` (for relative timestamps). */
export function useTick(ms = 30000) {
  const [, set] = useState(0);
  useInterval(() => set((x) => x + 1), ms);
}

/** The element currently shown fullscreen, if any. */
export function useFullscreenElement(): Element | null {
  const [el, setEl] = useState<Element | null>(() => document.fullscreenElement);
  useEffect(() => {
    const on = () => setEl(document.fullscreenElement);
    document.addEventListener("fullscreenchange", on);
    return () => document.removeEventListener("fullscreenchange", on);
  }, []);
  return el;
}

/** Browser fullscreen for one element (Esc leaves it). Give the element the `fs-host` class. */
export function useFullscreen(ref: RefObject<HTMLElement | null>): [boolean, () => void] {
  const el = useFullscreenElement();
  const toggle = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void ref.current?.requestFullscreen?.().catch(() => undefined);
  };
  return [!!el && el === ref.current, toggle];
}
