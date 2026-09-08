import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";

import {
  agentApi,
  AgentTurnCancelled,
  normalizeAgentCommandError,
  type AgentApi,
  type AgentCommandError,
  type ModelApiConfig,
} from "../../api/agent";
import { useAgentPreferencesStore } from "../../stores/agent-preferences-store";
import type { TerminalSessionModel } from "../terminal/terminal-session";
import {
  agentBackgroundByTab,
  agentReducer,
  aggregateAgentBackground,
  createAgentState,
  isActiveRunForSession,
  lastRetryableUser,
  type ProviderSnapshot,
} from "./agent-state";
import {
  createProvider as createProviderConfig,
  deleteProvider as deleteProviderConfig,
  ProviderMutationFailure,
  updateProvider as updateProviderConfig,
  type ProviderDraft,
} from "./provider-config-actions";

export type AgentControllerDependencies = {
  api: AgentApi;
  makeId: () => string;
};

const defaultDependencies: AgentControllerDependencies = {
  api: agentApi,
  makeId: () => crypto.randomUUID(),
};

const uiError = (code: string): AgentCommandError => ({
  code,
  message: code,
});

const providerFailure = (error: unknown): ProviderMutationFailure =>
  error instanceof ProviderMutationFailure
    ? error
    : new ProviderMutationFailure(normalizeAgentCommandError(error));

export type UseAgentControllerInput = {
  sessions: readonly TerminalSessionModel[];
  activeTabId: string | null;
};

export function useAgentController(
  { sessions, activeTabId }: UseAgentControllerInput,
  dependencies: AgentControllerDependencies = defaultDependencies,
) {
  const [state, dispatch] = useReducer(agentReducer, undefined, createAgentState);
  const stateRef = useRef(state);
  stateRef.current = state;
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;

  const [configs, setConfigs] = useState<ModelApiConfig[]>([]);
  const configsRef = useRef(configs);
  configsRef.current = configs;
  const [configsLoading, setConfigsLoading] = useState(true);
  const [configsError, setConfigsError] = useState<AgentCommandError | null>(null);
  const [providerMutationError, setProviderMutationError] =
    useState<ProviderMutationFailure | null>(null);
  const turnReservationsRef = useRef(new Set<string>());
  // 每个标签页拥有自己的网络请求，切换可见标签不改变取消目标。
  const turnControllersRef = useRef(new Map<string, AbortController>());
  // 独立 HTTP 与 SSE 可能先后到达；同步记录避免依赖 React render 时序。
  const approvalOutcomesRef = useRef(new Map<string, { tabId: string; requestToken: string; status: "APPROVED" | "REJECTED" }>());
  const recordApprovalOutcome = useCallback((tabId: string, requestToken: string, approvalId: string, status: "APPROVED" | "REJECTED") => {
    const previous = approvalOutcomesRef.current.get(approvalId);
    if (previous && previous.requestToken === requestToken && previous.status !== status) {
      throw Object.assign(new Error("Approval HTTP and SSE decisions conflict."), { code: "BACKEND_AGENT_STREAM_INVALID" });
    }
    approvalOutcomesRef.current.set(approvalId, { tabId, requestToken, status });
  }, []);

  useEffect(() => {
    const controllers = turnControllersRef.current;
    return () => {
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      approvalOutcomesRef.current.clear();
    };
  }, []);

  const refreshConfigs = useCallback(async (): Promise<ModelApiConfig[]> => {
    setConfigsLoading(true);
    setConfigsError(null);
    try {
      const next = await dependencies.api.listModelApiConfigs();
      const enabledIds = new Set(
        next
          .filter((item) => item.enabled)
          .map((item) => item.api_config_id),
      );
      setConfigs(next);
      configsRef.current = next;

      const preferences = useAgentPreferencesStore.getState();
      if (
        preferences.preferredApiConfigId !== null &&
        !enabledIds.has(preferences.preferredApiConfigId)
      ) {
        preferences.setPreferredApiConfigId(null);
      }
      const invalidSelectedIds = new Set(
        Object.values(stateRef.current.tabs)
          .map((tab) => tab.selectedApiConfigId)
          .filter(
            (id): id is string => id !== null && !enabledIds.has(id),
          ),
      );
      for (const apiConfigId of invalidSelectedIds) {
        dispatch({ type: "provider/invalidate", apiConfigId });
      }
      return next;
    } catch (error) {
      const normalized = normalizeAgentCommandError(error);
      useAgentPreferencesStore.getState().setPreferredApiConfigId(null);
      setConfigsError(normalized);
      throw normalized;
    } finally {
      setConfigsLoading(false);
    }
  }, [dependencies.api]);

  useEffect(() => {
    void refreshConfigs().catch(() => undefined);
  }, [refreshConfigs]);

  const ensureTab = useCallback((tabId: string) => {
    const preferred = useAgentPreferencesStore.getState().preferredApiConfigId;
    const selectedApiConfigId = configsRef.current.some(
      (config) =>
        config.enabled && config.api_config_id === preferred,
    )
      ? preferred
      : null;
    dispatch({ type: "tab/ensure", tabId, selectedApiConfigId });
  }, []);

  const removeTab = useCallback((tabId: string) => {
    turnControllersRef.current.get(tabId)?.abort();
    turnControllersRef.current.delete(tabId);
    turnReservationsRef.current.delete(tabId);
    for (const [id, outcome] of approvalOutcomesRef.current) {
      if (outcome.tabId === tabId) approvalOutcomesRef.current.delete(id);
    }
    dispatch({ type: "tab/remove", tabId });
  }, []);

  const changeDraft = useCallback((tabId: string, value: string) => {
    dispatch({ type: "draft/change", tabId, value });
  }, []);

  const selectProvider = useCallback(
    (tabId: string, apiConfigId: string | null) => {
      if (
        apiConfigId !== null &&
        !configsRef.current.some(
          (config) =>
            config.enabled && config.api_config_id === apiConfigId,
        )
      ) {
        dispatch({
          type: "error/set",
          tabId,
          error: uiError("UI_AGENT_PROVIDER_UNAVAILABLE"),
        });
        return;
      }
      dispatch({ type: "provider/select", tabId, apiConfigId });
    },
    [],
  );

  const dispatchTurn = useCallback(
    async (
      tabId: string,
      sshSessionId: string,
      expectedPhase: "IDLE" | "AWAITING_RISK_CONFIRMATION",
      retry = false,
    ): Promise<void> => {
      const tab = stateRef.current.tabs[tabId];
      if (
        !tab ||
        tab.phase !== expectedPhase ||
        tab.selectedApiConfigId === null ||
        turnReservationsRef.current.has(tabId)
      ) {
        return;
      }
      const retryUser = retry ? lastRetryableUser(tab) : null;
      if (retry && !retryUser) return;
      // React 尚未发布 RUNNING，因此同步预留状态，
      // 避免第二次 Enter 或点击为同一标签页启动另一轮。
      turnReservationsRef.current.add(tabId);

      try {
        let latest: ModelApiConfig[];
        try {
          latest = await refreshConfigs();
        } catch {
          return;
        }
        const config = latest.find(
          (item) =>
            item.api_config_id === tab.selectedApiConfigId && item.enabled,
        );
        if (!config) {
          dispatch({
            type: "error/set",
            tabId,
            error: uiError("UI_AGENT_PROVIDER_UNAVAILABLE"),
          });
          return;
        }

        const currentSession = sessionsRef.current.find(
          (item) => item.tabId === tabId,
        );
        if (
          stateRef.current.tabs[tabId] === undefined ||
          stateRef.current.tabs[tabId].messages !== tab.messages ||
          stateRef.current.tabs[tabId].conversationId !== tab.conversationId ||
          !currentSession ||
          currentSession.state !== "CONNECTED" ||
          currentSession.sshSessionId !== sshSessionId
        ) {
          if (stateRef.current.tabs[tabId] !== undefined) {
            dispatch({
              type: "error/set",
              tabId,
              error: uiError("UI_AGENT_ACTIVE_SESSION_REQUIRED"),
            });
          }
          return;
        }

        const requestToken = dependencies.makeId();
        const userMessageId = retryUser?.id ?? dependencies.makeId();
        const userMessage = retryUser?.text ?? tab.draft;
        const conversationId = tab.conversationId;
        const snapshot: ProviderSnapshot = {
          apiConfigId: config.api_config_id,
          displayName: config.display_name,
          apiType: config.api_type,
          baseUrl: config.base_url,
          model: config.model,
          updatedAt: config.updated_at,
        };
        const controller = new AbortController();
        turnControllersRef.current.set(tabId, controller);
        for (const [id, outcome] of approvalOutcomesRef.current) {
          if (outcome.tabId === tabId) approvalOutcomesRef.current.delete(id);
        }
        // 跨越每标签页流边界前冻结已验证的 Provider 和 Session 标识；
        // 完成处理由 reducer token 管理。
        dispatch({
          type: "run/start",
          tabId,
          requestToken,
          sshSessionId,
          provider: snapshot,
          userMessageId,
          userMessage,
          retry,
        });
        useAgentPreferencesStore
          .getState()
          .setPreferredApiConfigId(config.api_config_id);

        try {
          const terminal = await dependencies.api.streamAgentTurn(
            {
              conversationId,
              sshSessionId,
              apiConfigId: config.api_config_id,
              userMessage,
              userMessageId,
              retry,
            },
            (event) => {
              if (controller.signal.aborted) return;
              if (event.type === "agent.turn.started") {
                dispatch({
                  type: "run/stream-started",
                  tabId,
                  requestToken,
                  event,
                });
              } else if (event.type === "agent.turn.approval_requested") {
                dispatch({ type: "run/approval-requested", tabId, requestToken, event });
              } else if (event.type === "agent.turn.approval_resolved") {
                if (event.status !== "INVALIDATED") recordApprovalOutcome(tabId, requestToken, event.approval_id, event.status);
                dispatch({ type: "run/approval-resolved", tabId, requestToken, event });
              } else if (event.type === "agent.turn.tool_started") {
                dispatch({ type: "run/tool-started", tabId, requestToken, event });
              } else if (event.type === "agent.turn.text_replace") {
                dispatch({ type: "run/text-replace", tabId, requestToken, event });
              } else {
                dispatch({
                  type: "run/text-delta",
                  tabId,
                  requestToken,
                  event,
                });
              }
            },
            controller.signal,
          );
          if (terminal.type === "agent.turn.completed") {
            dispatch({
              type: "run/complete",
              tabId,
              requestToken,
              event: terminal,
              messageId: dependencies.makeId(),
            });
          } else {
            dispatch({
              type: "run/fail",
              tabId,
              requestToken,
              event: terminal,
              error: {
                code: terminal.error_code,
                message: terminal.message,
              },
              messageId: dependencies.makeId(),
            });
          }
        } catch (error) {
          if (error instanceof AgentTurnCancelled) {
            dispatch({ type: "run/cancel", tabId, requestToken, messageId: dependencies.makeId() });
            return;
          }
          dispatch({
            type: "run/fail",
            tabId,
            requestToken,
            event: null,
            error: normalizeAgentCommandError(error),
            messageId: dependencies.makeId(),
          });
        } finally {
          if (turnControllersRef.current.get(tabId) === controller) {
            turnControllersRef.current.delete(tabId);
          }
        }
      } finally {
        turnReservationsRef.current.delete(tabId);
      }
    },
    [dependencies, refreshConfigs, recordApprovalOutcome],
  );

  const requestSend = useCallback(
    async (tabId: string): Promise<void> => {
      const tab = stateRef.current.tabs[tabId];
      if (!tab || tab.phase !== "IDLE") return;
      const session = sessionsRef.current.find((item) => item.tabId === tabId);
      if (
        !session ||
        session.state !== "CONNECTED" ||
        session.sshSessionId === null
      ) {
        dispatch({
          type: "error/set",
          tabId,
          error: uiError("UI_AGENT_ACTIVE_SESSION_REQUIRED"),
        });
        return;
      }
      if ([...tab.draft].length < 1 || [...tab.draft].length > 65_536) {
        dispatch({
          type: "error/set",
          tabId,
          error: uiError("UI_AGENT_MESSAGE_INVALID"),
        });
        return;
      }
      if (tab.selectedApiConfigId === null) {
        dispatch({
          type: "error/set",
          tabId,
          error: uiError("UI_AGENT_PROVIDER_REQUIRED"),
        });
        return;
      }
      await dispatchTurn(tabId, session.sshSessionId, "IDLE");
    },
    [dispatchTurn],
  );

  const approvalSubmissionsRef = useRef(new Set<string>());
  const retryLastTurn = useCallback(async (tabId: string): Promise<void> => {
    const tab = stateRef.current.tabs[tabId];
    if (!tab || !lastRetryableUser(tab)) return;
    const session = sessionsRef.current.find(item => item.tabId === tabId);
    if (!session || session.state !== "CONNECTED" || session.sshSessionId === null) {
      dispatch({ type: "error/set", tabId, error: uiError("UI_AGENT_ACTIVE_SESSION_REQUIRED") });
      return;
    }
    if (tab.selectedApiConfigId === null) {
      dispatch({ type: "error/set", tabId, error: uiError("UI_AGENT_PROVIDER_REQUIRED") });
      return;
    }
    await dispatchTurn(tabId, session.sshSessionId, "IDLE", true);
  }, [dispatchTurn]);
  const decideApproval = useCallback(async (tabId: string, approvalId: string, decision: "approve" | "reject") => {
    const tab = stateRef.current.tabs[tabId];
    const message = tab?.messages.find(item => item.kind === "approval" && item.id === approvalId);
    if (!tab?.activeRun || tab.phase !== "RUNNING" || message?.kind !== "approval" || message.status !== "PENDING" ||
        message.submitting || approvalSubmissionsRef.current.has(approvalId)) return;
    const requestToken = tab.activeRun.requestToken;
    const request = message.request;
    if (request.agent_run_id !== tab.activeRun.agentRunId) return;
    approvalSubmissionsRef.current.add(approvalId);
    dispatch({ type: "approval/update", tabId, requestToken, approvalId, submitting: true, error: null });
    try {
      const result = await dependencies.api.decideAgentApproval(approvalId, {
        conversation_id: request.conversation_id, agent_run_id: request.agent_run_id,
        ssh_session_id: request.ssh_session_id, tool_call_id: request.tool_call_id, decision,
      }, turnControllersRef.current.get(tabId)?.signal);
      const current = stateRef.current.tabs[tabId];
      if (!current || current.activeRun && current.activeRun.requestToken !== requestToken) return;
      recordApprovalOutcome(tabId, requestToken, approvalId, result.status);
      dispatch({ type: "approval/update", tabId, requestToken, approvalId, submitting: false, status: result.status, error: null });
    } catch (error) {
      const normalized = normalizeAgentCommandError(error);
      if (normalized.code === "BACKEND_AGENT_STREAM_INVALID") {
        dispatch({ type: "approval/protocol-error", tabId, approvalId, agentRunId: request.agent_run_id, error: normalized });
        if (stateRef.current.tabs[tabId]?.activeRun?.requestToken === requestToken) {
          dispatch({ type: "run/fail", tabId, requestToken, event: null, error: normalized, messageId: dependencies.makeId() });
          turnControllersRef.current.get(tabId)?.abort();
        }
      }
      const inactive = ["AGENT_APPROVAL_NOT_FOUND", "AGENT_APPROVAL_INACTIVE", "AGENT_APPROVAL_CONFLICT"].includes(normalized.code);
      const retryable = ["REQUEST_CAPACITY_EXCEEDED", "REQUEST_VALIDATION_FAILED"].includes(normalized.code);
      dispatch({ type: "approval/update", tabId, requestToken, approvalId, submitting: false,
        status: inactive ? "INVALIDATED" : retryable ? "PENDING" : "UNKNOWN", error: normalized });
    } finally {
      approvalSubmissionsRef.current.delete(approvalId);
    }
  }, [dependencies, recordApprovalOutcome]);
  const cancelTurn = useCallback((tabId: string) => {
    // 等待网络读取退出后再收敛 UI；不提前把本轮标记为服务端 CANCELLED。
    turnControllersRef.current.get(tabId)?.abort();
  }, []);
  const resetConversation = useCallback((tabId: string) => {
    dispatch({ type: "conversation/reset", tabId });
  }, []);
  const markRead = useCallback((tabId: string) => {
    dispatch({ type: "background/read", tabId });
  }, []);

  const activeApiConfigIds = useMemo(
    () =>
      new Set(
        Object.values(state.tabs)
          .filter(
            (tab) => tab.phase === "RUNNING" && tab.activeRun !== null,
          )
          .map((tab) => tab.activeRun!.provider.apiConfigId),
      ),
    [state.tabs],
  );

  const runProviderMutation = useCallback(
    async (
      mutation: () => Promise<object>,
    ): Promise<void> => {
      setProviderMutationError(null);
      try {
        await mutation();
        await refreshConfigs();
      } catch (error) {
        const failure = providerFailure(error);
        setProviderMutationError(failure);
        throw failure;
      }
    },
    [refreshConfigs],
  );

  const createProvider = useCallback(
    (draft: ProviderDraft, apiKey: string) =>
      runProviderMutation(() =>
        createProviderConfig(dependencies.api, draft, apiKey),
      ),
    [dependencies.api, runProviderMutation],
  );

  const updateProvider = useCallback(
    async (
      config: ModelApiConfig,
      draft: ProviderDraft,
      apiKey: string,
    ) => {
      if (activeApiConfigIds.has(config.api_config_id)) {
        const failure = new ProviderMutationFailure(
          uiError("UI_MODEL_API_CONFIG_ACTIVE_RUN"),
        );
        setProviderMutationError(failure);
        throw failure;
      }
      await runProviderMutation(() =>
        updateProviderConfig(dependencies.api, config, draft, apiKey),
      );
    },
    [activeApiConfigIds, dependencies.api, runProviderMutation],
  );

  const deleteProvider = useCallback(
    async (config: ModelApiConfig) => {
      if (activeApiConfigIds.has(config.api_config_id)) {
        const failure = new ProviderMutationFailure(
          uiError("UI_MODEL_API_CONFIG_ACTIVE_RUN"),
        );
        setProviderMutationError(failure);
        throw failure;
      }
      await runProviderMutation(() =>
        deleteProviderConfig(dependencies.api, config),
      );
    },
    [activeApiConfigIds, dependencies.api, runProviderMutation],
  );

  const backgroundByTab = useMemo(
    () => agentBackgroundByTab(state),
    [state],
  );
  const aggregateBackground = useMemo(
    () => aggregateAgentBackground(backgroundByTab),
    [backgroundByTab],
  );
  const activeAgentRunTabIds = useMemo(
    () =>
      new Set(
        Object.entries(state.tabs)
          .filter(([, tab]) => tab.phase === "RUNNING")
          .map(([tabId]) => tabId),
      ),
    [state.tabs],
  );
  const activeAgentRunCount = activeAgentRunTabIds.size;
  const hasActiveRunForTab = useCallback(
    (tabId: string) => stateRef.current.tabs[tabId]?.phase === "RUNNING",
    [],
  );
  const hasActiveRunForSession = useCallback(
    (sshSessionId: string) =>
      isActiveRunForSession(stateRef.current, sshSessionId),
    [],
  );

  return {
    state,
    activeTab: activeTabId ? (state.tabs[activeTabId] ?? null) : null,
    configs,
    configsLoading,
    configsError,
    providerMutationError,
    backgroundByTab,
    aggregateBackground,
    activeAgentRunTabIds,
    activeAgentRunCount,
    activeApiConfigIds,
    ensureTab,
    removeTab,
    changeDraft,
    selectProvider,
    requestSend,
    retryLastTurn,
    decideApproval,
    cancelTurn,
    resetConversation,
    markRead,
    refreshConfigs,
    createProvider,
    updateProvider,
    deleteProvider,
    hasActiveRunForTab,
    hasActiveRunForSession,
  };
}
