import { describe, expect, it } from "vitest";

import {
  MAX_PENDING_PTY_OUTPUT_BYTES,
  PtyOutputBuffer,
  PtyOutputBufferError,
} from "./terminal-output-buffer";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

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
  it("replays output that arrives before the PTY tab is registered", () => {
    const buffer = new PtyOutputBuffer();

    const delivery = buffer.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: encoder.encode("Debian GNU/Linux\r\nroot@host:~# "),
    });

    expect(delivery).toBeNull();

    const registration = buffer.register("pty-1");

    expect(registration.initialOutput).toHaveLength(1);
    expect(registration.closed).toBe(false);
    expect(decoder.decode(registration.initialOutput[0])).toBe(
      "Debian GNU/Linux\r\nroot@host:~# ",
    );
  });

  it("rejects a non-contiguous stream sequence without appending it", () => {
    const buffer = new PtyOutputBuffer();
    buffer.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: encoder.encode("first"),
    });

    const error = captureBufferError(() =>
      buffer.ingest({
        ptySessionId: "pty-1",
        streamSequence: 3,
        data: encoder.encode("third"),
      }),
    );
    expect(error.code).toBe("PTY_STREAM_SEQUENCE_INVALID");

    const registrationError = captureBufferError(() =>
      buffer.register("pty-1"),
    );
    expect(registrationError.code).toBe("PTY_STREAM_SEQUENCE_INVALID");
  });

  it("rejects early output beyond the pending byte limit", () => {
    const buffer = new PtyOutputBuffer();
    buffer.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: new Uint8Array(MAX_PENDING_PTY_OUTPUT_BYTES),
    });

    const error = captureBufferError(() =>
      buffer.ingest({
        ptySessionId: "pty-1",
        streamSequence: 2,
        data: new Uint8Array([1]),
      }),
    );
    expect(error.code).toBe("PTY_PENDING_OUTPUT_LIMIT_EXCEEDED");

    const registrationError = captureBufferError(() =>
      buffer.register("pty-1"),
    );
    expect(registrationError.code).toBe(
      "PTY_PENDING_OUTPUT_LIMIT_EXCEEDED",
    );
  });

  it("preserves a close event that arrives before registration", () => {
    const buffer = new PtyOutputBuffer();
    buffer.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: encoder.encode("short-lived output"),
    });

    buffer.markClosed("pty-1");
    const registration = buffer.register("pty-1");

    expect(registration.closed).toBe(true);
    expect(
      registration.initialOutput.map((chunk) => decoder.decode(chunk)),
    ).toEqual(["short-lived output"]);
  });

  it("rejects output received after the stream was closed", () => {
    const buffer = new PtyOutputBuffer();
    buffer.markClosed("pty-1");

    const error = captureBufferError(() =>
      buffer.ingest({
        ptySessionId: "pty-1",
        streamSequence: 1,
        data: encoder.encode("late output"),
      }),
    );

    expect(error.code).toBe("PTY_STREAM_CLOSED");
  });
});
