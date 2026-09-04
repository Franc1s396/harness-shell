import type {
  AgentCommandError,
  AgentRunStatus,
  AgentTurnCompletedEvent,
  AgentTurnFailedEvent,
  AgentTurnStartedEvent,
  AgentTurnTextDeltaEvent,
  ApiType,
} from "../../api/agent";

export type ProviderSnapshot = {
  apiConfigId: string;
  displayName: string;
  apiType: ApiType;
  baseUrl: string;
  model: string;
  updatedAt: string;
};

export type AgentRunProjection = {
  agentRunId: string;
  status: AgentRunStatus;
  reactIteration: number;
  sshSessionId: string;
  provider: ProviderSnapshot;
};

export type AgentUiMessage =
  | { id: string; kind: "user"; text: string }
  | {
      id: string;
      kind: "assistant";
      text: string;
      run: AgentRunProjection;
    }
  | {
      id: string;
      kind: "error";
      error: AgentCommandError;
      run: AgentRunProjection | null;
    };

export type AgentBackgroundState =
  | "NONE"
  | "RUNNING"
  | "COMPLETED_UNREAD"
  | "FAILED_UNREAD";

export type AgentTabState = {
  conversationId: string | null;
  messages: AgentUiMessage[];
  draft: string;
  phase: "IDLE" | "AWAITING_RISK_CONFIRMATION" | "RUNNING";
  selectedApiConfigId: string | null;
  activeRun: {
    requestToken: string;
    sshSessionId: string;
    provider: ProviderSnapshot;
    conversationId: string | null;
    agentRunId: string | null;
    nextSequence: number;
    streamedText: string;
    reactIteration: number;
  } | null;
  pendingRiskSshSessionId: string | null;
  riskAcknowledgedSshSessionId: string | null;
  lastError: AgentCommandError | null;
  backgroundState: AgentBackgroundState;
};

export type AgentState = {
  tabs: Record<string, AgentTabState>;
};

export const createAgentState = (): AgentState => ({ tabs: {} });

export const createAgentTabState = (
  selectedApiConfigId: string | null,
): AgentTabState => ({
  conversationId: null,
  messages: [],
  draft: "",
  phase: "IDLE",
  selectedApiConfigId,
  activeRun: null,
  pendingRiskSshSessionId: null,
  riskAcknowledgedSshSessionId: null,
  lastError: null,
  backgroundState: "NONE",
});

export type AgentAction =
  | {
      type: "tab/ensure";
      tabId: string;
      selectedApiConfigId: string | null;
    }
  | { type: "tab/remove"; tabId: string }
  | { type: "draft/change"; tabId: string; value: string }
  | {
      type: "provider/select";
      tabId: string;
      apiConfigId: string | null;
    }
  | { type: "provider/invalidate"; apiConfigId: string }
  | { type: "risk/request"; tabId: string; sshSessionId: string }
  | { type: "risk/acknowledge"; tabId: string; sshSessionId: string }
  | { type: "risk/cancel"; tabId: string }
  | { type: "error/set"; tabId: string; error: AgentCommandError }
  | { type: "error/clear"; tabId: string }
  | {
      type: "run/start";
      tabId: string;
      requestToken: string;
      sshSessionId: string;
      provider: ProviderSnapshot;
      userMessageId: string;
      userMessage: string;
    }
  | {
      type: "run/stream-started";
      tabId: string;
      requestToken: string;
      event: AgentTurnStartedEvent;
    }
  | {
      type: "run/text-delta";
      tabId: string;
      requestToken: string;
      event: AgentTurnTextDeltaEvent;
    }
  | {
      type: "run/complete";
      tabId: string;
      requestToken: string;
      event: AgentTurnCompletedEvent;
      messageId: string;
    }
  | {
      type: "run/fail";
      tabId: string;
      requestToken: string;
      event: AgentTurnFailedEvent | null;
      error: AgentCommandError;
      messageId: string;
    }
  | { type: "conversation/reset"; tabId: string }
  | { type: "background/read"; tabId: string };

const updateTab = (
  state: AgentState,
  tabId: string,
  update: (tab: AgentTabState) => AgentTabState,
): AgentState => {
  const tab = state.tabs[tabId];
  if (!tab) return state;
  const next = update(tab);
  if (next === tab) return state;
  return { ...state, tabs: { ...state.tabs, [tabId]: next } };
};

const mapTabs = (
  state: AgentState,
  update: (tab: AgentTabState) => AgentTabState,
): AgentState => ({
  ...state,
  tabs: Object.fromEntries(
    Object.entries(state.tabs).map(([tabId, tab]) => [tabId, update(tab)]),
  ),
});

const activeRequestMatches = (
  tab: AgentTabState,
  requestToken: string,
) =>
  tab.activeRun?.requestToken === requestToken && tab.phase === "RUNNING";

const streamEventMatches = (
  tab: AgentTabState,
  requestToken: string,
  event: AgentTurnTextDeltaEvent | AgentTurnCompletedEvent | AgentTurnFailedEvent,
): boolean =>
  activeRequestMatches(tab, requestToken) &&
  tab.activeRun?.conversationId === event.conversation_id &&
  tab.activeRun.agentRunId === event.agent_run_id &&
  tab.activeRun.nextSequence === event.sequence;

export const agentReducer = (
  state: AgentState,
  action: AgentAction,
): AgentState => {
  switch (action.type) {
    case "tab/ensure":
      if (state.tabs[action.tabId]) return state;
      return {
        ...state,
        tabs: {
          ...state.tabs,
          [action.tabId]: createAgentTabState(action.selectedApiConfigId),
        },
      };
    case "tab/remove": {
      if (!state.tabs[action.tabId]) return state;
      const { [action.tabId]: _removed, ...tabs } = state.tabs;
      return { ...state, tabs };
    }
    case "draft/change":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "IDLE"
          ? { ...tab, draft: action.value, lastError: null }
          : tab,
      );
    case "provider/select":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "IDLE"
          ? {
              ...tab,
              selectedApiConfigId: action.apiConfigId,
              lastError: null,
            }
          : tab,
      );
    case "provider/invalidate":
      return mapTabs(state, (tab) =>
        tab.phase === "IDLE" &&
        tab.selectedApiConfigId === action.apiConfigId
          ? { ...tab, selectedApiConfigId: null }
          : tab,
      );
    case "risk/request":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "IDLE"
          ? {
              ...tab,
              phase: "AWAITING_RISK_CONFIRMATION",
              pendingRiskSshSessionId: action.sshSessionId,
              lastError: null,
            }
          : tab,
      );
    case "risk/acknowledge":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "RUNNING"
          ? tab
          : {
              ...tab,
              riskAcknowledgedSshSessionId: action.sshSessionId,
              lastError: null,
            },
      );
    case "risk/cancel":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "AWAITING_RISK_CONFIRMATION"
          ? {
              ...tab,
              phase: "IDLE",
              pendingRiskSshSessionId: null,
            }
          : tab,
      );
    case "error/set":
      return updateTab(state, action.tabId, (tab) => ({
        ...tab,
        lastError: action.error,
      }));
    case "error/clear":
      return updateTab(state, action.tabId, (tab) =>
        tab.lastError === null ? tab : { ...tab, lastError: null },
      );
    case "run/start":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "RUNNING"
          ? tab
          : {
              ...tab,
              messages: [
                ...tab.messages,
                {
                  id: action.userMessageId,
                  kind: "user",
                  text: action.userMessage,
                },
              ],
              draft: "",
              phase: "RUNNING",
              activeRun: {
                requestToken: action.requestToken,
                sshSessionId: action.sshSessionId,
                provider: action.provider,
                conversationId: tab.conversationId,
                agentRunId: null,
                nextSequence: 0,
                streamedText: "",
                reactIteration: 0,
              },
              pendingRiskSshSessionId: null,
              lastError: null,
              backgroundState: "RUNNING",
            },
      );
    case "run/stream-started":
      return updateTab(state, action.tabId, (tab) => {
        if (!activeRequestMatches(tab, action.requestToken)) return tab;
        const activeRun = tab.activeRun!;
        if (
          activeRun.agentRunId !== null ||
          activeRun.nextSequence !== 0 ||
          action.event.sequence !== 0 ||
          (activeRun.conversationId !== null &&
            activeRun.conversationId !== action.event.conversation_id)
        ) {
          return tab;
        }
        return {
          ...tab,
          activeRun: {
            ...activeRun,
            conversationId: action.event.conversation_id,
            agentRunId: action.event.agent_run_id,
            nextSequence: 1,
          },
        };
      });
    case "run/text-delta":
      return updateTab(state, action.tabId, (tab) => {
        if (!streamEventMatches(tab, action.requestToken, action.event)) return tab;
        const activeRun = tab.activeRun!;
        return {
          ...tab,
          activeRun: {
            ...activeRun,
            nextSequence: activeRun.nextSequence + 1,
            streamedText: activeRun.streamedText + action.event.delta,
          },
        };
      });
    case "run/complete":
      return updateTab(state, action.tabId, (tab) => {
        if (!streamEventMatches(tab, action.requestToken, action.event)) return tab;
        const activeRun = tab.activeRun!;
        const run: AgentRunProjection = {
          agentRunId: action.event.agent_run_id,
          status: action.event.status,
          reactIteration: action.event.react_iteration,
          sshSessionId: activeRun.sshSessionId,
          provider: activeRun.provider,
        };
        const message: AgentUiMessage = {
          id: action.messageId,
          kind: "assistant",
          text: activeRun.streamedText,
          run,
        };
        return {
          ...tab,
          conversationId: action.event.conversation_id,
          messages: [...tab.messages, message],
          phase: "IDLE",
          activeRun: null,
          lastError: null,
          backgroundState: "COMPLETED_UNREAD",
        };
      });
    case "run/fail":
      return updateTab(state, action.tabId, (tab) => {
        if (!activeRequestMatches(tab, action.requestToken)) return tab;
        if (
          action.event !== null &&
          !streamEventMatches(tab, action.requestToken, action.event)
        ) {
          return tab;
        }
        const activeRun = tab.activeRun!;
        const run: AgentRunProjection | null = action.event === null
          ? null
          : {
              agentRunId: action.event.agent_run_id,
              status: action.event.status,
              reactIteration: action.event.react_iteration,
              sshSessionId: activeRun.sshSessionId,
              provider: activeRun.provider,
            };
        return {
          ...tab,
          conversationId: action.event?.conversation_id ?? tab.conversationId,
          messages: [
            ...tab.messages,
            {
              id: action.messageId,
              kind: "error",
              error: action.error,
              run,
            },
          ],
          phase: "IDLE",
          activeRun: null,
          lastError: action.error,
          backgroundState: "FAILED_UNREAD",
        };
      });
    case "conversation/reset":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "IDLE"
          ? {
              ...createAgentTabState(tab.selectedApiConfigId),
              riskAcknowledgedSshSessionId:
                tab.riskAcknowledgedSshSessionId,
            }
          : tab,
      );
    case "background/read":
      return updateTab(state, action.tabId, (tab) =>
        tab.backgroundState === "COMPLETED_UNREAD" ||
        tab.backgroundState === "FAILED_UNREAD"
          ? { ...tab, backgroundState: "NONE" }
          : tab,
      );
  }
};

export const agentBackgroundByTab = (
  state: AgentState,
): Readonly<Record<string, AgentBackgroundState>> =>
  Object.fromEntries(
    Object.entries(state.tabs).map(([tabId, tab]) => [
      tabId,
      tab.backgroundState,
    ]),
  );

export const aggregateAgentBackground = (
  states: Readonly<Record<string, AgentBackgroundState>>,
): AgentBackgroundState => {
  const values = Object.values(states);
  if (values.includes("FAILED_UNREAD")) return "FAILED_UNREAD";
  if (values.includes("COMPLETED_UNREAD")) return "COMPLETED_UNREAD";
  if (values.includes("RUNNING")) return "RUNNING";
  return "NONE";
};

export const isActiveRunForSession = (
  state: AgentState,
  sshSessionId: string,
): boolean =>
  Object.values(state.tabs).some(
    (tab) =>
      tab.phase === "RUNNING" &&
      tab.activeRun?.sshSessionId === sshSessionId,
  );
