import { beforeEach, expect, it, vi } from "vitest";

import {
  BackendHttpClient,
  BackendProblem,
  type BackendSseFrame,
} from "./http-client";


const requestId = "10000000-0000-4000-8000-000000000001";
let fetchMock: ReturnType<typeof vi.fn>;
let client: BackendHttpClient;

const streamResponse = (
  chunks: Uint8Array[],
  headers: HeadersInit = {},
): Response =>
  new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(chunk);
        controller.close();
      },
    }),
    {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "X-Request-ID": requestId,
        "Cache-Control": "no-store",
        ...headers,
      },
    },
  );

const collectSse = async (): Promise<BackendSseFrame[]> => {
  const frames: BackendSseFrame[] = [];
  for await (const frame of client.postSse("/v1/agent/turns", { ok: true })) {
    frames.push(frame);
  }
  return frames;
};

beforeEach(() => {
  fetchMock = vi.fn();
  client = new BackendHttpClient("http://127.0.0.1:8765", {
    fetchImpl: fetchMock,
    randomUuid: () => requestId,
  });
});

it("sends one correlated JSON request and validates the response identity", async () => {
  fetchMock.mockResolvedValue(new Response(
    JSON.stringify({ request_id: requestId, connections: [] }),
    {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "X-Request-ID": requestId,
      },
    },
  ));

  await expect(client.request("GET", "/v1/connections")).resolves.toEqual({
    request_id: requestId,
    connections: [],
  });
  expect(fetchMock).toHaveBeenCalledWith(
    "http://127.0.0.1:8765/v1/connections",
    expect.objectContaining({
      method: "GET",
      headers: expect.objectContaining({ "X-Request-ID": requestId }),
    }),
  );
});

it("maps correlated Problem Details without returning a success-shaped value", async () => {
  fetchMock.mockResolvedValue(new Response(
    JSON.stringify({
      type: "urn:harness-shell:error:invalid-request-payload",
      title: "Invalid request payload",
      status: 422,
      error_code: "INVALID_REQUEST_PAYLOAD",
      message: "Request payload is invalid",
      request_id: requestId,
      details: {},
    }),
    {
      status: 422,
      headers: {
        "Content-Type": "application/problem+json",
        "X-Request-ID": requestId,
      },
    },
  ));

  await expect(
    client.request("POST", "/v1/connections", { body: { invalid: true } }),
  ).rejects.toMatchObject({
    code: "INVALID_REQUEST_PAYLOAD",
    requestId,
    status: 422,
  });
});

it("uploads one binary chunk without setting forbidden Content-Length", async () => {
  fetchMock.mockResolvedValue(new Response(
    JSON.stringify({ request_id: requestId, sequence: 0, next_offset: 3 }),
    {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "X-Request-ID": requestId,
      },
    },
  ));

  await client.putBinary("/v1/sftp/uploads/op/chunks/0", new Uint8Array([1, 2, 3]), 0);

  const init = fetchMock.mock.calls[0][1] as RequestInit;
  expect(init.headers).toEqual({
    "Content-Type": "application/octet-stream",
    "X-Chunk-Offset": "0",
    "X-Request-ID": requestId,
  });
  expect(init.headers).not.toHaveProperty("Content-Length");
});

it("downloads one exact binary chunk with validated metadata", async () => {
  fetchMock.mockResolvedValue(new Response(new Uint8Array([1, 2, 3]), {
    status: 200,
    headers: {
      "Content-Type": "application/octet-stream",
      "X-Request-ID": requestId,
      "X-Chunk-Sequence": "2",
      "X-Chunk-Offset": "9",
      "X-Chunk-Byte-Count": "3",
      "X-Chunk-EOF": "false",
    },
  }));

  await expect(
    client.getBinary("/v1/sftp/downloads/op/chunks/2", 9),
  ).resolves.toEqual({
    requestId,
    sequence: 2,
    offset: 9,
    byteCount: 3,
    eof: false,
    body: new Uint8Array([1, 2, 3]),
  });
  expect(fetchMock.mock.calls[0][0]).toBe(
    "http://127.0.0.1:8765/v1/sftp/downloads/op/chunks/2?offset=9",
  );
});

it("maps a binary-route Problem Details response before reading chunk headers", async () => {
  fetchMock.mockResolvedValue(new Response(
    JSON.stringify({
      type: "urn:harness-shell:error:sftp-download-stale",
      title: "Download changed",
      status: 409,
      error_code: "SFTP_DOWNLOAD_STALE",
      message: "The remote file changed.",
      request_id: requestId,
      details: {},
    }),
    {
      status: 409,
      headers: {
        "Content-Type": "application/problem+json",
        "X-Request-ID": requestId,
      },
    },
  ));

  await expect(
    client.getBinary("/v1/sftp/downloads/op/chunks/2", 9),
  ).rejects.toMatchObject({
    code: "SFTP_DOWNLOAD_STALE",
    requestId,
    status: 409,
  });
});

it.each([
  ["missing count", { "X-Chunk-Byte-Count": null }],
  ["wrong sequence", { "X-Chunk-Sequence": "3" }],
  ["wrong offset", { "X-Chunk-Offset": "10" }],
  ["wrong count", { "X-Chunk-Byte-Count": "4" }],
  ["malformed eof", { "X-Chunk-EOF": "False" }],
])("rejects binary response metadata: %s", async (_name, override) => {
  const headers = new Headers({
    "Content-Type": "application/octet-stream",
    "X-Request-ID": requestId,
    "X-Chunk-Sequence": "2",
    "X-Chunk-Offset": "9",
    "X-Chunk-Byte-Count": "3",
    "X-Chunk-EOF": "false",
  });
  for (const [name, value] of Object.entries(override)) {
    if (value === null) headers.delete(name);
    else headers.set(name, value);
  }
  fetchMock.mockResolvedValue(new Response(new Uint8Array([1, 2, 3]), {
    status: 200,
    headers,
  }));

  await expect(
    client.getBinary("/v1/sftp/downloads/op/chunks/2", 9),
  ).rejects.toThrow("BACKEND_BINARY_RESPONSE_INVALID");
});

it("rejects an oversized binary body before returning it", async () => {
  fetchMock.mockResolvedValue(new Response(new Uint8Array(262_145), {
    status: 200,
    headers: {
      "Content-Type": "application/octet-stream",
      "X-Request-ID": requestId,
      "X-Chunk-Sequence": "2",
      "X-Chunk-Offset": "9",
      "X-Chunk-Byte-Count": "262145",
      "X-Chunk-EOF": "false",
    },
  }));

  await expect(
    client.getBinary("/v1/sftp/downloads/op/chunks/2", 9),
  ).rejects.toThrow("BACKEND_BINARY_RESPONSE_TOO_LARGE");
});

it("parses strict SSE across UTF-8 and network chunk boundaries", async () => {
  const wire = [
    "event: agent.turn.text_delta\n",
    "id: 1\n",
    `data: {"request_id":"${requestId}","delta":"你好"}\n\n`,
  ].join("");
  const bytes = new TextEncoder().encode(wire);
  const split = bytes.indexOf(0xe5) + 1;
  fetchMock.mockResolvedValue(
    streamResponse([bytes.slice(0, split), bytes.slice(split)]),
  );

  await expect(collectSse()).resolves.toEqual([
    {
      requestId,
      event: "agent.turn.text_delta",
      id: "1",
      data: { request_id: requestId, delta: "你好" },
    },
  ]);
  expect(fetchMock.mock.calls[0][1]).toEqual(
    expect.objectContaining({
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-Request-ID": requestId,
      },
    }),
  );
});

it("parses multiple CRLF-delimited frames from one chunk", async () => {
  const wire = [
    "event: agent.turn.started\r\nid: 0\r\ndata: {}\r\n\r\n",
    "event: agent.turn.completed\r\nid: 1\r\ndata: {}\r\n\r\n",
  ].join("");
  fetchMock.mockResolvedValue(streamResponse([new TextEncoder().encode(wire)]));

  await expect(collectSse()).resolves.toMatchObject([
    { event: "agent.turn.started", id: "0", data: {} },
    { event: "agent.turn.completed", id: "1", data: {} },
  ]);
});

it.each([
  ["comment", ": no\nevent: x\nid: 0\ndata: {}\n\n"],
  ["retry", "event: x\nid: 0\nretry: 1\ndata: {}\n\n"],
  ["wrong order", "id: 0\nevent: x\ndata: {}\n\n"],
  ["duplicate data", "event: x\nid: 0\ndata: {}\ndata: {}\n\n"],
  ["bare carriage return", "event: x\rid: 0\rdata: {}\r\r"],
  ["bad json", "event: x\nid: 0\ndata: {\n\n"],
  ["unterminated", "event: x\nid: 0\ndata: {}"],
])("rejects invalid SSE framing: %s", async (_name, wire) => {
  fetchMock.mockResolvedValue(streamResponse([new TextEncoder().encode(wire)]));

  await expect(collectSse()).rejects.toMatchObject({
    kind: "INVALID",
  });
});

it("rejects empty SSE bodies", async () => {
  fetchMock.mockResolvedValue(streamResponse([]));

  await expect(collectSse()).rejects.toMatchObject({
    kind: "INVALID",
  });
});

it("enforces encoded frame and body byte limits", async () => {
  const oversizedFrame = `event: x\nid: 0\ndata: ${"x".repeat(65_536)}\n\n`;
  fetchMock
    .mockResolvedValueOnce(
      streamResponse([new TextEncoder().encode(oversizedFrame)]),
    )
    .mockResolvedValueOnce(streamResponse([new Uint8Array(4_194_305)]));

  await expect(collectSse()).rejects.toMatchObject({
    kind: "TOO_LARGE",
  });
  await expect(collectSse()).rejects.toMatchObject({
    kind: "TOO_LARGE",
  });
});

it("maps Problem Details before entering SSE framing", async () => {
  fetchMock.mockResolvedValue(
    new Response(
      JSON.stringify({
        type: "urn:harness-shell:error:model-api-config-not-found",
        title: "Missing config",
        status: 404,
        error_code: "MODEL_API_CONFIG_NOT_FOUND",
        message: "model API configuration was not found",
        request_id: requestId,
        details: {},
      }),
      {
        status: 404,
        headers: {
          "Content-Type": "application/problem+json",
          "X-Request-ID": requestId,
        },
      },
    ),
  );

  await expect(collectSse()).rejects.toBeInstanceOf(BackendProblem);
});

it.each([
  ["network rejection", new TypeError("network unavailable")],
  ["abort", new DOMException("aborted", "AbortError")],
])("maps initial fetch failure to an interrupted SSE stream: %s", async (_name, error) => {
  fetchMock.mockRejectedValue(error);

  await expect(collectSse()).rejects.toMatchObject({
    kind: "INTERRUPTED",
  });
});

it("rejects an SSE-shaped 201 response", async () => {
  const wire = `event: x\nid: 0\ndata: {}\n\n`;
  fetchMock.mockResolvedValue(new Response(wire, {
    status: 201,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Request-ID": requestId,
      "Cache-Control": "no-store",
    },
  }));

  await expect(collectSse()).rejects.toMatchObject({
    kind: "INVALID",
  });
});
