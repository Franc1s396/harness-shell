// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelApiConfig } from "../../api/agent";
import {
  AgentProviderDialog,
  validateProviderDraft,
} from "./AgentProviderDialog";

const config: ModelApiConfig = {
  api_config_id: "config-1",
  display_name: "Production",
  api_type: "RESPONSES",
  base_url: "https://api.example/v1",
  model: "gpt-5",
  api_key_secret_ref: "credential-must-never-render",
  enabled: true,
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:00Z",
};

describe("AgentProviderDialog", () => {
  afterEach(cleanup);

  it("never renders the stored credential reference and clears a failed replacement secret", async () => {
    const onSubmit = vi.fn(async () => {
      throw { code: "MODEL_API_CONFIG_PERSISTENCE_FAILED" };
    });
    render(
      <AgentProviderDialog
        open
        mode="edit"
        config={config}
        busy={false}
        error={{
          code: "MODEL_API_CONFIG_PERSISTENCE_FAILED",
          message: "Save failed.",
        }}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByText("API Key safely stored")).toBeVisible();
    expect(screen.queryByText(config.api_key_secret_ref)).not.toBeInTheDocument();
    const key = screen.getByLabelText("API Key");
    expect(key).toHaveAttribute("type", "password");
    expect(key).toHaveValue("");
    fireEvent.change(key, { target: { value: "replacement" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(key).toHaveValue(""));
    expect(screen.getByLabelText("Display name")).toHaveValue("Production");
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ displayName: "Production" }),
      "replacement",
    );
  });

  it("requires a Key only for create and rejects non-HTTP base URLs", () => {
    expect(
      validateProviderDraft(
        {
          displayName: "Production",
          apiType: "RESPONSES",
          baseUrl: "ssh://api.example",
          model: "gpt-5",
          enabled: true,
        },
        "",
        "create",
      ),
    ).toEqual({ baseUrl: "INVALID", apiKey: "REQUIRED" });
    expect(
      validateProviderDraft(
        {
          displayName: "Production",
          apiType: "RESPONSES",
          baseUrl: "https://api.example/v1",
          model: "gpt-5",
          enabled: true,
        },
        "",
        "edit",
      ),
    ).toEqual({});
  });

  it("clears a create secret after a validation-failed submit while preserving the draft", () => {
    const onSubmit = vi.fn(async () => undefined);
    render(
      <AgentProviderDialog
        open
        mode="create"
        config={null}
        busy={false}
        error={null}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "Draft Provider" },
    });
    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "must-be-cleared" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("Display name")).toHaveValue("Draft Provider");
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
