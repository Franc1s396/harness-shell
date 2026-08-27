import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getRuntimeStatus,
  openApprovalWindow,
  type RuntimeStatus,
} from "../../api/runtime";
import {
  closePty,
  confirmHostKey,
  connectSsh,
  createConnection,
  deleteConnection,
  disconnectSsh,
  inspectHostKey,
  openPty,
  replaceHostKey,
  resizePty,
  updateConnection,
  writePty,
  listConnections,
  type ConnectionProfile,
  type ConnectionProfileInput,
  type ConnectionStatus,
  type HostKeyCandidate,
  type SshCommandError,
} from "../../api/ssh";
import { ConnectionDialog } from "../connections/ConnectionDialog";
import { ConnectionNavigator } from "../connections/ConnectionNavigator";
import { HostKeyDialog } from "../connections/HostKeyDialog";
import {
  hostKeyTrustLabel,
  isInteractiveSshReady,
} from "../connections/connection-state";
import type { ConnectionSubmitIntent } from "../connections/connection-form";
import { ErrorNotice } from "../errors/ErrorNotice";
import { RuntimeFailureState } from "../errors/RuntimeFailureState";
import { AgentWorkspace } from "../shell/AgentWorkspace";
import { WorkspaceFrame } from "../shell/WorkspaceFrame";
import { useSshEvents } from "../ssh/useSshEvents";
import { base64ToBytes } from "../terminal/base64";
import {
  PtyOutputBuffer,
  PtyOutputBufferError,
} from "../terminal/terminal-output-buffer";
import {
  TerminalWorkspace,
  type TerminalTabModel,
} from "../terminal/TerminalWorkspace";
import { submitConnectionProfile } from "./connection-submit";
import { useTerminalUiStore } from "../../stores/terminal-ui-store";
import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";

type ConnectContext = {
  profileOverride?: ConnectionProfile;
  profileSavedBeforeConnect: boolean;
};

type HostKeyPrompt = {
  connectionId: string;
  candidate: HostKeyCandidate;
  trustedFingerprint: string | null;
  profileOverride: ConnectionProfile | null;
  profileSavedBeforeConnect: boolean;
};

export type WorkspaceFailure =
  | {
      scope: "connection";
      connectionId: string;
      error: SshCommandError;
      profileSavedBeforeConnect: boolean;
    }
  | {
      scope: "terminal";
      ptySessionId: string;
      error: SshCommandError;
    };

type PtyOwner = {
  sshSessionId: string;
  connectionId: string;
  ptyClosed: boolean;
};

type PtyCleanupResult = {
  disconnected: boolean;
  ptyError: SshCommandError | null;
  disconnectError: SshCommandError | null;
};

type ConnectTask = {
  task: Promise<"CONNECTED" | "HOST_KEY_REQUIRED">;
  context: ConnectContext;
};

const hostKeyInspectionError = (
  inspection: ConnectionStatus,
): SshCommandError => ({
  code: inspection.error_code ?? "HOST_KEY_INSPECTION_FAILED",
  message: "Host Key inspection failed.",
  details: {
    node: "host_key",
    recoverable: inspection.recoverable,
    correlation_id: inspection.correlation_id,
    remote_state: "pre_auth",
  },
});

export function WorkspaceController() {
  const { t } = useTranslation();
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [statuses, setStatuses] = useState<Record<string, ConnectionStatus>>(
    {},
  );
  const [tabs, setTabs] = useState<TerminalTabModel[]>([]);
  const [ptySizes, setPtySizes] = useState<
    Record<string, { cols: number; rows: number }>
  >({});
  const [hostKeyPrompt, setHostKeyPrompt] = useState<HostKeyPrompt | null>(
    null,
  );
  const [hostKeyBusy, setHostKeyBusy] = useState(false);
  const [hostKeyError, setHostKeyError] = useState<SshCommandError | null>(null);
  const [workspaceFailure, setWorkspaceFailure] =
    useState<WorkspaceFailure | null>(null);
  const [runtimeRefreshRevision, setRuntimeRefreshRevision] = useState(0);
  const connectionDialog = useWorkspaceUiStore(
    (state) => state.connectionDialog,
  );
  const layoutRevision = useWorkspaceUiStore(
    (state) => state.layoutRevision,
  );
  const sidebarVisible = useWorkspaceUiStore(
    (state) => state.sidebarVisible,
  );
  const agentWidth = useWorkspaceUiStore((state) => state.agentWidth);
  const activeTabId = useTerminalUiStore((state) => state.activeTabId);
  const outputBufferRef = useRef<PtyOutputBuffer | null>(null);
  const ptyOwnersRef = useRef(new Map<string, PtyOwner>());
  const closingPtysRef = useRef(
    new Map<string, Promise<PtyCleanupResult>>(),
  );
  const failedPtysRef = useRef(new Set<string>());
  const blockedPtysRef = useRef(new Set<string>());
  const disconnectingSshRef = useRef(new Set<string>());
  const disconnectTasksRef = useRef(
    new Map<string, Promise<ConnectionStatus>>(),
  );
  const connectTasksRef = useRef(new Map<string, ConnectTask>());

  if (outputBufferRef.current === null) {
    outputBufferRef.current = new PtyOutputBuffer();
  }
  const outputBuffer = outputBufferRef.current;
  const runtimeReady = runtime?.state === "READY";
  const selected = useMemo(
    () =>
      connections.find(
        (connection) => connection.connection_id === selectedId,
      ) ?? null,
    [connections, selectedId],
  );
  const dialogConnection =
    connectionDialog.kind === "edit"
      ? (connections.find(
          (connection) =>
            connection.connection_id === connectionDialog.connectionId,
        ) ?? null)
      : null;

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const status = await getRuntimeStatus();
        if (active) setRuntime(status);
      } catch {
        if (active) {
          setRuntime({
            state: "FAILED",
            error_code: "RUNTIME_STATUS_UNAVAILABLE",
            node: "core",
            recoverable: false,
            correlation_id: "unknown",
            last_sequence: 0,
            last_heartbeat_at: null,
          });
        }
      } finally {
        if (active) timer = window.setTimeout(() => void refresh(), 1_000);
      }
    };
    void refresh();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runtimeRefreshRevision]);

  useEffect(() => {
    if (!runtimeReady) {
      ptyOwnersRef.current.clear();
      closingPtysRef.current.clear();
      failedPtysRef.current.clear();
      blockedPtysRef.current.clear();
      disconnectingSshRef.current.clear();
      disconnectTasksRef.current.clear();
      setPtySizes({});
      setTabs((current) =>
        current.map((tab) => ({ ...tab, state: "DISCONNECTED" })),
      );
      return;
    }
    void listConnections()
      .then((profiles) => {
        setConnections(profiles);
        setSelectedId(
          (current) => current ?? profiles[0]?.connection_id ?? null,
        );
      })
      .catch((error) =>
        setRuntime(runtimeFailureStatus(error)),
      );
  }, [runtimeReady, outputBuffer]);

  useEffect(() => () => outputBuffer.clear(), [outputBuffer]);

  useEffect(() => {
    useTerminalUiStore
      .getState()
      .reconcileTabs(tabs.map((tab) => tab.tabId));
  }, [tabs]);

  const disconnectSshSession = (
    sshSessionId: string,
  ): Promise<ConnectionStatus> => {
    const currentTask = disconnectTasksRef.current.get(sshSessionId);
    if (currentTask) return currentTask;
    const task = disconnectSsh(sshSessionId);
    disconnectTasksRef.current.set(sshSessionId, task);
    void task.catch(() => {
      if (disconnectTasksRef.current.get(sshSessionId) === task) {
        disconnectTasksRef.current.delete(sshSessionId);
      }
    });
    return task;
  };

  const cleanupPtySession = (
    ptySessionId: string,
    sshSessionId: string,
    connectionId: string,
  ): Promise<PtyCleanupResult> => {
    const currentTask = closingPtysRef.current.get(ptySessionId);
    if (currentTask) return currentTask;

    const owner = ptyOwnersRef.current.get(ptySessionId);
    blockedPtysRef.current.add(ptySessionId);
    const task = (async (): Promise<PtyCleanupResult> => {
      let ptyError: SshCommandError | null = null;
      let disconnectError: SshCommandError | null = null;
      if (!(owner?.ptyClosed ?? false)) {
        try {
          await closePty(ptySessionId);
          if (owner) owner.ptyClosed = true;
        } catch (error) {
          ptyError = normalizeError(error);
        }
      }

      let disconnected = false;
      try {
        const status = await disconnectSshSession(
          owner?.sshSessionId ?? sshSessionId,
        );
        disconnected = true;
        setStatuses((current) => ({
          ...current,
          [owner?.connectionId ?? connectionId]: status,
        }));
        ptyOwnersRef.current.delete(ptySessionId);
        setPtySizes((current) => {
          const next = { ...current };
          delete next[ptySessionId];
          return next;
        });
      } catch (error) {
        disconnectError = normalizeError(error);
      }
      return { disconnected, ptyError, disconnectError };
    })();
    closingPtysRef.current.set(ptySessionId, task);
    void task.finally(() => {
      if (closingPtysRef.current.get(ptySessionId) === task) {
        closingPtysRef.current.delete(ptySessionId);
      }
    });
    return task;
  };

  const terminateFailedPty = async (ptySessionId: string) => {
    if (failedPtysRef.current.has(ptySessionId)) return;
    const owner = ptyOwnersRef.current.get(ptySessionId);
    if (!owner) return;
    failedPtysRef.current.add(ptySessionId);
    blockedPtysRef.current.add(ptySessionId);
    setTabs((current) =>
      current.map((item) =>
        item.ptySessionId === ptySessionId
          ? { ...item, state: "CLOSED" }
          : item,
      ),
    );
    const result = await cleanupPtySession(
      ptySessionId,
      owner.sshSessionId,
      owner.connectionId,
    );
    setTabs((current) =>
      current.map((item) =>
        item.ptySessionId === ptySessionId
          ? {
              ...item,
              state: result.disconnected ? "DISCONNECTED" : "CLOSED",
            }
          : item,
      ),
    );
    if (result.disconnectError || result.ptyError) {
      const cleanupError = result.disconnectError ?? result.ptyError!;
      setWorkspaceFailure({
        scope: "terminal",
        ptySessionId,
        error: {
          code: "PTY_FAIL_CLOSED_CLEANUP_FAILED",
          message: `PTY stream validation failed and cleanup was incomplete: ${cleanupError.message}`,
        },
      });
    }
  };

  const sshEventListenerState = useSshEvents(
    (event) => {
      if (event.event === "ssh.connection.status") {
        setStatuses((current) => ({
          ...current,
          [event.status.connection_id]: event.status,
        }));
      } else if (event.event === "ssh.pty.output") {
        try {
          outputBuffer.ingest({
            ptySessionId: event.pty_session_id,
            streamSequence: event.stream_sequence,
            data: base64ToBytes(event.data_b64),
          });
        } catch (error) {
          const normalized =
            error instanceof PtyOutputBufferError
              ? { code: error.code, message: error.message }
              : normalizeError(error);
          setWorkspaceFailure({
            scope: "terminal",
            ptySessionId: event.pty_session_id,
            error: normalized,
          });
          void terminateFailedPty(event.pty_session_id);
        }
      } else {
        outputBuffer.markClosed(event.pty_session_id);
        blockedPtysRef.current.add(event.pty_session_id);
        const owner = ptyOwnersRef.current.get(event.pty_session_id);
        if (owner) owner.ptyClosed = true;
        setTabs((current) =>
          current.map((tab) =>
            tab.ptySessionId === event.pty_session_id
              ? { ...tab, state: "CLOSED" }
              : tab,
          ),
        );
      }
    },
    (error) => setRuntime(runtimeFailureStatus(error)),
  );
  const interactiveReady = isInteractiveSshReady(
    runtimeReady,
    sshEventListenerState,
  );

  const establishSession = async (
    connectionId: string,
    profileOverride?: ConnectionProfile,
  ) => {
    const profile =
      profileOverride ??
      connections.find((item) => item.connection_id === connectionId);
    if (!profile) {
      throw {
        code: "PROFILE_NOT_FOUND",
        message: "Connection profile was not found.",
      } satisfies SshCommandError;
    }

    const status = await connectSsh(connectionId);
    setStatuses((current) => ({ ...current, [connectionId]: status }));
    if (status.state !== "READY" || !status.session_id) {
      throw {
        code: status.error_code ?? "SSH_CONNECTION_NOT_READY",
        message: "SSH connection did not become ready.",
        details: {
          recoverable: status.recoverable,
          correlation_id: status.correlation_id,
        },
      } satisfies SshCommandError;
    }
    const sshSessionId = status.session_id;
    disconnectTasksRef.current.delete(sshSessionId);

    let pty;
    try {
      pty = await openPty(sshSessionId, 80, 24);
    } catch (error) {
      try {
        const disconnected = await disconnectSshSession(sshSessionId);
        setStatuses((current) => ({
          ...current,
          [connectionId]: disconnected,
        }));
      } catch (cleanupError) {
        const openFailure = normalizeError(error);
        const cleanupFailure = normalizeError(cleanupError);
        throw {
          code: "PTY_OPEN_CLEANUP_FAILED",
          message: `${openFailure.code}: ${openFailure.message}; SSH cleanup failed: ${cleanupFailure.code}: ${cleanupFailure.message}`,
          details: cleanupFailure.details,
        } satisfies SshCommandError;
      }
      throw error;
    }
    let registration;
    try {
      registration = outputBuffer.register(pty.pty_session_id);
    } catch (error) {
      let cleanupError: unknown;
      try {
        await closePty(pty.pty_session_id);
      } catch (closeError) {
        cleanupError = closeError;
      }
      try {
        await disconnectSshSession(sshSessionId);
      } catch (disconnectError) {
        cleanupError ??= disconnectError;
      }
      outputBuffer.unregister(pty.pty_session_id);
      if (cleanupError) {
        throw {
          code: "PTY_FAIL_CLOSED_CLEANUP_FAILED",
          message: `PTY registration failed and cleanup was incomplete: ${normalizeError(cleanupError).message}`,
        } satisfies SshCommandError;
      }
      throw error;
    }

    ptyOwnersRef.current.set(pty.pty_session_id, {
      sshSessionId,
      connectionId,
      ptyClosed: registration.closed,
    });
    if (registration.closed) blockedPtysRef.current.add(pty.pty_session_id);
    else blockedPtysRef.current.delete(pty.pty_session_id);
    setPtySizes((current) => ({
      ...current,
      [pty.pty_session_id]: { cols: pty.cols, rows: pty.rows },
    }));
    setTabs((current) => [
      ...current,
      {
        tabId: crypto.randomUUID(),
        title: profile.display_name,
        ptySessionId: pty.pty_session_id,
        sshSessionId,
        connectionId,
        state: registration.closed ? "CLOSED" : "OPEN",
      },
    ]);
    if (registration.closed) {
      await disconnectSshSession(sshSessionId);
      ptyOwnersRef.current.delete(pty.pty_session_id);
      setTabs((current) =>
        current.map((tab) =>
          tab.ptySessionId === pty.pty_session_id
            ? { ...tab, state: "DISCONNECTED" }
            : tab,
        ),
      );
    }
  };

  const beginConnectOnce = async (
    connectionId: string,
    context: ConnectContext = { profileSavedBeforeConnect: false },
  ): Promise<"CONNECTED" | "HOST_KEY_REQUIRED"> => {
    if (sshEventListenerState !== "READY") {
      throw {
        code: "SSH_EVENT_SUBSCRIPTION_NOT_READY",
        message: "SSH event listener is not ready.",
      } satisfies SshCommandError;
    }
    setWorkspaceFailure((current) =>
      current?.scope === "connection" &&
      current.connectionId === connectionId
        ? null
        : current,
    );
    setHostKeyError(null);

    try {
      const inspection = await inspectHostKey(connectionId);
      setStatuses((current) => ({ ...current, [connectionId]: inspection }));
      if (inspection.host_key_candidate) {
        setHostKeyPrompt({
          connectionId,
          candidate: inspection.host_key_candidate,
          trustedFingerprint: inspection.trusted_fingerprint_sha256,
          profileOverride: context.profileOverride ?? null,
          profileSavedBeforeConnect: context.profileSavedBeforeConnect,
        });
        return "HOST_KEY_REQUIRED";
      }
      if (inspection.state === "FAILED") {
        throw hostKeyInspectionError(inspection);
      }
      await establishSession(connectionId, context.profileOverride);
      return "CONNECTED";
    } catch (error) {
      const normalized = normalizeError(error);
      if (
        normalized.code === "HOST_KEY_CHANGED" &&
        normalized.details?.host_key_candidate &&
        normalized.details.trusted_fingerprint_sha256
      ) {
        setHostKeyPrompt({
          connectionId,
          candidate: normalized.details.host_key_candidate,
          trustedFingerprint:
            normalized.details.trusted_fingerprint_sha256,
          profileOverride: context.profileOverride ?? null,
          profileSavedBeforeConnect: context.profileSavedBeforeConnect,
        });
        return "HOST_KEY_REQUIRED";
      }
      throw normalized;
    }
  };

  const beginConnect = (
    connectionId: string,
    context: ConnectContext = { profileSavedBeforeConnect: false },
  ): Promise<"CONNECTED" | "HOST_KEY_REQUIRED"> => {
    const existing = connectTasksRef.current.get(connectionId);
    if (existing) {
      const bothOrdinary =
        !existing.context.profileSavedBeforeConnect &&
        !existing.context.profileOverride &&
        !context.profileSavedBeforeConnect &&
        !context.profileOverride;
      if (bothOrdinary) return existing.task;
      return Promise.reject({
        code: "CONNECTION_OPERATION_IN_PROGRESS",
        message: "A connection operation with different context is already in progress.",
        details: {
          node: "ui_orchestration",
          recoverable: true,
          remote_state: "unknown",
        },
      } satisfies SshCommandError);
    }

    const task = beginConnectOnce(connectionId, context);
    connectTasksRef.current.set(connectionId, { task, context });
    const clearTask = () => {
      if (connectTasksRef.current.get(connectionId)?.task === task) {
        connectTasksRef.current.delete(connectionId);
      }
    };
    void task.then(clearTask, clearTask);
    return task;
  };

  const acceptHostKey = async (replace: boolean) => {
    if (!hostKeyPrompt) return;
    const prompt = hostKeyPrompt;
    setHostKeyBusy(true);
    setHostKeyError(null);
    try {
      if (replace) {
        if (!prompt.trustedFingerprint) {
          throw new Error("Trusted fingerprint is unavailable.");
        }
        await replaceHostKey(
          prompt.candidate,
          prompt.trustedFingerprint,
        );
      } else {
        await confirmHostKey(prompt.candidate);
      }
      setHostKeyPrompt(null);
      await beginConnect(prompt.connectionId, {
        profileOverride: prompt.profileOverride ?? undefined,
        profileSavedBeforeConnect: prompt.profileSavedBeforeConnect,
      });
    } catch (error) {
      const normalized = normalizeError(error);
      if (normalized.code === "HOST_KEY_REPLACE_CONFLICT") {
        try {
          const refreshed = await inspectHostKey(prompt.connectionId);
          setStatuses((current) => ({
            ...current,
            [prompt.connectionId]: refreshed,
          }));
          if (refreshed.state === "FAILED") {
            throw hostKeyInspectionError(refreshed);
          }
          if (refreshed.host_key_candidate) {
            setHostKeyPrompt({
              ...prompt,
              candidate: refreshed.host_key_candidate,
              trustedFingerprint:
                refreshed.trusted_fingerprint_sha256,
            });
          } else {
            setHostKeyPrompt(null);
          }
        } catch (refreshError) {
          const refreshFailure = normalizeError(refreshError);
          setHostKeyError(refreshFailure);
          setWorkspaceFailure({
            scope: "connection",
            connectionId: prompt.connectionId,
            error: refreshFailure,
            profileSavedBeforeConnect: prompt.profileSavedBeforeConnect,
          });
          return;
        }
      }
      setHostKeyError(normalized);
      setWorkspaceFailure({
        scope: "connection",
        connectionId: prompt.connectionId,
        error: normalized,
        profileSavedBeforeConnect: prompt.profileSavedBeforeConnect,
      });
    } finally {
      setHostKeyBusy(false);
    }
  };

  const closeTab = async (tab: TerminalTabModel) => {
    const removeLocalTab = () => {
      outputBuffer.unregister(tab.ptySessionId);
      ptyOwnersRef.current.delete(tab.ptySessionId);
      failedPtysRef.current.delete(tab.ptySessionId);
      blockedPtysRef.current.delete(tab.ptySessionId);
      setPtySizes((current) => {
        const next = { ...current };
        delete next[tab.ptySessionId];
        return next;
      });
      setTabs((current) => current.filter((item) => item.tabId !== tab.tabId));
    };
    if (!runtimeReady || tab.state === "DISCONNECTED") {
      removeLocalTab();
      return;
    }

    blockedPtysRef.current.add(tab.ptySessionId);
    setTabs((current) =>
      current.map((item) =>
        item.tabId === tab.tabId ? { ...item, state: "CLOSED" } : item,
      ),
    );
    const result = await cleanupPtySession(
      tab.ptySessionId,
      tab.sshSessionId,
      tab.connectionId,
    );
    if (result.disconnected) removeLocalTab();
    if (result.disconnectError || result.ptyError) {
      const failure = result.disconnectError
        ? {
            ...result.disconnectError,
            message: result.ptyError
              ? `${result.disconnectError.message}; PTY cleanup also failed: ${result.ptyError.code}: ${result.ptyError.message}`
              : result.disconnectError.message,
          }
        : result.ptyError!;
      setWorkspaceFailure({
        scope: "terminal",
        ptySessionId: tab.ptySessionId,
        error: failure,
      });
    }
  };

  const showApprovalWindow = () => {
    void openApprovalWindow().catch((error) =>
      setRuntime(runtimeFailureStatus(error)),
    );
  };

  const persistProfile = async (
    input: ConnectionProfileInput,
  ): Promise<ConnectionProfile> => {
    const saved = dialogConnection
      ? await updateConnection(dialogConnection.connection_id, input)
      : await createConnection(input);
    setConnections((current) => {
      const remaining = current.filter(
        (profile) => profile.connection_id !== saved.connection_id,
      );
      return [...remaining, saved];
    });
    setSelectedId(saved.connection_id);
    return saved;
  };

  const submitProfile = async (
    input: ConnectionProfileInput,
    intent: ConnectionSubmitIntent,
  ) => {
    const outcome = await submitConnectionProfile({
      intent,
      persist: () => persistProfile(input),
      closeDialog: () =>
        useWorkspaceUiStore.getState().closeConnectionDialog(),
      connect: (saved) =>
        beginConnect(saved.connection_id, {
          profileOverride: saved,
          profileSavedBeforeConnect: true,
        }),
    });
    if (outcome.kind === "saved-connect-failed") {
      setWorkspaceFailure({
        scope: "connection",
        connectionId: outcome.saved.connection_id,
        error: outcome.error,
        profileSavedBeforeConnect: true,
      });
    } else {
      setWorkspaceFailure((current) =>
        current?.scope === "connection" &&
        current.connectionId === outcome.saved.connection_id
          ? null
          : current,
      );
    }
  };

  const removeProfile = async (connectionId: string) => {
    await deleteConnection(connectionId);
    setConnections((current) =>
      current.filter(
        (connection) => connection.connection_id !== connectionId,
      ),
    );
    setSelectedId((current) => (current === connectionId ? null : current));
    useWorkspaceUiStore.getState().closeConnectionDialog();
  };

  const disconnectSession = async (
    sshSessionId: string,
    connectionId: string,
  ) => {
    if (disconnectingSshRef.current.has(sshSessionId)) return;
    disconnectingSshRef.current.add(sshSessionId);
    const ptySessionIds = [...ptyOwnersRef.current.entries()]
      .filter(([, owner]) => owner.sshSessionId === sshSessionId)
      .map(([ptySessionId]) => ptySessionId);
    ptySessionIds.forEach((ptySessionId) =>
      blockedPtysRef.current.add(ptySessionId),
    );
    setTabs((current) =>
      current.map((tab) =>
        tab.sshSessionId === sshSessionId
          ? { ...tab, state: "CLOSED" }
          : tab,
      ),
    );
    try {
      const status = await disconnectSshSession(sshSessionId);
      setStatuses((current) => ({
        ...current,
        [status.connection_id]: status,
      }));
      setTabs((current) =>
        current.map((tab) =>
          tab.sshSessionId === sshSessionId
            ? { ...tab, state: "DISCONNECTED" }
            : tab,
        ),
      );
      ptySessionIds.forEach((ptySessionId) => {
        outputBuffer.markClosed(ptySessionId);
        const owner = ptyOwnersRef.current.get(ptySessionId);
        if (owner) owner.ptyClosed = true;
        ptyOwnersRef.current.delete(ptySessionId);
      });
      setPtySizes((current) => {
        const next = { ...current };
        ptySessionIds.forEach((ptySessionId) => delete next[ptySessionId]);
        return next;
      });
    } catch (error) {
      setWorkspaceFailure({
        scope: "connection",
        connectionId,
        error: normalizeError(error),
        profileSavedBeforeConnect: false,
      });
    } finally {
      disconnectingSshRef.current.delete(sshSessionId);
    }
  };

  const selectedStatus = selected
    ? statuses[selected.connection_id]
    : undefined;
  const statusTab =
    tabs.find((tab) => tab.tabId === activeTabId) ??
    tabs[tabs.length - 1] ??
    null;
  const activePtySize = statusTab ? ptySizes[statusTab.ptySessionId] ?? null : null;
  const selectedConnectionFailure =
    workspaceFailure?.scope === "connection" &&
    workspaceFailure.connectionId === selectedId
      ? workspaceFailure
      : null;
  const terminalFailure =
    workspaceFailure?.scope === "terminal" ? workspaceFailure : null;

  const showConnectionFailure = (
    connectionId: string,
    error: SshCommandError,
    profileSavedBeforeConnect = false,
  ) =>
    setWorkspaceFailure({
      scope: "connection",
      connectionId,
      error,
      profileSavedBeforeConnect,
    });

  return (
    <>
      <WorkspaceFrame
        connectionNavigator={
          <ConnectionNavigator
            connections={connections}
            selectedId={selectedId}
            statuses={statuses}
            disabled={!interactiveReady}
            selectedErrorNotice={selectedConnectionFailure ? (
              <ErrorNotice
                error={selectedConnectionFailure.error}
                partialSuccess={selectedConnectionFailure.profileSavedBeforeConnect}
                onRetry={() => {
                  const failure = selectedConnectionFailure;
                  setWorkspaceFailure(null);
                  void beginConnect(failure.connectionId).catch((error) =>
                    showConnectionFailure(
                      failure.connectionId,
                      normalizeError(error),
                      failure.profileSavedBeforeConnect,
                    ),
                  );
                }}
                onEdit={() =>
                  useWorkspaceUiStore
                    .getState()
                    .openEditConnection(selectedConnectionFailure.connectionId)
                }
                onDismiss={() => setWorkspaceFailure(null)}
              />
            ) : null}
            onSelect={(connectionId) => {
              setSelectedId(connectionId);
              useWorkspaceUiStore
                .getState()
                .setMediumViewportDrawerOpen(false);
            }}
            onCreate={() =>
              useWorkspaceUiStore.getState().openCreateConnection()
            }
            onEdit={(connectionId) => {
              setSelectedId(connectionId);
              useWorkspaceUiStore
                .getState()
                .openEditConnection(connectionId);
            }}
            onDelete={(connectionId) => {
              setSelectedId(connectionId);
              useWorkspaceUiStore
                .getState()
                .openEditConnection(connectionId);
            }}
            onOpen={(connectionId) => {
              setSelectedId(connectionId);
              useWorkspaceUiStore
                .getState()
                .setMediumViewportDrawerOpen(false);
              void beginConnect(connectionId).catch((error) =>
                showConnectionFailure(connectionId, normalizeError(error)),
              );
            }}
            onDisconnect={(sshSessionId) => {
              const connectionId = connections.find(
                (connection) =>
                  statuses[connection.connection_id]?.session_id ===
                  sshSessionId,
              )?.connection_id;
              if (!connectionId) {
                throw new Error(
                  `No connection owns SSH session ${sshSessionId}.`,
                );
              }
              void disconnectSession(sshSessionId, connectionId);
            }}
          />
        }
        terminalWorkspace={
          <TerminalWorkspace
            tabs={tabs}
            outputBuffer={outputBuffer}
            runtimeReady={interactiveReady}
            fitRequestKey={layoutRevision}
            errorNotice={terminalFailure ? (
              <ErrorNotice
                error={terminalFailure.error}
                onDismiss={() => setWorkspaceFailure(null)}
              />
            ) : null}
            onSelectConnection={() => {
              const store = useWorkspaceUiStore.getState();
              store.setActiveActivity("connections");
              store.setSidebarVisible(true);
            }}
            onCreateConnection={sidebarVisible ? undefined : () =>
              useWorkspaceUiStore.getState().openCreateConnection()
            }
              onWrite={(ptySessionId, data) => {
                const owner = ptyOwnersRef.current.get(ptySessionId);
                if (
                  blockedPtysRef.current.has(ptySessionId) ||
                  !owner ||
                  owner.ptyClosed
                ) {
                  return Promise.reject({
                    code: "PTY_INPUT_BLOCKED",
                    message: "PTY input is blocked because the session is closing or closed.",
                    details: {
                      node: "ui_orchestration",
                      recoverable: false,
                      remote_state: "unknown",
                    },
                  } satisfies SshCommandError);
                }
                return writePty(ptySessionId, data)
                  .then(() => undefined)
                  .catch((error) => {
                    setWorkspaceFailure({
                      scope: "terminal",
                      ptySessionId,
                      error: normalizeError(error),
                    });
                  });
              }}
              onResize={(ptySessionId, cols, rows) => {
                const owner = ptyOwnersRef.current.get(ptySessionId);
                if (
                  blockedPtysRef.current.has(ptySessionId) ||
                  !owner ||
                  owner.ptyClosed
                ) return;
                if (
                  !Number.isInteger(cols) ||
                  cols < 20 ||
                  cols > 500 ||
                  !Number.isInteger(rows) ||
                  rows < 5 ||
                  rows > 300
                ) {
                  throw new Error("Invalid PTY dimensions.");
                }
                void resizePty(ptySessionId, cols, rows)
                  .then(() =>
                    setPtySizes((current) => ({
                      ...current,
                      [ptySessionId]: { cols, rows },
                    })),
                  )
                  .catch((error) =>
                    setWorkspaceFailure({
                      scope: "terminal",
                      ptySessionId,
                      error: normalizeError(error),
                    }),
                  );
              }}
              onClose={(tab) => void closeTab(tab)}
              onFocusChange={() => undefined}
          />
        }
        agentWorkspace={
          <AgentWorkspace
            width={agentWidth}
            onCollapse={() =>
              useWorkspaceUiStore.getState().setAgentVisible(false)
            }
          />
        }
        workspaceOverlay={runtime?.state === "FAILED" ? (
          <RuntimeFailureState
            errorCode={runtime.error_code ?? "RUNTIME_FAILED"}
            correlationId={runtime.correlation_id}
            onRetryStatus={() =>
              setRuntimeRefreshRevision((revision) => revision + 1)
            }
          />
        ) : null}
        runtimeState={runtime?.state ?? "unknown"}
        connectionState={
          selected
            ? selectedStatus?.state ?? "DISCONNECTED"
            : "unknown"
        }
        hostKeyState={hostKeyTrustLabel(selectedStatus)}
        ptySize={activePtySize}
        route={
          selected
            ? selected.proxy_jump_id
              ? "ProxyJump"
              : "Direct"
            : "unknown"
        }
        environmentLabel={t("topbar.localEnvironment")}
        connectionName={selected?.display_name ?? null}
        targetSummary={
          selected
            ? `${selected.username}@${selected.host}:${selected.port}`
            : null
        }
        agentWidth={null}
        activeTerminalAvailable={statusTab !== null}
        connectionActionsDisabled={!interactiveReady}
        onCreateConnection={() =>
          useWorkspaceUiStore.getState().openCreateConnection()
        }
        onEditConnection={() => {
          if (!selected) return;
          useWorkspaceUiStore
            .getState()
            .openEditConnection(selected.connection_id);
        }}
        onOpenApproval={showApprovalWindow}
        onFocusTerminal={() =>
          useTerminalUiStore.getState().requestFocus()
        }
      />

      <ConnectionDialog
        open={connectionDialog.kind !== "closed"}
        connection={dialogConnection}
        connections={connections}
        onClose={() =>
          useWorkspaceUiStore.getState().closeConnectionDialog()
        }
        onSubmit={submitProfile}
        onDelete={removeProfile}
      />

      {hostKeyPrompt ? (
        <HostKeyDialog
          candidate={hostKeyPrompt.candidate}
          trustedFingerprint={hostKeyPrompt.trustedFingerprint}
          error={hostKeyError}
          busy={hostKeyBusy}
          onConfirm={() => void acceptHostKey(false)}
          onReplace={() => void acceptHostKey(true)}
          onClose={() => setHostKeyPrompt(null)}
        />
      ) : null}
    </>
  );
}

const normalizeError = (error: unknown): SshCommandError => {
  if (typeof error === "object" && error !== null && "code" in error) {
    const value = error as SshCommandError;
    return {
      code: String(value.code),
      message: String(value.message ?? "SSH operation failed."),
      details: value.details,
    };
  }
  return {
    code: "SSH_OPERATION_FAILED",
    message:
      error instanceof Error ? error.message : "SSH operation failed.",
  };
};

const runtimeFailureStatus = (error: unknown): RuntimeStatus => {
  const normalized = normalizeError(error);
  return {
    state: "FAILED",
    error_code: normalized.code,
    node: normalized.details?.node ?? "core",
    recoverable: normalized.details?.recoverable ?? false,
    correlation_id: normalized.details?.correlation_id ?? "unknown",
    last_sequence: 0,
    last_heartbeat_at: null,
  };
};
