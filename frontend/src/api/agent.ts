import { getBackendClient } from "./bootstrap";
import { createCredentialEnvelope, type CredentialPublicKey } from "./credential-envelope";
import { BackendSseError, type BackendSseFrame } from "./http-client";

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
  streamAgentTurn: async (
    input: RunAgentTurnInput,
    onProgress: (event: AgentTurnProgressEvent) => void,
  ): Promise<AgentTurnTerminalEvent> => {
    let expectedSequence = 0;
    let identity: Readonly<{
      requestId: string;
      conversationId: string;
      agentRunId: string;
    }> | null = null;
    let terminal: AgentTurnTerminalEvent | null = null;
    try {
      for await (const frame of getBackendClient().http.postSse(
        "/v1/agent/turns",
        toWireTurnInput(input),
      )) {
        if (terminal !== null) throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
        const event = validateAgentFrame(frame);
        if (event.sequence !== expectedSequence) {
          throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
        }
        if (identity === null) {
          if (
            event.type !== "agent.turn.started" ||
            (input.conversationId !== null &&
              event.conversation_id !== input.conversationId)
          ) {
            throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
          }
          identity = {
            requestId: event.request_id,
            conversationId: event.conversation_id,
            agentRunId: event.agent_run_id,
          };
        } else {
          if (
            event.type === "agent.turn.started" ||
            event.request_id !== identity.requestId ||
            event.conversation_id !== identity.conversationId ||
            event.agent_run_id !== identity.agentRunId
          ) {
            throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
          }
        }
        expectedSequence += 1;
        if (event.type === "agent.turn.completed" || event.type === "agent.turn.failed") {
          terminal = event;
        } else {
          onProgress(event);
        }
      }
    } catch (error) {
      throw mapAgentStreamError(error);
    }
    if (terminal === null) throw agentStreamError("AGENT_STREAM_INTERRUPTED");
    return terminal;
  },
};

type AgentEventBase = Readonly<{
  schema_version: 1;
  request_id: string;
  sequence: number;
  conversation_id: string;
  agent_run_id: string;
}>;

export type AgentTurnStartedEvent = AgentEventBase & Readonly<{
  type: "agent.turn.started";
  status: "RUNNING";
  react_iteration: 0;
}>;

export type AgentTurnTextDeltaEvent = AgentEventBase & Readonly<{
  type: "agent.turn.text_delta";
  delta: string;
}>;

export type AgentTurnCompletedEvent = AgentEventBase & Readonly<{
  type: "agent.turn.completed";
  status: "COMPLETED";
  react_iteration: number;
  error_code: null;
}>;

export type AgentTurnFailedEvent = AgentEventBase & Readonly<{
  type: "agent.turn.failed";
  status: "FAILED" | "LIMIT_REACHED" | "CANCELLED";
  react_iteration: number;
  error_code: string;
  message: string;
}>;

export type AgentTurnProgressEvent = AgentTurnStartedEvent | AgentTurnTextDeltaEvent;
export type AgentTurnTerminalEvent = AgentTurnCompletedEvent | AgentTurnFailedEvent;

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

const toWireTurnInput = (input: RunAgentTurnInput) => ({
  conversation_id: input.conversationId,
  ssh_session_id: input.sshSessionId,
  api_config_id: input.apiConfigId,
  user_message: input.userMessage,
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

type AgentTurnEvent = AgentTurnProgressEvent | AgentTurnTerminalEvent;

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,127}$/;
const BASE_KEYS = [
  "agent_run_id",
  "conversation_id",
  "request_id",
  "schema_version",
  "sequence",
  "type",
];

const validateAgentFrame = (frame: BackendSseFrame): AgentTurnEvent => {
  if (!isRecord(frame.data)) throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
  const value = frame.data;
  if (
    value.schema_version !== 1 ||
    !isCanonicalUuid(value.request_id) ||
    value.request_id !== frame.requestId ||
    !isSafeCount(value.sequence) ||
    !isCanonicalUuid(value.conversation_id) ||
    !isCanonicalUuid(value.agent_run_id) ||
    typeof value.type !== "string" ||
    frame.event !== value.type ||
    frame.id !== String(value.sequence)
  ) {
    throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
  }
  if (value.type === "agent.turn.started") {
    requireExactKeys(value, [...BASE_KEYS, "status", "react_iteration"]);
    if (value.status !== "RUNNING" || value.react_iteration !== 0) {
      throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
    }
    return value as AgentTurnStartedEvent;
  }
  if (value.type === "agent.turn.text_delta") {
    requireExactKeys(value, [...BASE_KEYS, "delta"]);
    if (typeof value.delta !== "string" || value.delta.length === 0) {
      throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
    }
    return value as AgentTurnTextDeltaEvent;
  }
  if (value.type === "agent.turn.completed") {
    requireExactKeys(value, [
      ...BASE_KEYS,
      "status",
      "react_iteration",
      "error_code",
    ]);
    if (
      value.status !== "COMPLETED" ||
      !isReactIteration(value.react_iteration) ||
      value.error_code !== null
    ) {
      throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
    }
    return value as AgentTurnCompletedEvent;
  }
  if (value.type === "agent.turn.failed") {
    requireExactKeys(value, [
      ...BASE_KEYS,
      "status",
      "react_iteration",
      "error_code",
      "message",
    ]);
    if (
      (value.status !== "FAILED" &&
        value.status !== "LIMIT_REACHED" &&
        value.status !== "CANCELLED") ||
      !isReactIteration(value.react_iteration) ||
      typeof value.error_code !== "string" ||
      !ERROR_CODE_PATTERN.test(value.error_code) ||
      typeof value.message !== "string" ||
      value.message.length === 0 ||
      value.message.length > 256
    ) {
      throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
    }
    return value as AgentTurnFailedEvent;
  }
  throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
};

const requireExactKeys = (
  value: Record<string, unknown>,
  expected: string[],
): void => {
  const actual = Object.keys(value).sort();
  const keys = [...expected].sort();
  if (actual.length !== keys.length || actual.some((key, index) => key !== keys[index])) {
    throw agentStreamError("BACKEND_AGENT_STREAM_INVALID");
  }
};

const isCanonicalUuid = (value: unknown): value is string =>
  typeof value === "string" && UUID_PATTERN.test(value);

const isSafeCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

const isReactIteration = (value: unknown): value is number =>
  isSafeCount(value) && value <= 128;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const agentStreamError = (code: string): Error & { code: string } =>
  Object.assign(new Error(code), { code });

const mapAgentStreamError = (error: unknown): unknown => {
  if (!(error instanceof BackendSseError)) return error;
  if (error.kind === "TOO_LARGE") {
    return agentStreamError("BACKEND_AGENT_STREAM_TOO_LARGE");
  }
  if (error.kind === "INVALID") {
    return agentStreamError("BACKEND_AGENT_STREAM_INVALID");
  }
  return agentStreamError("AGENT_STREAM_INTERRUPTED");
};

export type AgentApi = typeof agentApi;
