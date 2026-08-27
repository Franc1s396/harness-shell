type PtyOutputChunk = {
  ptySessionId: string;
  streamSequence: number;
  data: Uint8Array;
};

export type PtyOutputListener = (data: Uint8Array) => void;

type PtyOutputStream = {
  registered: boolean;
  nextSequence: number;
  history: Uint8Array[];
  preRegistrationBytes: number;
  listener: PtyOutputListener | null;
  closed: boolean;
  failure: PtyOutputBufferError | null;
};

export type PtyOutputRegistration = {
  closed: boolean;
};

export const MAX_PENDING_PTY_OUTPUT_BYTES = 1024 * 1024;

export type PtyOutputBufferErrorCode =
  | "PTY_STREAM_SEQUENCE_INVALID"
  | "PTY_PENDING_OUTPUT_LIMIT_EXCEEDED"
  | "PTY_STREAM_CLOSED"
  | "PTY_STREAM_NOT_REGISTERED"
  | "PTY_STREAM_ALREADY_SUBSCRIBED";

export class PtyOutputBufferError extends Error {
  readonly code: PtyOutputBufferErrorCode;

  constructor(code: PtyOutputBufferErrorCode, message: string) {
    super(message);
    this.name = "PtyOutputBufferError";
    this.code = code;
  }
}

const createStream = (): PtyOutputStream => ({
  registered: false,
  nextSequence: 1,
  history: [],
  preRegistrationBytes: 0,
  listener: null,
  closed: false,
  failure: null,
});

export class PtyOutputBuffer {
  private readonly streams = new Map<string, PtyOutputStream>();
  private pendingBytes = 0;

  ingest(chunk: PtyOutputChunk): void {
    let stream = this.streams.get(chunk.ptySessionId);
    if (!stream) {
      stream = createStream();
      this.streams.set(chunk.ptySessionId, stream);
    }
    if (stream.failure) throw stream.failure;
    if (stream.closed) {
      throw this.fail(
        chunk.ptySessionId,
        new PtyOutputBufferError(
          "PTY_STREAM_CLOSED",
          `PTY ${chunk.ptySessionId} emitted output after it was closed.`,
        ),
      );
    }
    if (chunk.streamSequence !== stream.nextSequence) {
      throw this.fail(
        chunk.ptySessionId,
        new PtyOutputBufferError(
          "PTY_STREAM_SEQUENCE_INVALID",
          `PTY ${chunk.ptySessionId} expected stream sequence ${stream.nextSequence}, received ${chunk.streamSequence}.`,
        ),
      );
    }
    if (
      !stream.registered &&
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

    stream.history.push(chunk.data);
    stream.nextSequence += 1;
    if (stream.registered) {
      stream.listener?.(chunk.data);
    } else {
      stream.preRegistrationBytes += chunk.data.byteLength;
      this.pendingBytes += chunk.data.byteLength;
    }
  }

  register(ptySessionId: string): PtyOutputRegistration {
    let stream = this.streams.get(ptySessionId);
    if (!stream) {
      stream = createStream();
      stream.registered = true;
      this.streams.set(ptySessionId, stream);
      return { closed: false };
    }
    if (stream.failure) throw stream.failure;
    if (!stream.registered) {
      stream.registered = true;
      this.pendingBytes -= stream.preRegistrationBytes;
      stream.preRegistrationBytes = 0;
    }
    return { closed: stream.closed };
  }

  subscribe(
    ptySessionId: string,
    listener: PtyOutputListener,
  ): () => void {
    const stream = this.streams.get(ptySessionId);
    if (!stream?.registered) {
      throw new PtyOutputBufferError(
        "PTY_STREAM_NOT_REGISTERED",
        `PTY ${ptySessionId} must be registered before subscribing.`,
      );
    }
    if (stream.failure) throw stream.failure;
    if (stream.listener) {
      throw new PtyOutputBufferError(
        "PTY_STREAM_ALREADY_SUBSCRIBED",
        `PTY ${ptySessionId} already has a live output subscriber.`,
      );
    }

    stream.listener = listener;
    for (const data of stream.history) listener(data);
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      if (stream.listener === listener) stream.listener = null;
    };
  }

  markClosed(ptySessionId: string): void {
    let stream = this.streams.get(ptySessionId);
    if (!stream) {
      stream = createStream();
      this.streams.set(ptySessionId, stream);
    }
    stream.closed = true;
  }

  unregister(ptySessionId: string): void {
    const stream = this.streams.get(ptySessionId);
    if (!stream) return;
    this.pendingBytes -= stream.preRegistrationBytes;
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
    let stream = this.streams.get(ptySessionId);
    if (!stream) {
      stream = createStream();
      this.streams.set(ptySessionId, stream);
    }
    this.pendingBytes -= stream.preRegistrationBytes;
    stream.preRegistrationBytes = 0;
    stream.listener = null;
    stream.failure = error;
    return error;
  }
}
