import { beforeEach, expect, it, vi } from "vitest";

import { RuntimeWebSocket } from "./runtime-websocket";


class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readonly sent: string[] = [];
  readyState = FakeSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;

  send(value: string): void { this.sent.push(value); }
  close(): void {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.({ code: 1000, reason: "closed" });
  }
  open(): void {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  message(value: unknown): void { this.onmessage?.({ data: value }); }
}

const uuid = "10000000-0000-4000-8000-000000000001";
let sockets: FakeSocket[];
let runtime: RuntimeWebSocket;

beforeEach(() => {
  vi.useFakeTimers();
  sockets = [];
  runtime = new RuntimeWebSocket("http://127.0.0.1:8765", {
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    randomUuid: () => uuid,
    now: () => new Date("2026-09-02T00:00:00.000Z"),
  });
});

it("opens only one socket for multiple subscribers and sends typed heartbeat", () => {
  runtime.subscribe(vi.fn());
  runtime.subscribe(vi.fn());
  expect(sockets).toHaveLength(1);
  sockets[0].open();

  vi.advanceTimersByTime(5_000);

  expect(JSON.parse(sockets[0].sent[0])).toMatchObject({
    schema_version: 1,
    type: "runtime.ping",
    message_id: uuid,
    causation_id: null,
  });
});

it("sends strict PTY input and projects typed server events", () => {
  const listener = vi.fn();
  runtime.subscribe(listener);
  sockets[0].open();

  runtime.sendPtyInput({ ptySessionId: "pty-1", dataB64: "YQ==" });
  sockets[0].message(JSON.stringify({
    schema_version: 1,
    type: "ssh.connection_state",
    message_id: uuid,
    causation_id: null,
    timestamp: "2026-09-02T00:00:00.000Z",
    payload: {
      connection_id: "connection-1",
      state: "READY",
      session_id: "session-1",
      error_code: null,
      recoverable: false,
      correlation_id: uuid,
      host_key_candidate: null,
      trusted_fingerprint_sha256: null,
    },
  }));

  expect(JSON.parse(sockets[0].sent[0])).toMatchObject({
    type: "pty.input",
    payload: { pty_session_id: "pty-1", data_b64: "YQ==" },
  });
  expect(listener).toHaveBeenCalledWith(expect.objectContaining({
    type: "ssh.connection_state",
  }));
});

it("treats unknown messages and socket close as terminal without reconnect", () => {
  const listener = vi.fn();
  runtime.subscribe(listener);
  sockets[0].open();

  sockets[0].message(JSON.stringify({
    schema_version: 1,
    type: "unknown",
    message_id: uuid,
    causation_id: null,
    timestamp: "2026-09-02T00:00:00.000Z",
    payload: {},
  }));
  vi.advanceTimersByTime(60_000);

  expect(sockets).toHaveLength(1);
  expect(listener).toHaveBeenCalledWith({
    type: "runtime.disconnected",
    errorCode: "RUNTIME_WEBSOCKET_PROTOCOL_ERROR",
  });
});

it("bounds queued PTY frames before the socket opens", () => {
  runtime.subscribe(vi.fn());
  for (let index = 0; index < 64; index += 1) {
    runtime.sendPtyInput({ ptySessionId: "pty-1", dataB64: "YQ==" });
  }

  expect(() => runtime.sendPtyInput({
    ptySessionId: "pty-1",
    dataB64: "YQ==",
  })).toThrow("RUNTIME_WEBSOCKET_QUEUE_FULL");
});

it("explicit close is terminal and does not reconnect", () => {
  runtime.subscribe(vi.fn());
  sockets[0].open();

  runtime.close();
  vi.advanceTimersByTime(60_000);

  expect(sockets).toHaveLength(1);
  expect(sockets[0].readyState).toBe(FakeSocket.CLOSED);
});
