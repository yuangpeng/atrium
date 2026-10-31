// How the window is laid out. Besides desktops, Atrium runs on phones, the iPhone Duo first: its outer screen
// (382 × 580 portrait, 594 × 368 landscape), its inner screen open flat (669 × 867, 951 × 585) or half-folded,
// and one half of Split View (385 × 571). <html data-layout> carries the result; responsive.css lays panes out by it.
//   wide   desktop: workspace | chat (resizable) | rail
//   dual   a touch screen wide enough for both panes (the Duo's inner screen in landscape): split in the middle, at the fold
//   book   half-folded like a book: workspace on the left segment, chat + rail on the right
//   laptop half-folded like a laptop: workspace on the top segment, chat + rail on the bottom
//   single too narrow for both (the outer screen, the inner one in portrait, Split View): chat and workspace take turns,
//          and the rail is a drawer over them
// Safari reports the fold (book / laptop) only with its "Viewport Segments API" feature flag on; otherwise it's dual / single.
// Apart from the layout, <html data-compact> marks a phone-sized touch screen (every iPhone Duo pose, no tablet):
// smaller type and chrome there (responsive.css).
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
  // the Duo's largest pose is 951 × 902; a tablet has a side of 744 or more and the other one past 1000
  compact: matchMedia("(pointer: coarse) and (max-width: 1000px) and (max-height: 1000px)"),
};

function compute(): Layout {
  // book splits the width between the panes, so it needs the room single asks for; laptop stacks
  // the segments and each one is the full width, so it never has to fall back
  if (queries.book.matches && !queries.single.matches) return "book";
  if (queries.laptop.matches) return "laptop";
  if (queries.single.matches) return "single";
  return queries.touch.matches ? "dual" : "wide";
}

/** The browser's bars, and a Home Screen app's status bar, in the page's background: the theme sets it, and on a phone
 *  responsive.css makes it the rail's where the rail runs down the right edge (iPhone Duo's clock column). */
export function syncThemeColor() {
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", getComputedStyle(document.body).backgroundColor);
}

let current = compute();
let compact = queries.compact.matches;
document.documentElement.dataset.layout = current;
document.documentElement.toggleAttribute("data-compact", compact);
const listeners = new Set<() => void>();
for (const q of Object.values(queries))
  q.addEventListener("change", () => {
    const next = compute();
    if (next === current && queries.compact.matches === compact) return;
    current = next;
    compact = queries.compact.matches;
    document.documentElement.dataset.layout = next;
    document.documentElement.toggleAttribute("data-compact", compact);
    syncThemeColor();
    listeners.forEach((l) => l());
  });

/** The layout right now (outside React). */
export const layoutNow = (): Layout => current;
const compactNow = (): boolean => compact;

// one identity, so React keeps the subscription instead of dropping and remaking it every render
const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => void listeners.delete(cb);
};

export function useLayout(): Layout {
  return useSyncExternalStore(subscribe, layoutNow);
}

/** A phone-sized touch screen: smaller type and chrome (what CSS gets as <html data-compact>). */
export function useCompact(): boolean {
  return useSyncExternalStore(subscribe, compactNow);
}

/** A hover from a mouse or pen, not a finger: touch taps fire mouseenter too, and a tooltip or card shown then can
 *  swallow the tap (iOS turns a tap that reveals content into a hover; the click needs a second tap). */
export const realHover = (e: React.PointerEvent) => e.pointerType !== "touch";
