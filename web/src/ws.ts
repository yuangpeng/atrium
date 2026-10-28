// Single multiplexed websocket with topic subscriptions and auto-reconnect.

type Handler = (msg: any) => void;

class Socket {
  private ws: WebSocket | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private backoff = 500;
  private reconnectListeners = new Set<() => void>();
  private everConnected = false;
  connected = false;

  constructor() {
    this.connect();
  }

  private url() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }

  private connect() {
    const ws = new WebSocket(this.url());
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true;
      this.backoff = 500;
      for (const topic of this.handlers.keys()) ws.send(JSON.stringify({ op: "sub", topic }));
      if (this.everConnected) this.reconnectListeners.forEach((f) => f());
      this.everConnected = true;
    };
    ws.onmessage = (ev) => {
      let msg: any;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      this.handlers.get(msg.topic)?.forEach((h) => h(msg));
    };
    ws.onclose = () => {
      this.connected = false;
      setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
  }

  subscribe(topic: string, handler: Handler): () => void {
    let set = this.handlers.get(topic);
    if (!set) {
      set = new Set();
      this.handlers.set(topic, set);
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ op: "sub", topic }));
    }
    set.add(handler);
    return () => {
      const s = this.handlers.get(topic);
      if (!s) return;
      s.delete(handler);
      if (s.size === 0) {
        this.handlers.delete(topic);
        if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ op: "unsub", topic }));
      }
    };
  }

  onReconnect(f: () => void): () => void {
    this.reconnectListeners.add(f);
    return () => this.reconnectListeners.delete(f);
  }
}

export const socket = new Socket();
