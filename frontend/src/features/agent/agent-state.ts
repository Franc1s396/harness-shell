import type {
  AgentCommandError,
  AgentRunStatus,
  AgentTurnResult,
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
      type: "run/complete";
      tabId: string;
      requestToken: string;
      result: AgentTurnResult;
      messageId: string;
    }
  | {
      type: "run/fail";
      tabId: string;
      requestToken: string;
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

const resultErrorCode = (result: AgentTurnResult): string => {
  if (result.status === "COMPLETED" && result.final_text === null) {
    return "UI_AGENT_FINAL_TEXT_MISSING";
  }
  if (result.error_code) return result.error_code;
  if (result.status === "LIMIT_REACHED") return "REACT_LIMIT_REACHED";
  if (result.status === "CANCELLED") return "AGENT_CANCELLED";
  return "AGENT_TURN_FAILED";
};

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
              },
              pendingRiskSshSessionId: null,
              lastError: null,
              backgroundState: "RUNNING",
            },
      );
    case "run/complete":
      return updateTab(state, action.tabId, (tab) => {
        if (!activeRequestMatches(tab, action.requestToken)) return tab;
        const activeRun = tab.activeRun!;
        const run: AgentRunProjection = {
          agentRunId: action.result.agent_run_id,
          status: action.result.status,
          reactIteration: action.result.react_iteration,
          sshSessionId: activeRun.sshSessionId,
          provider: activeRun.provider,
        };
        const completed =
          action.result.status === "COMPLETED" &&
          action.result.final_text !== null;
        const errorCode = completed ? null : resultErrorCode(action.result);
        const message: AgentUiMessage = completed
          ? {
              id: action.messageId,
              kind: "assistant",
              text: action.result.final_text!,
              run,
            }
          : {
              id: action.messageId,
              kind: "error",
              error: { code: errorCode!, message: errorCode! },
              run,
            };
        return {
          ...tab,
          conversationId: action.result.conversation_id,
          messages: [...tab.messages, message],
          phase: "IDLE",
          activeRun: null,
          lastError: completed ? null : message.kind === "error" ? message.error : null,
          backgroundState: completed
            ? "COMPLETED_UNREAD"
            : "FAILED_UNREAD",
        };
      });
    case "run/fail":
      return updateTab(state, action.tabId, (tab) => {
        if (!activeRequestMatches(tab, action.requestToken)) return tab;
        return {
          ...tab,
          messages: [
            ...tab.messages,
            {
              id: action.messageId,
              kind: "error",
              error: action.error,
              run: null,
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
