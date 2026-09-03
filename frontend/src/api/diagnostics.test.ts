import { beforeEach, expect, it, vi } from "vitest";

const request = vi.hoisted(() => vi.fn());
vi.mock("./bootstrap", () => ({
  getBackendClient: () => ({ http: { request } }),
}));

import { diagnosticsApi } from "./diagnostics";

beforeEach(() => request.mockReset());

it("returns availability without exposing a local path", async () => {
  request.mockResolvedValue({ request_id: "r", available: true });

  await expect(diagnosticsApi.getLogDirectory()).resolves.toEqual({ available: true });
  expect(request).toHaveBeenCalledWith("GET", "/v1/diagnostics/log-directory");
});

it("opens the fixed Python-owned directory without a path argument", async () => {
  request.mockResolvedValue(undefined);

  await diagnosticsApi.openLogDirectory();

  expect(request).toHaveBeenCalledWith("POST", "/v1/diagnostics/log-directory/open");
});
