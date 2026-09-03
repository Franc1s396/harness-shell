import { beforeEach, expect, it, vi } from "vitest";

import {
  getBackendClient,
  initializeBackendClient,
  resetBackendClientForTest,
  validateBackendUrl,
} from "./bootstrap";


beforeEach(() => resetBackendClientForTest());

it("uses the explicit development URL without invoking Tauri", async () => {
  const invokeBootstrap = vi.fn();

  const client = await initializeBackendClient({
    devUrl: "http://127.0.0.1:8765",
    invokeBootstrap,
  });

  expect(client.baseUrl).toBe("http://127.0.0.1:8765");
  expect(invokeBootstrap).not.toHaveBeenCalled();
  expect(getBackendClient()).toBe(client);
});

it("uses the production Tauri bootstrap without guessing a default port", async () => {
  const invokeBootstrap = vi.fn().mockResolvedValue({
    backend_base_url: "http://127.0.0.1:49152",
  });

  const client = await initializeBackendClient({
    devUrl: undefined,
    invokeBootstrap,
  });

  expect(client.baseUrl).toBe("http://127.0.0.1:49152");
  expect(invokeBootstrap).toHaveBeenCalledWith("get_backend_bootstrap");
});

it("rejects non-loopback and noncanonical bootstrap URLs", () => {
  expect(() => validateBackendUrl("http://192.168.1.4:8765")).toThrow(
    "BACKEND_BOOTSTRAP_INVALID",
  );
  expect(() => validateBackendUrl("http://localhost:8765")).toThrow(
    "BACKEND_BOOTSTRAP_INVALID",
  );
  expect(() => validateBackendUrl("https://127.0.0.1:8765")).toThrow(
    "BACKEND_BOOTSTRAP_INVALID",
  );
  expect(() => validateBackendUrl("http://127.0.0.1:0")).toThrow(
    "BACKEND_BOOTSTRAP_INVALID",
  );
});
