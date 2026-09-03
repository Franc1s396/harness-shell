import { beforeEach, expect, it, vi } from "vitest";

import { BackendHttpClient } from "./http-client";


const requestId = "10000000-0000-4000-8000-000000000001";
let fetchMock: ReturnType<typeof vi.fn>;
let client: BackendHttpClient;

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
