import type { RefObject } from "react";
import { useEffect, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";

const EDGE = 4; // between the thumb and the ends of its track
const MIN = 44; // the shortest thumb: a fingertip's, however long the chat
const SETTLE = 150; // ms without a scroll event before the thumb takes a finger

/** Ways about a long chat, laid over its scroll box `target` (in a positioned box of its own, `.chat-body`): a
 *  scrollbar a finger can drag, and a button back to the end while scrolled well away from it. A touch screen's own
 *  scrollbar only shows while the chat moves and can't be held, so this thumb is drawn there (CSS; a mouse keeps the
 *  native one). It shows when the reader scrolls (not when the chat follows new output at its end) and fades after.
 *  It takes a finger only once the chat has stopped (`grab`): during a fling, a swipe that lands on it has to stop the
 *  fling and carry on scrolling, not jump the chat the other way by the thumb's ratio.
 *  `onEnd`: the button took the chat to its end (it pins itself there again). */
export function ScrollAids({ target, onEnd }: { target: RefObject<HTMLElement | null>; onEnd?: () => void }) {
  const thumb = useRef<HTMLDivElement>(null);
  const [away, setAway] = useState(false);

  useEffect(() => {
    const sc = target.current, th = thumb.current;
    if (!sc || !th) return;
    let hide = 0, settle = 0;
    let drag: { y: number; top: number; per: number } | null = null;
    const fade = () => {
      clearTimeout(hide);
      hide = window.setTimeout(() => !drag && th.classList.remove("on", "grab"), 1500);
    };
    // the thumb's size and place, written straight to it: this runs on every scroll event
    const place = (scrolled: boolean) => {
      const { scrollTop, scrollHeight, clientHeight } = sc;
      const range = scrollHeight - clientHeight;
      setAway(range - scrollTop > Math.max(160, clientHeight / 2));
      const track = clientHeight - 2 * EDGE;
      const h = Math.min(track, Math.max(MIN, (track * clientHeight) / scrollHeight));
      th.style.height = `${h}px`;
      th.style.top = `${EDGE + (range > 0 ? ((track - h) * scrollTop) / range : 0)}px`;
      if (range <= 1) return th.classList.remove("on", "grab");
      // at the very end: the chat following its output (or sent there), not someone reading back
      if (scrolled && range - scrollTop > 2) {
        th.classList.add("on");
        fade();
        // still moving: shown, not grabbable, until the scroll events stop (a drag of its own keeps it)
        if (!drag) {
          th.classList.remove("grab");
          clearTimeout(settle);
          settle = window.setTimeout(() => th.classList.contains("on") && th.classList.add("grab"), SETTLE);
        }
      }
    };
    const onScroll = () => place(true);
    const onDown = (e: PointerEvent) => {
      const range = sc.scrollHeight - sc.clientHeight;
      if (range <= 1) return;
      e.preventDefault();
      th.setPointerCapture(e.pointerId);
      // a pixel of thumb is this many pixels of chat
      drag = { y: e.clientY, top: sc.scrollTop, per: range / Math.max(1, sc.clientHeight - 2 * EDGE - th.offsetHeight) };
      th.classList.add("on", "drag");
      clearTimeout(hide);
    };
    const onMove = (e: PointerEvent) => {
      if (drag) sc.scrollTop = drag.top + (e.clientY - drag.y) * drag.per;
    };
    const onUp = () => {
      if (!drag) return;
      drag = null;
      th.classList.remove("drag");
      fade();
    };
    place(false);
    sc.addEventListener("scroll", onScroll, { passive: true });
    th.addEventListener("pointerdown", onDown);
    th.addEventListener("pointermove", onMove);
    th.addEventListener("pointerup", onUp);
    th.addEventListener("pointercancel", onUp);
    // the chat grows (output streams in) or the box changes size: the thumb follows, without showing
    const ro = new ResizeObserver(() => place(false));
    ro.observe(sc);
    if (sc.firstElementChild) ro.observe(sc.firstElementChild);
    return () => {
      clearTimeout(hide);
      clearTimeout(settle);
      ro.disconnect();
      sc.removeEventListener("scroll", onScroll);
      th.removeEventListener("pointerdown", onDown);
      th.removeEventListener("pointermove", onMove);
      th.removeEventListener("pointerup", onUp);
      th.removeEventListener("pointercancel", onUp);
    };
  }, [target]);

  const toEnd = () => {
    const sc = target.current;
    if (!sc) return;
    sc.scrollTop = sc.scrollHeight;
    onEnd?.();
  };
  return (
    <>
      <div ref={thumb} className="scroll-thumb" aria-hidden />
      {away && (
        <button className="to-end" aria-label="Jump to the latest" data-tip="Jump to the latest" onClick={toEnd}>
          <ArrowDown size={16} />
        </button>
      )}
    </>
  );
}
