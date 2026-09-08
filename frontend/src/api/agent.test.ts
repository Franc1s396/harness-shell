import { beforeEach, describe, expect, it, vi } from "vitest";

import validAgentFixtures from "../../../docs/protocol/http/fixtures/agent/valid-http-v1.json";
import invalidAgentFixtures from "../../../docs/protocol/http/fixtures/agent/invalid-http-v1.json";

const request = vi.hoisted(() => vi.fn());
const postSse = vi.hoisted(() => vi.fn());
const createCredentialEnvelope = vi.hoisted(() => vi.fn());
vi.mock("./bootstrap", () => ({
  getBackendClient: () => ({ http: { request, postSse } }),
}));
vi.mock("./credential-envelope", () => ({ createCredentialEnvelope }));

import { agentApi, type ModelApiConfigInput } from "./agent";
import { BackendHttpClient, BackendSseError } from "./http-client";

const requestId = "10000000-0000-4000-8000-000000000001";
const conversationId = "20000000-0000-4000-8000-000000000002";
const agentRunId = "30000000-0000-4000-8000-000000000003";
const baseEvent = {
  schema_version: 1,
  request_id: requestId,
  conversation_id: conversationId,
  agent_run_id: agentRunId,
} as const;

const sse = (...values: unknown[]) => async function* () {
  for (const data of values) {
    const value = data as { type: string; sequence: number };
    yield {
      requestId,
      event: value.type,
      id: String(value.sequence),
      data,
    };
  }
};

const input: ModelApiConfigInput = {
  display_name: "OpenAI Production",
  api_type: "RESPONSES",
  context_window_size: 128000,
  context_compaction_threshold_ratio: 0.75,
  max_output_tokens: 8192,
  base_url: "https://api.openai.com/v1",
  model: "gpt-5",
  enabled: true,
};

type InvalidStreamFixture = Readonly<{
  name: string;
  wire_utf8?: string;
  generated_wire?: Readonly<{
    kind: "single-frame" | "complete-body";
    encoded_bytes: number;
  }>;
  expected_error_code: string;
}>;

const invalidStreamFixtures = (
  invalidAgentFixtures.cases as InvalidStreamFixture[]
).filter((fixture) => fixture.wire_utf8 || fixture.generated_wire);

describe("agentApi", () => {
  it("sends the stable user identity and explicit retry flag through the SSE request", async () => {
    postSse.mockImplementation(sse(
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, type: "agent.turn.completed", sequence: 1, status: "COMPLETED", react_iteration: 0, error_code: null },
    ));
    await agentApi.streamAgentTurn({ conversationId, sshSessionId: "ssh-1", apiConfigId: "config-1",
      userMessage: "same", userMessageId: requestId, retry: true }, () => undefined);
    expect(postSse).toHaveBeenCalledWith("/v1/agent/turns", {
      conversation_id: conversationId, ssh_session_id: "ssh-1", api_config_id: "config-1",
      user_message: "same", user_message_id: requestId, retry: true,
    }, undefined);
  });
  it("passes cancellation to the HTTP stream and distinguishes it from interrupted EOF", async () => {
    const controller = new AbortController();
    const client = new BackendHttpClient("http://127.0.0.1:8765", {
      randomUuid: () => requestId,
      fetchImpl: async (_url, options) => {
        controller.abort();
        options?.signal?.throwIfAborted();
        throw new TypeError("AbortSignal was not forwarded");
      },
    });
    postSse.mockImplementation(client.postSse.bind(client));
    await expect(agentApi.streamAgentTurn({ conversationId: null, sshSessionId: "ssh-1", apiConfigId: "config-1", userMessage: "inspect" }, () => undefined, controller.signal))
      .rejects.toMatchObject({ name: "AgentTurnCancelled" });
  });

  it("keeps an already validated terminal result when cancellation races with EOF", async () => {
    const started = { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 };
    const completed = { ...baseEvent, type: "agent.turn.completed", sequence: 1, status: "COMPLETED", react_iteration: 1, error_code: null };
    postSse.mockImplementation(async function* () {
      yield* sse(started, completed)();
      throw new BackendSseError("CANCELLED");
    });
    await expect(agentApi.streamAgentTurn({ conversationId: null, sshSessionId: "ssh-1", apiConfigId: "config-1", userMessage: "inspect" }, () => undefined))
      .resolves.toEqual(completed);
  });

  beforeEach(() => {
    request.mockReset();
    postSse.mockReset();
    createCredentialEnvelope.mockReset().mockImplementation(
      async (_secret: string, loadPublicKey: () => Promise<unknown>) => {
        await loadPublicKey();
        return { version: 1 };
      },
    );
  });

  it("consumes the shared replacement fixture through real SSE framing", async () => {
    const fixture = validAgentFixtures.cases.find((value) => value.name === "agent-turn-text-replace")!;
    const client = new BackendHttpClient("http://127.0.0.1:8765", {
      randomUuid: () => requestId,
      fetchImpl: async () => new Response(fixture.wire_utf8, {
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8", "X-Request-ID": requestId, "Cache-Control": "no-store" },
      }),
    });
    postSse.mockImplementation((path, body) => client.postSse(path, body));
    const progress = vi.fn();
    const terminal = await agentApi.streamAgentTurn({ conversationId: null, sshSessionId: "ssh-1", apiConfigId: "config-1", userMessage: "inspect" }, progress);
    expect(progress.mock.calls[2][0]).toMatchObject({ type: "agent.turn.text_replace", text: "修订后的完整回答" });
    expect(terminal.type).toBe("agent.turn.completed");
  });

  it.each([null, 42, { text: "bad" }])("rejects invalid replacement text %j", async (text) => {
    postSse.mockImplementation(sse(
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, type: "agent.turn.text_replace", sequence: 1, text },
    ));
    await expect(agentApi.streamAgentTurn({ conversationId: null, sshSessionId: "ssh-1", apiConfigId: "config-1", userMessage: "inspect" }, () => undefined)).rejects.toMatchObject({ code: "BACKEND_AGENT_STREAM_INVALID" });
  });

  it("accepts full text updates including clearing provisional text", async () => {
    const started = { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 };
    const updates = ["draft", "", "final"].map((text, index) => ({
      ...baseEvent, type: "agent.turn.text_replace", sequence: index + 1, text,
    }));
    const completed = { ...baseEvent, type: "agent.turn.completed", sequence: 4, status: "COMPLETED", react_iteration: 0, error_code: null };
    postSse.mockImplementation(sse(started, ...updates, completed));
    const progress = vi.fn();
    await agentApi.streamAgentTurn({ conversationId: null, sshSessionId: "ssh-1", apiConfigId: "config-1", userMessage: "inspect" }, progress);
    expect(progress.mock.calls.map(([event]) => event)).toEqual([started, ...updates]);
  });

  it("maps Provider operations to direct HTTP with wire field names", async () => {
    request
      .mockResolvedValueOnce({ request_id: "r", configs: [] })
      .mockResolvedValueOnce({ request_id: "r", key_id: "key-1" })
      .mockResolvedValueOnce({ request_id: "r", config: {
        ...input,
        api_key_credential_id: "10000000-0000-4000-8000-000000000001",
      } });

    await agentApi.listModelApiConfigs();
    await agentApi.createModelApiConfig(input, "secret");
    expect(request.mock.calls).toEqual([
      ["GET", "/v1/agent/api-configs"],
      ["GET", "/v1/runtime/credential-encryption-key"],
      ["POST", "/v1/agent/api-configs", { body: {
        context_window_size: input.context_window_size,
        context_compaction_threshold_ratio: input.context_compaction_threshold_ratio,
        max_output_tokens: input.max_output_tokens,
        display_name: input.display_name,
        api_type: input.api_type,
        base_url: input.base_url,
        model: input.model,
        api_key_envelope: { version: 1 },
        enabled: input.enabled,
      } }],
    ]);
  });

  it("streams correlated progress and returns terminal only after EOF", async () => {
    const started = {
      ...baseEvent,
      type: "agent.turn.started",
      sequence: 0,
      status: "RUNNING",
      react_iteration: 0,
    } as const;
    const delta = {
      ...baseEvent,
      type: "agent.turn.text_delta",
      sequence: 1,
      delta: "hello",
    } as const;
    const completed = {
      ...baseEvent,
      type: "agent.turn.completed",
      sequence: 2,
      status: "COMPLETED",
      react_iteration: 0,
      error_code: null,
    } as const;
    postSse.mockImplementation(sse(started, delta, completed));
    const progress: unknown[] = [];

    await expect(agentApi.streamAgentTurn({
      conversationId: null,
      sshSessionId: "ssh-1",
      apiConfigId: "config-1",
      userMessage: "inspect the service",
    }, (event) => progress.push(event))).resolves.toEqual(completed);

    expect(progress).toEqual([started, delta]);
    expect(postSse).toHaveBeenCalledWith("/v1/agent/turns", {
      conversation_id: null,
      ssh_session_id: "ssh-1",
      api_config_id: "config-1",
      user_message: "inspect the service",
    }, undefined);
  });

  it.each([
    ["tool payload exposure", [
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, type: "agent.turn.tool_started", sequence: 1, tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" }, command: "pwd" },
    ]],
    ["tool sequence gap", [
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, type: "agent.turn.tool_started", sequence: 2, tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" } },
    ]],
    ["missing started", [{ ...baseEvent, type: "agent.turn.text_delta", sequence: 0, delta: "x" }]],
    ["sequence gap", [
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, type: "agent.turn.text_delta", sequence: 2, delta: "x" },
    ]],
    ["identity change", [
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, conversation_id: agentRunId, type: "agent.turn.text_delta", sequence: 1, delta: "x" },
    ]],
    ["unknown field", [
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0, extra: true },
    ]],
    ["duplicate started", [
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, type: "agent.turn.started", sequence: 1, status: "RUNNING", react_iteration: 0 },
    ]],
  ])("rejects invalid Agent streams: %s", async (_name, events) => {
    postSse.mockImplementation(sse(...events));

    await expect(agentApi.streamAgentTurn({
      conversationId: null,
      sshSessionId: "ssh-1",
      apiConfigId: "config-1",
      userMessage: "inspect",
    }, () => undefined)).rejects.toMatchObject({
      code: "BACKEND_AGENT_STREAM_INVALID",
    });
  });

  it("rejects a started event which replaces an existing conversation identity", async () => {
    const started = {
      ...baseEvent,
      type: "agent.turn.started",
      sequence: 0,
      status: "RUNNING",
      react_iteration: 0,
    } as const;
    postSse.mockImplementation(sse(started));

    await expect(agentApi.streamAgentTurn({
      conversationId: "60000000-0000-4000-8000-000000000006",
      sshSessionId: "ssh-1",
      apiConfigId: "config-1",
      userMessage: "inspect",
    }, () => undefined)).rejects.toMatchObject({
      code: "BACKEND_AGENT_STREAM_INVALID",
    });
  });

  it("reports a valid stream which ends without a terminal event as interrupted", async () => {
    postSse.mockImplementation(sse({
      ...baseEvent,
      type: "agent.turn.started",
      sequence: 0,
      status: "RUNNING",
      react_iteration: 0,
    }));

    await expect(agentApi.streamAgentTurn({
      conversationId: null,
      sshSessionId: "ssh-1",
      apiConfigId: "config-1",
      userMessage: "inspect",
    }, () => undefined)).rejects.toMatchObject({
      code: "AGENT_STREAM_INTERRUPTED",
    });
  });

  it.each(invalidStreamFixtures)(
    "rejects the frozen invalid stream fixture: $name",
    async (fixture) => {
      const generated = fixture.generated_wire;
      const bytes = fixture.wire_utf8 !== undefined
        ? new TextEncoder().encode(fixture.wire_utf8)
        : generated?.kind === "single-frame"
          ? new TextEncoder().encode(
              `event: x\nid: 0\ndata: ${"x".repeat(generated.encoded_bytes)}\n\n`,
            )
          : new Uint8Array(generated?.encoded_bytes ?? 0);
      const fixtureClient = new BackendHttpClient("http://127.0.0.1:8765", {
        randomUuid: () => requestId,
        fetchImpl: async () => new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Request-ID": requestId,
            "Cache-Control": "no-store",
          },
        }),
      });
      postSse.mockImplementation((path, body) =>
        fixtureClient.postSse(path, body));

      await expect(agentApi.streamAgentTurn({
        conversationId: null,
        sshSessionId: "ssh-1",
        apiConfigId: "config-1",
        userMessage: "inspect",
      }, () => undefined)).rejects.toMatchObject({
        code: fixture.expected_error_code,
      });
    },
  );

  it("does not expose standalone credential mutation methods", () => {
    expect(agentApi).not.toHaveProperty("storeModelApiKey");
    expect(agentApi).not.toHaveProperty("deleteModelApiKey");
  });
});


describe("tool event argument validation", () => {
  it.each([
    { tool_name: "unknown" }, { tool_call_id: "" }, { arguments: { command: "" } },
    { arguments: { command: "pwd", extra: true } }, { arguments: { command: "x\0" } },
    { arguments: { command: "x".repeat(4097) } },
  ])("rejects malformed tool metadata: %j", async (mutation) => {
    postSse.mockImplementation(sse(
      { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 },
      { ...baseEvent, type: "agent.turn.tool_started", sequence: 1, tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" }, ...mutation },
    ));
    await expect(agentApi.streamAgentTurn({ conversationId: null, sshSessionId: "ssh-1", apiConfigId: "config-1", userMessage: "inspect" }, () => undefined)).rejects.toMatchObject({ code: "BACKEND_AGENT_STREAM_INVALID" });
  });
});


describe("approval protocol", () => {
  const approvalId = "40000000-0000-4000-8000-000000000004";
  const sshSessionId = "50000000-0000-4000-8000-000000000005";
  const body = { conversation_id: conversationId, agent_run_id: agentRunId, ssh_session_id: sshSessionId, tool_call_id: "change", decision: "approve" as const };
  const started = { ...baseEvent, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 };
  const pending = { ...baseEvent, type: "agent.turn.approval_requested", sequence: 1, approval_id: approvalId,
    ssh_session_id: sshSessionId, tool_name: "execute_command", tool_call_id: "change", arguments: { command: "touch /tmp/test" },
    target: { display_name: "test", host: "localhost", port: 22, username: "tester" }, reason: "POSSIBLE_MUTATION_OR_UNKNOWN" };
  const resolved = { ...baseEvent, type: "agent.turn.approval_resolved", sequence: 2, approval_id: approvalId,
    tool_call_id: "change", status: "APPROVED", reason: "user_approved" };
  const completed = { ...baseEvent, type: "agent.turn.completed", sequence: 3, status: "COMPLETED", react_iteration: 1, error_code: null };
  const turn = { conversationId: null, sshSessionId, apiConfigId: "config-1", userMessage: "change" };

  it("posts only the frozen identity and forwards cancellation", async () => {
    const signal = new AbortController().signal;
    request.mockResolvedValue({ approval_id: approvalId, status: "APPROVED" });
    await agentApi.decideAgentApproval(approvalId, body, signal);
    expect(request).toHaveBeenLastCalledWith("POST", `/v1/agent/approvals/${approvalId}/decision`, { body, signal });
    request.mockResolvedValue({ approval_id: approvalId, status: "REJECTED" });
    await expect(agentApi.decideAgentApproval(approvalId, body)).rejects.toMatchObject({ code: "BACKEND_AGENT_STREAM_INVALID" });
  });

  it("continues the original stream after a correlated decision", async () => {
    postSse.mockReturnValue(sse(started, pending, resolved, completed)());
    const progress = vi.fn();
    await expect(agentApi.streamAgentTurn(turn, progress)).resolves.toEqual(completed);
    expect(progress.mock.calls.map(([event]) => event.type)).toContain("agent.turn.approval_requested");
  });

  it.each([
    { ...pending, expires_at: "2026-09-08" },
    { ...pending, ssh_session_id: approvalId },
  ])("rejects unknown fields and changed session", async invalid => {
    postSse.mockReturnValue(sse(started, invalid, resolved, completed)());
    await expect(agentApi.streamAgentTurn(turn, vi.fn())).rejects.toMatchObject({ code: "BACKEND_AGENT_STREAM_INVALID" });
  });

  it.each(["resolved-first", "duplicate-request", "unknown-resolution"])("rejects invalid approval ordering: %s", async kind => {
    const events = kind === "resolved-first" ? [started, { ...resolved, sequence: 1 }] : kind === "duplicate-request"
      ? [started, pending, { ...pending, sequence: 2 }] : [started, pending, { ...resolved, approval_id: sshSessionId }];
    postSse.mockReturnValue(sse(...events)());
    await expect(agentApi.streamAgentTurn(turn, vi.fn())).rejects.toMatchObject({ code: "BACKEND_AGENT_STREAM_INVALID" });
  });

  it("rejects completion while approval is pending", async () => {
    postSse.mockReturnValue(sse(started, pending, { ...completed, sequence: 2 })());
    await expect(agentApi.streamAgentTurn(turn, vi.fn())).rejects.toMatchObject({ code: "BACKEND_AGENT_STREAM_INVALID" });
  });
});


it.each(["agent-approval-approve", "agent-approval-reject", "agent-approval-invalidated"])("consumes shared approval fixture %s through real framing", async name => {
  const fixture = validAgentFixtures.cases.find(value => value.name === name)!;
  const client = new BackendHttpClient("http://127.0.0.1:8765", {
    randomUuid: () => requestId,
    fetchImpl: async () => new Response(fixture.wire_utf8, {
      status: 200, headers: { "Content-Type": "text/event-stream", "X-Request-ID": requestId, "Cache-Control": "no-store" },
    }),
  });
  postSse.mockImplementation(client.postSse.bind(client));
  const progress = vi.fn();
  await agentApi.streamAgentTurn({ conversationId: null, sshSessionId: "20000000-0000-4000-8000-000000000002", apiConfigId: "config-1", userMessage: "change" }, progress);
  expect(progress.mock.calls.map(([event]) => event.type)).toContain("agent.turn.approval_resolved");
});
