import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import {
  BrowserFileGateway,
  readFileChunks,
  type BrowserDownloadTarget,
  type BrowserUploadSource,
} from "./browser-file-gateway";
import { sha256File } from "./browser-sha256";


const PREPARATION_TTL_MS = 5 * 60 * 1_000;

export type TransferSnapshot = Readonly<{
  path: string;
  exists: boolean;
  entry_type: "file" | "directory" | "symlink" | "other" | null;
  size: number | null;
  mtime_ns: string | null;
  sha256: string | null;
}>;

export type RemoteFileHash = Readonly<{
  path: string;
  snapshot: TransferSnapshot;
  sha256: string;
  byte_count: number;
}>;

export type OperationTerminal = Readonly<{
  operation_id: string;
  state:
    | "succeeded"
    | "failed"
    | "cancelled"
    | "cleanup_required"
    | "outcome_unknown";
  error_code: string | null;
  message: string;
  sha256: string | null;
  byte_count: number | null;
  recovery_id: string | null;
}>;

export type UploadReady = Readonly<{
  operation_id: string;
  temp_path: string;
  next_sequence: number;
  next_offset: number;
}>;

export type UploadChunkAck = Readonly<{
  operation_id: string;
  sequence: number;
  offset: number;
  accepted_bytes: number;
}>;

export type DownloadReady = Readonly<{
  operation_id: string;
  path: string;
  snapshot: TransferSnapshot;
  sha256: string;
  byte_count: number;
  next_sequence: number;
  next_offset: number;
}>;

export type DownloadChunk = Readonly<{
  operation_id: string;
  sequence: number;
  offset: number;
  data: Uint8Array;
  eof: boolean;
}>;

export interface ManualSftpTransport {
  preflightUpload(
    sshSessionId: string,
    remotePath: string,
    signal?: AbortSignal,
  ): Promise<TransferSnapshot>;
  beginUpload(
    request: Readonly<{
      operation_id: string;
      ssh_session_id: string;
      path: string;
      source_sha256: string;
      source_byte_count: number;
      target_snapshot: TransferSnapshot;
    }>,
    signal?: AbortSignal,
  ): Promise<UploadReady>;
  putUploadChunk(
    operationId: string,
    sequence: number,
    offset: number,
    chunk: Uint8Array,
    signal?: AbortSignal,
  ): Promise<UploadChunkAck>;
  finishUpload(operationId: string, signal?: AbortSignal): Promise<OperationTerminal>;
  abortUpload(operationId: string): Promise<OperationTerminal>;
  preflightDownload(
    sshSessionId: string,
    remotePath: string,
    signal?: AbortSignal,
  ): Promise<RemoteFileHash>;
  beginDownload(
    request: Readonly<{
      operation_id: string;
      ssh_session_id: string;
      path: string;
    }>,
    signal?: AbortSignal,
  ): Promise<DownloadReady>;
  getDownloadChunk(
    operationId: string,
    sequence: number,
    offset: number,
    signal?: AbortSignal,
  ): Promise<DownloadChunk>;
  finishDownload(
    operationId: string,
    signal?: AbortSignal,
  ): Promise<OperationTerminal>;
  abortDownload(operationId: string): Promise<OperationTerminal>;
}

export type TransferPreparationSummary = Readonly<{
  preparation_id: string;
  direction: "upload" | "download";
  display_name: string;
  remote_path: string;
  byte_count: number;
  sha256: string;
  overwrite_required: boolean;
  expires_at_ms: number;
}>;

type UploadPreparation = Readonly<{
  summary: TransferPreparationSummary;
  sshSessionId: string;
  source: BrowserUploadSource;
  targetSnapshot: TransferSnapshot;
}>;

type DownloadPreparation = Readonly<{
  summary: TransferPreparationSummary;
  sshSessionId: string;
  target: BrowserDownloadTarget;
  remote: RemoteFileHash;
}>;

type Preparation = UploadPreparation | DownloadPreparation;

type ActiveTransfer = {
  direction: "upload" | "download";
  operationId: string | null;
  abortController: AbortController;
  remoteAbortAttempted: boolean;
  remoteSettled: boolean;
  suppressRemoteAbort: boolean;
  writable: FileSystemWritableFileStream | null;
};

export class BrowserTransferError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "BrowserTransferError";
    this.code = code;
  }
}

type CoordinatorOptions = Readonly<{
  gateway: BrowserFileGateway;
  transport: ManualSftpTransport;
  hashFile?: typeof sha256File;
  readChunks?: typeof readFileChunks;
  randomUUID?: () => string;
  now?: () => number;
}>;

export class BrowserTransferCoordinator {
  private readonly gateway: BrowserFileGateway;
  private readonly transport: ManualSftpTransport;
  private readonly hashFile: typeof sha256File;
  private readonly readChunks: typeof readFileChunks;
  private readonly randomUUID: () => string;
  private readonly now: () => number;
  private readonly preparations = new Map<string, Preparation>();
  private active: ActiveTransfer | null = null;

  constructor(options: CoordinatorOptions) {
    this.gateway = options.gateway;
    this.transport = options.transport;
    this.hashFile = options.hashFile ?? sha256File;
    this.readChunks = options.readChunks ?? readFileChunks;
    this.randomUUID = options.randomUUID ?? (() => crypto.randomUUID());
    this.now = options.now ?? (() => Date.now());
  }

  async prepareUpload(
    sshSessionId: string,
    remoteDirectory: string,
    targetName: string,
  ): Promise<TransferPreparationSummary | null> {
    const source = await this.gateway.selectUploadSource();
    if (source === null) return null;
    const remotePath = joinRemotePath(remoteDirectory, targetName);
    let sourceHash: string;
    try {
      sourceHash = await this.hashFile(source.file);
    } catch (error) {
      throw localReadError(error);
    }
    const targetSnapshot = await this.transport.preflightUpload(
      sshSessionId,
      remotePath,
    );
    const summary = this.newSummary({
      direction: "upload",
      displayName: source.displayName,
      remotePath,
      byteCount: source.byteCount,
      sha256: sourceHash,
      overwriteRequired: targetSnapshot.exists,
    });
    this.preparations.set(summary.preparation_id, {
      summary,
      sshSessionId,
      source,
      targetSnapshot,
    });
    return summary;
  }

  async prepareDownloadFromUserGesture(
    sshSessionId: string,
    remotePath: string,
    suggestedName: string,
  ): Promise<TransferPreparationSummary | null> {
    // Calling the async gateway executes its picker invocation synchronously up
    // to the gateway's first await, preserving transient user activation.
    const targetPromise = this.gateway.selectDownloadTarget(suggestedName);
    const target = await targetPromise;
    if (target === null) return null;
    const remote = await this.transport.preflightDownload(
      sshSessionId,
      remotePath,
    );
    const summary = this.newSummary({
      direction: "download",
      displayName: target.displayName,
      remotePath,
      byteCount: remote.byte_count,
      sha256: remote.sha256,
      overwriteRequired: true,
    });
    this.preparations.set(summary.preparation_id, {
      summary,
      sshSessionId,
      target,
      remote,
    });
    return summary;
  }

  prepareDownload(
    sshSessionId: string,
    remotePath: string,
    suggestedName: string,
  ): Promise<TransferPreparationSummary | null> {
    return this.prepareDownloadFromUserGesture(
      sshSessionId,
      remotePath,
      suggestedName,
    );
  }

  async executeUpload(
    preparationId: string,
    confirmed: boolean,
  ): Promise<OperationTerminal> {
    this.requireIdle();
    const preparation = this.consumePreparation(preparationId, "upload");
    if (preparation.targetSnapshot.exists && !confirmed) {
      throw new BrowserTransferError(
        "SFTP_CONFIRMATION_REQUIRED",
        "Upload confirmation is required",
      );
    }
    const active = this.startActive("upload");
    const operationId = this.randomUUID();
    active.operationId = operationId;
    let began = false;
    try {
      const ready = await this.transport.beginUpload(
        {
          operation_id: operationId,
          ssh_session_id: preparation.sshSessionId,
          path: preparation.summary.remote_path,
          source_sha256: preparation.summary.sha256,
          source_byte_count: preparation.summary.byte_count,
          target_snapshot: preparation.targetSnapshot,
        },
        active.abortController.signal,
      );
      requireUploadReady(ready, operationId);
      began = true;
      let sequence = ready.next_sequence;
      let offset = ready.next_offset;
      const digest = sha256.create();
      for await (const chunk of this.readChunks(preparation.source.file)) {
        requireActive(active);
        const ack = await this.transport.putUploadChunk(
          operationId,
          sequence,
          offset,
          chunk,
          active.abortController.signal,
        );
        requireUploadAck(ack, operationId, sequence, offset, chunk.byteLength);
        digest.update(chunk);
        offset = safeAdd(offset, chunk.byteLength);
        sequence += 1;
      }
      const secondHash = bytesToHex(digest.digest());
      if (
        offset !== preparation.summary.byte_count ||
        secondHash !== preparation.summary.sha256
      ) {
        throw new BrowserTransferError(
          "SFTP_LOCAL_SOURCE_CHANGED",
          "The selected local source changed before upload completed",
        );
      }
      const terminal = await this.transport.finishUpload(
        operationId,
        active.abortController.signal,
      );
      requireTerminalIdentity(terminal, operationId);
      active.remoteSettled = true;
      return terminal;
    } catch (error) {
      if (began && !active.remoteSettled && !active.suppressRemoteAbort) {
        await this.abortRemote(active);
      }
      throw normalizeTransferError(error, "SFTP_LOCAL_READ_FAILED");
    } finally {
      if (this.active === active) this.active = null;
    }
  }

  async executeDownload(
    preparationId: string,
    confirmed: boolean,
  ): Promise<OperationTerminal> {
    this.requireIdle();
    const preparation = this.consumePreparation(preparationId, "download");
    if (!confirmed) {
      throw new BrowserTransferError(
        "SFTP_CONFIRMATION_REQUIRED",
        "Download confirmation is required",
      );
    }
    const active = this.startActive("download");
    const operationId = this.randomUUID();
    active.operationId = operationId;
    let began = false;
    try {
      const ready = await this.transport.beginDownload(
        {
          operation_id: operationId,
          ssh_session_id: preparation.sshSessionId,
          path: preparation.summary.remote_path,
        },
        active.abortController.signal,
      );
      requireDownloadReady(ready, operationId, preparation.remote);
      began = true;
      active.writable = await preparation.target.handle.createWritable();
      let sequence = ready.next_sequence;
      let offset = ready.next_offset;
      const digest = sha256.create();
      while (true) {
        requireActive(active);
        const chunk = await this.transport.getDownloadChunk(
          operationId,
          sequence,
          offset,
          active.abortController.signal,
        );
        requireDownloadChunk(chunk, operationId, sequence, offset);
        if (chunk.data.byteLength > 0) {
          await active.writable.write(chunk.data);
          digest.update(chunk.data);
          offset = safeAdd(offset, chunk.data.byteLength);
        }
        sequence += 1;
        if (chunk.eof) break;
      }
      const observedHash = bytesToHex(digest.digest());
      if (
        offset !== preparation.remote.byte_count ||
        observedHash !== preparation.remote.sha256
      ) {
        throw new BrowserTransferError(
          "SFTP_REMOTE_SOURCE_CHANGED",
          "The remote source did not match its frozen digest",
        );
      }
      const terminal = await this.transport.finishDownload(
        operationId,
        active.abortController.signal,
      );
      requireTerminalIdentity(terminal, operationId);
      active.remoteSettled = true;
      if (terminal.state !== "succeeded") {
        throw new BrowserTransferError(
          terminal.error_code ?? "SFTP_DOWNLOAD_FINISH_FAILED",
          "Remote download finish did not succeed",
        );
      }
      try {
        await active.writable.close();
      } catch {
        throw new BrowserTransferError(
          "SFTP_LOCAL_WRITE_FAILED",
          "The local download could not be closed",
        );
      }
      active.writable = null;
      return terminal;
    } catch (error) {
      await abortWritable(active);
      if (began && !active.remoteSettled && !active.suppressRemoteAbort) {
        await this.abortRemote(active);
      }
      throw normalizeTransferError(error, "SFTP_LOCAL_WRITE_FAILED");
    } finally {
      if (this.active === active) this.active = null;
    }
  }

  discardPreparation(preparationId: string): void {
    this.requirePreparation(preparationId);
    this.preparations.delete(preparationId);
  }

  async cancelActive(): Promise<void> {
    const active = this.active;
    if (active === null) return;
    active.abortController.abort();
    await abortWritable(active);
    if (active.operationId !== null && !active.remoteSettled) {
      await this.abortRemote(active);
    }
  }

  async dispose(): Promise<void> {
    this.preparations.clear();
    const active = this.active;
    if (active === null) return;
    // Page teardown cannot prove a cancelled HTTP outcome, so it only drops
    // local ownership and leaves Python's remote recovery record authoritative.
    active.suppressRemoteAbort = true;
    active.abortController.abort();
    await abortWritable(active);
  }

  private newSummary(input: {
    direction: "upload" | "download";
    displayName: string;
    remotePath: string;
    byteCount: number;
    sha256: string;
    overwriteRequired: boolean;
  }): TransferPreparationSummary {
    const expiresAt = safeAdd(this.now(), PREPARATION_TTL_MS);
    return {
      preparation_id: this.randomUUID(),
      direction: input.direction,
      display_name: input.displayName,
      remote_path: input.remotePath,
      byte_count: input.byteCount,
      sha256: input.sha256,
      overwrite_required: input.overwriteRequired,
      expires_at_ms: expiresAt,
    };
  }

  private consumePreparation(
    preparationId: string,
    direction: "upload",
  ): UploadPreparation;
  private consumePreparation(
    preparationId: string,
    direction: "download",
  ): DownloadPreparation;
  private consumePreparation(
    preparationId: string,
    direction: "upload" | "download",
  ): Preparation {
    const preparation = this.requirePreparation(preparationId);
    this.preparations.delete(preparationId);
    if (preparation.summary.direction !== direction) throw preparationNotFound();
    return preparation;
  }

  private requirePreparation(preparationId: string): Preparation {
    const preparation = this.preparations.get(preparationId);
    if (
      preparation === undefined ||
      preparation.summary.expires_at_ms <= this.now()
    ) {
      this.preparations.delete(preparationId);
      throw preparationNotFound();
    }
    return preparation;
  }

  private startActive(direction: "upload" | "download"): ActiveTransfer {
    this.requireIdle();
    const active: ActiveTransfer = {
      direction,
      operationId: null,
      abortController: new AbortController(),
      remoteAbortAttempted: false,
      remoteSettled: false,
      suppressRemoteAbort: false,
      writable: null,
    };
    this.active = active;
    return active;
  }

  private requireIdle(): void {
    if (this.active !== null) {
      throw new BrowserTransferError(
        "SFTP_TRANSFER_BUSY",
        "Another local transfer is already active",
      );
    }
  }

  private async abortRemote(active: ActiveTransfer): Promise<void> {
    if (active.remoteAbortAttempted || active.operationId === null) return;
    active.remoteAbortAttempted = true;
    try {
      const terminal =
        active.direction === "upload"
          ? await this.transport.abortUpload(active.operationId)
          : await this.transport.abortDownload(active.operationId);
      requireTerminalIdentity(terminal, active.operationId);
      active.remoteSettled = true;
    } catch {
      throw new BrowserTransferError(
        "SFTP_TRANSFER_OUTCOME_UNKNOWN",
        "The remote abort outcome is unknown",
      );
    }
  }
}

function joinRemotePath(directory: string, basename: string): string {
  if (
    directory.length === 0 ||
    !directory.startsWith("/") ||
    basename.length === 0 ||
    basename.includes("/") ||
    basename.includes("\\")
  ) {
    throw new BrowserTransferError(
      "SFTP_PATH_INVALID",
      "The remote upload path is invalid",
    );
  }
  return directory === "/"
    ? `/${basename}`
    : `${directory.replace(/\/+$/, "")}/${basename}`;
}

function safeAdd(value: number, increment: number): number {
  const result = value + increment;
  if (!Number.isSafeInteger(result) || result < 0) {
    throw new BrowserTransferError(
      "SFTP_FILE_SIZE_UNSUPPORTED",
      "Transfer byte count exceeds the supported range",
    );
  }
  return result;
}

function requireUploadReady(value: UploadReady, operationId: string): void {
  if (
    value.operation_id !== operationId ||
    value.next_sequence !== 0 ||
    value.next_offset !== 0
  ) {
    throw protocolError();
  }
}

function requireUploadAck(
  value: UploadChunkAck,
  operationId: string,
  sequence: number,
  offset: number,
  byteCount: number,
): void {
  if (
    value.operation_id !== operationId ||
    value.sequence !== sequence ||
    value.offset !== offset ||
    value.accepted_bytes !== byteCount
  ) {
    throw protocolError();
  }
}

function requireDownloadReady(
  value: DownloadReady,
  operationId: string,
  preflight: RemoteFileHash,
): void {
  if (
    value.operation_id !== operationId ||
    value.next_sequence !== 0 ||
    value.next_offset !== 0 ||
    value.path !== preflight.path ||
    value.sha256 !== preflight.sha256 ||
    value.byte_count !== preflight.byte_count
  ) {
    throw new BrowserTransferError(
      "SFTP_REMOTE_SOURCE_CHANGED",
      "The remote source changed after preflight",
    );
  }
}

function requireDownloadChunk(
  value: DownloadChunk,
  operationId: string,
  sequence: number,
  offset: number,
): void {
  if (
    value.operation_id !== operationId ||
    value.sequence !== sequence ||
    value.offset !== offset ||
    (!(value.data instanceof Uint8Array)) ||
    (value.data.byteLength === 0 && !value.eof)
  ) {
    throw protocolError();
  }
}

function requireTerminalIdentity(
  value: OperationTerminal,
  operationId: string,
): void {
  if (value.operation_id !== operationId) throw protocolError();
}

function requireActive(active: ActiveTransfer): void {
  if (active.abortController.signal.aborted) {
    throw new BrowserTransferError("SFTP_REQUEST_CANCELLED", "Transfer cancelled");
  }
}

async function abortWritable(active: ActiveTransfer): Promise<void> {
  const writable = active.writable;
  active.writable = null;
  if (writable === null) return;
  try {
    await writable.abort();
  } catch {
    // The local stream has no durable recovery contract. Remote state remains
    // independently authoritative and the original transfer error is retained.
  }
}

function normalizeTransferError(error: unknown, fallbackCode: string): Error {
  if (error instanceof BrowserTransferError) return error;
  if (error instanceof Error && "code" in error) return error;
  return new BrowserTransferError(fallbackCode, "Local transfer failed");
}

function localReadError(error: unknown): Error {
  return normalizeTransferError(error, "SFTP_LOCAL_READ_FAILED");
}

function preparationNotFound(): BrowserTransferError {
  return new BrowserTransferError(
    "SFTP_PREPARATION_NOT_FOUND",
    "Transfer preparation was not found",
  );
}

function protocolError(): BrowserTransferError {
  return new BrowserTransferError(
    "SIDECAR_RESPONSE_INVALID",
    "Manual SFTP response identity is invalid",
  );
}
