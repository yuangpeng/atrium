import { ReactNode, RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { X } from "lucide-react";
import { realHover } from "../layout";
import { useApp } from "../store";
import { holdTouch, placeTip, tipLayer } from "../tips";
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

/** An image over the whole page, fit to the window; ✕, Escape or a tap beside the image closes it. Opening it in a
 *  new tab instead would strand the Home Screen app, which has no back button. In a portal: the panes are size
 *  containers, which would hold a fixed box inside them. */
export function ImageViewer({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return createPortal(
    <div className="image-viewer" role="dialog" aria-label={alt} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <img src={src} alt={alt} />
      <button className="image-viewer-x" onClick={onClose} aria-label="Close" data-tip="Close (Esc)" autoFocus>
        <X size={20} />
      </button>
    </div>,
    document.body,
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

type TipProps = {
  onPointerEnter: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerLeave: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  onClick?: (e: React.MouseEvent<HTMLElement>) => void;
  "data-rich-tip": ""; // tips.ts leaves the long press to this hook
};

/** Hover tooltip, a dark bubble over the element (after Codex's): spread `props` on the element, render `tip`.
 *  A finger gets it from a long press (realHover: a tap is not a hover), or with `tap` — for something that only
 *  shows information, like a read-only chip; its props then bring an onClick — from a plain tap. */
export function useTip(content: ReactNode, opts?: { tap?: boolean }): [TipProps, ReactNode] {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [touched, setTouched] = useState(false); // shown for a finger: there is no pointerleave to take it away
  const timer = useRef(0);
  const pointer = useRef("");
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const hide = () => {
    window.clearTimeout(timer.current);
    setAnchor(null);
  };
  const touch = (el: HTMLElement) => {
    setTouched(true);
    setAnchor(el);
  };
  useTouchDismiss(touched ? anchor : null, hide);
  const props: TipProps = {
    onPointerEnter: (e) => {
      if (!realHover(e)) return;
      const el = e.currentTarget;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => (setTouched(false), setAnchor(el)), 250);
    },
    onPointerLeave: (e) => void (realHover(e) && hide()), // a finger "leaves" as it lifts
    onPointerDown: (e) => {
      pointer.current = e.pointerType;
      if (!opts?.tap) hide(); // a click opens the element's menu instead
      const el = e.currentTarget;
      holdTouch(e.nativeEvent, () => touch(el));
    },
    "data-rich-tip": "",
  };
  if (opts?.tap) props.onClick = (e) => pointer.current === "touch" && (anchor ? hide() : touch(e.currentTarget));
  return [props, anchor && content ? createPortal(<TipBubble anchor={anchor}>{content}</TipBubble>, tipLayer()) : null];
}

/** While a tip shown for a finger is up: the next touch elsewhere, a scroll or a few seconds take it away. */
export function useTouchDismiss(anchor: HTMLElement | null, hide: () => void) {
  const hideRef = useRef(hide);
  hideRef.current = hide;
  useEffect(() => {
    if (!anchor) return;
    const off = () => hideRef.current();
    const down = (e: PointerEvent) => !anchor.contains(e.target as Node) && off();
    const t = window.setTimeout(off, 4000);
    document.addEventListener("pointerdown", down, true);
    document.addEventListener("scroll", off, true);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("pointerdown", down, true);
      document.removeEventListener("scroll", off, true);
    };
  }, [anchor]);
}

function TipBubble({ anchor, children }: { anchor: HTMLElement; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (ref.current) placeTip(ref.current, anchor);
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
  const move = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    set(Math.min(0.85, Math.max(0.2, (bottom - ev.clientY) / box.height)));
  };
  const up = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
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

export function useInterval(fn: () => void, ms: number | null) {
  const saved = useRef(fn);
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

// Fullscreen without the browser's: iPhone's Safari has none for page elements (only for video), so there the
// element fills the window instead (`data-fs-fill`, styles.css). One at a time, like the real thing.
let filled: HTMLElement | null = null;
const fillListeners = new Set<() => void>();

function setFilled(el: HTMLElement | null) {
  if (filled === el) return;
  filled?.removeAttribute("data-fs-fill");
  el?.setAttribute("data-fs-fill", "");
  filled = el;
  // the panes are size containers, which would hold a fixed box inside them: they let go while it fills
  document.documentElement.toggleAttribute("data-fs-filling", !!el);
  fillListeners.forEach((l) => l());
}

window.addEventListener("keydown", (e) => e.key === "Escape" && filled && setFilled(null));
// a panel or pane that goes away takes its filled element out of sight: leave it, don't strand it
useApp.subscribe((s, prev) => filled && (s.panel !== prev.panel || s.pane !== prev.pane) && setFilled(null));

/** The element currently shown fullscreen, if any (the browser's, or one filling the window). */
export function useFullscreenElement(): Element | null {
  const [el, setEl] = useState<Element | null>(() => document.fullscreenElement ?? filled);
  useEffect(() => {
    const on = () => setEl(document.fullscreenElement ?? filled);
    document.addEventListener("fullscreenchange", on);
    fillListeners.add(on);
    return () => {
      document.removeEventListener("fullscreenchange", on);
      fillListeners.delete(on);
    };
  }, []);
  return el;
}

/** Fullscreen for one element (Esc leaves it): the browser's where it has one, else the element fills the window.
 *  Give the element the `fs-host` class. */
export function useFullscreen(ref: RefObject<HTMLElement | null>): [boolean, () => void] {
  const el = useFullscreenElement();
  useEffect(() => {
    const node = ref.current;
    return () => void (node && filled === node && setFilled(null));
  }, [ref]);
  const toggle = () => {
    if (document.fullscreenElement) return void document.exitFullscreen();
    if (filled) return setFilled(null);
    const target = ref.current;
    if (!target) return;
    if (document.fullscreenEnabled && target.requestFullscreen) void target.requestFullscreen().catch(() => setFilled(target));
    else setFilled(target);
  };
  return [!!el && el === ref.current, toggle];
}
