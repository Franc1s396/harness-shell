import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from "react";

import {
  cancelManualSftpOperation,
  closeManualSftpListing,
  createManualSftpDirectory,
  discardManualSftpPreparation,
  executeManualSftpDelete,
  executeManualSftpDownload,
  executeManualSftpRecovery,
  executeManualSftpUpload,
  getManualSftpContext,
  hashManualSftpFile,
  inspectManualSftpEntry,
  inspectManualSftpRecovery,
  listManualSftpDirectory,
  listManualSftpRecoveries,
  nextManualSftpDirectoryBatch,
  normalizeManualSftpError,
  openManualSftpLink,
  preflightManualSftpDelete,
  prepareManualSftpDownload,
  prepareManualSftpUpload,
  removeManualSftpEntry,
  renameManualSftpEntry,
  type DeletePlanSummary,
  type OperationTerminalProjection,
  type RecoveryAction,
  type RecoverySummary,
  type RemoteEntry,
} from "../../api/manual-sftp";
import type { TerminalSessionModel } from "../terminal/terminal-session";
import {
  initialManualSftpState,
  manualSftpReducer,
} from "./manual-sftp-state";
import { useManualSftpEvents } from "./useManualSftpEvents";

type ManualSftpControllerInput = {
  enabled: boolean;
  sessions: readonly TerminalSessionModel[];
  activeTabId: string | null;
};

export const useManualSftpController = ({
  enabled,
  sessions,
  activeTabId,
}: ManualSftpControllerInput) => {
  const [state, dispatch] = useReducer(
    manualSftpReducer,
    initialManualSftpState,
  );
  const listingCursorRef = useRef<string | null>(null);
  const preparationRef = useRef(state.preparation);
  const transferOwnerTabRef = useRef<string | null>(null);
  const navigationRevision = useRef(0);
  const lastPaths = useRef(new Map<string, string>());
  preparationRef.current = state.preparation;

  const selectedSession = useMemo(() => {
    const selected = sessions.find((session) => session.tabId === activeTabId);
    return selected?.state === "CONNECTED" && selected.sshSessionId
      ? selected
      : null;
  }, [activeTabId, sessions]);
  const bindingRef = useRef<{
    entered: boolean;
    tabId: string | null;
    sshSessionId: string | null;
  }>({ entered: false, tabId: null, sshSessionId: null });
  if (!enabled) {
    bindingRef.current = { entered: false, tabId: null, sshSessionId: null };
  } else if (!bindingRef.current.entered) {
    bindingRef.current = {
      entered: true,
      tabId: selectedSession?.tabId ?? null,
      sshSessionId: selectedSession?.sshSessionId ?? null,
    };
  }
  const binding = bindingRef.current;
  const activeSession = useMemo(
    () =>
      sessions.find(
        (session) =>
          session.tabId === binding.tabId &&
          session.sshSessionId === binding.sshSessionId &&
          session.state === "CONNECTED",
      ) ?? null,
    [binding.sshSessionId, binding.tabId, sessions],
  );
  const sshSessionId = binding.sshSessionId;

  const closeCurrentListing = useCallback(async () => {
    const listingId = listingCursorRef.current;
    if (!listingId) return;
    listingCursorRef.current = null;
    await closeManualSftpListing(listingId);
  }, []);

  const navigate = useCallback(
    async (path: string) => {
      if (!sshSessionId) {
        dispatch({
          type: "listingFailed",
          error: {
            code: "NO_SESSION",
            message:
              "Manual SFTP requires an explicitly active connected terminal tab.",
          },
        });
        return;
      }
      if (!activeSession) {
        dispatch({
          type: "listingFailed",
          error: {
            code: "SFTP_SESSION_NOT_CONNECTED",
            message: "The bound Manual SFTP session is no longer connected.",
          },
        });
        return;
      }
      const revision = ++navigationRevision.current;
      dispatch({ type: "listingStarted", path });
      try {
        await closeCurrentListing();
        if (revision !== navigationRevision.current) return;
        let batch = await listManualSftpDirectory(sshSessionId, path);
        if (revision !== navigationRevision.current) {
          if (!batch.done) await closeManualSftpListing(batch.listing_id);
          return;
        }
        listingCursorRef.current = batch.done ? null : batch.listing_id;
        dispatch({ type: "listingBatch", batch });
        while (!batch.done) {
          batch = await nextManualSftpDirectoryBatch(
            batch.listing_id,
            batch.next_sequence,
          );
          if (revision !== navigationRevision.current) {
            if (!batch.done) await closeManualSftpListing(batch.listing_id);
            return;
          }
          listingCursorRef.current = batch.done ? null : batch.listing_id;
          dispatch({ type: "listingBatch", batch });
        }
        lastPaths.current.set(sshSessionId, path);
      } catch (error) {
        if (revision === navigationRevision.current) {
          dispatch({
            type: "listingFailed",
            error: normalizeManualSftpError(error),
          });
        }
      }
    },
    [activeSession, closeCurrentListing, sshSessionId],
  );

  useManualSftpEvents(
    (progress) => {
      transferOwnerTabRef.current ??= bindingRef.current.tabId;
      dispatch({ type: "transferProgress", progress });
    },
    (progress) => dispatch({ type: "operationProgress", progress }),
    (error) => dispatch({ type: "listingFailed", error }),
    enabled,
  );

  const loadRecoveries = useCallback(async () => {
    dispatch({ type: "recoveriesLoadStarted" });
    try {
      const recoveries = await listManualSftpRecoveries();
      dispatch({ type: "recoveriesLoaded", recoveries });
      return recoveries;
    } catch (error) {
      dispatch({
        type: "recoveriesLoadFailed",
        error: normalizeManualSftpError(error),
      });
      throw error;
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    dispatch({ type: "recoveriesLoadStarted" });
    void listManualSftpRecoveries().then(
      (recoveries) => {
        if (!disposed) dispatch({ type: "recoveriesLoaded", recoveries });
      },
      (error) => {
        if (!disposed) {
          dispatch({
            type: "recoveriesLoadFailed",
            error: normalizeManualSftpError(error),
          });
        }
      },
    );
    return () => {
      disposed = true;
    };
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    dispatch({ type: "contextLoadStarted" });
    const load = async () => {
      try {
        const context = await getManualSftpContext(sshSessionId);
        if (disposed) return;
        dispatch({ type: "contextLoaded", context });
        const path =
          lastPaths.current.get(context.ssh_session_id) ?? context.home;
        await navigate(path);
      } catch (error) {
        if (!disposed) {
          dispatch({
            type: "contextLoadFailed",
            error: normalizeManualSftpError(error),
          });
        }
      }
    };
    void load();
    return () => {
      disposed = true;
      navigationRevision.current += 1;
    };
  }, [enabled, navigate, sshSessionId]);

  useEffect(() => {
    const preparation = state.preparation;
    if (!preparation) return;
    const delay = Math.max(0, preparation.expires_at_ms - Date.now());
    const timer = window.setTimeout(() => {
      if (preparationRef.current?.preparation_id !== preparation.preparation_id) {
        return;
      }
      preparationRef.current = null;
      void discardManualSftpPreparation(preparation.preparation_id).then(
        () => dispatch({ type: "preparationDiscarded" }),
        (error) =>
          dispatch({
            type: "listingFailed",
            error: normalizeManualSftpError(error),
          }),
      );
    }, Math.min(delay, 2_147_483_647));
    return () => window.clearTimeout(timer);
  }, [state.preparation]);

  useEffect(
    () => () => {
      navigationRevision.current += 1;
      const listingId = listingCursorRef.current;
      listingCursorRef.current = null;
      if (listingId) void closeManualSftpListing(listingId);
      const preparation = preparationRef.current;
      if (preparation) {
        preparationRef.current = null;
        void discardManualSftpPreparation(preparation.preparation_id);
      }
    },
    [],
  );

  const requireSession = useCallback(() => {
    if (!sshSessionId) {
      throw {
        code: "NO_SESSION",
        message:
          "Manual SFTP requires an explicitly active connected terminal tab.",
      };
    }
    if (!activeSession) {
      throw {
        code: "SFTP_SESSION_NOT_CONNECTED",
        message: "The bound Manual SFTP session is no longer connected.",
      };
    }
    return sshSessionId;
  }, [activeSession, sshSessionId]);

  const publishTerminal = useCallback(
    (terminal: OperationTerminalProjection) => {
      transferOwnerTabRef.current = null;
      dispatch({ type: "operationTerminal", terminal });
      return terminal;
    },
    [],
  );

  const prepareUpload = useCallback(
    async (targetName: string) => {
      const sessionId = requireSession();
      const remoteDirectory = state.listing?.path ?? state.context?.home;
      if (!remoteDirectory) throw new Error("Manual SFTP directory is unavailable.");
      const preparation = await prepareManualSftpUpload(
        sessionId,
        remoteDirectory,
        targetName,
      );
      if (preparation) dispatch({ type: "preparationReady", preparation });
      return preparation;
    },
    [requireSession, state.context?.home, state.listing?.path],
  );

  const prepareDownload = useCallback(
    async (entry: RemoteEntry) => {
      const preparation = await prepareManualSftpDownload(
        requireSession(),
        entry.path,
        entry.name,
      );
      if (preparation) dispatch({ type: "preparationReady", preparation });
      return preparation;
    },
    [requireSession],
  );

  const executePrepared = useCallback(async () => {
    const preparation = preparationRef.current;
    if (!preparation) throw new Error("Manual SFTP preparation is unavailable.");
    preparationRef.current = null;
    const terminal =
      preparation.direction === "upload"
        ? await executeManualSftpUpload(preparation.preparation_id, true)
        : await executeManualSftpDownload(preparation.preparation_id, true);
    transferOwnerTabRef.current = null;
    dispatch({ type: "operationTerminal", terminal });
    return terminal;
  }, []);

  const discardPreparation = useCallback(async () => {
    const preparation = preparationRef.current;
    if (!preparation) return;
    preparationRef.current = null;
    await discardManualSftpPreparation(preparation.preparation_id);
    dispatch({ type: "preparationDiscarded" });
    transferOwnerTabRef.current = null;
  }, []);

  const createDirectory = useCallback(
    async (name: string) => {
      const parent = state.listing?.path ?? state.context?.home;
      if (!parent) throw new Error("Manual SFTP directory is unavailable.");
      return publishTerminal(
        await createManualSftpDirectory(requireSession(), parent, name),
      );
    },
    [publishTerminal, requireSession, state.context?.home, state.listing?.path],
  );

  const renameEntry = useCallback(
    async (
      sourcePath: string,
      targetPath: string,
      overwrite: boolean,
    ) =>
      publishTerminal(
        await renameManualSftpEntry(
          requireSession(),
          sourcePath,
          targetPath,
          overwrite,
        ),
      ),
    [publishTerminal, requireSession],
  );

  const removeEntry = useCallback(
    async (remotePath: string) =>
      publishTerminal(
        await removeManualSftpEntry(
          requireSession(),
          remotePath,
        ),
      ),
    [publishTerminal, requireSession],
  );

  const preflightDelete = useCallback(
    (remotePath: string): Promise<DeletePlanSummary> =>
      preflightManualSftpDelete(requireSession(), remotePath),
    [requireSession],
  );

  const executeDelete = useCallback(
    async (deletePlanId: string) =>
      publishTerminal(await executeManualSftpDelete(deletePlanId, true)),
    [publishTerminal],
  );

  const listTreeDirectories = useCallback(
    async (remotePath: string) => {
      const sessionId = requireSession();
      let cursor: string | null = null;
      const entries: RemoteEntry[] = [];
      try {
        let batch = await listManualSftpDirectory(sessionId, remotePath);
        cursor = batch.done ? null : batch.listing_id;
        entries.push(...batch.entries);
        while (!batch.done) {
          batch = await nextManualSftpDirectoryBatch(
            batch.listing_id,
            batch.next_sequence,
          );
          cursor = batch.done ? null : batch.listing_id;
          entries.push(...batch.entries);
        }
      } finally {
        if (cursor) await closeManualSftpListing(cursor);
      }
      return entries
        .filter((entry) => entry.entry_type === "directory")
        .map((entry) => ({ name: entry.name, path: entry.path }));
    },
    [requireSession],
  );

  const inspectRecovery = useCallback(
    async (recoveryId: string): Promise<RecoverySummary> => {
      dispatch({ type: "recoveriesLoadStarted" });
      try {
        const response = await inspectManualSftpRecovery(recoveryId);
        await loadRecoveries();
        return response;
      } catch (error) {
        dispatch({
          type: "recoveriesLoadFailed",
          error: normalizeManualSftpError(error),
        });
        throw error;
      }
    },
    [loadRecoveries],
  );

  const executeRecovery = useCallback(
    async (recoveryId: string, action: RecoveryAction) => {
      const response = await executeManualSftpRecovery(
        recoveryId,
        action,
        true,
      );
      await loadRecoveries();
      return response;
    },
    [loadRecoveries],
  );

  return {
    state: {
      ...state,
      context:
        state.context?.ssh_session_id === sshSessionId ? state.context : null,
    },
    activeSession,
    activeTransferTabId: state.transferProgress
      ? transferOwnerTabRef.current
      : null,
    navigate,
    refresh: () => {
      const path = state.listing?.path ?? state.context?.home;
      return path ? navigate(path) : Promise.resolve();
    },
    select: (path: string | null) =>
      dispatch({ type: "selectionChanged", path }),
    prepareUpload,
    prepareDownload,
    openLink: (entry: RemoteEntry) =>
      openManualSftpLink(requireSession(), entry.path),
    executePrepared,
    discardPreparation,
    createDirectory,
    renameEntry,
    removeEntry,
    preflightDelete,
    executeDelete,
    listTreeDirectories,
    inspectEntry: (entry: RemoteEntry) =>
      inspectManualSftpEntry(requireSession(), entry.path),
    hashFile: (entry: RemoteEntry) =>
      hashManualSftpFile(requireSession(), entry.path),
    cancelOperation: cancelManualSftpOperation,
    loadRecoveries,
    inspectRecovery,
    executeRecovery,
  };
};

export type ManualSftpController = ReturnType<typeof useManualSftpController>;
