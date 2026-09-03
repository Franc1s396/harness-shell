import { getBackendClient } from "./bootstrap";
import { createCredentialEnvelope, type CredentialPublicKey } from "./credential-envelope";

export type ApiType = "CHAT_COMPLETIONS" | "RESPONSES";

export type AgentRunStatus =
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "LIMIT_REACHED"
  | "CANCELLED";

export type ModelApiConfigInput = {
  display_name: string;
  api_type: ApiType;
  base_url: string;
  model: string;
  enabled: boolean;
};

export type ModelApiConfig = ModelApiConfigInput & {
  api_key_secret_ref: string;
  api_config_id: string;
  created_at: string;
  updated_at: string;
};

export type AgentTurnResult = {
  conversation_id: string;
  agent_run_id: string;
  status: AgentRunStatus;
  final_text: string | null;
  react_iteration: number;
  error_code: string | null;
};

export type RunAgentTurnInput = {
  conversationId: string | null;
  sshSessionId: string;
  apiConfigId: string;
  userMessage: string;
};

export type AgentCommandError = {
  code: string;
  message: string;
};

export const normalizeAgentCommandError = (
  error: unknown,
): AgentCommandError => {
  if (typeof error === "object" && error !== null && "code" in error) {
    const candidate = error as { code: unknown; message?: unknown };
    return {
      code: String(candidate.code),
      message: String(candidate.message ?? "Agent operation failed."),
    };
  }
  return {
    code: "AGENT_COMMAND_FAILED",
    message: "Agent operation failed.",
  };
};

export const agentApi = {
  listModelApiConfigs: () =>
    getBackendClient().http.request<{ request_id: string; configs: WireModelApiConfig[] }>(
      "GET", "/v1/agent/api-configs",
    ).then((value) => value.configs.map(fromWireConfig)),
  createModelApiConfig: async (input: ModelApiConfigInput, secret: string) => {
    const apiKeyEnvelope = await createApiKeyEnvelope(secret);
    const value = await getBackendClient().http.request<{
      request_id: string;
      config: WireModelApiConfig;
    }>("POST", "/v1/agent/api-configs", {
      body: { ...toWireInput(input), api_key_envelope: apiKeyEnvelope },
    });
    return fromWireConfig(value.config);
  },
  updateModelApiConfig: (
    apiConfigId: string,
    input: ModelApiConfigInput,
    replacementSecret?: string,
  ) => updateModelApiConfig(apiConfigId, input, replacementSecret),
  deleteModelApiConfig: (apiConfigId: string) =>
    getBackendClient().http.request<{ request_id: string; deleted: boolean }>(
      "DELETE", `/v1/agent/api-configs/${apiConfigId}`,
    ).then((value) => value.deleted),
  runAgentTurn: (input: RunAgentTurnInput) =>
    getBackendClient().http.request<AgentTurnResult & { request_id: string }>(
      "POST", "/v1/agent/turns", { body: {
        conversation_id: input.conversationId,
        ssh_session_id: input.sshSessionId,
        api_config_id: input.apiConfigId,
        user_message: input.userMessage,
      } },
    ),
};

type WireModelApiConfigInput = ModelApiConfigInput;

type WireModelApiConfig = WireModelApiConfigInput & {
  api_key_credential_id: string;
  api_config_id: string;
  created_at: string;
  updated_at: string;
};

const toWireInput = (input: ModelApiConfigInput): WireModelApiConfigInput => ({
  display_name: input.display_name,
  api_type: input.api_type,
  base_url: input.base_url,
  model: input.model,
  enabled: input.enabled,
});

const fromWireConfig = (value: WireModelApiConfig): ModelApiConfig => ({
  display_name: value.display_name,
  api_type: value.api_type,
  base_url: value.base_url,
  model: value.model,
  api_key_secret_ref: value.api_key_credential_id,
  enabled: value.enabled,
  api_config_id: value.api_config_id,
  created_at: value.created_at,
  updated_at: value.updated_at,
});

const createApiKeyEnvelope = async (secret: string) => {
  const http = getBackendClient().http;
  return createCredentialEnvelope(secret, async () =>
    http.request<CredentialPublicKey & { request_id: string }>(
      "GET", "/v1/runtime/credential-encryption-key",
    ));
};

const updateModelApiConfig = async (
  apiConfigId: string,
  input: ModelApiConfigInput,
  replacementSecret?: string,
): Promise<ModelApiConfig> => {
  const apiKeyEnvelope = replacementSecret === undefined
    ? undefined
    : await createApiKeyEnvelope(replacementSecret);
  const body = {
    ...toWireInput(input),
    ...(apiKeyEnvelope === undefined ? {} : { api_key_envelope: apiKeyEnvelope }),
  };
  const value = await getBackendClient().http.request<{
    request_id: string;
    config: WireModelApiConfig;
  }>("PATCH", `/v1/agent/api-configs/${apiConfigId}`, { body });
  return fromWireConfig(value.config);
};

export type AgentApi = typeof agentApi;
