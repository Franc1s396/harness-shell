import { describe, expect, it, vi } from "vitest";

import type { AgentApi, ModelApiConfig } from "../../api/agent";
import {
  createProvider,
  deleteProvider,
  ProviderMutationFailure,
  updateProvider,
} from "./provider-config-actions";

const current: ModelApiConfig = {
  api_config_id: "config-1",
  display_name: "Production",
  api_type: "RESPONSES",
  base_url: "https://api.example/v1",
  model: "gpt-5",
  api_key_secret_ref: "credential-old",
  enabled: true,
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:00Z",
};

const draft = {
  displayName: "Production",
  apiType: "RESPONSES" as const,
  baseUrl: "https://api.example/v1",
  model: "gpt-5",
  enabled: true,
};

const api = () =>
  ({
    listModelApiConfigs: vi.fn(),
    createModelApiConfig: vi.fn(),
    updateModelApiConfig: vi.fn(),
    deleteModelApiConfig: vi.fn(),
    storeModelApiKey: vi.fn(),
    deleteModelApiKey: vi.fn(),
    runAgentTurn: vi.fn(),
  }) satisfies AgentApi;

describe("provider config actions", () => {
  it("creates metadata only after storing the Key", async () => {
    const mock = api();
    mock.storeModelApiKey.mockResolvedValue({
      credential_id: "credential-new",
      kind: "api_key",
    });
    mock.createModelApiConfig.mockResolvedValue(current);

    await expect(createProvider(mock, draft, "secret")).resolves.toEqual({
      kind: "success",
      value: current,
      cleanupError: null,
    });
    expect(mock.storeModelApiKey.mock.invocationCallOrder[0]).toBeLessThan(
      mock.createModelApiConfig.mock.invocationCallOrder[0],
    );
  });

  it("deletes a newly stored key when create fails", async () => {
    const mock = api();
    mock.storeModelApiKey.mockResolvedValue({
      credential_id: "credential-new",
      kind: "api_key",
    });
    mock.createModelApiConfig.mockRejectedValue({
      code: "MODEL_API_CONFIG_PERSISTENCE_FAILED",
    });
    mock.deleteModelApiKey.mockResolvedValue(undefined);

    await expect(createProvider(mock, draft, "new-secret")).rejects.toMatchObject({
      name: "ProviderMutationFailure",
      primaryError: { code: "MODEL_API_CONFIG_PERSISTENCE_FAILED" },
      cleanupError: null,
    });
    expect(mock.deleteModelApiKey).toHaveBeenCalledWith("credential-new");
  });

  it("reports create and new-key cleanup failures together", async () => {
    const mock = api();
    mock.storeModelApiKey.mockResolvedValue({
      credential_id: "credential-new",
      kind: "api_key",
    });
    mock.createModelApiConfig.mockRejectedValue({
      code: "MODEL_API_CONFIG_PERSISTENCE_FAILED",
    });
    mock.deleteModelApiKey.mockRejectedValue({ code: "VAULT_OPERATION_FAILED" });

    await expect(createProvider(mock, draft, "new-secret")).rejects.toMatchObject({
      primaryError: { code: "MODEL_API_CONFIG_PERSISTENCE_FAILED" },
      cleanupError: { code: "VAULT_OPERATION_FAILED" },
    });
  });

  it("keeps the old Key reference when edit secret is blank", async () => {
    const mock = api();
    mock.updateModelApiConfig.mockResolvedValue(current);

    await updateProvider(mock, current, draft, "");

    expect(mock.storeModelApiKey).not.toHaveBeenCalled();
    expect(mock.updateModelApiConfig).toHaveBeenCalledWith(
      "config-1",
      expect.objectContaining({ api_key_secret_ref: "credential-old" }),
    );
  });

  it("reports update success with old-key cleanup failure", async () => {
    const mock = api();
    mock.storeModelApiKey.mockResolvedValue({
      credential_id: "credential-new",
      kind: "api_key",
    });
    mock.updateModelApiConfig.mockResolvedValue({
      ...current,
      api_key_secret_ref: "credential-new",
    });
    mock.deleteModelApiKey.mockRejectedValue({ code: "VAULT_OPERATION_FAILED" });

    await expect(
      updateProvider(mock, current, draft, "replacement"),
    ).resolves.toMatchObject({
      kind: "partial_success",
      value: { api_key_secret_ref: "credential-new" },
      cleanupError: { code: "VAULT_OPERATION_FAILED" },
    });
    expect(mock.updateModelApiConfig.mock.invocationCallOrder[0]).toBeLessThan(
      mock.deleteModelApiKey.mock.invocationCallOrder[0],
    );
  });

  it("reports both update and replacement-Key cleanup failures", async () => {
    const mock = api();
    mock.storeModelApiKey.mockResolvedValue({
      credential_id: "credential-new",
      kind: "api_key",
    });
    mock.updateModelApiConfig.mockRejectedValue({
      code: "MODEL_API_CONFIG_PERSISTENCE_FAILED",
    });
    mock.deleteModelApiKey.mockRejectedValue({ code: "VAULT_OPERATION_FAILED" });

    await expect(
      updateProvider(mock, current, draft, "replacement"),
    ).rejects.toMatchObject({
      primaryError: { code: "MODEL_API_CONFIG_PERSISTENCE_FAILED" },
      cleanupError: { code: "VAULT_OPERATION_FAILED" },
    });
  });

  it("never deletes the key when metadata deletion is rejected", async () => {
    const mock = api();
    mock.deleteModelApiConfig.mockRejectedValue({
      code: "MODEL_API_CONFIG_IN_USE",
    });

    await expect(deleteProvider(mock, current)).rejects.toBeInstanceOf(
      ProviderMutationFailure,
    );
    expect(mock.deleteModelApiKey).not.toHaveBeenCalled();
  });

  it("does not delete a Key when metadata deletion returns false", async () => {
    const mock = api();
    mock.deleteModelApiConfig.mockResolvedValue(false);

    await expect(deleteProvider(mock, current)).rejects.toMatchObject({
      primaryError: { code: "UI_MODEL_API_CONFIG_DELETE_FALSE" },
    });
    expect(mock.deleteModelApiKey).not.toHaveBeenCalled();
  });

  it("reports partial success when deleted metadata leaves an orphan Key", async () => {
    const mock = api();
    mock.deleteModelApiConfig.mockResolvedValue(true);
    mock.deleteModelApiKey.mockRejectedValue({ code: "VAULT_OPERATION_FAILED" });

    await expect(deleteProvider(mock, current)).resolves.toMatchObject({
      kind: "partial_success",
      value: true,
      cleanupError: { code: "VAULT_OPERATION_FAILED" },
    });
    expect(mock.deleteModelApiConfig.mock.invocationCallOrder[0]).toBeLessThan(
      mock.deleteModelApiKey.mock.invocationCallOrder[0],
    );
  });
});
