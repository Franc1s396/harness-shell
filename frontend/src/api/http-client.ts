const JSON_RESPONSE_LIMIT = 1_048_576;
const BINARY_CHUNK_LIMIT = 262_144;

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
