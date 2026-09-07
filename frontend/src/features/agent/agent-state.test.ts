import { describe, expect, it } from "vitest";

import {
  agentReducer,
  aggregateAgentBackground,
  createAgentState,
  isActiveRunForSession,
} from "./agent-state";

const provider = {
  apiConfigId: "config-1",
  displayName: "Production",
  apiType: "RESPONSES" as const,
  baseUrl: "https://api.example/v1",
  model: "gpt-5",
  updatedAt: "2026-08-31T00:00:00Z",
};

const conversationId = "conversation-1";
const agentRunId = "run-1";
const started = {
  schema_version: 1,
  type: "agent.turn.started",
  request_id: "request-id-1",
  sequence: 0,
  conversation_id: conversationId,
  agent_run_id: agentRunId,
  status: "RUNNING",
  react_iteration: 0,
} as const;
const delta = {
  schema_version: 1,
  type: "agent.turn.text_delta",
  request_id: "request-id-1",
  sequence: 1,
  conversation_id: conversationId,
  agent_run_id: agentRunId,
  delta: "hello",
} as const;
const completed = {
  schema_version: 1,
  type: "agent.turn.completed",
  request_id: "request-id-1",
  sequence: 2,
  conversation_id: conversationId,
  agent_run_id: agentRunId,
  status: "COMPLETED",
  react_iteration: 1,
  error_code: null,
} as const;

const startedState = (requestToken: string) => {
  let state = createAgentState();
  state = agentReducer(state, {
    type: "tab/ensure",
    tabId: "tab-a",
    selectedApiConfigId: "config-1",
  });
  return agentReducer(state, {
    type: "run/start",
    tabId: "tab-a",
    requestToken,
    sshSessionId: "ssh-a",
    provider,
    userMessageId: "user-1",
    userMessage: "inspect",
  });
};

describe("agentReducer", () => {
  it("keeps tool activity until the next valid event while preserving text", () => {
    let state = startedState("request-1");
    state = agentReducer(state, { type: "run/stream-started", tabId: "tab-a", requestToken: "request-1", event: started });
    state = agentReducer(state, { type: "run/text-delta", tabId: "tab-a", requestToken: "request-1", event: delta });
    const { delta: _delta, ...identity } = delta;
    const event = { ...identity, type: "agent.turn.tool_started", tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" }, sequence: 2 } as const;
    const action = { type: "run/tool-started", tabId: "tab-a", requestToken: "request-1", event } as const;
    state = agentReducer(state, action);
    expect(state.tabs["tab-a"].activeRun).toMatchObject({ toolExecuting: true, streamedText: "hello", nextSequence: 3 });
    expect(agentReducer(state, { ...action, requestToken: "old" })).toEqual(state);
    state = agentReducer(state, { ...action, event: { ...event, sequence: 3 } });
    expect(state.tabs["tab-a"].activeRun?.toolExecuting).toBe(true);
    state = agentReducer(state, { type: "run/text-replace", tabId: "tab-a", requestToken: "request-1",
      event: { ...identity, type: "agent.turn.text_replace", sequence: 4, text: "" } });
    expect(state.tabs["tab-a"].activeRun).toMatchObject({ toolExecuting: false, streamedText: "" });
    state = agentReducer(state, { type: "run/complete", tabId: "tab-a", requestToken: "request-1", messageId: "answer", event: { ...completed, sequence: 5 } });
    expect(state.tabs["tab-a"].messages[1]).toMatchObject({ tools: [
      { tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" } },
      { tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" } },
    ] });
  });
  it("cancels only the matching request and retains its known conversation without partial text", () => {
    let state = startedState("request-1");
    state = agentReducer(state, { type: "run/stream-started", tabId: "tab-a", requestToken: "request-1", event: started });
    state = agentReducer(state, { type: "run/text-delta", tabId: "tab-a", requestToken: "request-1", event: delta });
    const cancel = { type: "run/cancel", tabId: "tab-a", requestToken: "request-1", messageId: "cancel-1" } as const;
    expect(agentReducer(state, { ...cancel, requestToken: "old" })).toBe(state);
    state = agentReducer(state, cancel);
    expect(state.tabs["tab-a"]).toMatchObject({
      phase: "IDLE", conversationId: started.conversation_id, activeRun: null,
      lastError: null, backgroundState: "NONE",
      messages: [{ kind: "user", text: "inspect" }, { kind: "cancelled" }],
    });
    expect(agentReducer(state, cancel)).toBe(state);
    expect(agentReducer(state, { type: "run/complete", tabId: "tab-a", requestToken: "request-1", event: completed, messageId: "late" })).toBe(state);
  });

  it("replaces provisional text, rejects stale updates and commits the current snapshot", () => {
    let state = startedState("request-1");
    state = agentReducer(state, { type: "run/stream-started", tabId: "tab-a", requestToken: "request-1", event: started });
    state = agentReducer(state, { type: "run/text-delta", tabId: "tab-a", requestToken: "request-1", event: delta });
    const { delta: _delta, ...base } = delta;
    const replacement = { ...base, type: "agent.turn.text_replace", sequence: 2, text: "final" } as const;
    const update = { type: "run/text-replace", tabId: "tab-a", requestToken: "request-1", event: replacement } as const;
    expect(agentReducer(state, { ...update, requestToken: "old" })).toEqual(state);
    expect(agentReducer(state, { ...update, event: { ...replacement, agent_run_id: "other" } })).toEqual(state);
    state = agentReducer(state, update);
    expect(state.tabs["tab-a"].activeRun?.streamedText).toBe("final");
    expect(agentReducer(state, update)).toEqual(state);
    state = agentReducer(state, { ...update, event: { ...replacement, sequence: 3, text: "" } });
    expect(state.tabs["tab-a"].activeRun?.streamedText).toBe("");
    state = agentReducer(state, { type: "run/text-delta", tabId: "tab-a", requestToken: "request-1", event: { ...delta, sequence: 4, delta: "answer" } });
    state = agentReducer(state, { type: "run/complete", tabId: "tab-a", requestToken: "request-1", messageId: "answer-1", event: { ...completed, sequence: 5 } });
    expect(state.tabs["tab-a"].messages[1]).toMatchObject({ kind: "assistant", text: "answer" });
  });

  it("isolates two terminal tabs and rejects stale completion", () => {
    let state = createAgentState();
    state = agentReducer(state, {
      type: "tab/ensure",
      tabId: "tab-a",
      selectedApiConfigId: null,
    });
    state = agentReducer(state, {
      type: "tab/ensure",
      tabId: "tab-b",
      selectedApiConfigId: null,
    });
    state = agentReducer(state, {
      type: "run/start",
      tabId: "tab-a",
      requestToken: "request-new",
      sshSessionId: "ssh-a",
      provider,
      userMessageId: "user-1",
      userMessage: "inspect",
    });
    state = agentReducer(state, {
      type: "run/complete",
      tabId: "tab-a",
      requestToken: "request-old",
      event: completed,
      messageId: "assistant-old",
    });

    expect(state.tabs["tab-a"].phase).toBe("RUNNING");
    expect(state.tabs["tab-a"].messages).toHaveLength(1);
    expect(state.tabs["tab-b"].messages).toHaveLength(0);
  });

  it("resets only one conversation and preserves provider plus risk acknowledgement", () => {
    let state = createAgentState();
    state = agentReducer(state, {
      type: "tab/ensure",
      tabId: "tab-a",
      selectedApiConfigId: "config-1",
    });
    state = agentReducer(state, {
      type: "risk/acknowledge",
      tabId: "tab-a",
      sshSessionId: "ssh-a",
    });
    state = agentReducer(state, {
      type: "conversation/reset",
      tabId: "tab-a",
    });

    expect(state.tabs["tab-a"]).toMatchObject({
      conversationId: null,
      selectedApiConfigId: "config-1",
      riskAcknowledgedSshSessionId: "ssh-a",
      messages: [],
    });
  });

  it("keeps streamed text provisional until a correlated completion", () => {
    let state = startedState("request-1");
    state = agentReducer(state, {
      type: "run/stream-started",
      tabId: "tab-a",
      requestToken: "request-1",
      event: started,
    });
    state = agentReducer(state, {
      type: "run/text-delta",
      tabId: "tab-a",
      requestToken: "request-1",
      event: { ...delta, delta: "hel" },
    });
    state = agentReducer(state, {
      type: "run/text-delta",
      tabId: "tab-a",
      requestToken: "request-1",
      event: { ...delta, sequence: 2, delta: "lo" },
    });

    expect(state.tabs["tab-a"].activeRun?.streamedText).toBe("hello");
    expect(state.tabs["tab-a"].messages).toHaveLength(1);

    const next = agentReducer(state, {
      type: "run/complete",
      tabId: "tab-a",
      requestToken: "request-1",
      event: { ...completed, sequence: 3 },
      messageId: "assistant-1",
    });

    expect(
      next.tabs["tab-a"].messages[next.tabs["tab-a"].messages.length - 1],
    ).toMatchObject({
      kind: "assistant",
      text: "hello",
      run: {
        agentRunId: "run-1",
        sshSessionId: "ssh-a",
        provider,
      },
    });
    expect(next.tabs["tab-a"].backgroundState).toBe("COMPLETED_UNREAD");
  });

  it("discards partial text when the correlated stream fails", () => {
    let state = startedState("request-1");
    state = agentReducer(state, {
      type: "run/stream-started",
      tabId: "tab-a",
      requestToken: "request-1",
      event: started,
    });
    state = agentReducer(state, {
      type: "run/text-delta",
      tabId: "tab-a",
      requestToken: "request-1",
      event: delta,
    });
    const next = agentReducer(state, {
      type: "run/fail",
      tabId: "tab-a",
      requestToken: "request-1",
      event: {
        ...completed,
        type: "agent.turn.failed",
        status: "FAILED",
        error_code: "MODEL_RESPONSE_INVALID",
        message: "Model response was invalid",
      },
      error: {
        code: "MODEL_RESPONSE_INVALID",
        message: "Model response was invalid",
      },
      messageId: "error-1",
    });

    expect(next.tabs["tab-a"].activeRun).toBeNull();
    expect(next.tabs["tab-a"].messages).toHaveLength(2);
    const messages = next.tabs["tab-a"].messages;
    expect(messages[messages.length - 1]).toMatchObject({ kind: "error" });
    expect(next.tabs["tab-a"].messages.some(
      (message) => message.kind === "assistant" && message.text === "hello",
    )).toBe(false);
  });

  it("does not invalidate an active Run when its Provider is disabled", () => {
    const running = startedState("request-1");
    const next = agentReducer(running, {
      type: "provider/invalidate",
      apiConfigId: "config-1",
    });

    expect(next.tabs["tab-a"].activeRun?.provider.apiConfigId).toBe(
      "config-1",
    );
    expect(next.tabs["tab-a"].phase).toBe("RUNNING");
  });

  it("moves risk confirmation through request, acknowledge, and cancel without changing other tabs", () => {
    let state = createAgentState();
    state = agentReducer(state, {
      type: "tab/ensure",
      tabId: "tab-a",
      selectedApiConfigId: null,
    });
    state = agentReducer(state, {
      type: "tab/ensure",
      tabId: "tab-b",
      selectedApiConfigId: null,
    });
    state = agentReducer(state, {
      type: "risk/request",
      tabId: "tab-a",
      sshSessionId: "ssh-a",
    });
    expect(state.tabs["tab-a"]).toMatchObject({
      phase: "AWAITING_RISK_CONFIRMATION",
      pendingRiskSshSessionId: "ssh-a",
    });
    state = agentReducer(state, {
      type: "risk/acknowledge",
      tabId: "tab-a",
      sshSessionId: "ssh-a",
    });
    expect(state.tabs["tab-a"].riskAcknowledgedSshSessionId).toBe("ssh-a");
    state = agentReducer(state, { type: "risk/cancel", tabId: "tab-a" });
    expect(state.tabs["tab-a"]).toMatchObject({
      phase: "IDLE",
      pendingRiskSshSessionId: null,
    });
    expect(state.tabs["tab-b"].phase).toBe("IDLE");
  });

  it("removes only an existing tab and marks unread results as read", () => {
    let state = agentReducer(startedState("request-1"), {
      type: "run/fail",
      tabId: "tab-a",
      requestToken: "request-1",
      event: null,
      error: { code: "MODEL_NETWORK_TIMEOUT", message: "Timed out." },
      messageId: "error-1",
    });
    state = agentReducer(state, { type: "background/read", tabId: "tab-a" });
    expect(state.tabs["tab-a"].backgroundState).toBe("NONE");
    state = agentReducer(state, { type: "tab/remove", tabId: "tab-a" });
    expect(state.tabs["tab-a"]).toBeUndefined();
  });

  it("aggregates failed unread before completed unread before running", () => {
    expect(
      aggregateAgentBackground({ a: "RUNNING", b: "COMPLETED_UNREAD" }),
    ).toBe("COMPLETED_UNREAD");
    expect(
      aggregateAgentBackground({
        a: "FAILED_UNREAD",
        b: "COMPLETED_UNREAD",
      }),
    ).toBe("FAILED_UNREAD");
  });

  it("detects active runs by their frozen SSH Session", () => {
    const running = startedState("request-1");
    expect(isActiveRunForSession(running, "ssh-a")).toBe(true);
    expect(isActiveRunForSession(running, "ssh-b")).toBe(false);
  });
});


describe("tool status cleanup", () => {
  it.each(["complete", "failed", "interrupted", "cancel"])("clears tool status on %s", (ending) => {
    let state = startedState("request-1");
    const common = { tabId: "tab-a", requestToken: "request-1" };
    state = agentReducer(state, { ...common, type: "run/stream-started", event: started });
    const { delta: _delta, ...base } = delta;
    state = agentReducer(state, { ...common, type: "run/tool-started", event: { ...base, type: "agent.turn.tool_started", tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" } } });
    expect(state.tabs["tab-a"].activeRun?.toolExecuting).toBe(true);
    if (ending === "complete") state = agentReducer(state, { ...common, type: "run/complete", event: completed, messageId: "end" });
    else if (ending === "cancel") state = agentReducer(state, { ...common, type: "run/cancel", messageId: "end" });
    else state = agentReducer(state, { ...common, type: "run/fail", messageId: "end",
      error: { code: "AGENT_STREAM_INTERRUPTED", message: "Interrupted" },
      event: ending === "interrupted" ? null : { ...base, sequence: 2, type: "agent.turn.failed", status: "FAILED", react_iteration: 1, error_code: "SIDECAR_RUNTIME_FAILED", message: "Failed" } });
    expect(state.tabs["tab-a"].activeRun).toBeNull();
    expect(state.tabs["tab-a"].phase).toBe("IDLE");
  });
});
