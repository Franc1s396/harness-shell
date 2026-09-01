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

export type ProviderMutationResult<T> =
  | { kind: "success"; value: T; cleanupError: null }
  | {
      kind: "partial_success";
      value: T;
      cleanupError: AgentCommandError;
    };

export class ProviderMutationFailure extends Error {
  constructor(
    readonly primaryError: AgentCommandError,
    readonly cleanupError: AgentCommandError | null,
  ) {
    super(primaryError.message);
    this.name = "ProviderMutationFailure";
  }
}

const toInput = (
  draft: ProviderDraft,
  apiKeySecretRef: string,
): ModelApiConfigInput => ({
  display_name: draft.displayName,
  api_type: draft.apiType,
  base_url: draft.baseUrl,
  model: draft.model,
  api_key_secret_ref: apiKeySecretRef,
  enabled: draft.enabled,
});

async function compensateNewCredential(
  api: AgentApi,
  credentialId: string,
  primaryError: unknown,
): Promise<never> {
  try {
    await api.deleteModelApiKey(credentialId);
  } catch (cleanupError) {
    throw new ProviderMutationFailure(
      normalizeAgentCommandError(primaryError),
      normalizeAgentCommandError(cleanupError),
    );
  }
  throw new ProviderMutationFailure(
    normalizeAgentCommandError(primaryError),
    null,
  );
}

export async function createProvider(
  api: AgentApi,
  draft: ProviderDraft,
  secret: string,
): Promise<ProviderMutationResult<ModelApiConfig>> {
  const credential = await api.storeModelApiKey(secret);
  try {
    const config = await api.createModelApiConfig(
      toInput(draft, credential.credential_id),
    );
    return { kind: "success", value: config, cleanupError: null };
  } catch (error) {
    return compensateNewCredential(api, credential.credential_id, error);
  }
}

export async function updateProvider(
  api: AgentApi,
  current: ModelApiConfig,
  draft: ProviderDraft,
  replacementSecret: string,
): Promise<ProviderMutationResult<ModelApiConfig>> {
  if (replacementSecret.length === 0) {
    const config = await api.updateModelApiConfig(
      current.api_config_id,
      toInput(draft, current.api_key_secret_ref),
    );
    return { kind: "success", value: config, cleanupError: null };
  }

  const credential = await api.storeModelApiKey(replacementSecret);
  let updated: ModelApiConfig;
  try {
    updated = await api.updateModelApiConfig(
      current.api_config_id,
      toInput(draft, credential.credential_id),
    );
  } catch (error) {
    return compensateNewCredential(api, credential.credential_id, error);
  }

  try {
    await api.deleteModelApiKey(current.api_key_secret_ref);
    return { kind: "success", value: updated, cleanupError: null };
  } catch (cleanupError) {
    return {
      kind: "partial_success",
      value: updated,
      cleanupError: normalizeAgentCommandError(cleanupError),
    };
  }
}

export async function deleteProvider(
  api: AgentApi,
  current: ModelApiConfig,
): Promise<ProviderMutationResult<true>> {
  let deleted: boolean;
  try {
    deleted = await api.deleteModelApiConfig(current.api_config_id);
  } catch (error) {
    throw new ProviderMutationFailure(normalizeAgentCommandError(error), null);
  }

  if (!deleted) {
    throw new ProviderMutationFailure(
      {
        code: "UI_MODEL_API_CONFIG_DELETE_FALSE",
        message: "The provider was not deleted.",
      },
      null,
    );
  }

  try {
    await api.deleteModelApiKey(current.api_key_secret_ref);
    return { kind: "success", value: true, cleanupError: null };
  } catch (cleanupError) {
    return {
      kind: "partial_success",
      value: true,
      cleanupError: normalizeAgentCommandError(cleanupError),
    };
  }
}
