const JSON_RESPONSE_LIMIT = 1_048_576;
const BINARY_CHUNK_LIMIT = 262_144;
const SSE_FRAME_LIMIT = 65_536;
const SSE_BODY_LIMIT = 4_194_304;

type FetchLike = typeof fetch;

export type BinaryChunk = Readonly<{
  requestId: string;
  sequence: number;
  offset: number;
  byteCount: number;
  eof: boolean;
  body: Uint8Array;
}>;

export type BackendRequestOptions = Readonly<{
  body?: unknown;
  signal?: AbortSignal;
}>;

export type BackendSseFrame = Readonly<{
  requestId: string;
  event: string;
  id: string;
  data: unknown;
}>;

export class BackendSseError extends Error {
  readonly kind: "INVALID" | "TOO_LARGE" | "INTERRUPTED";

  constructor(kind: "INVALID" | "TOO_LARGE" | "INTERRUPTED") {
    super(`BACKEND_SSE_${kind}`);
    this.name = "BackendSseError";
    this.kind = kind;
  }
}

export class BackendProblem extends Error {
  readonly code: string;
  readonly requestId: string;
  readonly status: number;
  readonly details: Readonly<Record<string, unknown>>;

  constructor(input: {
    code: string;
    message: string;
    requestId: string;
    status: number;
    details: Readonly<Record<string, unknown>>;
  }) {
    super(input.message);
    this.name = "BackendProblem";
    this.code = input.code;
    this.requestId = input.requestId;
    this.status = input.status;
    this.details = input.details;
  }
}

export class BackendHttpClient {
  readonly #baseUrl: string;
  readonly #fetch: FetchLike;
  readonly #randomUuid: () => string;

  constructor(
    baseUrl: string,
    dependencies: {
      fetchImpl?: FetchLike;
      randomUuid?: () => string;
    } = {},
  ) {
    this.#baseUrl = baseUrl;
    this.#fetch = dependencies.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.#randomUuid = dependencies.randomUuid ?? (() => crypto.randomUUID());
  }

  async request<T>(
    method: "GET" | "POST" | "PATCH" | "DELETE",
    path: string,
    options: BackendRequestOptions = {},
  ): Promise<T> {
    const requestId = this.#randomUuid();
    const hasBody = options.body !== undefined;
    const response = await this.#fetch(this.#url(path), {
      method,
      headers: {
        ...(hasBody ? { "Content-Type": "application/json" } : {}),
        "X-Request-ID": requestId,
      },
      ...(hasBody ? { body: JSON.stringify(options.body) } : {}),
      ...(options.signal ? { signal: options.signal } : {}),
    });
    if (response.status === 204) {
      this.#requireResponseRequestId(response, requestId);
      return undefined as T;
    }
    return this.#readJson<T>(response, requestId);
  }

  async putBinary<T>(
    path: string,
    body: Uint8Array,
    chunkOffset: number,
    signal?: AbortSignal,
  ): Promise<T> {
    if (body.byteLength === 0 || body.byteLength > BINARY_CHUNK_LIMIT) {
      throw new Error("BACKEND_BINARY_REQUEST_INVALID");
    }
    const offset = canonicalCount(chunkOffset);
    const requestId = this.#randomUuid();
    const response = await this.#fetch(this.#url(path), {
      method: "PUT",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Chunk-Offset": offset,
        "X-Request-ID": requestId,
      },
      body,
      ...(signal ? { signal } : {}),
    });
    return this.#readJson<T>(response, requestId);
  }

  async *postSse(
    path: string,
    body: unknown,
    signal?: AbortSignal,
  ): AsyncGenerator<BackendSseFrame> {
    const requestId = this.#randomUuid();
    let response: Response;
    try {
      response = await this.#fetch(this.#url(path), {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
          "X-Request-ID": requestId,
        },
        body: JSON.stringify(body),
        ...(signal ? { signal } : {}),
      });
    } catch {
      throw new BackendSseError("INTERRUPTED");
    }
    if (!response.ok) {
      await this.#readJson<never>(response, requestId);
      return;
    }
    if (response.status !== 200) throw new BackendSseError("INVALID");
    this.#requireResponseRequestId(response, requestId);
    if (
      mediaType(response) !== "text/event-stream" ||
      response.headers.get("Cache-Control")?.trim().toLowerCase() !== "no-store" ||
      response.body === null
    ) {
      throw new BackendSseError("INVALID");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const encoder = new TextEncoder();
    let buffer = "";
    let totalBytes = 0;
    let sawFrame = false;
    let reachedEof = false;
    try {
      while (true) {
        let result: ReadableStreamReadResult<Uint8Array>;
        try {
          result = await reader.read();
        } catch {
          throw new BackendSseError("INTERRUPTED");
        }
        if (result.done) {
          reachedEof = true;
          try {
            buffer += decoder.decode();
          } catch {
            throw new BackendSseError("INVALID");
          }
          break;
        }
        totalBytes += result.value.byteLength;
        if (totalBytes > SSE_BODY_LIMIT) {
          throw new BackendSseError("TOO_LARGE");
        }
        try {
          buffer += decoder.decode(result.value, { stream: true });
        } catch {
          throw new BackendSseError("INVALID");
        }

        while (true) {
          const frame = takeFrame(buffer);
          if (frame === null) break;
          if (encoder.encode(frame.raw + frame.delimiter).byteLength > SSE_FRAME_LIMIT) {
            throw new BackendSseError("TOO_LARGE");
          }
          buffer = frame.rest;
          sawFrame = true;
          yield parseSseFrame(frame.raw, frame.delimiter, requestId);
        }
      }

      if (buffer.length !== 0 || !sawFrame) {
        throw new BackendSseError("INVALID");
      }
    } finally {
      if (!reachedEof) await reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
  }

  async getBinary(
    path: string,
    chunkOffset: number,
    signal?: AbortSignal,
  ): Promise<BinaryChunk> {
    const match = /\/chunks\/(0|[1-9]\d*)$/.exec(path);
    if (!match) throw new Error("BACKEND_BINARY_REQUEST_INVALID");
    const sequence = parseCanonicalCount(match[1]);
    const offsetText = canonicalCount(chunkOffset);
    const requestId = this.#randomUuid();
    const response = await this.#fetch(
      `${this.#url(path)}?offset=${offsetText}`,
      {
        method: "GET",
        headers: { "X-Request-ID": requestId },
        ...(signal ? { signal } : {}),
      },
    );
    if (mediaType(response) === "application/problem+json") {
      return this.#readJson<never>(response, requestId);
    }
    this.#requireResponseRequestId(response, requestId);
    if (!response.ok || mediaType(response) !== "application/octet-stream") {
      throw new Error("BACKEND_BINARY_RESPONSE_INVALID");
    }
    const headerSequence = requireCanonicalHeader(response, "X-Chunk-Sequence");
    const headerOffset = requireCanonicalHeader(response, "X-Chunk-Offset");
    const byteCount = requireCanonicalHeader(response, "X-Chunk-Byte-Count");
    const eofText = response.headers.get("X-Chunk-EOF");
    if (
      headerSequence !== sequence ||
      headerOffset !== chunkOffset ||
      (eofText !== "true" && eofText !== "false")
    ) {
      throw new Error("BACKEND_BINARY_RESPONSE_INVALID");
    }
    const arrayBuffer = await response.arrayBuffer();
    if (arrayBuffer.byteLength > BINARY_CHUNK_LIMIT) {
      throw new Error("BACKEND_BINARY_RESPONSE_TOO_LARGE");
    }
    if (byteCount !== arrayBuffer.byteLength) {
      throw new Error("BACKEND_BINARY_RESPONSE_INVALID");
    }
    return {
      requestId,
      sequence,
      offset: chunkOffset,
      byteCount,
      eof: eofText === "true",
      body: new Uint8Array(arrayBuffer.slice(0)),
    };
  }

  #url(path: string): string {
    if (!path.startsWith("/") || path.startsWith("//") || path.includes("#")) {
      throw new Error("BACKEND_PATH_INVALID");
    }
    return `${this.#baseUrl}${path}`;
  }

  async #readJson<T>(response: Response, requestId: string): Promise<T> {
    this.#requireResponseRequestId(response, requestId);
    const type = mediaType(response);
    if (type !== "application/json" && type !== "application/problem+json") {
      throw new Error("BACKEND_JSON_RESPONSE_INVALID");
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > JSON_RESPONSE_LIMIT) {
      throw new Error("BACKEND_JSON_RESPONSE_TOO_LARGE");
    }
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      throw new Error("BACKEND_JSON_RESPONSE_INVALID");
    }
    if (!isRecord(value) || value.request_id !== requestId) {
      throw new Error("BACKEND_RESPONSE_CORRELATION_INVALID");
    }
    if (type === "application/problem+json") {
      if (
        typeof value.error_code !== "string" ||
        typeof value.message !== "string" ||
        typeof value.status !== "number" ||
        value.status !== response.status ||
        !isRecord(value.details)
      ) {
        throw new Error("BACKEND_PROBLEM_INVALID");
      }
      throw new BackendProblem({
        code: value.error_code,
        message: value.message,
        requestId,
        status: value.status,
        details: value.details,
      });
    }
    if (!response.ok) throw new Error("BACKEND_JSON_RESPONSE_INVALID");
    return value as T;
  }

  #requireResponseRequestId(response: Response, requestId: string): void {
    if (response.headers.get("X-Request-ID") !== requestId) {
      throw new Error("BACKEND_RESPONSE_CORRELATION_INVALID");
    }
  }
}

const mediaType = (response: Response): string =>
  response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() ?? "";

const canonicalCount = (value: number): string => {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error("BACKEND_BINARY_REQUEST_INVALID");
  }
  return String(value);
};

const parseCanonicalCount = (value: string): number => {
  if (!/^(0|[1-9]\d*)$/.test(value)) {
    throw new Error("BACKEND_BINARY_RESPONSE_INVALID");
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error("BACKEND_BINARY_RESPONSE_INVALID");
  }
  return parsed;
};

const requireCanonicalHeader = (response: Response, name: string): number => {
  const value = response.headers.get(name);
  if (value === null) throw new Error("BACKEND_BINARY_RESPONSE_INVALID");
  return parseCanonicalCount(value);
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const takeFrame = (
  buffer: string,
): Readonly<{
  raw: string;
  rest: string;
  delimiter: "\n\n" | "\r\n\r\n";
}> | null => {
  const lfIndex = buffer.indexOf("\n\n");
  const crlfIndex = buffer.indexOf("\r\n\r\n");
  if (lfIndex === -1 && crlfIndex === -1) return null;
  const useCrlf = crlfIndex !== -1 && (lfIndex === -1 || crlfIndex <= lfIndex);
  const index = useCrlf ? crlfIndex : lfIndex;
  const delimiter = useCrlf ? "\r\n\r\n" : "\n\n";
  return {
    raw: buffer.slice(0, index),
    rest: buffer.slice(index + delimiter.length),
    delimiter,
  };
};

const parseSseFrame = (
  raw: string,
  delimiter: "\n\n" | "\r\n\r\n",
  requestId: string,
): BackendSseFrame => {
  const normalized = delimiter === "\r\n\r\n" ? raw.split("\r\n").join("\n") : raw;
  if (normalized.includes("\r")) throw new BackendSseError("INVALID");
  const lines = normalized.split("\n");
  if (lines.length !== 3) throw new BackendSseError("INVALID");
  const event = /^event: ([^\s]+)$/.exec(lines[0]);
  const id = /^id: (0|[1-9]\d*)$/.exec(lines[1]);
  const data = /^data: (.+)$/.exec(lines[2]);
  if (event === null || id === null || data === null) {
    throw new BackendSseError("INVALID");
  }
  let value: unknown;
  try {
    value = JSON.parse(data[1]);
  } catch {
    throw new BackendSseError("INVALID");
  }
  return {
    requestId,
    event: event[1],
    id: id[1],
    data: value,
  };
};
