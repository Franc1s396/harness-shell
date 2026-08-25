import { invoke } from "@tauri-apps/api/core";

export type RuntimeStatus = {
  state: "STARTING" | "HANDSHAKING" | "READY" | "PAUSED" | "FAILED" | "STOPPED";
  error_code: string | null;
  node: string;
  recoverable: boolean;
  correlation_id: string;
  last_sequence: number;
  last_heartbeat_at: string | null;
};

export const getRuntimeStatus = () => invoke<RuntimeStatus>("get_runtime_status");

export const openApprovalWindow = () => invoke<void>("open_approval_window");
