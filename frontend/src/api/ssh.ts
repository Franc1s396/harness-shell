import { bytesToBase64 } from "../features/terminal/base64";
import { getBackendClient } from "./bootstrap";
import { createCredentialEnvelope, type CredentialPublicKey } from "./credential-envelope";

type UnlistenFn = () => void;

export type ConnectionProfileFields = {
  display_name: string;
  group_name: string | null;
  host: string;
  port: number;
  username: string;
  auth_kind: "password" | "private_key";
  proxy_jump_id: string | null;
  favorite: boolean;
};

export type ConnectionProfileInput = ConnectionProfileFields & {
  credential_secret: string | null;
  passphrase_secret: string | null;
};

export type ConnectionProfile = ConnectionProfileFields & {
  credential_id: string;
  passphrase_credential_id: string | null;
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

export const listConnections = () =>
  getBackendClient().http.request<{ request_id: string; connections: ConnectionProfile[] }>(
    "GET", "/v1/connections",
  ).then((value) => value.connections);

export const createConnection = async (input: ConnectionProfileInput) => {
  if (input.credential_secret === null) {
    throw new Error("Connection credential is required.");
  }
  const body = await connectionMutationBody(input);
  const value = await getBackendClient().http.request<{
    request_id: string;
    connection: ConnectionProfile;
  }>("POST", "/v1/connections", { body });
  return value.connection;
};

export const updateConnection = async (
  connectionId: string,
  input: ConnectionProfileInput,
) => {
  const body = await connectionMutationBody(input);
  const value = await getBackendClient().http.request<{
    request_id: string;
    connection: ConnectionProfile;
  }>("PATCH", `/v1/connections/${connectionId}`, { body });
  return value.connection;
};

export const deleteConnection = (connectionId: string) =>
  getBackendClient().http.request<{ request_id: string; deleted: boolean }>(
    "DELETE", `/v1/connections/${connectionId}`,
  ).then((value) => value.deleted);

export const confirmHostKey = (candidate: HostKeyCandidate) =>
  getBackendClient().http.request<{ request_id: string; host_key: HostKeyRecord }>(
    "POST", "/v1/host-key-confirmations", { body: candidate },
  ).then((value) => value.host_key);

export const replaceHostKey = (
  candidate: HostKeyCandidate,
  expectedOldFingerprint: string,
) => getBackendClient().http.request<{ request_id: string; host_key: HostKeyRecord }>(
  "POST", "/v1/host-key-replacements", {
    body: { ...candidate, expected_old_fingerprint: expectedOldFingerprint },
  },
).then((value) => value.host_key);

export const inspectHostKey = (connectionId: string) =>
  getBackendClient().http.request<{ request_id: string; status: ConnectionStatus }>(
    "POST", "/v1/host-key-inspections", { body: { connection_id: connectionId } },
  ).then((value) => value.status);

export const connectSsh = (connectionId: string) =>
  getBackendClient().http.request<{ request_id: string; status: ConnectionStatus }>(
    "POST", "/v1/ssh/sessions", { body: { connection_id: connectionId } },
  ).then((value) => value.status);

export const disconnectSsh = (sshSessionId: string) =>
  getBackendClient().http.request<{ request_id: string; status: ConnectionStatus }>(
    "DELETE", `/v1/ssh/sessions/${sshSessionId}`,
  ).then((value) => value.status);

export const openPty = (sshSessionId: string, cols: number, rows: number) =>
  getBackendClient().http.request<{ request_id: string; pty_session: PtySession }>(
    "POST", "/v1/pty/sessions", { body: { ssh_session_id: sshSessionId, cols, rows } },
  ).then((value) => value.pty_session);

export const writePty = (ptySessionId: string, data: Uint8Array): void =>
  getBackendClient().runtimeWebSocket.sendPtyInput({
    ptySessionId,
    dataB64: bytesToBase64(data),
  });

export const resizePty = (ptySessionId: string, cols: number, rows: number) =>
  getBackendClient().http.request<{ request_id: string; pty_session: PtySession }>(
    "POST", `/v1/pty/sessions/${ptySessionId}/resize`, { body: { cols, rows } },
  ).then((value) => value.pty_session);

export const closePty = (ptySessionId: string) =>
  getBackendClient().http.request<{ request_id: string; pty_session: PtySession }>(
    "DELETE", `/v1/pty/sessions/${ptySessionId}`,
  ).then((value) => value.pty_session);

export const subscribeSshEvents = (
  onEvent: (event: SshEvent) => void,
  onProtocolError: (error: SshProtocolError) => void,
): Promise<UnlistenFn> => Promise.resolve(
  getBackendClient().runtimeWebSocket.subscribe((message) => {
    try {
      if (message.type === "ssh.connection_state") {
        onEvent(parseSshEvent({ event: "ssh.connection.status", status: message.payload }));
      } else if (message.type === "pty.output") {
        onEvent(parseSshEvent({ event: "ssh.pty.output", ...message.payload }));
      } else if (message.type === "pty.closed") {
        onEvent(parseSshEvent({ event: "ssh.pty.closed", ...message.payload }));
      } else if (message.type === "runtime.disconnected") {
        throw new SshProtocolError(message.errorCode);
      }
    } catch (error) {
      onProtocolError(
        error instanceof SshProtocolError
          ? error
          : new SshProtocolError("SSH event validation failed."),
      );
    }
  }),
);

const connectionMutationBody = async (input: ConnectionProfileInput) => {
  const http = getBackendClient().http;
  let publicKey: Promise<CredentialPublicKey> | null = null;
  const loadPublicKey = () => {
    publicKey ??= http.request<CredentialPublicKey & { request_id: string }>(
      "GET", "/v1/runtime/credential-encryption-key",
    );
    return publicKey;
  };
  const {
    credential_secret: credentialSecret,
    passphrase_secret: passphraseSecret,
    ...fields
  } = input;
  const credentialEnvelope = credentialSecret === null
    ? undefined
    : await createCredentialEnvelope(credentialSecret, loadPublicKey);
  const passphraseEnvelope = passphraseSecret === null
    ? undefined
    : await createCredentialEnvelope(passphraseSecret, loadPublicKey);
  return {
    ...fields,
    ...(credentialEnvelope === undefined
      ? {}
      : { credential_envelope: credentialEnvelope }),
    ...(passphraseEnvelope === undefined
      ? {}
      : { passphrase_envelope: passphraseEnvelope }),
  };
};

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
