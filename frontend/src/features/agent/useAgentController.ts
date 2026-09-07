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

  useEffect(() => {
    const controllers = turnControllersRef.current;
    return () => {
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
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
        const userMessageId = dependencies.makeId();
        const userMessage = tab.draft;
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
    [dependencies, refreshConfigs],
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
      if (tab.riskAcknowledgedSshSessionId !== session.sshSessionId) {
        dispatch({
          type: "risk/request",
          tabId,
          sshSessionId: session.sshSessionId,
        });
        return;
      }
      await dispatchTurn(tabId, session.sshSessionId, "IDLE");
    },
    [dispatchTurn],
  );

  const confirmRiskAndSend = useCallback(
    async (tabId: string): Promise<void> => {
      const tab = stateRef.current.tabs[tabId];
      if (!tab || tab.phase !== "AWAITING_RISK_CONFIRMATION") return;
      const session = sessionsRef.current.find((item) => item.tabId === tabId);
      if (
        !session ||
        session.state !== "CONNECTED" ||
        session.sshSessionId === null ||
        session.sshSessionId !== tab.pendingRiskSshSessionId
      ) {
        dispatch({ type: "risk/cancel", tabId });
        dispatch({
          type: "error/set",
          tabId,
          error: uiError("UI_AGENT_ACTIVE_SESSION_REQUIRED"),
        });
        return;
      }
      dispatch({
        type: "risk/acknowledge",
        tabId,
        sshSessionId: session.sshSessionId,
      });
      await dispatchTurn(
        tabId,
        session.sshSessionId,
        "AWAITING_RISK_CONFIRMATION",
      );
    },
    [dispatchTurn],
  );

  const cancelRisk = useCallback((tabId: string) => {
    dispatch({ type: "risk/cancel", tabId });
  }, []);
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
    confirmRiskAndSend,
    cancelRisk,
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
