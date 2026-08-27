// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
import { WorkspaceFrame } from "./WorkspaceFrame";

const setViewport = (width: number) => {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: width,
  });
};

const renderFrame = (overrides: Partial<React.ComponentProps<typeof WorkspaceFrame>> = {}) => {
  const callbacks = {
    onCreateConnection: vi.fn(),
    onEditConnection: vi.fn(),
    onFocusTerminal: vi.fn(),
    onOpenApproval: vi.fn(),
  };
  render(
    <WorkspaceFrame
      connectionNavigator={<div>Connection navigator</div>}
      terminalWorkspace={<div>Terminal surface</div>}
      agentWorkspace={<div>Agent surface</div>}
      runtimeState="READY"
      connectionState="CONNECTED"
      hostKeyState="trusted"
      ptySize={{ cols: 120, rows: 32 }}
      route="Direct"
      environmentLabel="Local"
      connectionName="Production"
      targetSummary="admin@example.com"
      agentWidth={null}
      activeTerminalAvailable
      {...callbacks}
      {...overrides}
    />,
  );
  return callbacks;
};

describe("WorkspaceFrame", () => {
  afterEach(cleanup);

  beforeEach(() => {
    setViewport(1440);
    useWorkspaceUiStore.getState().reset();
  });

  it("renders the wide four-region workbench with independent separators", () => {
    useWorkspaceUiStore.getState().setAgentVisible(true);
    renderFrame({ agentWidth: 480 });

    expect(screen.getByText("Connection navigator")).toBeVisible();
    expect(screen.getByText("Terminal surface")).toBeVisible();
    expect(screen.getByText("Agent surface")).toBeVisible();
    expect(screen.getAllByRole("separator")).toHaveLength(2);
    expect(screen.getByTestId("terminal-region")).toHaveClass(
      "min-h-0",
      "min-w-0",
    );
  });

  it("moves the requested sidebar into a modal Drawer at medium width", () => {
    setViewport(1200);
    renderFrame();

    expect(screen.queryByText("Connection navigator")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /SSH Connections/i }));
    expect(screen.getByRole("dialog", { name: /SSH Connections/i })).toBeVisible();
    expect(screen.getByText("Connection navigator")).toBeVisible();
    expect(useWorkspaceUiStore.getState().sidebarVisible).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /Close/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(useWorkspaceUiStore.getState().sidebarVisible).toBe(true);
  });

  it("opens Agent at 480px and preserves the requested width when constrained", () => {
    renderFrame();
    fireEvent.click(screen.getByRole("button", { name: /Expand Agent/i }));
    expect(screen.getByTestId("agent-region")).toHaveStyle({ width: "480px" });

    setViewport(960);
    fireEvent(window, new Event("resize"));
    expect(screen.getByTestId("agent-region")).toHaveStyle({ width: "352px" });
    expect(useWorkspaceUiStore.getState().agentWidth).toBe(480);
  });

  it("keeps only context and one quick-actions entry in the top bar", () => {
    const callbacks = renderFrame();
    const header = screen.getByRole("banner");
    expect(within(header).getByText("Harness Shell")).toBeVisible();
    expect(within(header).getByText("Local")).toBeVisible();
    expect(within(header).getByText("Production")).toBeVisible();
    expect(within(header).getByText("admin@example.com")).toBeVisible();
    expect(within(header).getByText("CONNECTED")).toBeVisible();
    expect(within(header).queryByText("Runtime")).not.toBeInTheDocument();
    expect(within(header).queryByText("Host Key")).not.toBeInTheDocument();
    expect(within(header).queryByRole("combobox")).not.toBeInTheDocument();

    fireEvent.click(
      within(header).getByRole("button", { name: /Quick actions/i }),
    );
    const menu = screen.getByRole("menu", { name: /Quick actions/i });
    expect(within(menu).getAllByRole("menuitem")).toHaveLength(4);
    fireEvent.click(within(menu).getByRole("menuitem", { name: /Focus/i }));
    expect(callbacks.onFocusTerminal).toHaveBeenCalledTimes(1);
  });

  it("keeps operational detail in the 23px status strip", () => {
    useWorkspaceUiStore.getState().setAgentVisible(true);
    renderFrame({ agentWidth: 480 });
    const status = screen.getByRole("contentinfo");
    expect(status).toHaveClass("h-[23px]");
    expect(status).toHaveTextContent("Runtime: READY");
    expect(status).toHaveTextContent("SSH: CONNECTED");
    expect(status).toHaveTextContent("Host Key: trusted");
    expect(status).toHaveTextContent("PTY size: 120×32");
    expect(status).toHaveTextContent("Agent: 480px");
    expect(status).toHaveTextContent("Route: Direct");
  });

  it("enables Connections, Approval, and Settings but not future activities", () => {
    const callbacks = renderFrame();
    expect(screen.getByRole("button", { name: /Files/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /SFTP/i })).toBeDisabled();
    const settings = screen.getByRole("button", { name: /Settings/i });
    const approval = screen.getByRole("button", { name: /Approval/i });
    expect(settings).toBeEnabled();
    expect(approval).toBeEnabled();
    fireEvent.click(settings);
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Language" })).toBeVisible();
    fireEvent.click(approval);
    expect(callbacks.onOpenApproval).toHaveBeenCalledTimes(1);
  });
});
