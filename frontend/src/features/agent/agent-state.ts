import type {
  AgentCommandError,
  AgentTurnApprovalRequestedEvent,
  AgentTurnApprovalResolvedEvent,
  AgentExecutedTool,
  AgentRunStatus,
  AgentTurnCompletedEvent,
  AgentTurnFailedEvent,
  AgentTurnStartedEvent,
  AgentTurnTextDeltaEvent,
  AgentTurnTextReplaceEvent,
  AgentTurnToolStartedEvent,
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

export type AgentApprovalMessage = {
  id: string; kind: "approval"; request: AgentTurnApprovalRequestedEvent;
  status: "PENDING" | "APPROVED" | "REJECTED" | "INVALIDATED" | "UNKNOWN";
  submitting: boolean; error: AgentCommandError | null;
};

export type AgentUiMessage =
  | AgentApprovalMessage
  | { id: string; kind: "user"; text: string }
  | { id: string; kind: "cancelled"; approvalIds?: string[] }
  | {
      id: string;
      kind: "assistant";
      approvalIds?: string[];
      tools: AgentExecutedTool[];
      text: string;
      run: AgentRunProjection;
    }
  | {
      id: string;
      kind: "error";
      approvalIds?: string[];
      error: AgentCommandError;
      run: AgentRunProjection | null;
    };

export type AgentBackgroundState =
  | "NONE"
  | "AWAITING_APPROVAL"
  | "RUNNING"
  | "COMPLETED_UNREAD"
  | "FAILED_UNREAD";

export type AgentTabState = {
  conversationId: string | null;
  messages: AgentUiMessage[];
  draft: string;
  phase: "IDLE" | "RUNNING";
  selectedApiConfigId: string | null;
  activeRun: {
    requestToken: string;
    sshSessionId: string;
    provider: ProviderSnapshot;
    conversationId: string | null;
    agentRunId: string | null;
    nextSequence: number;
    streamedText: string;
    toolExecuting: boolean;
    tools: AgentExecutedTool[];
    reactIteration: number;
  } | null;
  lastError: AgentCommandError | null;
  backgroundState: AgentBackgroundState;
};

export type AgentState = {
  tabs: Record<string, AgentTabState>;
};

export const createAgentState = (): AgentState => ({ tabs: {} });

/** 只有末尾可见回复结束后才能重试，审核元数据不充当回复。 */
export function lastRetryableUser(tab: AgentTabState) {
  if (tab.phase !== "IDLE") return null;
  const visible = tab.messages.filter(message => message.kind !== "approval");
  const last = visible[visible.length - 1];
  if (!last || last.kind === "user") return null;
  return [...visible].reverse().find(message => message.kind === "user") ?? null;
}

export const createAgentTabState = (
  selectedApiConfigId: string | null,
): AgentTabState => ({
  conversationId: null,
  messages: [],
  draft: "",
  phase: "IDLE",
  selectedApiConfigId,
  activeRun: null,
  lastError: null,
  backgroundState: "NONE",
});

export type AgentAction =
  | { type: "run/approval-requested"; tabId: string; requestToken: string; event: AgentTurnApprovalRequestedEvent }
  | { type: "run/approval-resolved"; tabId: string; requestToken: string; event: AgentTurnApprovalResolvedEvent }
  | { type: "approval/protocol-error"; tabId: string; approvalId: string; agentRunId: string; error: AgentCommandError }
  | { type: "approval/update"; tabId: string; requestToken: string; approvalId: string; submitting: boolean; status?: AgentApprovalMessage["status"]; error: AgentCommandError | null }
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
      retry?: boolean;
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
      type: "run/tool-started";
      tabId: string;
      requestToken: string;
      event: AgentTurnToolStartedEvent;
    }
  | {
      type: "run/text-replace";
      tabId: string;
      requestToken: string;
      event: AgentTurnTextReplaceEvent;
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
  | { type: "run/cancel"; tabId: string; requestToken: string; messageId: string }
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
  event: AgentTurnApprovalRequestedEvent | AgentTurnApprovalResolvedEvent | AgentTurnToolStartedEvent | AgentTurnTextDeltaEvent | AgentTurnTextReplaceEvent | AgentTurnCompletedEvent | AgentTurnFailedEvent,
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
              messages: action.retry ? tab.messages.slice(0, tab.messages.findIndex(message => message.id === action.userMessageId) + 1) : [
                ...tab.messages,
                {
                  id: action.userMessageId,
                  kind: "user",
                  text: action.userMessage,
                },
              ],
              draft: action.retry ? tab.draft : "",
              phase: "RUNNING",
              activeRun: {
                requestToken: action.requestToken,
                sshSessionId: action.sshSessionId,
                provider: action.provider,
                conversationId: tab.conversationId,
                agentRunId: null,
                nextSequence: 0,
                streamedText: "",
                toolExecuting: false,
                tools: [],
                reactIteration: 0,
              },
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
    case "run/approval-requested":
      return updateTab(state, action.tabId, tab => {
        if (!streamEventMatches(tab, action.requestToken, action.event)) return tab;
        return { ...tab, backgroundState: "AWAITING_APPROVAL", messages: [...tab.messages, { id: action.event.approval_id, kind: "approval",
          request: action.event, status: "PENDING", submitting: false, error: null }],
          activeRun: { ...tab.activeRun!, toolExecuting: false, nextSequence: tab.activeRun!.nextSequence + 1 } };
      });
    case "run/approval-resolved":
      return updateTab(state, action.tabId, tab => {
        if (!streamEventMatches(tab, action.requestToken, action.event)) return tab;
        return { ...tab, backgroundState: "RUNNING", messages: tab.messages.map(message => message.kind === "approval" && message.id === action.event.approval_id
          ? { ...message, status: action.event.status, submitting: false, error: null } : message),
          activeRun: { ...tab.activeRun!, nextSequence: tab.activeRun!.nextSequence + 1 } };
      });
    case "approval/protocol-error":
      return updateTab(state, action.tabId, tab => ({ ...tab,
        messages: tab.messages.map(message => message.kind === "approval" && message.id === action.approvalId &&
          message.request.agent_run_id === action.agentRunId ? { ...message, error: action.error, submitting: false } : message),
      }));
    case "approval/update":
      return updateTab(state, action.tabId, tab => {
        if (!activeRequestMatches(tab, action.requestToken)) return tab;
        return { ...tab, messages: tab.messages.map(message => {
          if (message.kind !== "approval" || message.id !== action.approvalId ||
              (message.status !== "PENDING" && message.status !== "UNKNOWN")) return message;
          return { ...message, status: action.status ?? message.status, submitting: action.submitting, error: action.error };
        }) };
      });
    case "run/tool-started":
    case "run/text-replace":
    case "run/text-delta":
      return updateTab(state, action.tabId, (tab) => {
        if (!streamEventMatches(tab, action.requestToken, action.event)) return tab;
        const activeRun = tab.activeRun!;
        return {
          ...tab,
          activeRun: {
            ...activeRun,
            nextSequence: activeRun.nextSequence + 1,
            // 工具提示持续至下一合法事件；连续工具事件继续显示。
            toolExecuting: action.type === "run/tool-started",
            tools: action.type === "run/tool-started"
              ? [...activeRun.tools, { tool_call_id: action.event.tool_call_id,
                  tool_name: action.event.tool_name, arguments: { ...action.event.arguments } }]
              : activeRun.tools,
            streamedText: action.type === "run/tool-started"
              ? activeRun.streamedText
              : action.type === "run/text-replace"
              ? action.event.text
              : activeRun.streamedText + action.event.delta,
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
          approvalIds: runApprovalIds(tab),
          tools: activeRun.tools,
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
            ...invalidateApprovals(tab.messages, action.event ? "INVALIDATED" : "UNKNOWN"),
            {
              id: action.messageId,
              kind: "error",
              approvalIds: runApprovalIds(tab),
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
    case "run/cancel":
      return updateTab(state, action.tabId, (tab) => {
        if (!activeRequestMatches(tab, action.requestToken)) return tab;
        return {
          ...tab,
          conversationId: tab.activeRun!.conversationId ?? tab.conversationId,
          messages: [...invalidateApprovals(tab.messages, "INVALIDATED"), { id: action.messageId, kind: "cancelled", approvalIds: runApprovalIds(tab) }],
          phase: "IDLE",
          activeRun: null,
          lastError: null,
          backgroundState: "NONE",
        };
      });
    case "conversation/reset":
      return updateTab(state, action.tabId, (tab) =>
        tab.phase === "IDLE"
          ? {
              ...createAgentTabState(tab.selectedApiConfigId),

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
  if (values.includes("AWAITING_APPROVAL")) return "AWAITING_APPROVAL";
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

function invalidateApprovals(messages: AgentUiMessage[], status: "INVALIDATED" | "UNKNOWN"): AgentUiMessage[] {
  return messages.map(message => message.kind === "approval" && (message.status === "PENDING" || message.status === "UNKNOWN")
    ? { ...message, status, submitting: false } : message);
}

/** 终态只保存本轮审核 ID；详情仍引用原记录，以保留晚到的决定响应。 */
function runApprovalIds(tab: AgentTabState): string[] {
  return tab.messages.filter(message => message.kind === "approval" &&
    message.request.agent_run_id === tab.activeRun?.agentRunId).map(message => message.id);
}
