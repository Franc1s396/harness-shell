// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

import { agentApi, type ModelApiConfigInput } from "./agent";

const input: ModelApiConfigInput = {
  display_name: "OpenAI Production",
  api_type: "RESPONSES",
  base_url: "https://api.openai.com/v1",
  model: "gpt-5",
  api_key_secret_ref: "10000000-0000-4000-8000-000000000001",
  enabled: true,
};

describe("agentApi", () => {
  beforeEach(() => invoke.mockReset());

  it("uses only the seven existing Agent commands with camelCase Tauri args", async () => {
    invoke.mockResolvedValue(undefined);
    await agentApi.listModelApiConfigs();
    await agentApi.createModelApiConfig(input);
    await agentApi.updateModelApiConfig("config-1", input);
    await agentApi.deleteModelApiConfig("config-1");
    await agentApi.storeModelApiKey("secret-value");
    await agentApi.deleteModelApiKey("credential-1");
    await agentApi.runAgentTurn({
      conversationId: null,
      sshSessionId: "ssh-1",
      apiConfigId: "config-1",
      userMessage: "inspect the service",
    });

    expect(invoke.mock.calls).toEqual([
      ["list_model_api_configs"],
      ["create_model_api_config", { input }],
      ["update_model_api_config", { apiConfigId: "config-1", input }],
      ["delete_model_api_config", { apiConfigId: "config-1" }],
      ["store_model_api_key", { secret: "secret-value" }],
      ["delete_model_api_key", { credentialId: "credential-1" }],
      [
        "run_agent_turn",
        {
          conversationId: null,
          sshSessionId: "ssh-1",
          apiConfigId: "config-1",
          userMessage: "inspect the service",
        },
      ],
    ]);
  });

  it("normalizes structured command errors without exposing unknown details", async () => {
    const { normalizeAgentCommandError } = await import("./agent");

    expect(
      normalizeAgentCommandError({
        code: "MODEL_REQUEST_FAILED",
        message: "Request failed.",
        secret: "must-not-leak",
      }),
    ).toEqual({ code: "MODEL_REQUEST_FAILED", message: "Request failed." });
    expect(normalizeAgentCommandError(new Error("sensitive detail"))).toEqual({
      code: "AGENT_COMMAND_FAILED",
      message: "Agent operation failed.",
    });
  });
});
