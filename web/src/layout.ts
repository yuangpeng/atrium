// How the window is laid out. Besides desktops, Atrium runs on phones, the iPhone Duo first: its outer screen
// (382 × 580 portrait, 594 × 368 landscape), its inner screen open flat (669 × 867, 951 × 585) or half-folded,
// and one half of Split View (385 × 571). <html data-layout> carries the result; responsive.css lays panes out by it.
//   wide   desktop: workspace | chat (resizable) | rail
//   dual   a touch screen wide enough for both panes (the Duo's inner screen in landscape): split in the middle, at the fold
//   book   half-folded like a book: workspace on the left segment, chat + rail on the right
//   laptop half-folded like a laptop: workspace on the top segment, chat + rail on the bottom
//   single too narrow for both (the outer screen, the inner one in portrait, Split View): chat and workspace take turns
// Safari reports the fold (book / laptop) only with its "Viewport Segments API" feature flag on; otherwise it's dual / single.
import { useSyncExternalStore } from "react";

export type Layout = "wide" | "dual" | "book" | "laptop" | "single";

/** Up to this width the chat and the workspace take turns: half of the screen has to hold the workspace (420),
 *  the other half the chat (340) and the rail (52). */
export const SINGLE_MAX = 839;

const queries = {
  book: matchMedia("(horizontal-viewport-segments: 2)"),
  laptop: matchMedia("(vertical-viewport-segments: 2)"),
  single: matchMedia(`(max-width: ${SINGLE_MAX}px)`),
  touch: matchMedia("(pointer: coarse)"),
};

function compute(): Layout {
  if (queries.book.matches) return "book";
  if (queries.laptop.matches) return "laptop";
  if (queries.single.matches) return "single";
  return queries.touch.matches ? "dual" : "wide";
}

let current = compute();
document.documentElement.dataset.layout = current;
const listeners = new Set<() => void>();
for (const q of Object.values(queries))
  q.addEventListener("change", () => {
    const next = compute();
    if (next === current) return;
    current = next;
    document.documentElement.dataset.layout = next;
    listeners.forEach((l) => l());
  });

/** The layout right now (outside React). */
export const layoutNow = (): Layout => current;

export function useLayout(): Layout {
  return useSyncExternalStore((cb) => (listeners.add(cb), () => void listeners.delete(cb)), layoutNow);
}

/** A hover from a mouse or pen, not a finger: touch taps fire mouseenter too, and a tooltip or card shown then can
 *  swallow the tap (iOS turns a tap that reveals content into a hover; the click needs a second tap). */
export const realHover = (e: React.PointerEvent) => e.pointerType !== "touch";
