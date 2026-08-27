import { describe, expect, it, vi } from "vitest";

import {
  MAX_PENDING_PTY_OUTPUT_BYTES,
  PtyOutputBufferError,
  TerminalOutputBuffer,
} from "./terminal-output-buffer";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

const chunk = (sequence: number, text: string, ptySessionId = "pty-1") => ({
  ptySessionId,
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

describe("TerminalOutputBuffer", () => {
  it("inserts the reconnect divider before buffered new PTY output", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.registerTab("tab-1", 1);
    buffer.ingest(chunk(1, "new prompt", "pty-2"));
    buffer.advanceGeneration("tab-1", 2);
    buffer.bindPty({
      ptySessionId: "pty-2",
      tabId: "tab-1",
      generation: 2,
      separator: encoder.encode("\r\n── Reconnected ──\r\n"),
    });

    const listener = vi.fn();
    buffer.subscribe("tab-1", listener);
    expect(listener.mock.calls.map(([data]) => decoder.decode(data))).toEqual([
      "\r\n── Reconnected ──\r\n",
      "new prompt",
    ]);
  });

  it("does not deliver output from an old generation", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.registerTab("tab-1", 1);
    buffer.bindPty({
      ptySessionId: "pty-1",
      tabId: "tab-1",
      generation: 1,
    });
    const listener = vi.fn();
    buffer.subscribe("tab-1", listener);
    buffer.ingest(chunk(1, "old"));
    buffer.advanceGeneration("tab-1", 2);
    buffer.ingest(chunk(2, "late"));
    expect(listener.mock.calls.map(([data]) => decoder.decode(data))).toEqual([
      "old",
    ]);
  });

  it("delivers bound output directly and replays tab history on remount", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.registerTab("tab-1", 1);
    buffer.bindPty({
      ptySessionId: "pty-1",
      tabId: "tab-1",
      generation: 1,
    });
    const first = vi.fn();
    const unsubscribe = buffer.subscribe("tab-1", first);
    buffer.ingest(chunk(1, "first"));
    buffer.ingest(chunk(2, "second"));
    expect(first.mock.calls.map(([data]) => decoder.decode(data))).toEqual([
      "first",
      "second",
    ]);

    unsubscribe();
    unsubscribe();
    const remount = vi.fn();
    buffer.subscribe("tab-1", remount);
    expect(remount.mock.calls.map(([data]) => decoder.decode(data))).toEqual([
      "first",
      "second",
    ]);
  });

  it("rejects a second live subscriber and a subscription before registration", () => {
    const buffer = new TerminalOutputBuffer();
    expect(
      captureBufferError(() => buffer.subscribe("tab-1", vi.fn())).code,
    ).toBe("TERMINAL_TAB_NOT_REGISTERED");

    buffer.registerTab("tab-1", 1);
    buffer.subscribe("tab-1", vi.fn());
    expect(
      captureBufferError(() => buffer.subscribe("tab-1", vi.fn())).code,
    ).toBe("PTY_STREAM_ALREADY_SUBSCRIBED");
  });

  it("rejects a non-contiguous sequence and keeps the PTY stream failed", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.ingest(chunk(1, "first"));
    expect(
      captureBufferError(() => buffer.ingest(chunk(3, "third"))).code,
    ).toBe("PTY_STREAM_SEQUENCE_INVALID");
    buffer.registerTab("tab-1", 1);
    expect(
      captureBufferError(() =>
        buffer.bindPty({
          ptySessionId: "pty-1",
          tabId: "tab-1",
          generation: 1,
        }),
      ).code,
    ).toBe("PTY_STREAM_SEQUENCE_INVALID");
  });

  it("applies the 1 MiB limit only before PTY binding", () => {
    const pending = new TerminalOutputBuffer();
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

    const bound = new TerminalOutputBuffer();
    bound.registerTab("tab-1", 1);
    bound.bindPty({
      ptySessionId: "pty-1",
      tabId: "tab-1",
      generation: 1,
    });
    bound.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: new Uint8Array(MAX_PENDING_PTY_OUTPUT_BYTES + 1),
    });
  });

  it("preserves pre-bind output when closed and rejects later output", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.ingest(chunk(1, "short-lived output"));
    buffer.markPtyClosed("pty-1");
    buffer.registerTab("tab-1", 1);
    expect(
      buffer.bindPty({
        ptySessionId: "pty-1",
        tabId: "tab-1",
        generation: 1,
      }),
    ).toEqual({ closed: true });
    const listener = vi.fn();
    buffer.subscribe("tab-1", listener);
    expect(decoder.decode(listener.mock.calls[0][0])).toBe(
      "short-lived output",
    );
    expect(
      captureBufferError(() => buffer.ingest(chunk(2, "late output"))).code,
    ).toBe("PTY_STREAM_CLOSED");
  });

  it("removes retained history on unregisterTab and clear", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.registerTab("tab-1", 1);
    buffer.bindPty({
      ptySessionId: "pty-1",
      tabId: "tab-1",
      generation: 1,
    });
    buffer.ingest(chunk(1, "history"));
    buffer.unregisterTab("tab-1");
    expect(
      captureBufferError(() => buffer.subscribe("tab-1", vi.fn())).code,
    ).toBe("TERMINAL_TAB_NOT_REGISTERED");

    buffer.registerTab("tab-1", 1);
    buffer.unregisterPty("pty-1");
    buffer.clear();
    expect(
      captureBufferError(() => buffer.subscribe("tab-1", vi.fn())).code,
    ).toBe("TERMINAL_TAB_NOT_REGISTERED");
  });
});
