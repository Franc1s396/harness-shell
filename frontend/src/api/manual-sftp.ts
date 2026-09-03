import { getBackendClient } from "./bootstrap";
import { BrowserFileGateway } from "../features/sftp/browser-file-gateway";
import {
  BrowserTransferCoordinator,
  type ManualSftpTransport,
  type TransferPreparationSummary as BrowserTransferPreparation,
} from "../features/sftp/browser-transfer-coordinator";

type UnlistenFn = () => void;

export type EntryType = "file" | "directory" | "symlink" | "other";
export type TransferDirection = "upload" | "download";
export type OperationPhase =
  | "preparing"
  | "transferring"
  | "verifying"
  | "committing";
export type MutationKind =
  | "mkdir"
  | "rename"
  | "remove"
  | "recursive_delete"
  | "recovery";
export type MutationPhase =
  | "preparing"
  | "isolating"
  | "deleting"
  | "cleaning"
  | "committing";
export type OperationTerminalState =
  | "succeeded"
  | "failed"
  | "cancelled"
  | "cleanup_required"
  | "outcome_unknown";
export type RecoveryKind =
  | "upload_temp"
  | "delete_tombstone"
  | "mutation_unknown";
export type RecoveryState =
  | "cleanup_required"
  | "outcome_unknown"
  | "recovery_required";
export type RecoveryAction =
  | "verify"
  | "delete_temp"
  | "continue_delete"
  | "restore_tombstone"
  | "keep";

export type ManualSftpContext = {
  ssh_session_id: string;
  connection_id: string;
  home: string;
  host_label: string;
  sftp_version: number;
};

export type RemoteEntry = {
  name: string;
  path: string;
  entry_type: EntryType;
  size: number | null;
  mode: number;
  mtime_ns: string | null;
  link_target: string | null;
};

export type ListingBatch = {
  listing_id: string;
  path: string;
  entries: RemoteEntry[];
  next_sequence: number;
  done: boolean;
  observed_entry_count: number;
  complete: boolean;
};

export type TransferSnapshot = {
  path: string;
  exists: boolean;
  entry_type: EntryType | null;
  size: number | null;
  mtime_ns: string | null;
  sha256: string | null;
};

export type RemoteFileHash = {
  path: string;
  snapshot: TransferSnapshot;
  sha256: string;
  byte_count: number;
};

export type TransferPreparationSummary = BrowserTransferPreparation;

export type TransferProgressProjection = {
  operation_id: string;
  direction: TransferDirection;
  phase: OperationPhase;
  display_name: string;
  remote_path: string;
  host_label: string;
  bytes_completed: number;
  bytes_total: number;
  cancellable: boolean;
};

export type MutationProgressProjection = {
  operation_id: string;
  kind: MutationKind;
  phase: MutationPhase;
  display_name: string;
  remote_path: string;
  host_label: string;
  items_completed: number | null;
  items_total: number | null;
  cancellable: false;
};

export type DeletePlanSummary = {
  delete_plan_id: string;
  operation_id: string;
  root_path: string;
  root_snapshot: TransferSnapshot;
  file_count: number;
  directory_count: number;
  symlink_count: number;
  total_byte_count: number;
  manifest_sha256: string;
  complete: boolean;
};

export type OperationTerminalProjection = {
  operation_id: string;
  state: OperationTerminalState;
  error_code: string | null;
  message: string;
  sha256: string | null;
  byte_count: number | null;
  recovery_id: string | null;
};

export type RecoverySummary = {
  recovery_id: string;
  operation_id: string;
  kind: RecoveryKind;
  host_label: string;
  remote_path: string | null;
  display_name: string;
  state: RecoveryState;
  created_at: string;
  available_actions: RecoveryAction[];
};

export type ManualSftpCommandError = {
  code: string;
  message: string;
  details?: {
    correlation_id?: string;
    recoverable?: boolean;
  };
};

export class ManualSftpProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ManualSftpProtocolError";
  }
}

export const getManualSftpContext = (sshSessionId: string | null) =>
  getBackendClient().http.request<{ request_id: string; context: ManualSftpContext }>(
    "POST", "/v1/sftp/contexts", { body: { ssh_session_id: sshSessionId } },
  ).then((value) => value.context);

export const listManualSftpDirectory = (
  sshSessionId: string,
  remotePath: string,
) => getBackendClient().http.request<{ request_id: string; batch: ListingBatch }>(
  "POST", "/v1/sftp/listings", {
    body: { ssh_session_id: sshSessionId, path: remotePath },
  },
).then((value) => value.batch);

export const nextManualSftpDirectoryBatch = (
  listingId: string,
  sequence: number,
) => getBackendClient().http.request<{ request_id: string; batch: ListingBatch }>(
  "GET", `/v1/sftp/listings/${listingId}/batches/${sequence}`,
).then((value) => value.batch);

export const closeManualSftpListing = (listingId: string) =>
  getBackendClient().http.request<{ request_id: string; closed: boolean }>(
    "DELETE", `/v1/sftp/listings/${listingId}`,
  ).then((value) => value.closed);

export const inspectManualSftpEntry = (
  sshSessionId: string,
  remotePath: string,
) => remoteEntry("/v1/sftp/metadata/lstat", sshSessionId, remotePath);

export const hashManualSftpFile = (
  sshSessionId: string,
  remotePath: string,
) => getBackendClient().http.request<{ request_id: string; hash: RemoteFileHash }>(
  "POST", "/v1/sftp/hashes/sha256", {
    body: { ssh_session_id: sshSessionId, path: remotePath },
  },
).then((value) => value.hash);

export const openManualSftpLink = (
  sshSessionId: string,
  remotePath: string,
) => remoteEntry("/v1/sftp/metadata/readlink", sshSessionId, remotePath);

export const prepareManualSftpUpload = (
  sshSessionId: string,
  remoteDirectory: string,
  targetName: string,
) => transferCoordinator.prepareUpload(sshSessionId, remoteDirectory, targetName);

export const executeManualSftpUpload = (
  preparationId: string,
  confirmed: boolean,
) => transferCoordinator.executeUpload(preparationId, confirmed);

export const prepareManualSftpDownload = (
  sshSessionId: string,
  remotePath: string,
  displayName: string,
) => transferCoordinator.prepareDownloadFromUserGesture(
  sshSessionId, remotePath, displayName,
);

export const executeManualSftpDownload = (
  preparationId: string,
  confirmed: boolean,
) => transferCoordinator.executeDownload(preparationId, confirmed);

export const discardManualSftpPreparation = async (preparationId: string) => {
  transferCoordinator.discardPreparation(preparationId);
};

export const putManualSftpUploadChunk = (
  operationId: string,
  sequence: number,
  offset: number,
  chunk: Uint8Array,
  signal?: AbortSignal,
) => getBackendClient().http.putBinary<{
  request_id: string;
  operation_id: string;
  sequence: number;
  offset: number;
  accepted_bytes: number;
}>(`/v1/sftp/uploads/${operationId}/chunks/${sequence}`, chunk, offset, signal);

export const getManualSftpDownloadChunk = async (
  operationId: string,
  sequence: number,
  offset: number,
  signal?: AbortSignal,
) => {
  const chunk = await getBackendClient().http.getBinary(
    `/v1/sftp/downloads/${operationId}/chunks/${sequence}`,
    offset,
    signal,
  );
  return {
    operation_id: operationId,
    sequence: chunk.sequence,
    offset: chunk.offset,
    data: chunk.body,
    eof: chunk.eof,
  };
};

export const createManualSftpDirectory = (
  sshSessionId: string,
  parentPath: string,
  name: string,
) => mutation("/v1/sftp/directories", {
  operation_id: crypto.randomUUID(),
  ssh_session_id: sshSessionId,
  parent_path: parentPath,
  name,
});

export const renameManualSftpEntry = (
  sshSessionId: string,
  sourcePath: string,
  targetPath: string,
  overwrite: boolean,
) => mutation("/v1/sftp/renames", {
  operation_id: crypto.randomUUID(),
  ssh_session_id: sshSessionId,
  source_path: sourcePath,
  target_path: targetPath,
  overwrite,
  source_snapshot: null,
  target_snapshot: null,
});

export const removeManualSftpEntry = (
  sshSessionId: string,
  remotePath: string,
) => removeWithSnapshot(sshSessionId, remotePath);

export const preflightManualSftpDelete = (
  sshSessionId: string,
  remotePath: string,
) => getBackendClient().http.request<{ request_id: string; delete_plan: DeletePlanSummary }>(
  "POST", "/v1/sftp/deletions/preflight", { body: {
    operation_id: crypto.randomUUID(),
    ssh_session_id: sshSessionId,
    path: remotePath,
  } },
).then((value) => value.delete_plan);

export const executeManualSftpDelete = (
  deletePlanId: string,
  confirmed: boolean,
) => {
  if (!confirmed) return Promise.reject(new Error("SFTP_CONFIRMATION_REQUIRED"));
  return getBackendClient().http.request<{
    request_id: string;
    terminal: OperationTerminalProjection;
  }>("POST", `/v1/sftp/deletions/${deletePlanId}/execute`)
    .then((value) => value.terminal);
};

export const cancelManualSftpOperation = (_operationId: string) =>
  transferCoordinator.cancelActive();

export const listManualSftpRecoveries = () =>
  getBackendClient().http.request<{ request_id: string; recoveries: RecoverySummary[] }>(
    "GET", "/v1/sftp/recoveries",
  ).then((value) => value.recoveries);

export const inspectManualSftpRecovery = (recoveryId: string) =>
  getBackendClient().http.request<{ request_id: string; recovery: RecoverySummary }>(
    "GET", `/v1/sftp/recoveries/${recoveryId}`,
  ).then((value) => value.recovery);

export const executeManualSftpRecovery = (
  recoveryId: string,
  action: RecoveryAction,
  confirmed: boolean,
) => {
  if (!confirmed) return Promise.reject(new Error("SFTP_CONFIRMATION_REQUIRED"));
  return getBackendClient().http.request<{ request_id: string; recovery: RecoverySummary }>(
    "POST", `/v1/sftp/recoveries/${recoveryId}/actions`, { body: {
      operation_id: crypto.randomUUID(),
      action,
    } },
  ).then((value) => value.recovery);
};

const remoteEntry = (
  path: string,
  sshSessionId: string,
  remotePath: string,
): Promise<RemoteEntry> => getBackendClient().http.request<{
  request_id: string;
  entry: RemoteEntry;
}>("POST", path, {
  body: { ssh_session_id: sshSessionId, path: remotePath },
}).then((value) => value.entry);

const mutation = (
  path: string,
  body: Readonly<Record<string, unknown>>,
): Promise<OperationTerminalProjection> => getBackendClient().http.request<{
  request_id: string;
  terminal: OperationTerminalProjection;
}>("POST", path, { body }).then((value) => value.terminal);

const removeWithSnapshot = async (
  sshSessionId: string,
  remotePath: string,
): Promise<OperationTerminalProjection> => {
  const entry = await inspectManualSftpEntry(sshSessionId, remotePath);
  const expectedSnapshot = entry.entry_type === "file"
    ? (await hashManualSftpFile(sshSessionId, remotePath)).snapshot
    : {
        path: entry.path,
        exists: true,
        entry_type: entry.entry_type,
        size: entry.size,
        mtime_ns: entry.mtime_ns,
        sha256: null,
      };
  return mutation("/v1/sftp/removals", {
    operation_id: crypto.randomUUID(),
    ssh_session_id: sshSessionId,
    path: remotePath,
    expected_snapshot: expectedSnapshot,
  });
};

const manualSftpTransport: ManualSftpTransport = {
  preflightUpload: (sshSessionId, remotePath, signal) =>
    getBackendClient().http.request<{ request_id: string; snapshot: TransferSnapshot }>(
      "POST", "/v1/sftp/uploads/preflight", {
        body: { ssh_session_id: sshSessionId, path: remotePath },
        signal,
      },
    ).then((value) => value.snapshot),
  beginUpload: (body, signal) =>
    getBackendClient().http.request<{ request_id: string; upload: {
      operation_id: string;
      temp_path: string;
      next_sequence: number;
      next_offset: number;
    } }>("POST", "/v1/sftp/uploads", { body, signal }).then((value) => value.upload),
  putUploadChunk: putManualSftpUploadChunk,
  finishUpload: (operationId, signal) => transferTerminal(
    `/v1/sftp/uploads/${operationId}/finish`, signal,
  ),
  abortUpload: (operationId) => transferTerminal(
    `/v1/sftp/uploads/${operationId}/abort`,
  ),
  preflightDownload: (sshSessionId, remotePath, signal) =>
    getBackendClient().http.request<{ request_id: string; hash: RemoteFileHash }>(
      "POST", "/v1/sftp/hashes/sha256", {
        body: { ssh_session_id: sshSessionId, path: remotePath },
        signal,
      },
    ).then((value) => value.hash),
  beginDownload: (body, signal) =>
    getBackendClient().http.request<{ request_id: string; download: {
      operation_id: string;
      path: string;
      snapshot: TransferSnapshot;
      sha256: string;
      byte_count: number;
      next_sequence: number;
      next_offset: number;
    } }>("POST", "/v1/sftp/downloads", { body, signal })
      .then((value) => value.download),
  getDownloadChunk: getManualSftpDownloadChunk,
  finishDownload: (operationId, signal) => transferTerminal(
    `/v1/sftp/downloads/${operationId}/finish`, signal,
  ),
  abortDownload: (operationId) => transferTerminal(
    `/v1/sftp/downloads/${operationId}/abort`,
  ),
};

const transferTerminal = (
  path: string,
  signal?: AbortSignal,
): Promise<OperationTerminalProjection> => getBackendClient().http.request<{
  request_id: string;
  terminal: OperationTerminalProjection;
}>("POST", path, { signal }).then((value) => value.terminal);

const transferCoordinator = new BrowserTransferCoordinator({
  gateway: new BrowserFileGateway(),
  transport: manualSftpTransport,
});

export const normalizeManualSftpError = (
  error: unknown,
): ManualSftpCommandError => {
  if (!isRecord(error) || typeof error.code !== "string") {
    return {
      code: "SFTP_OPERATION_FAILED",
      message:
        error instanceof Error ? error.message : "Manual SFTP operation failed.",
    };
  }
  const details = isRecord(error.details)
    ? {
        ...(typeof error.details.correlation_id === "string"
          ? { correlation_id: error.details.correlation_id }
          : {}),
        ...(typeof error.details.recoverable === "boolean"
          ? { recoverable: error.details.recoverable }
          : {}),
      }
    : undefined;
  return {
    code: error.code,
    message:
      typeof error.message === "string"
        ? error.message
        : "Manual SFTP operation failed.",
    ...(details && Object.keys(details).length > 0 ? { details } : {}),
  };
};

export const parseTransferProgress = (
  value: unknown,
): TransferProgressProjection => {
  const keys = [
    "operation_id",
    "direction",
    "phase",
    "display_name",
    "remote_path",
    "host_label",
    "bytes_completed",
    "bytes_total",
    "cancellable",
  ] as const;
  if (
    !hasExactKeys(value, keys) ||
    typeof value.operation_id !== "string" ||
    !isOneOf(value.direction, ["upload", "download"] as const) ||
    !isOneOf(
      value.phase,
      ["preparing", "transferring", "verifying", "committing"] as const,
    ) ||
    typeof value.display_name !== "string" ||
    typeof value.remote_path !== "string" ||
    typeof value.host_label !== "string" ||
    !isSafeCount(value.bytes_completed) ||
    !isSafeCount(value.bytes_total) ||
    value.bytes_completed > value.bytes_total ||
    typeof value.cancellable !== "boolean"
  ) {
    throw new ManualSftpProtocolError(
      "Manual SFTP transfer progress payload is invalid.",
    );
  }
  return value as TransferProgressProjection;
};

export const parseMutationProgress = (
  value: unknown,
): MutationProgressProjection => {
  const keys = [
    "operation_id",
    "kind",
    "phase",
    "display_name",
    "remote_path",
    "host_label",
    "items_completed",
    "items_total",
    "cancellable",
  ] as const;
  if (
    !hasExactKeys(value, keys) ||
    typeof value.operation_id !== "string" ||
    !isOneOf(
      value.kind,
      ["mkdir", "rename", "remove", "recursive_delete", "recovery"] as const,
    ) ||
    !isOneOf(
      value.phase,
      ["preparing", "isolating", "deleting", "cleaning", "committing"] as const,
    ) ||
    typeof value.display_name !== "string" ||
    typeof value.remote_path !== "string" ||
    typeof value.host_label !== "string" ||
    !isNullableSafeCount(value.items_completed) ||
    !isNullableSafeCount(value.items_total) ||
    value.cancellable !== false
  ) {
    throw new ManualSftpProtocolError(
      "Manual SFTP operation progress payload is invalid.",
    );
  }
  return value as MutationProgressProjection;
};

export const subscribeManualSftpEvents = async (
  onTransfer: (progress: TransferProgressProjection) => void,
  onOperation: (progress: MutationProgressProjection) => void,
  onProtocolError: (error: ManualSftpProtocolError) => void,
): Promise<UnlistenFn> => Promise.resolve(
  getBackendClient().runtimeWebSocket.subscribe((message) => {
    if (message.type === "sftp.operation_progress") {
      parseEvent(message.payload, parseMutationProgress, onOperation, onProtocolError);
    } else if (message.type === "runtime.disconnected") {
      onProtocolError(new ManualSftpProtocolError(message.errorCode));
    }
    // Transfer byte progress is now owned by the browser coordinator. It does
    // not invent remote acknowledgements from a WebSocket observation channel.
    void onTransfer;
  }),
);

const parseEvent = <T>(
  payload: unknown,
  parse: (value: unknown) => T,
  publish: (value: T) => void,
  fail: (error: ManualSftpProtocolError) => void,
) => {
  try {
    publish(parse(payload));
  } catch (error) {
    fail(
      error instanceof ManualSftpProtocolError
        ? error
        : new ManualSftpProtocolError(
            "Manual SFTP event validation failed.",
          ),
    );
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const hasExactKeys = <K extends string>(
  value: unknown,
  keys: readonly K[],
): value is Record<K, unknown> =>
  isRecord(value) &&
  Object.keys(value).length === keys.length &&
  keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));

const isOneOf = <T extends string>(
  value: unknown,
  values: readonly T[],
): value is T => typeof value === "string" && values.includes(value as T);

const isSafeCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

const isNullableSafeCount = (value: unknown): value is number | null =>
  value === null || isSafeCount(value);
