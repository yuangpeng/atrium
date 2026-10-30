// One terminal: xterm.js on a WebSocket to its shell on the server (atrium/terminal.py; the wire format is
// described there). In a chunk of its own, loaded when the Terminal panel first shows a terminal.
import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { ClipboardPaste } from "lucide-react";
import { type ITheme, Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { clsx, isMac } from "../util";

const coarse = matchMedia("(pointer: coarse)");
const encoder = new TextEncoder();

// VS Code's terminal colours (Light+ / Dark+); the background, the text and the cursor are Atrium's
const ANSI_LIGHT = {
  black: "#000000", red: "#cd3131", green: "#107c10", yellow: "#949800", blue: "#0451a5", magenta: "#bc05bc", cyan: "#0598bc", white: "#555555",
  brightBlack: "#666666", brightRed: "#cd3131", brightGreen: "#14ce14", brightYellow: "#b5ba00", brightBlue: "#0451a5", brightMagenta: "#bc05bc", brightCyan: "#0598bc", brightWhite: "#a5a5a5",
};
const ANSI_DARK = {
  black: "#000000", red: "#cd3131", green: "#0dbc79", yellow: "#e5e510", blue: "#2472c8", magenta: "#bc3fbc", cyan: "#11a8cd", white: "#e5e5e5",
  brightBlack: "#666666", brightRed: "#f14c4c", brightGreen: "#23d18b", brightYellow: "#f5f543", brightBlue: "#3b8eea", brightMagenta: "#d670d6", brightCyan: "#29b8db", brightWhite: "#e5e5e5",
};

function theme(): ITheme {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  const dark = document.documentElement.dataset.theme === "dark";
  return {
    ...(dark ? ANSI_DARK : ANSI_LIGHT),
    background: v("--bg"),
    foreground: v("--text"),
    cursor: v("--text"),
    cursorAccent: v("--bg"),
    selectionBackground: dark ? "rgba(217, 119, 87, 0.38)" : "rgba(217, 119, 87, 0.26)",
    scrollbarSliderBackground: dark ? "rgba(255, 255, 255, 0.16)" : "rgba(31, 30, 29, 0.16)",
    scrollbarSliderHoverBackground: dark ? "rgba(255, 255, 255, 0.26)" : "rgba(31, 30, 29, 0.26)",
    scrollbarSliderActiveBackground: dark ? "rgba(255, 255, 255, 0.34)" : "rgba(31, 30, 29, 0.34)",
  };
}

/** Control + a key, for the key row's sticky Ctrl: what a keyboard's Control key sends. */
function withCtrl(data: string): string {
  if (data.length !== 1) return data;
  if (data === " ") return "\x00";
  if (data === "?") return "\x7f";
  const c = data.toUpperCase().charCodeAt(0);
  return c >= 64 && c <= 95 ? String.fromCharCode(c & 0x1f) : data;
}

/** xterm 6.0 doesn't scroll by touch (6.1 will: xtermjs/xterm.js#5489; drop this then). A one-finger vertical
 *  drag scrolls the scrollback, with some glide; where there is none (vim, less, htop: the alternate screen) or the
 *  program reads the mouse, each line goes to xterm as a wheel event, which it turns into an arrow key or a mouse
 *  report. A tap stays a tap: xterm focuses and the keyboard comes up. */
function touchScroll(host: HTMLElement, term: Terminal): () => void {
  let startY = NaN, y = 0, x = 0, last = 0, v = 0, rest = 0, raf = 0, dragging = false;
  const cell = () => (term.element?.querySelector(".xterm-screen")?.clientHeight ?? 0) / term.rows || 16;
  const toApp = () => term.buffer.active.type === "alternate" || term.modes.mouseTrackingMode !== "none";
  const scroll = (dy: number) => {
    rest += dy / cell();
    const n = Math.trunc(rest);
    if (!n) return;
    rest -= n;
    if (!toApp()) return term.scrollLines(n);
    for (let i = 0; i < Math.abs(n); i++)
      term.element?.dispatchEvent(new WheelEvent("wheel", { deltaY: Math.sign(n), deltaMode: WheelEvent.DOM_DELTA_LINE, clientX: x, clientY: y, bubbles: true, cancelable: true }));
  };
  const start = (e: TouchEvent) => {
    cancelAnimationFrame(raf);
    dragging = false;
    startY = e.touches.length === 1 ? e.touches[0].clientY : NaN; // two fingers: not ours
    y = startY;
    x = e.touches[0]?.clientX ?? 0;
    last = e.timeStamp;
    v = rest = 0;
  };
  const move = (e: TouchEvent) => {
    if (Number.isNaN(startY) || e.touches.length !== 1) return;
    const t = e.touches[0];
    if (!dragging && Math.abs(t.clientY - startY) < 8) return; // still a tap
    dragging = true;
    e.preventDefault();
    const dy = y - t.clientY;
    v = 0.8 * (dy / Math.max(1, e.timeStamp - last)) + 0.2 * v; // px per ms
    y = t.clientY;
    x = t.clientX;
    last = e.timeStamp;
    scroll(dy);
  };
  const end = (e: TouchEvent) => {
    if (!dragging) return;
    e.preventDefault(); // no click after a drag: it doesn't focus, the keyboard stays where it was
    dragging = false;
    if (e.timeStamp - last > 100 || toApp()) return; // the finger had stopped; a program gets no glide of arrow keys
    let prev = performance.now();
    const glide = (now: number) => {
      const dt = now - prev;
      prev = now;
      v *= Math.pow(0.996, dt);
      if (Math.abs(v) < 0.03) return;
      scroll(v * dt);
      raf = requestAnimationFrame(glide);
    };
    if (Math.abs(v) > 0.2) raf = requestAnimationFrame(glide);
  };
  host.addEventListener("touchstart", start, { passive: true });
  host.addEventListener("touchmove", move, { passive: false });
  host.addEventListener("touchend", end, { passive: false });
  host.addEventListener("touchcancel", end, { passive: false });
  return () => {
    cancelAnimationFrame(raf);
    host.removeEventListener("touchstart", start);
    host.removeEventListener("touchmove", move);
    host.removeEventListener("touchend", end);
    host.removeEventListener("touchcancel", end);
  };
}

/** The terminal and its socket, outside React: reconnects, replays, and keeps the shell's size. */
class Session {
  readonly term: Terminal;
  private readonly fit = new FitAddon();
  private ws: WebSocket | null = null;
  private opened = false; // xterm is in the page (waits until its box has a size: it measures the font then)
  private replaying = false; // between hello and ready, the replay is drawn at the size the shell has
  private pty = { cols: 0, rows: 0 }; // that size: the page that resized or typed last set it
  private backoff = 500;
  private timer = 0;
  private stopped = false; // its socket is done: the terminal was closed, or exited with 0
  private disposed = false;
  ctrl = false; // the key row's Ctrl: applies to the next key typed
  onCtrl: (on: boolean) => void = () => {};
  onExit: (code: number | null) => void = () => {};

  constructor(
    private readonly url: string,
    private readonly host: HTMLElement,
    onTitle: (title: string) => void,
    private readonly onEnd: () => void,
  ) {
    this.term = new Terminal({
      // Nerd Fonts first, where installed: prompts such as powerlevel10k draw their icons with them
      fontFamily: `"MesloLGS NF", "MesloLGS Nerd Font", "JetBrainsMono Nerd Font", "Hack Nerd Font", "FiraCode Nerd Font", ${getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim() || "monospace"}`,
      fontSize: coarse.matches ? 12 : 13,
      lineHeight: 1.2,
      scrollback: 5000,
      cursorBlink: true,
      allowProposedApi: true, // unicode11
      theme: theme(),
    });
    this.term.loadAddon(this.fit);
    this.term.loadAddon(new Unicode11Addon());
    this.term.unicode.activeVersion = "11"; // emoji and CJK widths as today's shells and editors count them
    this.term.onTitleChange(onTitle);
    this.term.onData((d) => {
      if (this.ctrl) {
        d = withCtrl(d);
        this.setCtrl(false);
      }
      this.input(encoder.encode(d));
    });
    this.term.onBinary((d) => this.input(Uint8Array.from(d, (c) => c.charCodeAt(0) & 0xff)));
    this.term.attachCustomKeyEventHandler((e) => {
      if (!isMac || e.type !== "keydown" || e.ctrlKey) return true;
      if (e.metaKey && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault(); // ⌘K clears, as in Terminal.app (elsewhere in Atrium it searches projects)
        this.term.clear();
        return false;
      }
      if (e.altKey && !e.metaKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
        e.preventDefault(); // ⌥← / ⌥→: a word back / forward (emacs keys, which zsh and bash bind)
        this.input(encoder.encode(e.key === "ArrowLeft" ? "\x1bb" : "\x1bf"));
        return false;
      }
      return true;
    });
    this.connect();
  }

  /** Into the page, once its box has a size; then fit it and give the shell that size. */
  layout() {
    if (this.disposed || !this.host.clientWidth || !this.host.clientHeight) return; // gone, or hidden
    if (!this.opened) {
      this.opened = true;
      this.term.open(this.host);
      this.term.loadAddon(new WebLinksAddon((_e, uri) => window.open(uri, "_blank", "noopener")));
      this.term.textarea?.addEventListener("focus", () => this.claim());
    }
    this.claim();
  }

  focus() {
    if (this.opened) this.term.focus();
  }

  private claim() {
    if (!this.opened || this.replaying || this.stopped || !this.host.clientWidth || !this.host.clientHeight) return;
    const d = this.fit.proposeDimensions();
    if (!d || !Number.isFinite(d.cols) || !Number.isFinite(d.rows)) return;
    if (d.cols !== this.term.cols || d.rows !== this.term.rows) this.term.resize(d.cols, d.rows);
    if (d.cols !== this.pty.cols || d.rows !== this.pty.rows) {
      this.pty = { cols: d.cols, rows: d.rows };
      this.send(JSON.stringify({ type: "resize", cols: d.cols, rows: d.rows }));
    }
  }

  input(bytes: Uint8Array) {
    this.claim(); // typing here takes the size back from another page
    this.send(bytes);
  }

  /** A key from the key row: arrows follow the program's cursor-key mode (vim, less use the application one). */
  key(seq: string | ((app: boolean) => string)) {
    this.input(encoder.encode(typeof seq === "string" ? seq : seq(this.term.modes.applicationCursorKeysMode)));
  }

  setCtrl(on: boolean) {
    this.ctrl = on;
    this.onCtrl(on);
  }

  paste(text: string) {
    this.term.paste(text); // bracketed when the shell asks for it
  }

  retheme() {
    this.term.options.theme = theme();
  }

  private send(data: string | Uint8Array) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(data);
  }

  /** Right away, e.g. back from the background on a phone (Safari drops its sockets there). */
  reconnect() {
    if (this.stopped || (this.ws && this.ws.readyState <= WebSocket.OPEN)) return;
    clearTimeout(this.timer);
    this.backoff = 500;
    this.connect();
  }

  private connect() {
    const ws = new WebSocket(this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") {
        this.term.write(new Uint8Array(ev.data as ArrayBuffer));
        return;
      }
      const msg = JSON.parse(ev.data);
      if (msg.type === "hello") {
        // the terminal from scratch: its recent output follows, drawn at the size it was written for
        this.backoff = 500;
        this.replaying = true;
        this.pty = { cols: msg.cols, rows: msg.rows };
        this.term.reset();
        this.term.resize(msg.cols, msg.rows);
        this.ended(msg.exit_code);
      } else if (msg.type === "ready") {
        this.term.write("", () => {
          this.replaying = false;
          this.claim();
        });
      } else if (msg.type === "size") {
        this.pty = { cols: msg.cols, rows: msg.rows };
      } else if (msg.type === "exit") {
        this.ended(msg.code);
      }
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws || this.stopped) return;
      this.ws = null;
      if (ev.code === 4000 || ev.code === 4404) {
        // closed (here or elsewhere), or its shell exited: the panel drops the tab
        this.stopped = true;
        this.onEnd();
        return;
      }
      this.timer = window.setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
  }

  private ended(code: number | null) {
    this.term.options.disableStdin = code !== null;
    this.term.options.cursorBlink = code === null;
    this.onExit(code);
  }

  dispose() {
    this.stopped = this.disposed = true;
    clearTimeout(this.timer);
    this.ws?.close();
    this.term.dispose();
  }
}

const KEYS: { label: ReactNode; title: string; seq?: string | ((app: boolean) => string); ctrl?: true }[] = [
  { label: "Esc", title: "Escape", seq: "\x1b" },
  { label: "Tab", title: "Tab (complete)", seq: "\t" },
  { label: "Ctrl", title: "Control, for the next key you type", ctrl: true },
  { label: "^C", title: "Ctrl-C: stop the command", seq: "\x03" },
  { label: "←", title: "Left", seq: (app) => (app ? "\x1bOD" : "\x1b[D") },
  { label: "↑", title: "Up (the previous command)", seq: (app) => (app ? "\x1bOA" : "\x1b[A") },
  { label: "↓", title: "Down", seq: (app) => (app ? "\x1bOB" : "\x1b[B") },
  { label: "→", title: "Right", seq: (app) => (app ? "\x1bOC" : "\x1b[C") },
];

export interface TerminalViewProps {
  pid: string;
  tid: string;
  active: boolean; // its tab is the open one and the panel is shown: it fits, takes the size and (with a mouse) the focus
  onTitle: (title: string) => void;
  onEnd: () => void; // closed elsewhere, or its shell exited with 0
}

export default function TerminalView({ pid, tid, active, onTitle, onEnd }: TerminalViewProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const session = useRef<Session | null>(null);
  const callbacks = useRef({ onTitle, onEnd });
  callbacks.current = { onTitle, onEnd };
  const [ctrl, setCtrl] = useState(false);
  const [exited, setExited] = useState(false);
  const [touch, setTouch] = useState(coarse.matches);

  useEffect(() => {
    const host = hostRef.current!;
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/terminal/${pid}/${tid}`;
    const s = new Session(url, host, (t) => callbacks.current.onTitle(t), () => callbacks.current.onEnd());
    s.onCtrl = setCtrl;
    s.onExit = (code) => setExited(code !== null);
    session.current = s;
    const ro = new ResizeObserver(() => requestAnimationFrame(() => s.layout())); // not inside the observer's own callback
    ro.observe(host);
    const unTouch = touchScroll(host, s.term);
    const themeWatch = new MutationObserver(() => s.retheme());
    themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const wake = () => document.visibilityState === "visible" && s.reconnect();
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);
    const onPointer = () => setTouch(coarse.matches);
    coarse.addEventListener("change", onPointer);
    return () => {
      ro.disconnect();
      unTouch();
      themeWatch.disconnect();
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
      coarse.removeEventListener("change", onPointer);
      s.dispose();
      session.current = null;
    };
  }, [pid, tid]);

  useEffect(() => {
    if (!active) return;
    const s = session.current;
    s?.layout();
    if (!coarse.matches) s?.focus(); // on a phone that would bring the keyboard up for a look at the output
  }, [active]);

  // the key row keeps the focus where it is, so the keyboard stays up while its keys are tapped: mousedown, not
  // pointerdown (WebKit drops the click of a tap whose pointerdown was cancelled)
  const keep = (e: React.MouseEvent) => e.preventDefault();
  return (
    <div className="term-view">
      <div className="term-host" ref={hostRef} />
      {touch && !exited && (
        <div className="term-keys">
          {KEYS.map((k) => (
            <button
              key={k.title}
              className={clsx(k.ctrl && ctrl && "on")}
              title={k.title}
              aria-label={k.title}
              onMouseDown={keep}
              onClick={() => (k.ctrl ? session.current?.setCtrl(!ctrl) : k.seq && session.current?.key(k.seq))}
            >
              {k.label}
            </button>
          ))}
          {navigator.clipboard?.readText && (
            <button
              title="Paste"
              aria-label="Paste"
              onMouseDown={keep}
              onClick={() => void navigator.clipboard.readText().then((t) => t && session.current?.paste(t), () => undefined)}
            >
              <ClipboardPaste size={15} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
