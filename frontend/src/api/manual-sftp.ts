import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

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
  | "download_part"
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
  | "open_local_folder"
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

export type TransferPreparationSummary = {
  preparation_id: string;
  operation_id: string;
  direction: TransferDirection;
  display_name: string;
  remote_path: string;
  host_label: string;
  source_sha256: string;
  source_byte_count: number;
  target_snapshot: TransferSnapshot;
  overwrite_required: boolean;
  expires_at: string;
};

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

export type RecoveryResponse = RecoverySummary | OperationTerminalProjection;

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
  invoke<ManualSftpContext>("get_manual_sftp_context", { sshSessionId });

export const listManualSftpDirectory = (
  sshSessionId: string,
  remotePath: string,
) =>
  invoke<ListingBatch>("list_manual_sftp_directory", {
    sshSessionId,
    remotePath,
  });

export const nextManualSftpDirectoryBatch = (
  listingId: string,
  sequence: number,
) =>
  invoke<ListingBatch>("next_manual_sftp_directory_batch", {
    listingId,
    sequence,
  });

export const closeManualSftpListing = (listingId: string) =>
  invoke<boolean>("close_manual_sftp_listing", { listingId });

export const inspectManualSftpEntry = (
  sshSessionId: string,
  remotePath: string,
) =>
  invoke<RemoteEntry>("inspect_manual_sftp_entry", {
    sshSessionId,
    remotePath,
  });

export const hashManualSftpFile = (
  sshSessionId: string,
  remotePath: string,
) =>
  invoke<RemoteFileHash>("hash_manual_sftp_file", {
    sshSessionId,
    remotePath,
  });

export const openManualSftpLink = (
  sshSessionId: string,
  remotePath: string,
) =>
  invoke<RemoteEntry>("open_manual_sftp_link", {
    sshSessionId,
    remotePath,
  });

export const prepareManualSftpUpload = (
  sshSessionId: string,
  remoteDirectory: string,
  targetName: string,
) =>
  invoke<TransferPreparationSummary | null>("prepare_manual_sftp_upload", {
    sshSessionId,
    remoteDirectory,
    targetName,
  });

export const executeManualSftpUpload = (
  preparationId: string,
  confirmed: boolean,
) =>
  invoke<OperationTerminalProjection>("execute_manual_sftp_upload", {
    preparationId,
    confirmed,
  });

export const prepareManualSftpDownload = (
  sshSessionId: string,
  remotePath: string,
  displayName: string,
) =>
  invoke<TransferPreparationSummary | null>("prepare_manual_sftp_download", {
    sshSessionId,
    remotePath,
    displayName,
  });

export const executeManualSftpDownload = (
  preparationId: string,
  confirmed: boolean,
) =>
  invoke<OperationTerminalProjection>("execute_manual_sftp_download", {
    preparationId,
    confirmed,
  });

export const discardManualSftpPreparation = (preparationId: string) =>
  invoke<void>("discard_manual_sftp_preparation", { preparationId });

export const createManualSftpDirectory = (
  sshSessionId: string,
  parentPath: string,
  name: string,
) =>
  invoke<OperationTerminalProjection>("create_manual_sftp_directory", {
    sshSessionId,
    parentPath,
    name,
  });

export const renameManualSftpEntry = (
  sshSessionId: string,
  sourcePath: string,
  targetPath: string,
  overwrite: boolean,
) =>
  invoke<OperationTerminalProjection>("rename_manual_sftp_entry", {
    sshSessionId,
    sourcePath,
    targetPath,
    overwrite,
  });

export const removeManualSftpEntry = (
  sshSessionId: string,
  remotePath: string,
) =>
  invoke<OperationTerminalProjection>("remove_manual_sftp_entry", {
    sshSessionId,
    remotePath,
  });

export const preflightManualSftpDelete = (
  sshSessionId: string,
  remotePath: string,
) =>
  invoke<DeletePlanSummary>("preflight_manual_sftp_delete", {
    sshSessionId,
    remotePath,
  });

export const executeManualSftpDelete = (
  deletePlanId: string,
  confirmed: boolean,
) =>
  invoke<OperationTerminalProjection>("execute_manual_sftp_delete", {
    deletePlanId,
    confirmed,
  });

export const cancelManualSftpOperation = (operationId: string) =>
  invoke<void>("cancel_manual_sftp_operation", { operationId });

export const listManualSftpRecoveries = () =>
  invoke<RecoverySummary[]>("list_manual_sftp_recoveries");

export const inspectManualSftpRecovery = (recoveryId: string) =>
  invoke<RecoveryResponse>("inspect_manual_sftp_recovery", { recoveryId });

export const executeManualSftpRecovery = (
  recoveryId: string,
  action: RecoveryAction,
  confirmed: boolean,
) =>
  invoke<RecoveryResponse>("execute_manual_sftp_recovery", {
    recoveryId,
    action,
    confirmed,
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
): Promise<UnlistenFn> => {
  const transferUnlisten = await listen<unknown>(
    "manual-sftp://transfer-state",
    ({ payload }) => parseEvent(payload, parseTransferProgress, onTransfer, onProtocolError),
  );
  try {
    const operationUnlisten = await listen<unknown>(
      "manual-sftp://operation-state",
      ({ payload }) =>
        parseEvent(payload, parseMutationProgress, onOperation, onProtocolError),
    );
    return () => {
      transferUnlisten();
      operationUnlisten();
    };
  } catch (error) {
    transferUnlisten();
    throw error;
  }
};

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
