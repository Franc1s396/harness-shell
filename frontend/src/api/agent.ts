import { invoke } from "@tauri-apps/api/core";

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
  api_key_secret_ref: string;
  enabled: boolean;
};

export type ModelApiConfig = ModelApiConfigInput & {
  api_config_id: string;
  created_at: string;
  updated_at: string;
};

export type ApiKeyCredentialReference = {
  credential_id: string;
  kind: "api_key";
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
    invoke<ModelApiConfig[]>("list_model_api_configs"),
  createModelApiConfig: (input: ModelApiConfigInput) =>
    invoke<ModelApiConfig>("create_model_api_config", { input }),
  updateModelApiConfig: (
    apiConfigId: string,
    input: ModelApiConfigInput,
  ) =>
    invoke<ModelApiConfig>("update_model_api_config", { apiConfigId, input }),
  deleteModelApiConfig: (apiConfigId: string) =>
    invoke<boolean>("delete_model_api_config", { apiConfigId }),
  storeModelApiKey: (secret: string) =>
    invoke<ApiKeyCredentialReference>("store_model_api_key", { secret }),
  deleteModelApiKey: (credentialId: string) =>
    invoke<void>("delete_model_api_key", { credentialId }),
  runAgentTurn: (input: RunAgentTurnInput) =>
    invoke<AgentTurnResult>("run_agent_turn", input),
};

export type AgentApi = typeof agentApi;
