import { describe, expect, it, vi } from "vitest";

import {
  MAX_PENDING_PTY_OUTPUT_BYTES,
  PtyOutputBuffer,
  PtyOutputBufferError,
} from "./terminal-output-buffer";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

const chunk = (sequence: number, text: string) => ({
  ptySessionId: "pty-1",
  streamSequence: sequence,
  data: encoder.encode(text),
});

function captureBufferError(action: () => void): PtyOutputBufferError {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(PtyOutputBufferError);
    return error as PtyOutputBufferError;
  }
  throw new Error("Expected PtyOutputBufferError to be thrown.");
}

describe("PtyOutputBuffer", () => {
  it("replays output that arrives before registration to the first subscriber", () => {
    const buffer = new PtyOutputBuffer();
    buffer.ingest(chunk(1, "Debian GNU/Linux\r\n"));
    expect(buffer.register("pty-1")).toEqual({ closed: false });

    const listener = vi.fn();
    buffer.subscribe("pty-1", listener);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(decoder.decode(listener.mock.calls[0][0])).toBe(
      "Debian GNU/Linux\r\n",
    );
  });

  it("delivers registered output directly and replays history on remount", () => {
    const buffer = new PtyOutputBuffer();
    buffer.register("pty-1");
    const first = vi.fn();
    const unsubscribe = buffer.subscribe("pty-1", first);
    buffer.ingest(chunk(1, "first"));
    buffer.ingest(chunk(2, "second"));
    expect(first.mock.calls.map(([data]) => decoder.decode(data))).toEqual([
      "first",
      "second",
    ]);

    unsubscribe();
    unsubscribe();
    const remount = vi.fn();
    buffer.subscribe("pty-1", remount);
    expect(remount.mock.calls.map(([data]) => decoder.decode(data))).toEqual([
      "first",
      "second",
    ]);
  });

  it("rejects a second live subscriber and a subscription before registration", () => {
    const buffer = new PtyOutputBuffer();
    expect(
      captureBufferError(() => buffer.subscribe("pty-1", vi.fn())).code,
    ).toBe("PTY_STREAM_NOT_REGISTERED");

    buffer.register("pty-1");
    buffer.subscribe("pty-1", vi.fn());
    expect(
      captureBufferError(() => buffer.subscribe("pty-1", vi.fn())).code,
    ).toBe("PTY_STREAM_ALREADY_SUBSCRIBED");
  });

  it("rejects a non-contiguous sequence and keeps the stream failed", () => {
    const buffer = new PtyOutputBuffer();
    buffer.ingest(chunk(1, "first"));
    expect(
      captureBufferError(() => buffer.ingest(chunk(3, "third"))).code,
    ).toBe("PTY_STREAM_SEQUENCE_INVALID");
    expect(
      captureBufferError(() => buffer.register("pty-1")).code,
    ).toBe("PTY_STREAM_SEQUENCE_INVALID");
  });

  it("applies the 1MiB limit only before registration", () => {
    const pending = new PtyOutputBuffer();
    pending.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: new Uint8Array(MAX_PENDING_PTY_OUTPUT_BYTES),
    });
    expect(
      captureBufferError(() =>
        pending.ingest({
          ptySessionId: "pty-1",
          streamSequence: 2,
          data: new Uint8Array([1]),
        }),
      ).code,
    ).toBe("PTY_PENDING_OUTPUT_LIMIT_EXCEEDED");

    const registered = new PtyOutputBuffer();
    registered.register("pty-1");
    registered.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: new Uint8Array(MAX_PENDING_PTY_OUTPUT_BYTES + 1),
    });
  });

  it("preserves history when closed and rejects later output", () => {
    const buffer = new PtyOutputBuffer();
    buffer.ingest(chunk(1, "short-lived output"));
    buffer.markClosed("pty-1");
    expect(buffer.register("pty-1")).toEqual({ closed: true });
    const listener = vi.fn();
    buffer.subscribe("pty-1", listener);
    expect(decoder.decode(listener.mock.calls[0][0])).toBe(
      "short-lived output",
    );
    expect(
      captureBufferError(() => buffer.ingest(chunk(2, "late output"))).code,
    ).toBe("PTY_STREAM_CLOSED");
  });

  it("removes retained history on unregister and clear", () => {
    const buffer = new PtyOutputBuffer();
    buffer.register("pty-1");
    buffer.ingest(chunk(1, "history"));
    buffer.unregister("pty-1");
    expect(
      captureBufferError(() => buffer.subscribe("pty-1", vi.fn())).code,
    ).toBe("PTY_STREAM_NOT_REGISTERED");

    buffer.register("pty-1");
    buffer.clear();
    expect(
      captureBufferError(() => buffer.subscribe("pty-1", vi.fn())).code,
    ).toBe("PTY_STREAM_NOT_REGISTERED");
  });
});
