import { beforeEach, expect, it, vi } from "vitest";

const request = vi.hoisted(() => vi.fn());
const sendPtyInput = vi.hoisted(() => vi.fn());
const createCredentialEnvelope = vi.hoisted(() => vi.fn());
vi.mock("./bootstrap", () => ({
  getBackendClient: () => ({
    http: { request },
    runtimeWebSocket: { sendPtyInput, subscribe: vi.fn() },
  }),
}));
vi.mock("./credential-envelope", () => ({ createCredentialEnvelope }));

import { connectSsh, createConnection, listConnections, writePty } from "./ssh";

beforeEach(() => {
  request.mockReset();
  sendPtyInput.mockReset();
  createCredentialEnvelope.mockReset().mockImplementation(
    async (_secret: string, loadPublicKey: () => Promise<unknown>) => {
      await loadPublicKey();
      return { version: 1 };
    },
  );
});

it("maps connection and SSH identity calls to direct HTTP", async () => {
  request
    .mockResolvedValueOnce({ request_id: "request", connections: [] })
    .mockResolvedValueOnce({ request_id: "request", status: { state: "READY" } });

  await expect(listConnections()).resolves.toEqual([]);
  await expect(connectSsh("connection-1")).resolves.toEqual({ state: "READY" });

  expect(request.mock.calls).toEqual([
    ["GET", "/v1/connections"],
    ["POST", "/v1/ssh/sessions", { body: { connection_id: "connection-1" } }],
  ]);
});

it("encrypts a password into the connection mutation without credential HTTP calls", async () => {
  request
    .mockResolvedValueOnce({ key_id: "key-1" })
    .mockResolvedValueOnce({ request_id: "request", connection: { connection_id: "c1" } });

  await createConnection({
    display_name: "Prod",
    group_name: null,
    host: "prod.example",
    port: 22,
    username: "root",
    auth_kind: "password",
    credential_secret: "secret",
    passphrase_secret: null,
    proxy_jump_id: null,
    favorite: false,
  });

  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[1]).toEqual([
    "POST",
    "/v1/connections",
    { body: {
      display_name: "Prod",
      group_name: null,
      host: "prod.example",
      port: 22,
      username: "root",
      auth_kind: "password",
      credential_envelope: { version: 1 },
      proxy_jump_id: null,
      favorite: false,
    } },
  ]);
  expect(request.mock.calls.flat().join(" ")).not.toContain("/v1/credentials");
});

it("sends PTY bytes only through the Runtime WebSocket", () => {
  writePty("pty-1", new Uint8Array([97]));

  expect(sendPtyInput).toHaveBeenCalledWith({
    ptySessionId: "pty-1",
    dataB64: "YQ==",
  });
  expect(request).not.toHaveBeenCalled();
});
