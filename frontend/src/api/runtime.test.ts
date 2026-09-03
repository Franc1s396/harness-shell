import { expect, it, vi } from "vitest";

const request = vi.hoisted(() => vi.fn());
vi.mock("./bootstrap", () => ({
  getBackendClient: () => ({ http: { request } }),
}));

import { getRuntimeStatus } from "./runtime";

it("reads Runtime state over HTTP", async () => {
  request.mockResolvedValue({ request_id: "r", state: "READY" });

  await expect(getRuntimeStatus()).resolves.toMatchObject({ state: "READY" });

  expect(request).toHaveBeenCalledWith("GET", "/v1/runtime/state");
});
