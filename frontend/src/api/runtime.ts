import { getBackendClient } from "./bootstrap";

export type RuntimeStatus = {
  state: "STARTING" | "HANDSHAKING" | "READY" | "PAUSED" | "FAILED" | "STOPPED";
  error_code: string | null;
  node: string;
  recoverable: boolean;
  correlation_id: string;
  last_heartbeat_at: string | null;
};

export const getRuntimeStatus = async (): Promise<RuntimeStatus> => {
  const value = await getBackendClient().http.request<{
    request_id: string;
    state: "READY" | "FAILED" | "STOPPED";
  }>("GET", "/v1/runtime/state");
  return {
    state: value.state,
    error_code: value.state === "FAILED" ? "SIDECAR_RUNTIME_FAILED" : null,
    node: "python_backend",
    recoverable: false,
    correlation_id: value.request_id,
    last_heartbeat_at: null,
  };
};
