type PtyOutputChunk = {
  ptySessionId: string;
  streamSequence: number;
  data: Uint8Array;
};

type PtyOutputStream = {
  registered: boolean;
  nextSequence: number;
  pending: Uint8Array[];
  closed: boolean;
  failure: PtyOutputBufferError | null;
};

export type PtyOutputRegistration = {
  initialOutput: Uint8Array[];
  closed: boolean;
};

export const MAX_PENDING_PTY_OUTPUT_BYTES = 1024 * 1024;

export type PtyOutputBufferErrorCode =
  | "PTY_STREAM_SEQUENCE_INVALID"
  | "PTY_PENDING_OUTPUT_LIMIT_EXCEEDED"
  | "PTY_STREAM_CLOSED";

export class PtyOutputBufferError extends Error {
  readonly code: PtyOutputBufferErrorCode;

  constructor(code: PtyOutputBufferErrorCode, message: string) {
    super(message);
    this.name = "PtyOutputBufferError";
    this.code = code;
  }
}

export class PtyOutputBuffer {
  private readonly streams = new Map<string, PtyOutputStream>();
  private pendingBytes = 0;

  ingest(chunk: PtyOutputChunk): Uint8Array | null {
    const stream = this.streams.get(chunk.ptySessionId);
    if (stream?.failure) {
      throw stream.failure;
    }
    if (stream?.closed) {
      throw this.fail(
        chunk.ptySessionId,
        new PtyOutputBufferError(
          "PTY_STREAM_CLOSED",
          `PTY ${chunk.ptySessionId} emitted output after it was closed.`,
        ),
      );
    }
    const expectedSequence = stream?.nextSequence ?? 1;
    if (chunk.streamSequence !== expectedSequence) {
      throw this.fail(
        chunk.ptySessionId,
        new PtyOutputBufferError(
          "PTY_STREAM_SEQUENCE_INVALID",
          `PTY ${chunk.ptySessionId} expected stream sequence ${expectedSequence}, received ${chunk.streamSequence}.`,
        ),
      );
    }

    if (stream?.registered) {
      stream.nextSequence += 1;
      return chunk.data;
    }

    if (
      this.pendingBytes + chunk.data.byteLength >
      MAX_PENDING_PTY_OUTPUT_BYTES
    ) {
      throw this.fail(
        chunk.ptySessionId,
        new PtyOutputBufferError(
          "PTY_PENDING_OUTPUT_LIMIT_EXCEEDED",
          `PTY output received before tab registration exceeded ${MAX_PENDING_PTY_OUTPUT_BYTES} bytes.`,
        ),
      );
    }

    if (stream) {
      stream.pending.push(chunk.data);
      stream.nextSequence += 1;
    } else {
      this.streams.set(chunk.ptySessionId, {
        registered: false,
        nextSequence: 2,
        pending: [chunk.data],
        closed: false,
        failure: null,
      });
    }
    this.pendingBytes += chunk.data.byteLength;

    return null;
  }

  register(ptySessionId: string): PtyOutputRegistration {
    const stream = this.streams.get(ptySessionId);
    if (!stream) {
      this.streams.set(ptySessionId, {
        registered: true,
        nextSequence: 1,
        pending: [],
        closed: false,
        failure: null,
      });
      return { initialOutput: [], closed: false };
    }
    if (stream.failure) {
      throw stream.failure;
    }

    stream.registered = true;
    const initialOutput = stream.pending;
    stream.pending = [];
    this.pendingBytes -= initialOutput.reduce(
      (total, chunk) => total + chunk.byteLength,
      0,
    );
    return { initialOutput, closed: stream.closed };
  }

  markClosed(ptySessionId: string): void {
    const stream = this.streams.get(ptySessionId);
    if (stream) {
      stream.closed = true;
      return;
    }
    this.streams.set(ptySessionId, {
      registered: false,
      nextSequence: 1,
      pending: [],
      closed: true,
      failure: null,
    });
  }

  unregister(ptySessionId: string): void {
    const stream = this.streams.get(ptySessionId);
    if (!stream) {
      return;
    }

    this.pendingBytes -= stream.pending.reduce(
      (total, chunk) => total + chunk.byteLength,
      0,
    );
    this.streams.delete(ptySessionId);
  }

  clear(): void {
    this.streams.clear();
    this.pendingBytes = 0;
  }

  private fail(
    ptySessionId: string,
    error: PtyOutputBufferError,
  ): PtyOutputBufferError {
    const stream = this.streams.get(ptySessionId);
    if (stream) {
      this.pendingBytes -= stream.pending.reduce(
        (total, chunk) => total + chunk.byteLength,
        0,
      );
      stream.pending = [];
      stream.failure = error;
    } else {
      this.streams.set(ptySessionId, {
        registered: false,
        nextSequence: 1,
        pending: [],
        closed: false,
        failure: error,
      });
    }
    return error;
  }
}
