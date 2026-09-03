const HEARTBEAT_INTERVAL_MS = 5_000;
const HEARTBEAT_TIMEOUT_MS = 15_000;
const OUTBOUND_QUEUE_LIMIT = 64;

export type PtyInputFrame = Readonly<{
  ptySessionId: string;
  dataB64: string;
}>;

export type RuntimeServerEvent = Readonly<{
  schema_version: 1;
  type:
    | "pty.input_result"
    | "pty.output"
    | "pty.closed"
    | "ssh.connection_state"
    | "sftp.operation_progress"
    | "runtime.pong"
    | "runtime.error";
  message_id: string;
  causation_id: string | null;
  timestamp: string;
  payload: Readonly<Record<string, unknown>>;
}>;

export type RuntimeDisconnectedEvent = Readonly<{
  type: "runtime.disconnected";
  errorCode: string;
}>;

export type RuntimeEvent = RuntimeServerEvent | RuntimeDisconnectedEvent;
export type RuntimeEventListener = (event: RuntimeEvent) => void;
export type Unsubscribe = () => void;

type SocketLike = {
  readyState: number;
  onopen: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: (() => void) | null;
  onclose: ((event: { code: number; reason: string }) => void) | null;
  send(value: string): void;
  close(code?: number, reason?: string): void;
};

export class RuntimeWebSocket {
  readonly #url: string;
  readonly #socketFactory: (url: string) => SocketLike;
  readonly #randomUuid: () => string;
  readonly #now: () => Date;
  readonly #listeners = new Set<RuntimeEventListener>();
  readonly #queue: string[] = [];
  readonly #pendingPings = new Set<string>();
  #socket: SocketLike | null = null;
  #heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  #lastPongAt = 0;
  #terminal = false;

  constructor(
    baseUrl: string,
    dependencies: {
      socketFactory?: (url: string) => SocketLike;
      randomUuid?: () => string;
      now?: () => Date;
    } = {},
  ) {
    const parsed = new URL(baseUrl);
    parsed.protocol = "ws:";
    parsed.pathname = "/v1/runtime/events";
    parsed.search = "";
    parsed.hash = "";
    this.#url = parsed.toString();
    this.#socketFactory = dependencies.socketFactory ?? ((url) => {
      // The owner uses only the WebSocket members modeled by SocketLike. DOM
      // callbacks accept event arguments that this boundary intentionally ignores.
      return new WebSocket(url) as unknown as SocketLike;
    });
    this.#randomUuid = dependencies.randomUuid ?? (() => crypto.randomUUID());
    this.#now = dependencies.now ?? (() => new Date());
  }

  subscribe(listener: RuntimeEventListener): Unsubscribe {
    this.#listeners.add(listener);
    if (this.#socket === null && !this.#terminal) this.#connect();
    return () => this.#listeners.delete(listener);
  }

  sendPtyInput(frame: PtyInputFrame): void {
    const messageId = this.#randomUuid();
    const encoded = JSON.stringify({
      schema_version: 1,
      type: "pty.input",
      message_id: messageId,
      causation_id: null,
      timestamp: this.#now().toISOString(),
      payload: {
        pty_session_id: frame.ptySessionId,
        data_b64: frame.dataB64,
      },
    });
    this.#sendOrQueue(encoded);
  }

  close(): void {
    if (this.#terminal) return;
    this.#terminal = true;
    this.#clearHeartbeat();
    this.#queue.length = 0;
    this.#pendingPings.clear();
    this.#socket?.close(1000, "application teardown");
  }

  #connect(): void {
    const socket = this.#socketFactory(this.#url);
    this.#socket = socket;
    socket.onopen = () => {
      if (this.#terminal) return;
      this.#lastPongAt = Date.now();
      for (const encoded of this.#queue.splice(0)) socket.send(encoded);
      this.#heartbeatTimer = setInterval(() => this.#heartbeat(), HEARTBEAT_INTERVAL_MS);
    };
    socket.onmessage = (event) => this.#receive(event.data);
    socket.onerror = () => this.#fail("RUNTIME_WEBSOCKET_DISCONNECTED");
    socket.onclose = () => {
      if (!this.#terminal) this.#fail("RUNTIME_WEBSOCKET_DISCONNECTED");
    };
  }

  #heartbeat(): void {
    if (Date.now() - this.#lastPongAt >= HEARTBEAT_TIMEOUT_MS) {
      this.#fail("RUNTIME_WEBSOCKET_HEARTBEAT_TIMEOUT");
      return;
    }
    const messageId = this.#randomUuid();
    this.#pendingPings.add(messageId);
    this.#sendOrQueue(JSON.stringify({
      schema_version: 1,
      type: "runtime.ping",
      message_id: messageId,
      causation_id: null,
      timestamp: this.#now().toISOString(),
      payload: { client_timestamp: this.#now().toISOString() },
    }));
  }

  #receive(raw: unknown): void {
    if (typeof raw !== "string" || new TextEncoder().encode(raw).byteLength > 65_536) {
      this.#fail("RUNTIME_WEBSOCKET_PROTOCOL_ERROR");
      return;
    }
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      this.#fail("RUNTIME_WEBSOCKET_PROTOCOL_ERROR");
      return;
    }
    const event = parseServerEvent(value);
    if (event === null) {
      this.#fail("RUNTIME_WEBSOCKET_PROTOCOL_ERROR");
      return;
    }
    if (event.type === "runtime.pong") {
      if (event.causation_id === null || !this.#pendingPings.delete(event.causation_id)) {
        this.#fail("RUNTIME_WEBSOCKET_PROTOCOL_ERROR");
        return;
      }
      this.#lastPongAt = Date.now();
    }
    for (const listener of this.#listeners) listener(event);
  }

  #sendOrQueue(encoded: string): void {
    if (this.#terminal) throw new Error("RUNTIME_WEBSOCKET_DISCONNECTED");
    if (this.#socket?.readyState === 1) {
      this.#socket.send(encoded);
      return;
    }
    if (this.#queue.length >= OUTBOUND_QUEUE_LIMIT) {
      throw new Error("RUNTIME_WEBSOCKET_QUEUE_FULL");
    }
    this.#queue.push(encoded);
  }

  #fail(errorCode: string): void {
    if (this.#terminal) return;
    this.#terminal = true;
    this.#clearHeartbeat();
    this.#queue.length = 0;
    this.#pendingPings.clear();
    const socket = this.#socket;
    if (socket !== null && socket.readyState < 2) socket.close(4400, "runtime failure");
    for (const listener of this.#listeners) {
      listener({ type: "runtime.disconnected", errorCode });
    }
  }

  #clearHeartbeat(): void {
    if (this.#heartbeatTimer !== null) clearInterval(this.#heartbeatTimer);
    this.#heartbeatTimer = null;
  }
}

const SERVER_TYPES = new Set([
  "pty.input_result",
  "pty.output",
  "pty.closed",
  "ssh.connection_state",
  "sftp.operation_progress",
  "runtime.pong",
  "runtime.error",
]);

const parseServerEvent = (value: unknown): RuntimeServerEvent | null => {
  if (!isRecord(value) || !hasExactKeys(value, [
    "schema_version", "type", "message_id", "causation_id", "timestamp", "payload",
  ])) return null;
  if (
    value.schema_version !== 1 ||
    typeof value.type !== "string" ||
    !SERVER_TYPES.has(value.type) ||
    typeof value.message_id !== "string" ||
    (value.causation_id !== null && typeof value.causation_id !== "string") ||
    typeof value.timestamp !== "string" ||
    Number.isNaN(Date.parse(value.timestamp)) ||
    !isRecord(value.payload)
  ) return null;
  if (value.type === "runtime.pong" && !hasExactKeys(value.payload, ["server_timestamp"])) {
    return null;
  }
  return value as RuntimeServerEvent;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const hasExactKeys = (value: Record<string, unknown>, keys: readonly string[]): boolean =>
  Object.keys(value).length === keys.length &&
  keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
