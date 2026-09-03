import type {
  ListingBatch,
  ManualSftpCommandError,
  ManualSftpContext,
  MutationProgressProjection,
  OperationTerminalProjection,
  RecoverySummary,
  RemoteEntry,
  TransferPreparationSummary,
  TransferProgressProjection,
} from "../../api/manual-sftp";

export type ManualSftpListingState = {
  listingId: string;
  path: string;
  entries: RemoteEntry[];
  nextSequence: number;
  done: boolean;
  observedEntryCount: number;
  complete: boolean;
};

export type ManualSftpState = {
  context: ManualSftpContext | null;
  contextLoading: boolean;
  requestedPath: string | null;
  listing: ManualSftpListingState | null;
  listingLoading: boolean;
  selectedPath: string | null;
  preparation: TransferPreparationSummary | null;
  operationProgress: MutationProgressProjection | null;
  transferProgress: TransferProgressProjection | null;
  terminal: OperationTerminalProjection | null;
  recoveries: RecoverySummary[];
  recoveriesLoading: boolean;
  error: ManualSftpCommandError | null;
};

export type ManualSftpAction =
  | { type: "contextLoadStarted" }
  | { type: "contextLoaded"; context: ManualSftpContext }
  | { type: "contextLoadFailed"; error: ManualSftpCommandError }
  | { type: "listingStarted"; path: string }
  | { type: "listingBatch"; batch: ListingBatch }
  | { type: "listingFailed"; error: ManualSftpCommandError }
  | { type: "selectionChanged"; path: string | null }
  | {
      type: "preparationReady";
      preparation: TransferPreparationSummary;
    }
  | { type: "preparationDiscarded" }
  | { type: "operationProgress"; progress: MutationProgressProjection }
  | { type: "transferProgress"; progress: TransferProgressProjection }
  | { type: "operationTerminal"; terminal: OperationTerminalProjection }
  | { type: "recoveriesLoadStarted" }
  | { type: "recoveriesLoadFailed"; error: ManualSftpCommandError }
  | { type: "recoveriesLoaded"; recoveries: RecoverySummary[] };

export const initialManualSftpState: ManualSftpState = {
  context: null,
  contextLoading: true,
  requestedPath: null,
  listing: null,
  listingLoading: false,
  selectedPath: null,
  preparation: null,
  operationProgress: null,
  transferProgress: null,
  terminal: null,
  recoveries: [],
  recoveriesLoading: true,
  error: null,
};

export class ManualSftpStateError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ManualSftpStateError";
  }
}

export const manualSftpReducer = (
  state: ManualSftpState,
  action: ManualSftpAction,
): ManualSftpState => {
  switch (action.type) {
    case "contextLoadStarted":
      return {
        ...state,
        context: null,
        contextLoading: true,
        requestedPath: null,
        listing: null,
        listingLoading: false,
        selectedPath: null,
        error: null,
      };
    case "contextLoaded":
      return {
        ...initialManualSftpState,
        recoveries: state.recoveries,
        recoveriesLoading: state.recoveriesLoading,
        context: action.context,
        contextLoading: false,
        requestedPath: action.context.home,
      };
    case "contextLoadFailed":
      return {
        ...state,
        context: null,
        contextLoading: false,
        requestedPath: null,
        listing: null,
        listingLoading: false,
        selectedPath: null,
        error: action.error,
      };
    case "listingStarted":
      return {
        ...state,
        requestedPath: action.path,
        listing: null,
        listingLoading: true,
        selectedPath: null,
        error: null,
      };
    case "listingBatch":
      return mergeListingBatch(state, action.batch);
    case "listingFailed":
      return { ...state, listingLoading: false, error: action.error };
    case "selectionChanged":
      return { ...state, selectedPath: action.path };
    case "preparationReady":
      return { ...state, preparation: action.preparation, error: null };
    case "preparationDiscarded":
      return { ...state, preparation: null };
    case "operationProgress":
      return { ...state, operationProgress: action.progress, error: null };
    case "transferProgress":
      return { ...state, transferProgress: action.progress, error: null };
    case "operationTerminal":
      return {
        ...state,
        preparation: null,
        operationProgress: null,
        transferProgress: null,
        terminal: action.terminal,
      };
    case "recoveriesLoadStarted":
      return { ...state, recoveriesLoading: true };
    case "recoveriesLoadFailed":
      return {
        ...state,
        recoveriesLoading: false,
        error: action.error,
      };
    case "recoveriesLoaded":
      return {
        ...state,
        recoveries: action.recoveries,
        recoveriesLoading: false,
      };
  }
};

const mergeListingBatch = (
  state: ManualSftpState,
  batch: ListingBatch,
): ManualSftpState => {
  if (state.requestedPath !== batch.path) {
    throw new ManualSftpStateError(
      "Manual SFTP listing path does not match the active request.",
    );
  }
  if (batch.entries.length > 200 || batch.observed_entry_count > 50_000) {
    throw new ManualSftpStateError(
      "Manual SFTP listing exceeds the bounded contract.",
    );
  }
  const current = state.listing;
  if (!current) {
    if (batch.next_sequence < 1) {
      throw new ManualSftpStateError(
        "Manual SFTP listing sequence is invalid.",
      );
    }
    if (batch.observed_entry_count !== batch.entries.length) {
      throw new ManualSftpStateError(
        "Manual SFTP listing count does not match the first batch.",
      );
    }
    return {
      ...state,
      listing: fromBatch(batch),
      listingLoading: !batch.done,
      error: null,
    };
  }
  if (current.listingId !== batch.listing_id) {
    throw new ManualSftpStateError(
      "Manual SFTP listing identity changed between batches.",
    );
  }
  if (current.path !== batch.path) {
    throw new ManualSftpStateError(
      "Manual SFTP listing path changed between batches.",
    );
  }
  if (current.done || batch.next_sequence !== current.nextSequence + 1) {
    throw new ManualSftpStateError(
      "Manual SFTP listing sequence is stale or out of order.",
    );
  }
  if (
    batch.observed_entry_count !==
    current.observedEntryCount + batch.entries.length
  ) {
    throw new ManualSftpStateError(
      "Manual SFTP listing observed count is inconsistent.",
    );
  }
  return {
    ...state,
    listing: {
      listingId: current.listingId,
      path: current.path,
      entries: [...current.entries, ...batch.entries],
      nextSequence: batch.next_sequence,
      done: batch.done,
      observedEntryCount: batch.observed_entry_count,
      complete: batch.complete,
    },
    listingLoading: !batch.done,
    error: null,
  };
};

const fromBatch = (batch: ListingBatch): ManualSftpListingState => ({
  listingId: batch.listing_id,
  path: batch.path,
  entries: [...batch.entries],
  nextSequence: batch.next_sequence,
  done: batch.done,
  observedEntryCount: batch.observed_entry_count,
  complete: batch.complete,
});
