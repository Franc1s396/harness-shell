import type { SessionBinding } from "./terminal-session";

type PtyOutputChunk = {
  ptySessionId: string;
  streamSequence: number;
  data: Uint8Array;
};

export type PtyOutputListener = (data: Uint8Array) => void;

type PtyStream = {
  nextSequence: number;
  pending: Uint8Array[];
  pendingBytes: number;
  binding: SessionBinding | null;
  closed: boolean;
  failure: PtyOutputBufferError | null;
};

type TabStream = {
  generation: number;
  history: Uint8Array[];
  listener: PtyOutputListener | null;
};

export type PtyOutputRegistration = {
  closed: boolean;
};

export const MAX_PENDING_PTY_OUTPUT_BYTES = 1024 * 1024;

export type PtyOutputBufferErrorCode =
  | "PTY_STREAM_SEQUENCE_INVALID"
  | "PTY_PENDING_OUTPUT_LIMIT_EXCEEDED"
  | "PTY_STREAM_CLOSED"
  | "PTY_STREAM_ALREADY_BOUND"
  | "PTY_STREAM_ALREADY_SUBSCRIBED"
  | "TERMINAL_TAB_NOT_REGISTERED"
  | "TERMINAL_TAB_GENERATION_INVALID";

export class PtyOutputBufferError extends Error {
  readonly code: PtyOutputBufferErrorCode;

  constructor(code: PtyOutputBufferErrorCode, message: string) {
    super(message);
    this.name = "PtyOutputBufferError";
    this.code = code;
  }
}

const createPtyStream = (): PtyStream => ({
  nextSequence: 1,
  pending: [],
  pendingBytes: 0,
  binding: null,
  closed: false,
  failure: null,
});

export class TerminalOutputBuffer {
  private readonly ptys = new Map<string, PtyStream>();
  private readonly tabs = new Map<string, TabStream>();
  private pendingBytes = 0;

  registerTab(tabId: string, generation: number): void {
    const current = this.tabs.get(tabId);
    if (current) {
      if (current.generation === generation) return;
      throw new PtyOutputBufferError(
        "TERMINAL_TAB_GENERATION_INVALID",
        `Terminal tab ${tabId} is already registered at generation ${current.generation}.`,
      );
    }
    this.tabs.set(tabId, { generation, history: [], listener: null });
  }

  advanceGeneration(tabId: string, generation: number): void {
    const tab = this.requireTab(tabId);
    if (generation <= tab.generation) {
      throw new PtyOutputBufferError(
        "TERMINAL_TAB_GENERATION_INVALID",
        `Terminal tab ${tabId} cannot advance from generation ${tab.generation} to ${generation}.`,
      );
    }
    tab.generation = generation;
  }

  bindPty(
    input: SessionBinding & { ptySessionId: string; separator?: Uint8Array },
  ): PtyOutputRegistration {
    const tab = this.requireTab(input.tabId);
    if (tab.generation !== input.generation) {
      throw new PtyOutputBufferError(
        "TERMINAL_TAB_GENERATION_INVALID",
        `Terminal tab ${input.tabId} is at generation ${tab.generation}, not ${input.generation}.`,
      );
    }

    let stream = this.ptys.get(input.ptySessionId);
    if (!stream) {
      stream = createPtyStream();
      this.ptys.set(input.ptySessionId, stream);
    }
    if (stream.failure) throw stream.failure;
    if (stream.binding) {
      if (
        stream.binding.tabId === input.tabId &&
        stream.binding.generation === input.generation
      ) {
        return { closed: stream.closed };
      }
      throw new PtyOutputBufferError(
        "PTY_STREAM_ALREADY_BOUND",
        `PTY ${input.ptySessionId} is already bound to terminal tab ${stream.binding.tabId}.`,
      );
    }

    stream.binding = { tabId: input.tabId, generation: input.generation };
    if (input.separator) this.deliver(tab, input.separator);
    for (const data of stream.pending) this.deliver(tab, data);
    this.pendingBytes -= stream.pendingBytes;
    stream.pending = [];
    stream.pendingBytes = 0;
    return { closed: stream.closed };
  }

  ingest(chunk: PtyOutputChunk): void {
    let stream = this.ptys.get(chunk.ptySessionId);
    if (!stream) {
      stream = createPtyStream();
      this.ptys.set(chunk.ptySessionId, stream);
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
    stream.nextSequence += 1;

    if (!stream.binding) {
      if (
        this.pendingBytes + chunk.data.byteLength >
        MAX_PENDING_PTY_OUTPUT_BYTES
      ) {
        throw this.fail(
          chunk.ptySessionId,
          new PtyOutputBufferError(
            "PTY_PENDING_OUTPUT_LIMIT_EXCEEDED",
            `PTY output received before tab binding exceeded ${MAX_PENDING_PTY_OUTPUT_BYTES} bytes.`,
          ),
        );
      }
      stream.pending.push(chunk.data);
      stream.pendingBytes += chunk.data.byteLength;
      this.pendingBytes += chunk.data.byteLength;
      return;
    }

    const tab = this.tabs.get(stream.binding.tabId);
    if (!tab || stream.binding.generation !== tab.generation) return;
    this.deliver(tab, chunk.data);
  }

  subscribe(tabId: string, listener: PtyOutputListener): () => void {
    const tab = this.requireTab(tabId);
    if (tab.listener) {
      throw new PtyOutputBufferError(
        "PTY_STREAM_ALREADY_SUBSCRIBED",
        `Terminal tab ${tabId} already has a live output subscriber.`,
      );
    }

    tab.listener = listener;
    for (const data of tab.history) listener(data);
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      if (tab.listener === listener) tab.listener = null;
    };
  }

  markPtyClosed(ptySessionId: string): void {
    let stream = this.ptys.get(ptySessionId);
    if (!stream) {
      stream = createPtyStream();
      this.ptys.set(ptySessionId, stream);
    }
    stream.closed = true;
  }

  unregisterPty(ptySessionId: string): void {
    const stream = this.ptys.get(ptySessionId);
    if (!stream) return;
    this.pendingBytes -= stream.pendingBytes;
    this.ptys.delete(ptySessionId);
  }

  unregisterTab(tabId: string): void {
    this.tabs.delete(tabId);
    for (const [ptySessionId, stream] of this.ptys) {
      if (stream.binding?.tabId === tabId) this.unregisterPty(ptySessionId);
    }
  }

  clear(): void {
    this.ptys.clear();
    this.tabs.clear();
    this.pendingBytes = 0;
  }

  private deliver(tab: TabStream, data: Uint8Array): void {
    tab.history.push(data);
    tab.listener?.(data);
  }

  private requireTab(tabId: string): TabStream {
    const tab = this.tabs.get(tabId);
    if (!tab) {
      throw new PtyOutputBufferError(
        "TERMINAL_TAB_NOT_REGISTERED",
        `Terminal tab ${tabId} must be registered before use.`,
      );
    }
    return tab;
  }

  private fail(
    ptySessionId: string,
    error: PtyOutputBufferError,
  ): PtyOutputBufferError {
    let stream = this.ptys.get(ptySessionId);
    if (!stream) {
      stream = createPtyStream();
      this.ptys.set(ptySessionId, stream);
    }
    this.pendingBytes -= stream.pendingBytes;
    stream.pending = [];
    stream.pendingBytes = 0;
    stream.failure = error;
    return error;
  }
}
