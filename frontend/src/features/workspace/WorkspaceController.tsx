import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
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
  listConnections,
  openPty,
  replaceHostKey,
  resizePty,
  updateConnection,
  writePty,
  type ConnectionProfile,
  type ConnectionProfileInput,
  type ConnectionStatus,
  type HostKeyCandidate,
  type SshCommandError,
} from "../../api/ssh";
import { useTerminalUiStore } from "../../stores/terminal-ui-store";
import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
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
import { ManualSftpWorkspace } from "../sftp/ManualSftpWorkspace";
import { useManualSftpController } from "../sftp/useManualSftpController";
import { normalizeManualSftpError } from "../../api/manual-sftp";
import { AgentWorkspace } from "../agent/AgentWorkspace";
import { ModelProvidersPanel } from "../agent/ModelProvidersPanel";
import { useAgentController } from "../agent/useAgentController";
import { WorkspaceFrame } from "../shell/WorkspaceFrame";
import { useSshEvents } from "../ssh/useSshEvents";
import { base64ToBytes } from "../terminal/base64";
import { CleanupFailureNotice } from "../terminal/CleanupFailureNotice";
import {
  createCleanupJob,
  runCleanupJob,
  type SessionCleanupJob,
} from "../terminal/session-cleanup";
import {
  isCurrentBinding,
  type SessionBinding,
  type TerminalSessionModel,
} from "../terminal/terminal-session";
import {
  PtyOutputBufferError,
  TerminalOutputBuffer,
} from "../terminal/terminal-output-buffer";
import { TerminalWorkspace } from "../terminal/TerminalWorkspace";
import { submitConnectionProfile } from "./connection-submit";

type ConnectContext = {
  profileOverride?: ConnectionProfile;
  profileSavedBeforeConnect: boolean;
};

type ConnectIntent = "initial" | "reconnect";

type HostKeyPrompt = {
  tabId: string;
  generation: number;
  connectionId: string;
  candidate: HostKeyCandidate;
  trustedFingerprint: string | null;
  profileOverride: ConnectionProfile | null;
  profileSavedBeforeConnect: boolean;
  intent: ConnectIntent;
};

type DisconnectTransferDecision = {
  session: TerminalSessionModel;
  phase: "choice" | "cancelling" | "recovery";
  error: ReturnType<typeof normalizeManualSftpError> | null;
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
      tabId: string;
      error: SshCommandError;
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

const sessionNotReadyError = (status: ConnectionStatus): SshCommandError => ({
  code: status.error_code ?? "SSH_CONNECTION_NOT_READY",
  message: "SSH connection did not become ready.",
  details: {
    recoverable: status.recoverable,
    correlation_id: status.correlation_id,
  },
});

const profileNotFoundError = (): SshCommandError => ({
  code: "PROFILE_NOT_FOUND",
  message: "Connection profile was not found.",
});

export function WorkspaceController() {
  const { t } = useTranslation();
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [connectionChecks, setConnectionChecks] = useState<
    Record<string, ConnectionStatus>
  >({});
  const [sessions, setSessions] = useState<TerminalSessionModel[]>([]);
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
  const [cleanupFailures, setCleanupFailures] = useState<
    SessionCleanupJob[]
  >([]);
  const [retryingCleanupIds, setRetryingCleanupIds] = useState<Set<string>>(
    new Set(),
  );
  const [runtimeRefreshRevision, setRuntimeRefreshRevision] = useState(0);
  const [disconnectTransferDecision, setDisconnectTransferDecision] =
    useState<DisconnectTransferDecision | null>(null);
  const [providerSettingsRequestKey, setProviderSettingsRequestKey] =
    useState(0);

  const connectionDialog = useWorkspaceUiStore(
    (state) => state.connectionDialog,
  );
  const layoutRevision = useWorkspaceUiStore((state) => state.layoutRevision);
  const sidebarVisible = useWorkspaceUiStore((state) => state.sidebarVisible);
  const agentWidth = useWorkspaceUiStore((state) => state.agentWidth);
  const activeActivity = useWorkspaceUiStore((state) => state.activeActivity);
  const activeTabId = useTerminalUiStore((state) => state.activeTabId);
  const manualSftp = useManualSftpController({
    enabled: activeActivity === "sftp",
    sessions,
    activeTabId,
  });
  const agent = useAgentController({ sessions, activeTabId });

  useEffect(() => {
    const currentTabIds = new Set(sessions.map((session) => session.tabId));
    for (const session of sessions) agent.ensureTab(session.tabId);
    for (const tabId of Object.keys(agent.state.tabs)) {
      if (!currentTabIds.has(tabId) && !agent.hasActiveRunForTab(tabId)) {
        agent.removeTab(tabId);
      }
    }
  }, [
    agent.ensureTab,
    agent.hasActiveRunForTab,
    agent.removeTab,
    agent.state.tabs,
    sessions,
  ]);

  const sessionsRef = useRef(new Map<string, TerminalSessionModel>());
  const sshBindingsRef = useRef(new Map<string, SessionBinding>());
  const ptyBindingsRef = useRef(new Map<string, SessionBinding>());
  const retiredPtyIdsRef = useRef(new Set<string>());
  const connectTasksRef = useRef(
    new Map<string, Promise<"CONNECTED" | "HOST_KEY_REQUIRED">>(),
  );
  const cancelledSessionsRef = useRef(new Set<string>());
  const outputBufferRef = useRef<TerminalOutputBuffer | null>(null);

  if (outputBufferRef.current === null) {
    outputBufferRef.current = new TerminalOutputBuffer();
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

  const publishSessions = () => {
    const next = [...sessionsRef.current.values()];
    setSessions(next);
    useTerminalUiStore
      .getState()
      .reconcileTabs(next.map((session) => session.tabId));
  };

  const addSession = (session: TerminalSessionModel) => {
    sessionsRef.current.set(session.tabId, session);
    publishSessions();
  };

  const replaceSession = (
    tabId: string,
    update: (session: TerminalSessionModel) => TerminalSessionModel,
  ) => {
    const current = sessionsRef.current.get(tabId);
    if (!current) return null;
    const next = update(current);
    sessionsRef.current.set(tabId, next);
    publishSessions();
    return next;
  };

  const sessionIsCurrent = (tabId: string, generation: number) => {
    const session = sessionsRef.current.get(tabId);
    return (
      !cancelledSessionsRef.current.has(tabId) &&
      session !== undefined &&
      session.generation === generation
    );
  };

  const removeBindingsFor = (session: TerminalSessionModel) => {
    if (session.sshSessionId) {
      const binding = sshBindingsRef.current.get(session.sshSessionId);
      if (binding && isCurrentBinding(session, binding)) {
        sshBindingsRef.current.delete(session.sshSessionId);
      }
    }
    if (session.ptySessionId) {
      const binding = ptyBindingsRef.current.get(session.ptySessionId);
      if (binding && isCurrentBinding(session, binding)) {
        ptyBindingsRef.current.delete(session.ptySessionId);
      }
      retiredPtyIdsRef.current.add(session.ptySessionId);
      outputBuffer.unregisterPty(session.ptySessionId);
    }
    setPtySizes((current) => {
      if (!(session.tabId in current)) return current;
      const next = { ...current };
      delete next[session.tabId];
      return next;
    });
  };

  const publishCleanupResult = (result: {
    complete: boolean;
    job: SessionCleanupJob;
  }) => {
    setCleanupFailures((current) =>
      result.complete
        ? current.filter(
            (item) => item.cleanupJobId !== result.job.cleanupJobId,
          )
        : [
            ...current.filter(
              (item) => item.cleanupJobId !== result.job.cleanupJobId,
            ),
            result.job,
          ],
    );
    return result;
  };

  const executeCleanup = async (job: SessionCleanupJob) =>
    publishCleanupResult(
      await runCleanupJob(job, {
        closePty,
        disconnectSsh,
        normalizeError,
      }),
    );

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const status = await getRuntimeStatus();
        if (active) setRuntime(status);
      } catch (error) {
        if (active) setRuntime(runtimeFailureStatus(error));
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
      for (const current of sessionsRef.current.values()) {
        removeBindingsFor(current);
        sessionsRef.current.set(current.tabId, {
          ...current,
          state: "DISCONNECTED",
          sshSessionId: null,
          ptySessionId: null,
        });
      }
      sshBindingsRef.current.clear();
      ptyBindingsRef.current.clear();
      setPtySizes({});
      publishSessions();
      return;
    }
    void listConnections()
      .then((profiles) => {
        setConnections(profiles);
        setSelectedId(
          (current) => current ?? profiles[0]?.connection_id ?? null,
        );
      })
      .catch((error) => setRuntime(runtimeFailureStatus(error)));
  }, [runtimeReady]);

  useEffect(() => () => outputBuffer.clear(), [outputBuffer]);

  const failSession = (
    tabId: string,
    generation: number,
    error: SshCommandError,
  ) => {
    if (!sessionIsCurrent(tabId, generation)) return;
    replaceSession(tabId, (session) => ({ ...session, state: "FAILED" }));
    setWorkspaceFailure({ scope: "terminal", tabId, error });
  };

  const cleanupRemoteClosedSession = (current: TerminalSessionModel) => {
    if (!current.ptySessionId) return;
    const job = {
      ...createCleanupJob({
        tabId: current.tabId,
        sessionTitle: current.title,
        connectionId: current.connectionId,
        generation: current.generation,
        ptySessionId: current.ptySessionId,
        sshSessionId: current.sshSessionId,
      }),
      ptyClosed: true,
    };
    replaceSession(current.tabId, (session) => ({
      ...session,
      state: "DISCONNECTING",
    }));
    void executeCleanup(job).then((result) => {
      const latest = sessionsRef.current.get(current.tabId);
      if (!latest || latest.generation !== current.generation) return;
      removeBindingsFor(latest);
      replaceSession(current.tabId, (session) => ({
        ...session,
        state: result.complete ? "DISCONNECTED" : "FAILED",
        sshSessionId: null,
        ptySessionId: null,
      }));
    });
  };

  const sshEventListenerState = useSshEvents(
    (event) => {
      if (event.event === "ssh.connection.status") {
        if (!event.status.session_id) return;
        const binding = sshBindingsRef.current.get(event.status.session_id);
        const session = binding
          ? sessionsRef.current.get(binding.tabId)
          : undefined;
        if (!binding || !session || !isCurrentBinding(session, binding)) return;
        const state =
          event.status.state === "READY"
            ? "CONNECTED"
            : event.status.state === "CLOSING"
              ? "DISCONNECTING"
              : event.status.state === "FAILED"
                ? "FAILED"
                : event.status.state === "DISCONNECTED"
                  ? "DISCONNECTED"
                  : null;
        if (state) replaceSession(session.tabId, (current) => ({ ...current, state }));
        return;
      }

      const binding = ptyBindingsRef.current.get(event.pty_session_id);
      const session = binding
        ? sessionsRef.current.get(binding.tabId)
        : undefined;
      if (binding && (!session || !isCurrentBinding(session, binding))) return;
      if (!binding && retiredPtyIdsRef.current.has(event.pty_session_id)) return;

      if (event.event === "ssh.pty.output") {
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
          if (session) failSession(session.tabId, session.generation, normalized);
          else setRuntime(runtimeFailureStatus(normalized));
        }
        return;
      }

      outputBuffer.markPtyClosed(event.pty_session_id);
      if (!session) return;
      cleanupRemoteClosedSession(session);
    },
    (error) => setRuntime(runtimeFailureStatus(error)),
  );
  const interactiveReady = isInteractiveSshReady(
    runtimeReady,
    sshEventListenerState,
  );

  const establishSession = async (
    tabId: string,
    generation: number,
    intent: ConnectIntent,
    profile: ConnectionProfile,
  ) => {
    const status = await connectSsh(profile.connection_id);
    if (status.state !== "READY" || !status.session_id) {
      throw sessionNotReadyError(status);
    }
    // The successful connect result is authoritative for the persisted Host Key.
    setConnectionChecks((checks) => ({
      ...checks,
      [profile.connection_id]: status,
    }));
    const sshSessionId = status.session_id;
    if (!sessionIsCurrent(tabId, generation)) {
      await executeCleanup(
        createCleanupJob({
          tabId,
          sessionTitle: profile.display_name,
          connectionId: profile.connection_id,
          generation,
          ptySessionId: null,
          sshSessionId,
        }),
      );
      return;
    }
    const binding = { tabId, generation };
    sshBindingsRef.current.set(sshSessionId, binding);

    let pty;
    try {
      pty = await openPty(sshSessionId, 80, 24);
    } catch (error) {
      sshBindingsRef.current.delete(sshSessionId);
      const cleanup = await executeCleanup(
        createCleanupJob({
          tabId,
          sessionTitle: profile.display_name,
          connectionId: profile.connection_id,
          generation,
          ptySessionId: null,
          sshSessionId,
        }),
      );
      if (!cleanup.complete) {
        const cleanupError = cleanup.job.lastSshError;
        const openError = normalizeError(error);
        throw {
          code: "PTY_OPEN_CLEANUP_FAILED",
          message: `${openError.code}: ${openError.message}; SSH cleanup failed: ${cleanupError?.code}: ${cleanupError?.message}`,
          details: cleanupError?.details,
        } satisfies SshCommandError;
      }
      throw error;
    }

    if (!sessionIsCurrent(tabId, generation)) {
      sshBindingsRef.current.delete(sshSessionId);
      retiredPtyIdsRef.current.add(pty.pty_session_id);
      outputBuffer.unregisterPty(pty.pty_session_id);
      await executeCleanup(
        createCleanupJob({
          tabId,
          sessionTitle: profile.display_name,
          connectionId: profile.connection_id,
          generation,
          ptySessionId: pty.pty_session_id,
          sshSessionId,
        }),
      );
      return;
    }

    retiredPtyIdsRef.current.delete(pty.pty_session_id);
    ptyBindingsRef.current.set(pty.pty_session_id, binding);
    let registration;
    try {
      registration = outputBuffer.bindPty({
        ptySessionId: pty.pty_session_id,
        tabId,
        generation,
        separator:
          intent === "reconnect"
            ? new TextEncoder().encode(
                `\r\n${t("terminal.reconnectDivider")}\r\n`,
              )
            : undefined,
      });
    } catch (error) {
      ptyBindingsRef.current.delete(pty.pty_session_id);
      sshBindingsRef.current.delete(sshSessionId);
      await executeCleanup(
        createCleanupJob({
          tabId,
          sessionTitle: profile.display_name,
          connectionId: profile.connection_id,
          generation,
          ptySessionId: pty.pty_session_id,
          sshSessionId,
        }),
      );
      throw error;
    }

    setPtySizes((current) => ({
      ...current,
      [tabId]: { cols: pty.cols, rows: pty.rows },
    }));
    replaceSession(tabId, (session) => ({
      ...session,
      sshSessionId,
      ptySessionId: pty.pty_session_id,
      state: registration.closed ? "DISCONNECTED" : "CONNECTED",
    }));
    if (registration.closed) {
      cleanupRemoteClosedSession(
        sessionsRef.current.get(tabId) ?? {
          tabId,
          connectionId: profile.connection_id,
          title: profile.display_name,
          state: "DISCONNECTED",
          sshSessionId,
          ptySessionId: pty.pty_session_id,
          generation,
        },
      );
    }
  };

  const connectSession = (
    tabId: string,
    intent: ConnectIntent,
    context: ConnectContext,
  ): Promise<"CONNECTED" | "HOST_KEY_REQUIRED"> => {
    const existing = connectTasksRef.current.get(tabId);
    if (existing) return existing;
    const task = (async () => {
      if (sshEventListenerState !== "READY") {
        throw {
          code: "SSH_EVENT_SUBSCRIPTION_NOT_READY",
          message: "SSH event listener is not ready.",
        } satisfies SshCommandError;
      }
      const current = sessionsRef.current.get(tabId);
      if (!current) return "CONNECTED";
      const generation = current.generation + 1;
      outputBuffer.advanceGeneration(tabId, generation);
      replaceSession(tabId, (session) => ({
        ...session,
        generation,
        state: "CONNECTING",
        sshSessionId: null,
        ptySessionId: null,
      }));
      setWorkspaceFailure((failure) =>
        failure?.scope === "terminal" && failure.tabId === tabId
          ? null
          : failure,
      );
      setHostKeyError(null);

      const profile =
        context.profileOverride ??
        connections.find(
          (item) => item.connection_id === current.connectionId,
        );
      if (!profile) throw profileNotFoundError();
      try {
        const inspection = await inspectHostKey(current.connectionId);
        setConnectionChecks((checks) => ({
          ...checks,
          [current.connectionId]: inspection,
        }));
        if (!sessionIsCurrent(tabId, generation)) return "CONNECTED";
        if (inspection.host_key_candidate) {
          replaceSession(tabId, (session) => ({
            ...session,
            state: "HOST_KEY_REQUIRED",
          }));
          setHostKeyPrompt({
            tabId,
            generation,
            connectionId: current.connectionId,
            candidate: inspection.host_key_candidate,
            trustedFingerprint: inspection.trusted_fingerprint_sha256,
            profileOverride: context.profileOverride ?? null,
            profileSavedBeforeConnect: context.profileSavedBeforeConnect,
            intent,
          });
          return "HOST_KEY_REQUIRED";
        }
        if (inspection.state === "FAILED") {
          throw hostKeyInspectionError(inspection);
        }
        await establishSession(tabId, generation, intent, profile);
        return "CONNECTED";
      } catch (error) {
        const normalized = normalizeError(error);
        if (
          normalized.code === "HOST_KEY_CHANGED" &&
          normalized.details?.host_key_candidate &&
          normalized.details.trusted_fingerprint_sha256
        ) {
          replaceSession(tabId, (session) => ({
            ...session,
            state: "HOST_KEY_REQUIRED",
          }));
          setHostKeyPrompt({
            tabId,
            generation,
            connectionId: current.connectionId,
            candidate: normalized.details.host_key_candidate,
            trustedFingerprint:
              normalized.details.trusted_fingerprint_sha256,
            profileOverride: context.profileOverride ?? null,
            profileSavedBeforeConnect: context.profileSavedBeforeConnect,
            intent,
          });
          return "HOST_KEY_REQUIRED";
        }
        failSession(tabId, generation, normalized);
        throw normalized;
      }
    })();
    connectTasksRef.current.set(tabId, task);
    const clear = () => {
      if (connectTasksRef.current.get(tabId) === task) {
        connectTasksRef.current.delete(tabId);
      }
    };
    void task.then(clear, clear);
    return task;
  };

  const openSession = (
    connectionId: string,
    context: ConnectContext = { profileSavedBeforeConnect: false },
  ) => {
    const profile =
      context.profileOverride ??
      connections.find((item) => item.connection_id === connectionId);
    if (!profile) return Promise.reject(profileNotFoundError());
    const tabId = crypto.randomUUID();
    const session: TerminalSessionModel = {
      tabId,
      connectionId,
      title: profile.display_name,
      state: "CONNECTING",
      sshSessionId: null,
      ptySessionId: null,
      generation: 0,
    };
    outputBuffer.registerTab(tabId, 0);
    addSession(session);
    useTerminalUiStore.getState().setActiveTab(tabId);
    return connectSession(tabId, "initial", context);
  };

  const performDisconnectSession = async (requested: TerminalSessionModel) => {
    const current = sessionsRef.current.get(requested.tabId);
    if (!current || current.state !== "CONNECTED") return;
    replaceSession(current.tabId, (session) => ({
      ...session,
      state: "DISCONNECTING",
    }));
    const result = await executeCleanup(
      createCleanupJob({
        tabId: current.tabId,
        sessionTitle: current.title,
        connectionId: current.connectionId,
        generation: current.generation,
        ptySessionId: current.ptySessionId,
        sshSessionId: current.sshSessionId,
      }),
    );
    const latest = sessionsRef.current.get(current.tabId);
    if (!latest || latest.generation !== current.generation) return;
    removeBindingsFor(latest);
    replaceSession(current.tabId, (session) => ({
      ...session,
      state: result.complete ? "DISCONNECTED" : "FAILED",
      ptySessionId: null,
      sshSessionId: null,
    }));
  };

  const disconnectSession = async (requested: TerminalSessionModel) => {
    if (agent.hasActiveRunForTab(requested.tabId)) return;
    const activeTransfer = manualSftp.state.transferProgress;
    if (
      activeTransfer &&
      manualSftp.activeTransferTabId === requested.tabId
    ) {
      setDisconnectTransferDecision({
        session: requested,
        phase: "choice",
        error: null,
      });
      return;
    }
    await performDisconnectSession(requested);
  };

  useEffect(() => {
    if (
      disconnectTransferDecision?.phase !== "cancelling" ||
      manualSftp.state.transferProgress
    ) {
      return;
    }
    if (
      manualSftp.state.terminal?.state === "cleanup_required" ||
      manualSftp.state.terminal?.state === "outcome_unknown"
    ) {
      setDisconnectTransferDecision((current) =>
        current?.phase === "cancelling"
          ? { ...current, phase: "recovery" }
          : current,
      );
      return;
    }
    const session = disconnectTransferDecision.session;
    setDisconnectTransferDecision(null);
    void performDisconnectSession(session);
  }, [
    disconnectTransferDecision,
    manualSftp.state.terminal?.state,
    manualSftp.state.transferProgress,
  ]);

  const closeSessionOptimistically = (requested: TerminalSessionModel) => {
    if (agent.hasActiveRunForTab(requested.tabId)) return;
    const current = sessionsRef.current.get(requested.tabId);
    if (!current) return;
    cancelledSessionsRef.current.add(current.tabId);
    sessionsRef.current.delete(current.tabId);
    removeBindingsFor(current);
    outputBuffer.unregisterTab(current.tabId);
    publishSessions();

    const job = createCleanupJob({
      tabId: current.tabId,
      sessionTitle: current.title,
      connectionId: current.connectionId,
      generation: current.generation,
      ptySessionId: current.ptySessionId,
      sshSessionId: current.sshSessionId,
    });
    if (!job.ptyClosed || !job.sshDisconnected) {
      void executeCleanup(job);
    }
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
        await replaceHostKey(prompt.candidate, prompt.trustedFingerprint);
      } else {
        await confirmHostKey(prompt.candidate);
      }
      setHostKeyPrompt(null);
      if (!sessionIsCurrent(prompt.tabId, prompt.generation)) return;
      const profile =
        prompt.profileOverride ??
        connections.find(
          (item) => item.connection_id === prompt.connectionId,
        );
      if (!profile) throw profileNotFoundError();
      if (prompt.candidate.connection_id !== prompt.connectionId) {
        const inspection = await inspectHostKey(prompt.connectionId);
        setConnectionChecks((checks) => ({
          ...checks,
          [prompt.connectionId]: inspection,
        }));
        if (inspection.host_key_candidate) {
          replaceSession(prompt.tabId, (session) => ({
            ...session,
            state: "HOST_KEY_REQUIRED",
          }));
          setHostKeyPrompt({
            ...prompt,
            candidate: inspection.host_key_candidate,
            trustedFingerprint: inspection.trusted_fingerprint_sha256,
          });
          return;
        }
        if (inspection.state === "FAILED") {
          throw hostKeyInspectionError(inspection);
        }
      }
      await establishSession(
        prompt.tabId,
        prompt.generation,
        prompt.intent,
        profile,
      );
    } catch (error) {
      const normalized = normalizeError(error);
      if (normalized.code === "HOST_KEY_REPLACE_CONFLICT") {
        try {
          const refreshed = await inspectHostKey(prompt.connectionId);
          setConnectionChecks((checks) => ({
            ...checks,
            [prompt.connectionId]: refreshed,
          }));
          if (refreshed.state === "FAILED") {
            throw hostKeyInspectionError(refreshed);
          }
          if (refreshed.host_key_candidate) {
            setHostKeyPrompt({
              ...prompt,
              candidate: refreshed.host_key_candidate,
              trustedFingerprint: refreshed.trusted_fingerprint_sha256,
            });
          } else {
            setHostKeyPrompt(null);
            replaceSession(prompt.tabId, (session) => ({
              ...session,
              state: "DISCONNECTED",
            }));
          }
        } catch (refreshError) {
          const refreshFailure = normalizeError(refreshError);
          setHostKeyError(refreshFailure);
          failSession(prompt.tabId, prompt.generation, refreshFailure);
          setWorkspaceFailure({
            scope: "connection",
            connectionId: prompt.connectionId,
            error: refreshFailure,
            profileSavedBeforeConnect: prompt.profileSavedBeforeConnect,
          });
          return;
        }
      } else {
        setHostKeyError(normalized);
        failSession(prompt.tabId, prompt.generation, normalized);
      }
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

  const retryCleanup = async (job: SessionCleanupJob) => {
    setRetryingCleanupIds((current) => new Set(current).add(job.cleanupJobId));
    try {
      await executeCleanup(job);
    } finally {
      setRetryingCleanupIds((current) => {
        const next = new Set(current);
        next.delete(job.cleanupJobId);
        return next;
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
        openSession(saved.connection_id, {
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

  const selectedCheck = selected
    ? connectionChecks[selected.connection_id]
    : undefined;
  const activeSession =
    sessions.find((session) => session.tabId === activeTabId) ?? null;
  const activePtySize = activeSession
    ? ptySizes[activeSession.tabId] ?? null
    : null;
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
            disabled={!interactiveReady}
            selectedErrorNotice={
              selectedConnectionFailure ? (
                <ErrorNotice
                  error={selectedConnectionFailure.error}
                  partialSuccess={
                    selectedConnectionFailure.profileSavedBeforeConnect
                  }
                  onRetry={() => {
                    const failure = selectedConnectionFailure;
                    setWorkspaceFailure(null);
                    void openSession(failure.connectionId).catch((error) =>
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
                      .openEditConnection(
                        selectedConnectionFailure.connectionId,
                      )
                  }
                  onDismiss={() => setWorkspaceFailure(null)}
                />
              ) : null
            }
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
              void openSession(connectionId).catch((error) =>
                showConnectionFailure(connectionId, normalizeError(error)),
              );
            }}
          />
        }
        primaryWorkspace={
          activeActivity === "sftp" ? (
            <ManualSftpWorkspace
              controller={manualSftp}
              onSelectConnection={() => {
                const store = useWorkspaceUiStore.getState();
                store.setActiveActivity("connections");
                store.setSidebarVisible(true);
              }}
            />
          ) : (
            <TerminalWorkspace
            sessions={sessions}
            outputBuffer={outputBuffer}
            runtimeReady={interactiveReady}
            fitRequestKey={layoutRevision}
            agentBackgroundByTab={agent.backgroundByTab}
            activeAgentRunTabIds={agent.activeAgentRunTabIds}
            errorNotice={
              terminalFailure ? (
                <ErrorNotice
                  error={terminalFailure.error}
                  onDismiss={() => setWorkspaceFailure(null)}
                />
              ) : null
            }
            cleanupNotices={cleanupFailures.map((job) => (
              <CleanupFailureNotice
                key={job.cleanupJobId}
                job={job}
                retrying={retryingCleanupIds.has(job.cleanupJobId)}
                onRetry={() => void retryCleanup(job)}
              />
            ))}
            onSelectConnection={() => {
              const store = useWorkspaceUiStore.getState();
              store.setActiveActivity("connections");
              store.setSidebarVisible(true);
            }}
            onCreateConnection={
              sidebarVisible
                ? undefined
                : () =>
                    useWorkspaceUiStore.getState().openCreateConnection()
            }
            onWrite={(tabId, data) => {
              const current = sessionsRef.current.get(tabId);
              if (
                !current ||
                current.state !== "CONNECTED" ||
                !current.ptySessionId
              ) {
                return Promise.reject({
                  code: "PTY_INPUT_BLOCKED",
                  message:
                    "PTY input is blocked because the session is closing or closed.",
                  details: {
                    node: "ui_orchestration",
                    recoverable: false,
                    remote_state: "unknown",
                  },
                } satisfies SshCommandError);
              }
              return writePty(current.ptySessionId, data)
                .then(() => undefined)
                .catch((error) => {
                  setWorkspaceFailure({
                    scope: "terminal",
                    tabId,
                    error: normalizeError(error),
                  });
                });
            }}
            onResize={(tabId, cols, rows) => {
              const current = sessionsRef.current.get(tabId);
              if (
                !current ||
                current.state !== "CONNECTED" ||
                !current.ptySessionId
              ) {
                return;
              }
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
              const ptySessionId = current.ptySessionId;
              void resizePty(ptySessionId, cols, rows)
                .then(() =>
                  setPtySizes((sizes) => ({
                    ...sizes,
                    [tabId]: { cols, rows },
                  })),
                )
                .catch((error) =>
                  setWorkspaceFailure({
                    scope: "terminal",
                    tabId,
                    error: normalizeError(error),
                  }),
                );
            }}
            onReconnect={(requested) => {
              const current = sessionsRef.current.get(requested.tabId);
              if (
                !current ||
                (current.state !== "DISCONNECTED" &&
                  current.state !== "FAILED")
              ) {
                return;
              }
              void connectSession(current.tabId, "reconnect", {
                profileSavedBeforeConnect: false,
              }).catch((error) =>
                showConnectionFailure(
                  current.connectionId,
                  normalizeError(error),
                ),
              );
            }}
            onDisconnect={(session) => void disconnectSession(session)}
            onCloseConfirmed={closeSessionOptimistically}
            onFocusChange={() => undefined}
            />
          )
        }
        agentWorkspace={
          <AgentWorkspace
            width={agentWidth}
            tabTitle={activeSession?.title ?? null}
            tab={agent.activeTab}
            configs={agent.configs}
            configsLoading={agent.configsLoading}
            onCollapse={() =>
              useWorkspaceUiStore.getState().setAgentVisible(false)
            }
            onDraftChange={(value) => {
              if (activeTabId) agent.changeDraft(activeTabId, value);
            }}
            onProviderSelect={(apiConfigId) => {
              if (activeTabId) agent.selectProvider(activeTabId, apiConfigId);
            }}
            onOpenProviderSettings={() => {
              setProviderSettingsRequestKey((current) => current + 1);
              void agent.refreshConfigs().catch(() => undefined);
            }}
            onRequestSend={() => {
              if (activeTabId) void agent.requestSend(activeTabId);
            }}
            onConfirmRiskAndSend={() => {
              if (activeTabId) void agent.confirmRiskAndSend(activeTabId);
            }}
            onCancelRisk={() => {
              if (activeTabId) agent.cancelRisk(activeTabId);
            }}
            onResetConversation={() => {
              if (activeTabId) agent.resetConversation(activeTabId);
            }}
            onMarkRead={() => {
              if (activeTabId) agent.markRead(activeTabId);
            }}
          />
        }
        modelProviders={
          <ModelProvidersPanel
            configs={agent.configs}
            loading={agent.configsLoading}
            error={agent.configsError}
            mutationError={agent.providerMutationError}
            cleanupError={agent.providerCleanupError}
            activeApiConfigIds={agent.activeApiConfigIds}
            onCreate={agent.createProvider}
            onUpdate={agent.updateProvider}
            onDelete={agent.deleteProvider}
            onRetry={async () => {
              await agent.refreshConfigs();
            }}
          />
        }
        workspaceOverlay={
          runtime?.state === "FAILED" ? (
            <RuntimeFailureState
              errorCode={runtime.error_code ?? "RUNTIME_FAILED"}
              correlationId={runtime.correlation_id}
              onRetryStatus={() =>
                setRuntimeRefreshRevision((revision) => revision + 1)
              }
            />
          ) : null
        }
        runtimeState={runtime?.state ?? "unknown"}
        hostKeyState={hostKeyTrustLabel(selectedCheck)}
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
        activeTerminalAvailable={
          interactiveReady && activeSession?.state === "CONNECTED"
        }
        activeAgentRunCount={agent.activeAgentRunCount}
        agentBadge={agent.aggregateBackground}
        providerSettingsRequestKey={providerSettingsRequestKey}
        connectionActionsDisabled={!interactiveReady}
        activeSftpTransfer={manualSftp.state.transferProgress}
        activeSftpTerminal={manualSftp.state.terminal}
        onCancelActiveSftpTransfer={manualSftp.cancelOperation}
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
        onSettingsOpening={() => {
          void agent.refreshConfigs().catch(() => undefined);
        }}
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

      <Dialog
        open={disconnectTransferDecision !== null}
        title={t("sftp.disconnectTransferTitle")}
        onClose={() => setDisconnectTransferDecision(null)}
      >
        <p className="mt-3 text-sm text-ink-muted">
          {manualSftp.state.transferProgress?.cancellable === false
            ? t("sftp.disconnectCommittingBody")
            : disconnectTransferDecision?.phase === "recovery"
              ? t("sftp.disconnectRecoveryBody")
              : t("sftp.disconnectTransferBody")}
        </p>
        {disconnectTransferDecision?.error ? (
          <p role="alert" className="mt-3 text-sm text-danger">
            <strong>{disconnectTransferDecision.error.code}</strong>: {disconnectTransferDecision.error.message}
          </p>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={() => setDisconnectTransferDecision(null)}
          >
            {t("applicationClose.continueWaiting")}
          </Button>
          {disconnectTransferDecision?.phase === "choice" &&
          manualSftp.state.transferProgress?.cancellable ? (
            <Button
              variant="danger"
              onClick={async () => {
                const operationId = manualSftp.state.transferProgress?.operation_id;
                if (!operationId) return;
                setDisconnectTransferDecision((current) =>
                  current ? { ...current, phase: "cancelling", error: null } : current,
                );
                try {
                  await manualSftp.cancelOperation(operationId);
                } catch (error) {
                  // A failed cancel leaves the SSH session and transfer lifecycle unchanged.
                  setDisconnectTransferDecision((current) =>
                    current
                      ? {
                          ...current,
                          phase: "choice",
                          error: normalizeManualSftpError(error),
                        }
                      : current,
                  );
                }
              }}
            >
              {t("applicationClose.cancelAndCleanUp")}
            </Button>
          ) : disconnectTransferDecision?.phase === "recovery" ? (
            <Button
              onClick={() => {
                const session = disconnectTransferDecision.session;
                setDisconnectTransferDecision(null);
                void performDisconnectSession(session);
              }}
            >
              {t("sftp.keepRecoveryAndDisconnect")}
            </Button>
          ) : null}
        </div>
      </Dialog>

      {hostKeyPrompt ? (
        <HostKeyDialog
          candidate={hostKeyPrompt.candidate}
          trustedFingerprint={hostKeyPrompt.trustedFingerprint}
          error={hostKeyError}
          busy={hostKeyBusy}
          onConfirm={() => void acceptHostKey(false)}
          onReplace={() => void acceptHostKey(true)}
          onClose={() => {
            const prompt = hostKeyPrompt;
            setHostKeyPrompt(null);
            setHostKeyError(null);
            if (sessionIsCurrent(prompt.tabId, prompt.generation)) {
              replaceSession(prompt.tabId, (session) => ({
                ...session,
                state: "DISCONNECTED",
              }));
            }
          }}
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
