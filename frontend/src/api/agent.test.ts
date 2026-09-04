import { beforeEach, describe, expect, it, vi } from "vitest";

import invalidAgentFixtures from "../../../docs/protocol/http/fixtures/agent/invalid-http-v1.json";

const request = vi.hoisted(() => vi.fn());
const postSse = vi.hoisted(() => vi.fn());
const createCredentialEnvelope = vi.hoisted(() => vi.fn());
vi.mock("./bootstrap", () => ({
  getBackendClient: () => ({ http: { request, postSse } }),
}));
vi.mock("./credential-envelope", () => ({ createCredentialEnvelope }));

import { agentApi, type ModelApiConfigInput } from "./agent";
import { BackendHttpClient } from "./http-client";

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
    });
  });

  it.each([
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
