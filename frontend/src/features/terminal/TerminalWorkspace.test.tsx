// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { useTerminalUiStore } from "../../stores/terminal-ui-store";
import { TerminalOutputBuffer } from "./terminal-output-buffer";
import type {
  TerminalSessionModel,
  TerminalSessionState,
} from "./terminal-session";
import { TerminalWorkspace } from "./TerminalWorkspace";

vi.mock("./TerminalTab", () => ({
  TerminalTab: () => <div />,
}));

const session = (
  id: string,
  state: TerminalSessionState,
): TerminalSessionModel => ({
  tabId: id,
  title: `Tab ${id}`,
  ptySessionId: state === "CONNECTED" ? `pty-${id}` : null,
  sshSessionId: state === "CONNECTED" ? `ssh-${id}` : null,
  connectionId: `connection-${id}`,
  state,
  generation: 1,
});

const connected = (id: string) => session(id, "CONNECTED");
const disconnected = (id: string) => session(id, "DISCONNECTED");

const makeProps = () => ({
  outputBuffer: new TerminalOutputBuffer(),
  runtimeReady: true,
  fitRequestKey: 0,
  onWrite: vi.fn().mockResolvedValue(undefined),
  onResize: vi.fn(),
  onReconnect: vi.fn(),
  onDisconnect: vi.fn(),
  onCloseConfirmed: vi.fn(),
  onFocusChange: vi.fn(),
  onSelectConnection: vi.fn(),
  onCreateConnection: vi.fn(),
});

const renderWorkspace = (sessions: TerminalSessionModel[]) => {
  const props = makeProps();
  for (const item of sessions) {
    props.outputBuffer.registerTab(item.tabId, item.generation);
  }
  return {
    props,
    view: render(<TerminalWorkspace {...props} sessions={sessions} />),
  };
};

describe("TerminalWorkspace", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
  });
  beforeEach(() => {
    useTerminalUiStore.getState().reset();
  });
  afterEach(cleanup);

  it("switches the right-edge status with the active session", async () => {
    renderWorkspace([connected("a"), disconnected("b")]);
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Tab b/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(screen.getByTestId("active-session-status")).toHaveTextContent(
      "Disconnected",
    );
    fireEvent.click(screen.getByRole("tab", { name: /Tab a/ }));
    expect(screen.getByTestId("active-session-status")).toHaveTextContent(
      "Connected",
    );
  });

  it("uses mutually exclusive actions for the right-clicked session", () => {
    const { props } = renderWorkspace([connected("a")]);
    fireEvent.contextMenu(screen.getByRole("tab", { name: /Tab a/ }), {
      clientX: 20,
      clientY: 40,
    });
    expect(
      screen.getByRole("menuitem", { name: "Reconnect" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("menuitem", { name: "Disconnect" }));
    expect(props.onDisconnect).toHaveBeenCalledWith(
      expect.objectContaining({ tabId: "a" }),
    );
  });

  it("requires confirmation before closing a tab", () => {
    const { props } = renderWorkspace([connected("a")]);
    fireEvent.click(screen.getByRole("button", { name: "Close Tab a" }));
    expect(props.onCloseConfirmed).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Close session?" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Close session" }));
    expect(props.onCloseConfirmed).toHaveBeenCalledWith(
      expect.objectContaining({ tabId: "a" }),
    );
  });

  it("keeps close controls outside tabs and reconciles the selected sibling", async () => {
    const { props, view } = renderWorkspace([connected("a"), connected("b")]);
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Tab b/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    const close = screen.getByRole("button", { name: "Close Tab b" });
    expect(close.closest('[role="tab"]')).toBeNull();
    view.rerender(
      <TerminalWorkspace {...props} sessions={[connected("a")]} />,
    );
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Tab a/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
  });

  it("keeps the stage as the explicit flexible region with cleanup notices", () => {
    const props = makeProps();
    const { rerender } = render(
      <TerminalWorkspace {...props} sessions={[]} errorNotice={null} />,
    );
    expect(screen.getByTestId("terminal-stage")).toHaveClass(
      "min-h-0",
      "min-w-0",
      "flex-1",
    );
    rerender(
      <TerminalWorkspace
        {...props}
        sessions={[]}
        errorNotice={<div role="alert">Failure</div>}
        cleanupNotices={<div>Cleanup failure</div>}
      />,
    );
    expect(screen.getByTestId("terminal-stage")).toHaveClass(
      "min-h-0",
      "min-w-0",
      "flex-1",
    );
    expect(screen.getByText("Cleanup failure")).toBeVisible();
  });
});
