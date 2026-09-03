import { invoke } from "@tauri-apps/api/core";

import { BackendHttpClient } from "./http-client";
import { RuntimeWebSocket } from "./runtime-websocket";

export type BackendClient = Readonly<{
  baseUrl: string;
  http: BackendHttpClient;
  runtimeWebSocket: RuntimeWebSocket;
}>;

type BackendBootstrap = Readonly<{ backend_base_url: string }>;

let backendClient: BackendClient | null = null;

export const validateBackendUrl = (value: string): string => {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("BACKEND_BOOTSTRAP_INVALID");
  }
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    url.username !== "" ||
    url.password !== "" ||
    url.pathname !== "/" ||
    url.search !== "" ||
    url.hash !== "" ||
    url.port === "" ||
    Number(url.port) < 1 ||
    Number(url.port) > 65_535
  ) {
    throw new Error("BACKEND_BOOTSTRAP_INVALID");
  }
  return `http://127.0.0.1:${url.port}`;
};

export const initializeBackendClient = async (
  dependencies: {
    devUrl?: string;
    invokeBootstrap?: (command: string) => Promise<BackendBootstrap>;
  } = {},
): Promise<BackendClient> => {
  if (backendClient !== null) return backendClient;
  const devUrl = Object.prototype.hasOwnProperty.call(dependencies, "devUrl")
    ? dependencies.devUrl
    : (import.meta.env.DEV ? import.meta.env.VITE_BACKEND_URL : undefined);
  let baseUrl: string;
  if (devUrl === undefined) {
    const invokeBootstrap = dependencies.invokeBootstrap
      ?? ((command: string) => invoke<BackendBootstrap>(command));
    const bootstrap = await invokeBootstrap("get_backend_bootstrap");
    baseUrl = validateBackendUrl(bootstrap.backend_base_url);
  } else {
    baseUrl = validateBackendUrl(devUrl);
  }
  backendClient = {
    baseUrl,
    http: new BackendHttpClient(baseUrl),
    runtimeWebSocket: new RuntimeWebSocket(baseUrl),
  };
  return backendClient;
};

export const getBackendClient = (): BackendClient => {
  if (backendClient === null) throw new Error("BACKEND_CLIENT_NOT_INITIALIZED");
  return backendClient;
};

export const resetBackendClientForTest = (): void => {
  backendClient?.runtimeWebSocket.close();
  backendClient = null;
};
