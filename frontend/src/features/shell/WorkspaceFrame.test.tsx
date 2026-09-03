// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
import { WorkspaceFrame } from "./WorkspaceFrame";

const tauriWindow = vi.hoisted(() => ({
  close: vi.fn(),
  onCloseRequested: vi.fn(),
  emitCloseRequested: null as null | ((event: { preventDefault: () => void }) => void),
}));

const diagnosticsApi = vi.hoisted(() => ({
  getLogDirectory: vi.fn(),
  openLogDirectory: vi.fn(),
}));

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => tauriWindow,
}));

vi.mock("../../api/diagnostics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/diagnostics")>()),
  diagnosticsApi,
}));

const setViewport = (width: number) => {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: width,
  });
};

const activeTransfer = {
  operation_id: "operation-active",
  direction: "download" as const,
  phase: "transferring" as const,
  display_name: "payload.bin",
  remote_path: "/home/tester/payload.bin",
  host_label: "test.example",
  bytes_completed: 4,
  bytes_total: 8,
  cancellable: true,
};

const renderFrame = (overrides: Partial<React.ComponentProps<typeof WorkspaceFrame>> = {}) => {
  const callbacks = {
    onCreateConnection: vi.fn(),
    onEditConnection: vi.fn(),
    onFocusTerminal: vi.fn(),
    onSettingsOpening: vi.fn(),
  };
  render(
    <WorkspaceFrame
      connectionNavigator={<div>Connection navigator</div>}
      primaryWorkspace={<div>Terminal surface</div>}
      agentWorkspace={<div>Agent surface</div>}
      modelProviders={<div>Provider panel</div>}
      runtimeState="READY"
      hostKeyState="trusted"
      ptySize={{ cols: 120, rows: 32 }}
      route="Direct"
      environmentLabel="Local"
      connectionName="Production"
      targetSummary="admin@example.com"
      agentWidth={null}
      activeTerminalAvailable
      activeAgentRunCount={0}
      agentBadge="NONE"
      {...callbacks}
      {...overrides}
    />,
  );
  return callbacks;
};

const requestWindowClose = async () => {
  const event = { preventDefault: vi.fn() };
  await vi.waitFor(() =>
    expect(tauriWindow.emitCloseRequested).toBeTypeOf("function"),
  );
  await act(async () => tauriWindow.emitCloseRequested!(event));
  return event;
};

describe("WorkspaceFrame", () => {
  afterEach(cleanup);

  beforeEach(() => {
    setViewport(1440);
    useWorkspaceUiStore.getState().reset();
    tauriWindow.close.mockReset().mockResolvedValue(undefined);
    tauriWindow.emitCloseRequested = null;
    tauriWindow.onCloseRequested.mockReset().mockImplementation(async (handler) => {
      tauriWindow.emitCloseRequested = handler;
      return () => {
        tauriWindow.emitCloseRequested = null;
      };
    });
    diagnosticsApi.getLogDirectory.mockReset().mockResolvedValue({
      available: true,
    });
    diagnosticsApi.openLogDirectory.mockReset().mockResolvedValue(undefined);
  });

  it("confirms an application close even without an active terminal", async () => {
    renderFrame({ activeTerminalAvailable: false });
    const event = { preventDefault: vi.fn() };

    await vi.waitFor(() => expect(tauriWindow.emitCloseRequested).toBeTypeOf("function"));
    await act(async () => tauriWindow.emitCloseRequested!(event));

    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("dialog", { name: "Exit Harness Shell?" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Exit Harness Shell?" })).not.toBeInTheDocument();
    expect(tauriWindow.close).not.toHaveBeenCalled();
  });

  it("requires an explicit wait-or-cancel decision before closing with an active transfer", async () => {
    const cancelTransfer = vi.fn(async () => undefined);
    renderFrame({
      activeSftpTransfer: activeTransfer,
      onCancelActiveSftpTransfer: cancelTransfer,
    });
    const event = { preventDefault: vi.fn() };

    await vi.waitFor(() => expect(tauriWindow.emitCloseRequested).toBeTypeOf("function"));
    await act(async () => tauriWindow.emitCloseRequested!(event));

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Continue waiting" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Cancel and clean up" })).toBeVisible();
    expect(cancelTransfer).not.toHaveBeenCalled();
    expect(tauriWindow.close).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Cancel and clean up" }));
    await vi.waitFor(() =>
      expect(cancelTransfer).toHaveBeenCalledWith("operation-active"),
    );
    expect(tauriWindow.close).not.toHaveBeenCalled();
  });

  it("keeps the close decision open and exposes a failed transfer cancellation", async () => {
    const cancelTransfer = vi.fn(async () => {
      throw {
        code: "SFTP_CANCEL_TOO_LATE",
        message: "The operation is already committing.",
      };
    });
    renderFrame({
      activeSftpTransfer: activeTransfer,
      onCancelActiveSftpTransfer: cancelTransfer,
    });

    await vi.waitFor(() => expect(tauriWindow.emitCloseRequested).toBeTypeOf("function"));
    await act(async () =>
      tauriWindow.emitCloseRequested!({ preventDefault: vi.fn() }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel and clean up" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "SFTP_CANCEL_TOO_LATE: The operation is already committing.",
    );
    expect(
      screen.getByRole("dialog", { name: "Exit Harness Shell?" }),
    ).toBeVisible();
    expect(tauriWindow.close).not.toHaveBeenCalled();
  });

  it("offers only continued waiting while a transfer is committing", async () => {
    renderFrame({
      activeSftpTransfer: {
        ...activeTransfer,
        phase: "committing",
        cancellable: false,
      },
      onCancelActiveSftpTransfer: vi.fn(),
    });

    await vi.waitFor(() => expect(tauriWindow.emitCloseRequested).toBeTypeOf("function"));
    await act(async () =>
      tauriWindow.emitCloseRequested!({ preventDefault: vi.fn() }),
    );

    expect(screen.getByRole("button", { name: "Continue waiting" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cancel and clean up" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Exit Harness Shell" })).not.toBeInTheDocument();
  });

  it("offers Force exit for Agent-only work but never bypasses an SFTP committing gate", async () => {
    renderFrame({ activeAgentRunCount: 1, activeSftpTransfer: null });
    await requestWindowClose();
    expect(screen.getByRole("button", { name: "Force exit" })).toBeVisible();

    cleanup();
    renderFrame({
      activeAgentRunCount: 1,
      activeSftpTransfer: {
        ...activeTransfer,
        phase: "committing",
        cancellable: false,
      },
    });
    await requestWindowClose();
    expect(
      screen.queryByRole("button", { name: "Force exit" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Continue waiting" }),
    ).toBeVisible();
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
    expect(within(header).queryByText("CONNECTED")).not.toBeInTheDocument();
    expect(within(header).queryByText("Runtime")).not.toBeInTheDocument();
    expect(within(header).queryByText("Host Key")).not.toBeInTheDocument();
    expect(within(header).queryByRole("combobox")).not.toBeInTheDocument();

    fireEvent.click(
      within(header).getByRole("button", { name: /Quick actions/i }),
    );
    const menu = screen.getByRole("menu", { name: /Quick actions/i });
    expect(within(menu).getAllByRole("menuitem")).toHaveLength(3);
    fireEvent.click(within(menu).getByRole("menuitem", { name: /Focus/i }));
    expect(callbacks.onFocusTerminal).toHaveBeenCalledTimes(1);
  });

  it("keeps operational detail in the 23px status strip", () => {
    useWorkspaceUiStore.getState().setAgentVisible(true);
    renderFrame({ agentWidth: 480 });
    const status = screen.getByRole("contentinfo");
    expect(status).toHaveClass("h-[23px]");
    expect(status).toHaveTextContent("Runtime: READY");
    expect(status).not.toHaveTextContent(/SSH:/);
    expect(status).toHaveTextContent("Host Key: trusted");
    expect(status).toHaveTextContent("PTY size: 120×32");
    expect(status).toHaveTextContent("Agent: 480px");
    expect(status).toHaveTextContent("Route: Direct");
  });

  it("enables Connections, SFTP, and Settings without exposing the removed Approval activity", () => {
    renderFrame();
    expect(screen.getByRole("button", { name: /Files/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /SFTP/i })).toBeEnabled();
    const settings = screen.getByRole("button", { name: /Settings/i });
    expect(settings).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Approval/i })).not.toBeInTheDocument();
    fireEvent.click(settings);
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Language" })).toBeVisible();
  });

  it("hides both Agent surfaces while the SFTP activity is active", () => {
    useWorkspaceUiStore.getState().setAgentVisible(true);
    useWorkspaceUiStore.getState().setActiveActivity("sftp");
    renderFrame({ agentWidth: 480 });
    expect(screen.queryByTestId("agent-region")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Expand Agent/i })).not.toBeInTheDocument();
  });

  it("does not refocus the open language selector when the frame rerenders", () => {
    renderFrame();
    fireEvent.click(screen.getByRole("button", { name: /Settings/i }));
    const language = screen.getByRole("combobox", { name: "Language" });
    const focusSpy = vi.spyOn(language, "focus");

    try {
      setViewport(1439);
      fireEvent(window, new Event("resize"));

      expect(focusSpy).not.toHaveBeenCalled();
    } finally {
      focusSpy.mockRestore();
    }
  });
});
