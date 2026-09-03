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
      // Reserve synchronously because React has not published RUNNING yet; this
      // prevents a second Enter/click from starting another turn for the tab.
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
        // Freeze the verified Provider and Session identities before crossing
        // the non-streaming Promise boundary. The reducer token owns completion.
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
          const result = await dependencies.api.runAgentTurn({
            conversationId,
            sshSessionId,
            apiConfigId: config.api_config_id,
            userMessage,
          });
          dispatch({
            type: "run/complete",
            tabId,
            requestToken,
            result,
            messageId: dependencies.makeId(),
          });
        } catch (error) {
          dispatch({
            type: "run/fail",
            tabId,
            requestToken,
            error: normalizeAgentCommandError(error),
            messageId: dependencies.makeId(),
          });
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
