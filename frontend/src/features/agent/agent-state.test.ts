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
      result: {
        conversation_id: "conversation-old",
        agent_run_id: "run-old",
        status: "COMPLETED",
        final_text: "stale",
        react_iteration: 0,
        error_code: null,
      },
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

  it.each([
    ["COMPLETED", null, null, "UI_AGENT_FINAL_TEXT_MISSING"],
    ["FAILED", null, "MODEL_REQUEST_FAILED", "MODEL_REQUEST_FAILED"],
    ["LIMIT_REACHED", null, null, "REACT_LIMIT_REACHED"],
    ["CANCELLED", null, null, "AGENT_CANCELLED"],
  ] as const)(
    "maps %s to an explicit error",
    (status, finalText, backendCode, expectedCode) => {
      const running = startedState("request-1");
      const next = agentReducer(running, {
        type: "run/complete",
        tabId: "tab-a",
        requestToken: "request-1",
        result: {
          conversation_id: "conversation-1",
          agent_run_id: "run-1",
          status,
          final_text: finalText,
          react_iteration: status === "LIMIT_REACHED" ? 128 : 0,
          error_code: backendCode,
        },
        messageId: "result-1",
      });

      expect(next.tabs["tab-a"].conversationId).toBe("conversation-1");
      expect(
        next.tabs["tab-a"].messages[
          next.tabs["tab-a"].messages.length - 1
        ],
      ).toMatchObject({
        kind: "error",
        error: { code: expectedCode },
      });
      expect(next.tabs["tab-a"].backgroundState).toBe("FAILED_UNREAD");
    },
  );

  it("projects a completed result from the frozen provider and session", () => {
    const next = agentReducer(startedState("request-1"), {
      type: "run/complete",
      tabId: "tab-a",
      requestToken: "request-1",
      result: {
        conversation_id: "conversation-1",
        agent_run_id: "run-1",
        status: "COMPLETED",
        final_text: "healthy",
        react_iteration: 1,
        error_code: null,
      },
      messageId: "assistant-1",
    });

    expect(
      next.tabs["tab-a"].messages[next.tabs["tab-a"].messages.length - 1],
    ).toMatchObject({
      kind: "assistant",
      text: "healthy",
      run: {
        agentRunId: "run-1",
        sshSessionId: "ssh-a",
        provider,
      },
    });
    expect(next.tabs["tab-a"].backgroundState).toBe("COMPLETED_UNREAD");
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
