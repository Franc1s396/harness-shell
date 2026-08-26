// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
import { AppShell } from "./AppShell";

const renderShell = () =>
  render(
    <AppShell
      explorer={<div>Explorer</div>}
      terminal={<div>Terminal surface</div>}
      runtimeState="READY"
      sshState="DISCONNECTED"
      hostKeyState="unknown"
    />,
  );

describe("AppShell", () => {
  afterEach(cleanup);

  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1440,
    });
    useWorkspaceUiStore.getState().reset();
  });

  it("keeps terminal content in the flexible workbench region", () => {
    renderShell();
    expect(screen.getByText("Terminal surface").parentElement).toHaveClass(
      "min-h-0",
      "min-w-0",
    );
  });

  it("shows future activities as disabled milestone controls", () => {
    renderShell();
    expect(screen.getByRole("button", { name: /Files/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /SFTP/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Settings/i })).toBeDisabled();
    expect(screen.getAllByText(/M3/).length).toBeGreaterThanOrEqual(4);
  });

  it("collapses the explorer without hiding the terminal", () => {
    renderShell();
    fireEvent.click(
      screen.getByRole("button", { name: /toggle connection sidebar/i }),
    );
    expect(screen.queryByText("Explorer")).not.toBeInTheDocument();
    expect(screen.getByText("Terminal surface")).toBeVisible();
  });

  it("keeps edge tooltips inside the viewport", () => {
    renderShell();

    const menuTooltip = screen.getByRole("tooltip", {
      name: /toggle connection sidebar/i,
    });
    expect(menuTooltip).toHaveClass("top-full", "left-0");
    expect(menuTooltip).not.toHaveClass("-translate-x-1/2");

    const milestoneTooltips = screen
      .getAllByRole("tooltip")
      .filter((item) => item.textContent?.includes("M3"));
    expect(milestoneTooltips).toHaveLength(3);
    for (const tooltip of milestoneTooltips) {
      expect(tooltip).toHaveClass(
        "left-full",
        "top-1/2",
        "ml-2",
        "-translate-y-1/2",
      );
    }
  });

  it("keeps the Agent rail informational in M2", () => {
    useWorkspaceUiStore.getState().setAgentRailExpanded(true);
    renderShell();
    expect(screen.getByText("AI Agent")).toBeVisible();
    expect(screen.getByText("M3")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /send|execute|pause|resume|stop/i }),
    ).not.toBeInTheDocument();
  });

  it("resizes the explorer with the keyboard", () => {
    renderShell();
    const separator = screen.getByRole("separator", {
      name: "Resize connection sidebar",
    });
    expect(separator).toHaveAttribute("aria-valuenow", "280");

    fireEvent.keyDown(separator, { key: "ArrowRight" });

    expect(useWorkspaceUiStore.getState().sidebarWidth).toBe(288);
    expect(separator).toHaveAttribute("aria-valuenow", "288");
  });
});
