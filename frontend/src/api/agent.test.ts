import { beforeEach, describe, expect, it, vi } from "vitest";

const request = vi.hoisted(() => vi.fn());
const createCredentialEnvelope = vi.hoisted(() => vi.fn());
vi.mock("./bootstrap", () => ({
  getBackendClient: () => ({ http: { request } }),
}));
vi.mock("./credential-envelope", () => ({ createCredentialEnvelope }));

import { agentApi, type ModelApiConfigInput } from "./agent";

const input: ModelApiConfigInput = {
  display_name: "OpenAI Production",
  api_type: "RESPONSES",
  base_url: "https://api.openai.com/v1",
  model: "gpt-5",
  enabled: true,
};

describe("agentApi", () => {
  beforeEach(() => {
    request.mockReset();
    createCredentialEnvelope.mockReset().mockImplementation(
      async (_secret: string, loadPublicKey: () => Promise<unknown>) => {
        await loadPublicKey();
        return { version: 1 };
      },
    );
  });

  it("maps Provider and turn operations to direct HTTP with wire field names", async () => {
    request
      .mockResolvedValueOnce({ request_id: "r", configs: [] })
      .mockResolvedValueOnce({ request_id: "r", key_id: "key-1" })
      .mockResolvedValueOnce({ request_id: "r", config: {
        ...input,
        api_key_credential_id: "10000000-0000-4000-8000-000000000001",
      } })
      .mockResolvedValueOnce({ request_id: "r", conversation_id: "c", status: "COMPLETED" });

    await agentApi.listModelApiConfigs();
    await agentApi.createModelApiConfig(input, "secret");
    await agentApi.runAgentTurn({
      conversationId: null,
      sshSessionId: "ssh-1",
      apiConfigId: "config-1",
      userMessage: "inspect the service",
    });

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
      ["POST", "/v1/agent/turns", { body: {
        conversation_id: null,
        ssh_session_id: "ssh-1",
        api_config_id: "config-1",
        user_message: "inspect the service",
      } }],
    ]);
  });

  it("does not expose standalone credential mutation methods", () => {
    expect(agentApi).not.toHaveProperty("storeModelApiKey");
    expect(agentApi).not.toHaveProperty("deleteModelApiKey");
  });
});
