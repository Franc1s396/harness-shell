import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import { bytesToBase64 } from "../features/terminal/base64";

export type CredentialKind =
  | "ssh_password"
  | "private_key_passphrase"
  | "imported_private_key";

export type CredentialReference = {
  credential_id: string;
  kind: CredentialKind;
};

export type ConnectionProfileInput = {
  display_name: string;
  group_name: string | null;
  host: string;
  port: number;
  username: string;
  auth_kind: "password" | "private_key";
  credential_id: string;
  passphrase_credential_id: string | null;
  proxy_jump_id: string | null;
  favorite: boolean;
};

export type ConnectionProfile = ConnectionProfileInput & {
  connection_id: string;
  version: number;
  created_at: string;
  updated_at: string;
};

export type HostKeyCandidate = {
  connection_id: string;
  host: string;
  port: number;
  key_algorithm: string;
  fingerprint_sha256: string;
  public_key_openssh_b64: string;
};

export type HostKeyRecord = Omit<HostKeyCandidate, "host" | "port"> & {
  host_key_id: string;
  status: "active" | "replaced";
  confirmed_at: string;
  replaced_at: string | null;
};

export type ConnectionStatus = {
  connection_id: string;
  state:
    | "DISCONNECTED"
    | "CONNECTING"
    | "HOST_KEY_REQUIRED"
    | "READY"
    | "CLOSING"
    | "FAILED";
  session_id: string | null;
  error_code: string | null;
  recoverable: boolean;
  correlation_id: string;
  host_key_candidate: HostKeyCandidate | null;
  trusted_fingerprint_sha256: string | null;
};

export type PtySession = {
  pty_session_id: string;
  ssh_session_id: string;
  connection_id: string;
  cols: number;
  rows: number;
  state: "OPEN" | "CLOSED" | "FAILED";
};

export type SshEvent =
  | { event: "ssh.connection.status"; status: ConnectionStatus }
  | {
      event: "ssh.pty.output";
      pty_session_id: string;
      stream_sequence: number;
      data_b64: string;
    }
  | {
      event: "ssh.pty.closed";
      pty_session_id: string;
      exit_status: number | null;
      exit_signal: string | null;
    };

export class SshProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SshProtocolError";
  }
}

export type SshCommandError = {
  code: string;
  message: string;
  details?: {
    node?: string;
    recoverable?: boolean;
    correlation_id?: string;
    remote_state?:
      | "not_contacted"
      | "pre_auth"
      | "authenticated"
      | "channel_dispatched"
      | "unknown";
    host_key_candidate?: HostKeyCandidate;
    trusted_fingerprint_sha256?: string;
  };
};

export const storeSshPassword = (secret: string) =>
  invoke<CredentialReference>("store_ssh_password", { secret });

export const storePrivateKeyPassphrase = (secret: string) =>
  invoke<CredentialReference>("store_private_key_passphrase", { secret });

export const importPrivateKey = () =>
  invoke<CredentialReference | null>("import_private_key");

export const deleteSshCredential = (credentialId: string) =>
  invoke<void>("delete_ssh_credential", { credentialId });

export const listConnections = () =>
  invoke<ConnectionProfile[]>("list_connections");

export const createConnection = (input: ConnectionProfileInput) =>
  invoke<ConnectionProfile>("create_connection", { input });

export const updateConnection = (
  connectionId: string,
  input: ConnectionProfileInput,
) => invoke<ConnectionProfile>("update_connection", { connectionId, input });

export const deleteConnection = (connectionId: string) =>
  invoke<boolean>("delete_connection", { connectionId });

export const confirmHostKey = (candidate: HostKeyCandidate) =>
  invoke<HostKeyRecord>("confirm_host_key", { candidate });

export const replaceHostKey = (
  candidate: HostKeyCandidate,
  expectedOldFingerprint: string,
) =>
  invoke<HostKeyRecord>("replace_host_key", {
    candidate,
    expectedOldFingerprint,
  });

export const inspectHostKey = (connectionId: string) =>
  invoke<ConnectionStatus>("inspect_host_key", { connectionId });

export const connectSsh = (connectionId: string) =>
  invoke<ConnectionStatus>("connect_ssh", { connectionId });

export const disconnectSsh = (sshSessionId: string) =>
  invoke<ConnectionStatus>("disconnect_ssh", { sshSessionId });

export const openPty = (sshSessionId: string, cols: number, rows: number) =>
  invoke<PtySession>("open_pty", { sshSessionId, cols, rows });

export const writePty = (ptySessionId: string, data: Uint8Array) =>
  invoke<number>("write_pty", {
    ptySessionId,
    dataB64: bytesToBase64(data),
  });

export const resizePty = (ptySessionId: string, cols: number, rows: number) =>
  invoke<PtySession>("resize_pty", { ptySessionId, cols, rows });

export const closePty = (ptySessionId: string) =>
  invoke<PtySession>("close_pty", { ptySessionId });

export const subscribeSshEvents = (
  onEvent: (event: SshEvent) => void,
  onProtocolError: (error: SshProtocolError) => void,
): Promise<UnlistenFn> =>
  listen<unknown>("ssh://event", ({ payload }) => {
    try {
      onEvent(parseSshEvent(payload));
    } catch (error) {
      onProtocolError(
        error instanceof SshProtocolError
          ? error
          : new SshProtocolError("SSH event validation failed."),
      );
    }
  });

export const parseSshEvent = (payload: unknown): SshEvent => {
  if (!isRecord(payload) || typeof payload.event !== "string") {
    throw new SshProtocolError("SSH event payload is not an object.");
  }
  switch (payload.event) {
    case "ssh.connection.status":
      if (!isConnectionStatus(payload.status)) {
        throw new SshProtocolError("SSH connection status event is invalid.");
      }
      return { event: payload.event, status: payload.status };
    case "ssh.pty.output":
      if (
        typeof payload.pty_session_id !== "string" ||
        !Number.isSafeInteger(payload.stream_sequence) ||
        (payload.stream_sequence as number) <= 0 ||
        typeof payload.data_b64 !== "string"
      ) {
        throw new SshProtocolError("SSH PTY output event is invalid.");
      }
      return {
        event: payload.event,
        pty_session_id: payload.pty_session_id,
        stream_sequence: payload.stream_sequence as number,
        data_b64: payload.data_b64,
      };
    case "ssh.pty.closed":
      if (
        typeof payload.pty_session_id !== "string" ||
        !isNullableNumber(payload.exit_status) ||
        !isNullableString(payload.exit_signal)
      ) {
        throw new SshProtocolError("SSH PTY closed event is invalid.");
      }
      return {
        event: payload.event,
        pty_session_id: payload.pty_session_id,
        exit_status: payload.exit_status,
        exit_signal: payload.exit_signal,
      };
    default:
      throw new SshProtocolError(`Unknown SSH event: ${payload.event}`);
  }
};

const isConnectionStatus = (value: unknown): value is ConnectionStatus => {
  if (!isRecord(value)) return false;
  const states = new Set<ConnectionStatus["state"]>([
    "DISCONNECTED",
    "CONNECTING",
    "HOST_KEY_REQUIRED",
    "READY",
    "CLOSING",
    "FAILED",
  ]);
  return (
    typeof value.connection_id === "string" &&
    typeof value.state === "string" &&
    states.has(value.state as ConnectionStatus["state"]) &&
    isNullableString(value.session_id) &&
    isNullableString(value.error_code) &&
    typeof value.recoverable === "boolean" &&
    typeof value.correlation_id === "string" &&
    (value.host_key_candidate === null || isHostKeyCandidate(value.host_key_candidate))
    && isNullableString(value.trusted_fingerprint_sha256)
  );
};

const isHostKeyCandidate = (value: unknown): value is HostKeyCandidate =>
  isRecord(value) &&
  typeof value.connection_id === "string" &&
  typeof value.host === "string" &&
  Number.isSafeInteger(value.port) &&
  typeof value.key_algorithm === "string" &&
  typeof value.fingerprint_sha256 === "string" &&
  typeof value.public_key_openssh_b64 === "string";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isNullableString = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

const isNullableNumber = (value: unknown): value is number | null =>
  value === null || (typeof value === "number" && Number.isSafeInteger(value));
