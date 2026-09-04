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
    streamAgentTurn: vi.fn(),
  }) satisfies AgentApi;

describe("provider config actions", () => {
  it("creates Provider metadata and Key through one aggregate API call", async () => {
    const mock = api();
    mock.createModelApiConfig.mockResolvedValue(current);

    await expect(createProvider(mock, draft, "secret")).resolves.toEqual({
      kind: "success",
      value: current,
    });
    expect(mock.createModelApiConfig).toHaveBeenCalledWith(
      expect.not.objectContaining({ api_key_secret_ref: expect.anything() }),
      "secret",
    );
  });

  it("surfaces an aggregate create failure without UI compensation", async () => {
    const mock = api();
    mock.createModelApiConfig.mockRejectedValue({
      code: "MODEL_API_CONFIG_PERSISTENCE_FAILED",
    });

    await expect(createProvider(mock, draft, "secret")).rejects.toMatchObject({
      name: "ProviderMutationFailure",
      primaryError: { code: "MODEL_API_CONFIG_PERSISTENCE_FAILED" },
    });
  });

  it("omits a replacement Key when edit secret is blank", async () => {
    const mock = api();
    mock.updateModelApiConfig.mockResolvedValue(current);

    await updateProvider(mock, current, draft, "");

    expect(mock.updateModelApiConfig).toHaveBeenCalledWith(
      "config-1",
      expect.not.objectContaining({ api_key_secret_ref: expect.anything() }),
      undefined,
    );
  });

  it("passes a replacement Key through the Provider update", async () => {
    const mock = api();
    mock.updateModelApiConfig.mockResolvedValue(current);

    await updateProvider(mock, current, draft, "replacement");

    expect(mock.updateModelApiConfig).toHaveBeenCalledWith(
      "config-1",
      expect.any(Object),
      "replacement",
    );
  });

  it("deletes Provider metadata and its owned Key through one API call", async () => {
    const mock = api();
    mock.deleteModelApiConfig.mockResolvedValue(true);

    await expect(deleteProvider(mock, current)).resolves.toEqual({
      kind: "success",
      value: true,
    });
    expect(mock.deleteModelApiConfig).toHaveBeenCalledOnce();
  });

  it("rejects a false aggregate deletion", async () => {
    const mock = api();
    mock.deleteModelApiConfig.mockResolvedValue(false);

    await expect(deleteProvider(mock, current)).rejects.toBeInstanceOf(
      ProviderMutationFailure,
    );
  });
});
