import {
  normalizeAgentCommandError,
  type AgentApi,
  type AgentCommandError,
  type ApiType,
  type ModelApiConfig,
  type ModelApiConfigInput,
} from "../../api/agent";

export type ProviderDraft = {
  displayName: string;
  apiType: ApiType;
  baseUrl: string;
  model: string;
  enabled: boolean;
};

export type ProviderMutationResult<T> = {
  kind: "success";
  value: T;
};

export class ProviderMutationFailure extends Error {
  constructor(readonly primaryError: AgentCommandError) {
    super(primaryError.message);
    this.name = "ProviderMutationFailure";
  }
}

const toInput = (draft: ProviderDraft): ModelApiConfigInput => ({
  display_name: draft.displayName,
  api_type: draft.apiType,
  base_url: draft.baseUrl,
  model: draft.model,
  enabled: draft.enabled,
});

export async function createProvider(
  api: AgentApi,
  draft: ProviderDraft,
  secret: string,
): Promise<ProviderMutationResult<ModelApiConfig>> {
  try {
    const config = await api.createModelApiConfig(toInput(draft), secret);
    return { kind: "success", value: config };
  } catch (error) {
    throw new ProviderMutationFailure(normalizeAgentCommandError(error));
  }
}

export async function updateProvider(
  api: AgentApi,
  current: ModelApiConfig,
  draft: ProviderDraft,
  replacementSecret: string,
): Promise<ProviderMutationResult<ModelApiConfig>> {
  try {
    const config = await api.updateModelApiConfig(
      current.api_config_id,
      toInput(draft),
      replacementSecret.length === 0 ? undefined : replacementSecret,
    );
    return { kind: "success", value: config };
  } catch (error) {
    throw new ProviderMutationFailure(normalizeAgentCommandError(error));
  }
}

export async function deleteProvider(
  api: AgentApi,
  current: ModelApiConfig,
): Promise<ProviderMutationResult<true>> {
  try {
    const deleted = await api.deleteModelApiConfig(current.api_config_id);
    if (!deleted) {
      throw {
        code: "UI_MODEL_API_CONFIG_DELETE_FALSE",
        message: "The provider was not deleted.",
      };
    }
    return { kind: "success", value: true };
  } catch (error) {
    throw new ProviderMutationFailure(normalizeAgentCommandError(error));
  }
}
