// Tooltips that don't need a component: the same dark bubble as useTip (components/ui.tsx), for plain text.
// A mouse gets an element's `data-tip` after a short hover. A finger gets a long press on anything labelled
// (`data-tip`, else `title` or `aria-label`), since a tap is the click itself and iOS shows no `title`; the click
// that the release makes is dropped, so holding a button to read it doesn't press it. Elements with a richer tip
// of their own (useTip, the rail's agent cards) carry `data-rich-tip` and are left to it.

const HOVER = 300; // ms before a mouse gets its tip
const HOLD = 450; // ms a finger rests on a control before it counts as asking what it is
const SLOP = 10; // px the finger may wander meanwhile; more is a scroll
const SHOWN = 4000; // ms a long-pressed tip stays up (or until the next touch)
const LABELLED = "button, a[href], [role='button'], [data-tip], [title], .chip";

let swallowUntil = 0; // a click before then ends a long press: not a tap
let cancelHold: (() => void) | null = null;

/** Puts a tip bubble over `anchor` (under it when there's no room above), inside the window; beside it, on its
 *  left, for an anchor with `data-tip-side="left"` (the rail's buttons, at the window's right edge). */
export function placeTip(el: HTMLElement, anchor: Element) {
  const r = anchor.getBoundingClientRect();
  const left = r.left - el.offsetWidth - 8;
  if (anchor instanceof HTMLElement && anchor.dataset.tipSide === "left" && left >= 8) {
    el.style.left = `${left}px`;
    el.style.top = `${Math.max(8, Math.min(window.innerHeight - el.offsetHeight - 8, r.top + r.height / 2 - el.offsetHeight / 2))}px`;
  } else {
    const above = r.top - el.offsetHeight - 8;
    el.style.left = `${Math.max(8, Math.min(window.innerWidth - el.offsetWidth - 8, r.left + r.width / 2 - el.offsetWidth / 2))}px`;
    el.style.top = `${above >= 8 ? above : r.bottom + 8}px`;
  }
  el.style.opacity = "1";
}

/** Where a bubble has to live to be seen: inside the element shown with the browser's fullscreen, if any. */
export const tipLayer = (): Element => document.fullscreenElement ?? document.body;

/** A finger held still on the element of `e` (a pointerdown): `fire` runs, and the click its release makes is
 *  dropped. Mouse and pen presses are ignored. */
export function holdTouch(e: PointerEvent, fire: () => void) {
  if (e.pointerType !== "touch") return;
  cancelHold?.();
  const { pointerId: id, clientX: x, clientY: y } = e;
  let fired = false;
  const timer = window.setTimeout(() => {
    fired = true;
    navigator.vibrate?.(8);
    fire();
  }, HOLD);
  const move = (ev: PointerEvent) => ev.pointerId === id && Math.hypot(ev.clientX - x, ev.clientY - y) > SLOP && end();
  const up = (ev: PointerEvent) => {
    if (ev.pointerId !== id) return;
    if (fired) swallowUntil = Date.now() + 600;
    end();
  };
  const end = () => {
    window.clearTimeout(timer);
    window.removeEventListener("pointermove", move, true);
    window.removeEventListener("pointerup", up, true);
    window.removeEventListener("pointercancel", end, true);
    if (cancelHold === end) cancelHold = null;
  };
  window.addEventListener("pointermove", move, true);
  window.addEventListener("pointerup", up, true);
  window.addEventListener("pointercancel", end, true);
  cancelHold = end;
}

// ---------------------------------------------------------------- the plain-text bubble
let bubble: HTMLDivElement | null = null;
let hovered: Element | null = null; // under the mouse: its tip shown, or on its way
let timer = 0;

function show(anchor: Element, text: string) {
  bubble ??= Object.assign(document.createElement("div"), { className: "hover-tip", role: "tooltip" });
  bubble.textContent = text;
  bubble.style.opacity = "0";
  tipLayer().appendChild(bubble);
  placeTip(bubble, anchor);
}

function hide() {
  window.clearTimeout(timer);
  bubble?.remove();
  hovered = null;
}

function labelled(target: EventTarget | null): HTMLElement | null {
  const el = target instanceof Element ? target.closest<HTMLElement>(LABELLED) : null;
  // (the terminal's key row: a key held a little long is still a key press)
  if (!el || el.closest("[data-rich-tip], .monaco-editor, .term-keys")) return null;
  return el.dataset.tip || el.title || el.getAttribute("aria-label") ? el : null;
}

/** Once, at startup. */
export function installTips() {
  const opts = { capture: true, passive: true };
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType === "touch") return;
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-tip]") : null;
    if (el === hovered) return;
    hide();
    if (!el?.dataset.tip || el.closest("[data-rich-tip]")) return;
    hovered = el;
    timer = window.setTimeout(() => show(el, el.dataset.tip!), HOVER);
  }, opts);
  // left before the tip came up, too: its timer goes with it
  document.addEventListener("pointerout", (e) => {
    if (e.pointerType !== "touch" && hovered && !(e.relatedTarget instanceof Node && hovered.contains(e.relatedTarget))) hide();
  }, opts);
  document.addEventListener("pointerdown", (e) => {
    hide();
    swallowUntil = 0; // a new touch: whatever the last long press left to drop has come or never will
    const el = e.pointerType === "touch" ? labelled(e.target) : null;
    if (el)
      holdTouch(e, () => {
        show(el, el.dataset.tip || el.title || el.getAttribute("aria-label") || "");
        timer = window.setTimeout(hide, SHOWN);
      });
  }, opts);
  // the end of a long press: neither a click nor (Android) the context menu
  document.addEventListener("click", (e) => {
    if (Date.now() >= swallowUntil) return;
    swallowUntil = 0;
    e.preventDefault();
    e.stopPropagation();
  }, true);
  document.addEventListener("contextmenu", (e) => {
    if (cancelHold || Date.now() < swallowUntil) e.preventDefault();
  }, true);
  for (const type of ["scroll", "wheel", "keydown"]) document.addEventListener(type, hide, opts);
  window.addEventListener("blur", hide);
}
