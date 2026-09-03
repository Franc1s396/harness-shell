// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const diagnosticsMocks = vi.hoisted(() => ({
  getLogDirectory: vi.fn(),
  openLogDirectory: vi.fn(),
}));
vi.mock("../../api/diagnostics", () => ({
  diagnosticsApi: diagnosticsMocks,
  normalizeDiagnosticsCommandError: (error: unknown) => {
    if (typeof error === "object" && error !== null && "code" in error) {
      const candidate = error as { code: unknown; message?: unknown };
      return {
        code: String(candidate.code),
        message: String(candidate.message ?? "Diagnostics operation failed."),
      };
    }
    return {
      code: "DIAGNOSTICS_COMMAND_FAILED",
      message: "Diagnostics operation failed.",
    };
  },
}));

import { i18n, i18nReady } from "../../i18n";
import { useLocaleStore } from "../../stores/locale-store";
import { SettingsDialog } from "./SettingsDialog";

describe("SettingsDialog", () => {
  afterEach(cleanup);

  beforeEach(async () => {
    localStorage.clear();
    await i18nReady;
    await i18n.changeLanguage("en");
    useLocaleStore.getState().reset();
    diagnosticsMocks.getLogDirectory.mockReset();
    diagnosticsMocks.openLogDirectory.mockReset();
    diagnosticsMocks.getLogDirectory.mockResolvedValue(
      { available: true },
    );
    diagnosticsMocks.openLogDirectory.mockResolvedValue(undefined);
  });

  it("uses modal categories and preserves the Language setting", async () => {
    render(
      <SettingsDialog
        open
        initialCategory="general"
        onClose={vi.fn()}
        modelProviders={<div>Provider panel</div>}
      />,
    );

    expect(screen.getByRole("dialog", { name: "Settings" })).toBeVisible();
    expect(screen.getByRole("button", { name: "General" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Model Providers" }),
    ).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Language" })).toBeVisible();

    fireEvent.change(screen.getByRole("combobox", { name: "Language" }), {
      target: { value: "zh-CN" },
    });
    await waitFor(() =>
      expect(useLocaleStore.getState().languageMode).toBe("zh-CN"),
    );
    await i18n.changeLanguage("en");
    fireEvent.click(screen.getByRole("button", { name: "Model Providers" }));
    expect(screen.getByText("Provider panel")).toBeVisible();
  });

  it("exposes an explicit close action", () => {
    const onClose = vi.fn();
    render(
      <SettingsDialog
        open
        initialCategory="general"
        onClose={onClose}
        modelProviders={<div>Provider panel</div>}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close settings" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("opens directly on the requested category only on an open transition", () => {
    const props = {
      onClose: vi.fn(),
      modelProviders: <div>Provider panel</div>,
    };
    const view = render(
      <SettingsDialog
        {...props}
        open={false}
        initialCategory="general"
      />,
    );
    view.rerender(
      <SettingsDialog
        {...props}
        open
        initialCategory="modelProviders"
      />,
    );
    expect(screen.getByText("Provider panel")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "General" }));
    view.rerender(
      <SettingsDialog
        {...props}
        open
        initialCategory="modelProviders"
      />,
    );
    expect(screen.getByRole("combobox", { name: "Language" })).toBeVisible();
  });

  it("shows availability without displaying an absolute log path", async () => {
    diagnosticsMocks.getLogDirectory.mockResolvedValueOnce({ available: true });

    render(
      <SettingsDialog
        open
        initialCategory="general"
        onClose={vi.fn()}
        modelProviders={<div>Provider panel</div>}
      />,
    );

    expect(await screen.findByText("The log directory is available.")).toBeVisible();
    expect(screen.queryByText(/C:\\/)).not.toBeInTheDocument();
    expect(diagnosticsMocks.getLogDirectory).toHaveBeenCalledOnce();
  });

  it("opens the fixed log directory without passing a path", async () => {
    render(
      <SettingsDialog
        open
        initialCategory="general"
        onClose={vi.fn()}
        modelProviders={<div>Provider panel</div>}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Open log directory" }),
    );
    await waitFor(() =>
      expect(diagnosticsMocks.openLogDirectory).toHaveBeenCalledOnce(),
    );
    expect(diagnosticsMocks.openLogDirectory).toHaveBeenCalledWith();
  });

  it("renders the structured path resolution failure", async () => {
    diagnosticsMocks.getLogDirectory.mockRejectedValueOnce({
      code: "LOG_DIRECTORY_UNAVAILABLE",
      message: "The application log directory is not available.",
    });

    render(
      <SettingsDialog
        open
        initialCategory="general"
        onClose={vi.fn()}
        modelProviders={<div>Provider panel</div>}
      />,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("LOG_DIRECTORY_UNAVAILABLE");
    expect(alert).toHaveTextContent(
      "The application log directory is not available.",
    );
    expect(alert.querySelector("strong")).toHaveTextContent(
      "LOG_DIRECTORY_UNAVAILABLE",
    );
  });

  it("keeps an open failure visible and permits retry", async () => {
    diagnosticsMocks.openLogDirectory
      .mockRejectedValueOnce({
        code: "LOG_DIRECTORY_OPEN_FAILED",
        message: "The application log directory could not be opened.",
      })
      .mockResolvedValueOnce(undefined);

    render(
      <SettingsDialog
        open
        initialCategory="general"
        onClose={vi.fn()}
        modelProviders={<div>Provider panel</div>}
      />,
    );

    const button = await screen.findByRole("button", {
      name: "Open log directory",
    });
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "LOG_DIRECTORY_OPEN_FAILED",
    );
    fireEvent.click(button);
    await waitFor(() =>
      expect(diagnosticsMocks.openLogDirectory).toHaveBeenCalledTimes(2),
    );
  });

  it("ignores an old pending availability response after close and reopen", async () => {
    let resolveOld: (value: { available: boolean }) => void = () => undefined;
    const oldRequest = new Promise<{ available: boolean }>((resolve) => {
      resolveOld = resolve;
    });
    diagnosticsMocks.getLogDirectory
      .mockReturnValueOnce(oldRequest)
      .mockResolvedValueOnce({ available: true });
    const props = {
      initialCategory: "general" as const,
      onClose: vi.fn(),
      modelProviders: <div>Provider panel</div>,
    };
    const view = render(<SettingsDialog {...props} open />);

    view.rerender(<SettingsDialog {...props} open={false} />);
    view.rerender(<SettingsDialog {...props} open />);
    expect(await screen.findByText("The log directory is available.")).toBeVisible();

    resolveOld({ available: false });
    await waitFor(() =>
      expect(screen.getByText("The log directory is available.")).toBeVisible(),
    );
  });
});
