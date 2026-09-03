// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelApiConfig } from "../../api/agent";
import { ModelProvidersPanel, type ModelProvidersPanelProps } from "./ModelProvidersPanel";
import { ProviderMutationFailure } from "./provider-config-actions";

const config: ModelApiConfig = {
  api_config_id: "config-1",
  display_name: "Production",
  api_type: "RESPONSES",
  base_url: "https://api.example/v1",
  model: "gpt-5",
  api_key_secret_ref: "credential-secret-ref",
  enabled: true,
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:00Z",
};

const props: ModelProvidersPanelProps = {
  configs: [config],
  loading: false,
  error: null,
  mutationError: null,
  activeApiConfigIds: new Set(),
  onCreate: vi.fn(async () => undefined),
  onUpdate: vi.fn(async () => undefined),
  onDelete: vi.fn(async () => undefined),
  onRetry: vi.fn(async () => undefined),
};

describe("ModelProvidersPanel", () => {
  afterEach(cleanup);

  it("disables edit and delete only for a config used by an active local Run", () => {
    render(
      <ModelProvidersPanel
        {...props}
        activeApiConfigIds={new Set(["config-1"])}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Edit Production" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Delete Production" }),
    ).toBeDisabled();
    expect(screen.getByText("In use by an active Run")).toBeVisible();
    expect(screen.queryByText(config.api_key_secret_ref)).not.toBeInTheDocument();
  });

  it("keeps delete confirmation open when deletion rejects", async () => {
    const onDelete = vi.fn(async () => {
      throw new Error("delete rejected");
    });
    const view = render(<ModelProvidersPanel {...props} onDelete={onDelete} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Production" }));
    expect(screen.getByRole("dialog", { name: "Delete Provider?" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() => expect(onDelete).toHaveBeenCalledWith(config));
    expect(screen.getByRole("dialog", { name: "Delete Provider?" })).toBeVisible();

    view.rerender(
      <ModelProvidersPanel
        {...props}
        onDelete={onDelete}
        mutationError={new ProviderMutationFailure(
          {
            code: "MODEL_API_CONFIG_PERSISTENCE_FAILED",
            message: "Delete failed.",
          },
        )}
      />,
    );
    expect(
      within(screen.getByRole("dialog", { name: "Delete Provider?" })).getByRole(
        "alert",
      ),
    ).toHaveTextContent(
      "Provider operation failed: MODEL_API_CONFIG_PERSISTENCE_FAILED",
    );
  });

  it("renders explicit loading, empty, and load-error states", () => {
    const view = render(<ModelProvidersPanel {...props} configs={[]} loading />);
    expect(screen.getByText("Loading providers…")).toBeVisible();
    view.rerender(
      <ModelProvidersPanel
        {...props}
        configs={[]}
        error={{ code: "MODEL_LIST_FAILED", message: "List failed." }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("MODEL_LIST_FAILED");
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    view.rerender(<ModelProvidersPanel {...props} configs={[]} />);
    expect(screen.getByText("No model providers configured.")).toBeVisible();
  });

  it("shows one aggregate Provider mutation failure", () => {
    render(
      <ModelProvidersPanel
        {...props}
        mutationError={new ProviderMutationFailure({
          code: "MODEL_API_CONFIG_PERSISTENCE_FAILED",
          message: "Save failed.",
        })}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Provider operation failed: MODEL_API_CONFIG_PERSISTENCE_FAILED",
    );
  });
});
