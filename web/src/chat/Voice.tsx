// Voice input, the composer's mic: a recording goes to the server's speech-to-text model, then — with the agent's
// conversation for context — to a model that writes it the way it was meant (atrium/voice.py). The text lands in the
// draft at the caret, to be read and edited before it is sent. Shown once Atrium's settings name a speech-to-text
// model (voice.base_url + voice.asr_model).
import type { JSX, RefObject } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, Mic, Square, X } from "lucide-react";
import { api } from "../api";
import { useApp } from "../store";
import { clsx } from "../util";
import { Spinner, useTip } from "../components/ui";

const MAX_SECONDS = 300; // a recording stops by itself after 5 minutes, and is transcribed
const MIN_MS = 500; // shorter: a stray tap
// what is sent: 16 kHz mono 16-bit WAV (32 kB a second), which every speech-to-text API and every model that listens
// takes — MediaRecorder makes webm or ogg in Chrome and Firefox, which chat models refuse
const RATE = 16000;

type Clip = { blob: Blob; name: string };
type Phase =
  | { kind: "idle" }
  | { kind: "starting" } // waiting for the microphone (the browser may be asking for it)
  | { kind: "recording"; since: number }
  | { kind: "transcribing" }
  | { kind: "refining"; heard: string }
  // in the draft (as it was right after, `draft`): the refined text, or what was heard instead — either swaps in
  | { kind: "refined"; heard: string; refined: string; shown: "refined" | "heard"; draft: string }
  | { kind: "failed"; error: string; clip: Clip }; // the recording is kept, to try again
const IDLE: Phase = { kind: "idle" };

type Recording = {
  stream: MediaStream;
  audio: AudioContext;
  source: MediaStreamAudioSourceNode;
  node: ScriptProcessorNode;
  pcm: Int16Array<ArrayBuffer>[];
  rate: number;
  since: number;
};

const CJK = /[⺀-鿿豈-﫿＀-￯　-〿]/;
/** Whether two pieces of text want a space between them: not around Chinese or Japanese, nor before punctuation. */
const spaced = (a: string, b: string) => !!a && !!b && !/[\s([{]$/.test(a) && !/^[\s.,;:!?)\]}]/.test(b) && !CJK.test(a.slice(-1)) && !CJK.test(b[0]!);
const clock = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
const coarse = () => matchMedia("(pointer: coarse)").matches;

function release(r: Recording) {
  r.node.onaudioprocess = null;
  r.source.disconnect();
  r.node.disconnect();
  r.stream.getTracks().forEach((t) => t.stop()); // and the browser's recording indicator goes
  void r.audio.close().catch(() => undefined);
}

/** The microphone's samples (at `rate`) as 16 kHz 16-bit PCM, block by block as they come: each sample made is the
 *  mean of the ones it spans, filter enough for speech. Counted in whole samples, so the rate can't drift. */
function downsampler(rate: number) {
  const out = Math.min(rate, RATE);
  let seen = 0;
  let made = 0;
  let sum = 0;
  let n = 0;
  return {
    rate: out,
    push(input: Float32Array): Int16Array<ArrayBuffer> {
      const pcm = new Int16Array(Math.ceil((input.length * out) / rate) + 1);
      let k = 0;
      for (const x of input) {
        sum += x;
        n++;
        seen++;
        if (seen * out >= (made + 1) * rate) {
          const v = Math.max(-1, Math.min(1, sum / n));
          pcm[k++] = v < 0 ? v * 0x8000 : v * 0x7fff;
          made++;
          sum = n = 0;
        }
      }
      return pcm.slice(0, k);
    },
  };
}

/** 16-bit mono PCM as a WAV file. */
function wav(pcm: Int16Array<ArrayBuffer>[], rate: number): Blob {
  const bytes = pcm.reduce((n, c) => n + c.byteLength, 0);
  const h = new DataView(new ArrayBuffer(44));
  const tag = (at: number, s: string) => [...s].forEach((c, i) => h.setUint8(at + i, c.charCodeAt(0)));
  tag(0, "RIFF");
  h.setUint32(4, 36 + bytes, true);
  tag(8, "WAVE");
  tag(12, "fmt ");
  h.setUint32(16, 16, true);
  h.setUint16(20, 1, true); // PCM
  h.setUint16(22, 1, true); // mono
  h.setUint32(24, rate, true);
  h.setUint32(28, rate * 2, true);
  h.setUint16(32, 2, true);
  h.setUint16(34, 16, true);
  tag(36, "data");
  h.setUint32(40, bytes, true);
  return new Blob([h, ...pcm], { type: "audio/wav" });
}

/** The mic of a composer holding `text`, typed into `input`. The refine model gets the agent's conversation (`sid`),
 *  or for a new agent the project (`cwd`: where it will work). Gives the button (next to send) and the strip over the
 *  bar that shows what it is doing; both are null while voice input isn't set up. */
export function useVoiceInput({ pid, sid, cwd, text, setText, input }: {
  pid: string;
  sid?: string;
  cwd?: string;
  text: string;
  setText: (text: string) => void;
  input: RefObject<HTMLTextAreaElement | null>;
}): { button: JSX.Element | null; strip: JSX.Element | null } {
  const voice = useApp((s) => s.meta?.settings.voice);
  const toast = useApp((s) => s.toast);
  const [phase, setPhase] = useState<Phase>(IDLE);
  const [now, setNow] = useState(0);
  const rec = useRef<Recording | null>(null);
  const work = useRef<AbortController | null>(null);
  const gen = useRef(0); // bumped by cancel: a microphone granted after that is let go
  const caret = useRef<{ start: number; end: number; text: string } | null>(null);
  const meter = useRef<HTMLSpanElement>(null);
  // for the audio callbacks and the requests, which outlive the render that started them
  const latest = useRef({ pid, sid, cwd, text, setText, refine: !!voice?.refine_model });
  latest.current = { pid, sid, cwd, text, setText, refine: !!voice?.refine_model };

  /** The caret as the mic is tapped, while the text field still has the focus the tap is about to take away (the tap
   *  that stops a recording finds the focus gone already: the caret from the first one stays). */
  const keepCaret = () => {
    const el = input.current;
    if (el && document.activeElement === el) caret.current = { start: el.selectionStart, end: el.selectionEnd, text: latest.current.text };
  };

  /** Puts `add` at the caret: where it is, if the field has the focus; else where it was when the mic was tapped,
   *  if the draft hasn't changed since; else at the end. Returns the draft. */
  const insert = (add: string): string => {
    const { text, setText } = latest.current;
    const el = input.current;
    let [start, end] = [text.length, text.length];
    if (el && document.activeElement === el) [start, end] = [el.selectionStart, el.selectionEnd];
    else if (caret.current?.text === text) [start, end] = [caret.current.start, caret.current.end];
    const before = text.slice(0, start);
    const after = text.slice(end);
    const pre = spaced(before, add) ? " " : "";
    const next = before + pre + add + (spaced(add, after) ? " " : "") + after;
    const at = before.length + pre.length + add.length;
    setText(next);
    caret.current = { start: at, end: at, text: next };
    if (el && !coarse()) requestAnimationFrame(() => (el.focus(), el.setSelectionRange(at, at))); // a phone keeps its keyboard down
    return next;
  };

  const cancel = useCallback(() => {
    gen.current++;
    const r = rec.current;
    rec.current = null;
    if (r) release(r);
    work.current?.abort();
    work.current = null;
    setPhase(IDLE);
  }, []);
  useEffect(() => cancel, [pid, sid, cancel]); // another chat, or none: nothing of this one lands there

  const transcribe = async (clip: Clip) => {
    work.current?.abort();
    const ac = (work.current = new AbortController());
    setPhase({ kind: "transcribing" });
    let heard: string;
    try {
      heard = (await api.transcribe(clip.blob, clip.name, ac.signal)).text.trim();
    } catch (e: any) {
      if (!ac.signal.aborted) setPhase({ kind: "failed", error: e.message || String(e), clip });
      return;
    }
    if (ac.signal.aborted) return;
    if (!heard) {
      setPhase(IDLE);
      return toast("Nothing was recognized in the recording", "info");
    }
    const { pid, sid, cwd, text, refine } = latest.current;
    if (!refine) {
      insert(heard);
      return setPhase(IDLE);
    }
    setPhase({ kind: "refining", heard });
    try {
      const res = await api.refineVoice(pid, { text: heard, sid, cwd, draft: text }, ac.signal);
      if (ac.signal.aborted) return;
      const draft = res.text ? insert(res.text) : latest.current.text;
      setPhase(res.refined && res.text !== heard ? { kind: "refined", heard, refined: res.text, shown: "refined", draft } : IDLE);
    } catch (e: any) {
      if (ac.signal.aborted) return;
      insert(heard);
      setPhase(IDLE);
      toast(`Couldn't refine it, so it is in as heard: ${e.message || e}`, "error");
    }
  };

  /** Stops recording and sends what was said. */
  const finish = () => {
    const r = rec.current;
    if (!r) return;
    rec.current = null;
    release(r);
    if (Date.now() - r.since < MIN_MS) {
      setPhase(IDLE);
      return toast("That was too short: tap the mic, speak, then tap it again", "info");
    }
    void transcribe({ blob: wav(r.pcm, r.rate), name: "voice.wav" });
  };

  const start = async () => {
    const md = navigator.mediaDevices;
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!md?.getUserMedia || !Ctx) {
      const why = window.isSecureContext ? "This browser can't record audio" : "The microphone needs a secure page: open Atrium over HTTPS (or on localhost)";
      return toast(why, "error");
    }
    const g = ++gen.current;
    work.current?.abort();
    const audio = new Ctx(); // during the tap: Safari keeps an AudioContext made afterwards suspended
    void audio.resume().catch(() => undefined);
    setPhase({ kind: "starting" });
    let stream: MediaStream;
    try {
      stream = await md.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e: any) {
      void audio.close().catch(() => undefined);
      if (g !== gen.current) return;
      setPhase(IDLE);
      const name = e?.name;
      return toast(
        name === "NotAllowedError" ? "Microphone access was denied: allow it for this site in the browser's settings"
          : name === "NotFoundError" ? "No microphone found"
          : `Can't use the microphone: ${e?.message || e}`,
        "error",
      );
    }
    if (g !== gen.current) {
      // cancelled while the browser asked
      stream.getTracks().forEach((t) => t.stop());
      void audio.close().catch(() => undefined);
      return;
    }
    void audio.resume().catch(() => undefined);
    const source = audio.createMediaStreamSource(stream);
    // deprecated, but every browser runs it, iOS Safari included, with no module to load first; it only runs while
    // connected to the speakers, here through a gain of 0
    const node = audio.createScriptProcessor(2048, 1, 1);
    const mute = audio.createGain();
    mute.gain.value = 0;
    source.connect(node);
    node.connect(mute);
    mute.connect(audio.destination);
    const down = downsampler(audio.sampleRate);
    const r: Recording = { stream, audio, source, node, pcm: [], rate: down.rate, since: Date.now() };
    node.onaudioprocess = (e) => {
      const block = e.inputBuffer.getChannelData(0);
      r.pcm.push(down.push(block));
      // the level meter: how loud this block was, as the bars' --lvl (0..1)
      let sum = 0;
      for (const x of block) sum += x * x;
      meter.current?.style.setProperty("--lvl", Math.min(1, Math.sqrt(sum / block.length) * 5).toFixed(2));
    };
    // the microphone went away (a phone call came in): what was said so far still counts
    stream.getAudioTracks()[0]?.addEventListener("ended", () => rec.current === r && finish());
    rec.current = r;
    setNow(r.since);
    setPhase({ kind: "recording", since: r.since });
  };

  // the clock, and the 5-minute stop
  useEffect(() => {
    if (phase.kind !== "recording") return;
    const id = setInterval(() => {
      setNow(Date.now());
      if (Date.now() - phase.since >= MAX_SECONDS * 1000) finish();
    }, 250);
    return () => clearInterval(id);
  }, [phase]);

  // Escape lets go of a recording, or of the requests (keyboards)
  const active = phase.kind === "starting" || phase.kind === "recording" || phase.kind === "transcribing" || phase.kind === "refining";
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && cancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, cancel]);

  // a refined dictation edited or sent: its strip has done its job
  useEffect(() => {
    if (phase.kind !== "refined" || text === phase.draft) return;
    const shown = phase.shown === "refined" ? phase.refined : phase.heard;
    if (!shown || !text.includes(shown)) setPhase(IDLE);
  }, [text, phase]);

  /** The other version in place of the one in the draft. */
  const swap = () => {
    if (phase.kind !== "refined") return;
    const [from, to] = phase.shown === "refined" ? [phase.refined, phase.heard] : [phase.heard, phase.refined];
    const { text, setText } = latest.current;
    const i = from ? text.lastIndexOf(from) : -1;
    let draft = text;
    if (i >= 0) setText((draft = text.slice(0, i) + to + text.slice(i + from.length)));
    else if (to) draft = insert(to);
    setPhase({ ...phase, shown: phase.shown === "refined" ? "heard" : "refined", draft });
  };

  const asHeard = () => {
    if (phase.kind !== "refining") return;
    work.current?.abort();
    insert(phase.heard);
    setPhase(IDLE);
  };

  const recording = phase.kind === "recording";
  const [tipProps, tip] = useTip(
    recording ? (
      <div>Stop and transcribe</div>
    ) : (
      <>
        <div>Voice input</div>
        <div className="k">{voice?.refine_model ? "tap, speak, tap again · refined with the conversation" : "tap, speak, tap again"}</div>
      </>
    ),
  );
  if (!voice?.base_url || !voice.asr_model) return { button: null, strip: null };

  const busy = phase.kind === "starting" || phase.kind === "transcribing" || phase.kind === "refining";
  const button = (
    <button
      className={clsx("send-btn mic", recording && "rec")}
      disabled={busy}
      aria-label={recording ? "Stop and transcribe" : "Voice input"}
      {...tipProps}
      onPointerDown={(e) => (tipProps.onPointerDown(e), keepCaret())}
      onClick={() => (recording ? finish() : void start())}
    >
      {busy ? <Spinner /> : recording ? <Square size={12} fill="currentColor" /> : <Mic size={17} />}
      {tip}
    </button>
  );

  const x = (label: string, onClick: () => void) => (
    <button className="icon-btn sm voice-x" onClick={onClick} aria-label={label} data-tip={label}>
      <X size={14} />
    </button>
  );
  let strip: JSX.Element | null = null;
  switch (phase.kind) {
    case "starting":
      strip = (
        <div className="voice-strip">
          <Spinner />
          <span className="voice-text">Waiting for the microphone…</span>
          {x("Cancel", cancel)}
        </div>
      );
      break;
    case "recording":
      strip = (
        <div className="voice-strip">
          <span className="voice-dot" />
          <span className="voice-time">{clock(Math.max(0, now - phase.since))}</span>
          <span className="voice-meter" ref={meter} aria-hidden>
            <i /><i /><i /><i /><i />
          </span>
          <span className="voice-text">Listening… ■ when done</span>
          {x("Discard the recording", cancel)}
        </div>
      );
      break;
    case "transcribing":
      strip = (
        <div className="voice-strip">
          <Spinner />
          <span className="voice-text">Transcribing…</span>
          {x("Cancel", cancel)}
        </div>
      );
      break;
    // the status on one line, the text it is about (what was heard, the error) on its own below: a phone's
    // composer leaves a line too little room for both
    case "refining":
      strip = (
        <div className="voice-strip">
          <Spinner />
          <span className="voice-label">Refining…</span>
          <span className="voice-fill" />
          <button className="voice-act" onClick={asHeard} data-tip="Put it in as heard, without waiting">
            As heard
          </button>
          {x("Cancel", cancel)}
          <span className="voice-detail heard">{phase.heard}</span>
        </div>
      );
      break;
    case "refined":
      strip = (
        <div className="voice-strip">
          <Check size={14} className="voice-ok" />
          <span className="voice-label" data-tip="Rewritten with the conversation as context: paths, names and English fixed">
            {phase.shown === "heard" ? "As heard" : phase.refined ? "Refined" : "Refining left nothing"}
          </span>
          <span className="voice-fill" />
          <button className="voice-act" onClick={swap}>
            {phase.shown === "refined" ? "As heard" : "Refined"}
          </button>
          {x("Close", () => setPhase(IDLE))}
        </div>
      );
      break;
    case "failed":
      strip = (
        <div className="voice-strip">
          <AlertTriangle size={14} className="voice-err" />
          <span className="voice-label">Couldn't transcribe</span>
          <span className="voice-fill" />
          <button className="voice-act" onClick={() => void transcribe(phase.clip)}>
            Retry
          </button>
          {x("Discard the recording", () => setPhase(IDLE))}
          <span className="voice-detail err">{phase.error}</span>
        </div>
      );
  }
  return { button, strip };
}
