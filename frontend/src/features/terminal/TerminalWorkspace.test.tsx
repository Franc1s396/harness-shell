// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { useTerminalUiStore } from "../../stores/terminal-ui-store";
import {
  TerminalWorkspace,
  type TerminalTabModel,
} from "./TerminalWorkspace";

vi.mock("./TerminalTab", () => ({
  TerminalTab: ({ active, enabled }: { active: boolean; enabled: boolean }) => (
    <div
      data-testid="xterm"
      data-active={String(active)}
      data-enabled={String(enabled)}
    />
  ),
}));

const tab = (id: string): TerminalTabModel => ({
  tabId: id,
  title: `Tab ${id}`,
  ptySessionId: `pty-${id}`,
  sshSessionId: `ssh-${id}`,
  connectionId: `connection-${id}`,
  output: [],
  state: "OPEN",
});

const props = {
  runtimeReady: true,
  fitRequestKey: 0,
  onWrite: vi.fn().mockResolvedValue(undefined),
  onResize: vi.fn(),
  onClose: vi.fn(),
  onFocusChange: vi.fn(),
  onSelectConnection: vi.fn(),
  onCreateConnection: vi.fn(),
};

describe("TerminalWorkspace", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
  });
  beforeEach(() => {
    useTerminalUiStore.getState().reset();
    props.onClose.mockReset();
  });
  afterEach(cleanup);

  it("reconciles selection and uses sibling close buttons", async () => {
    const view = render(
      <TerminalWorkspace {...props} tabs={[tab("a"), tab("b")]} />,
    );
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Tab b/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    const close = screen.getByRole("button", { name: "Close Tab b" });
    expect(close.closest('[role="tab"]')).toBeNull();
    fireEvent.click(close);
    expect(props.onClose).toHaveBeenCalledWith(
      expect.objectContaining({ tabId: "b" }),
    );
    view.rerender(<TerminalWorkspace {...props} tabs={[tab("a")]} />);
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Tab a/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
  });

  it("keeps the stage as the explicit flexible region with or without errors", () => {
    const { rerender } = render(
      <TerminalWorkspace {...props} tabs={[]} errorNotice={null} />,
    );
    expect(screen.getByTestId("terminal-stage")).toHaveClass(
      "min-h-0",
      "min-w-0",
      "flex-1",
    );
    rerender(
      <TerminalWorkspace
        {...props}
        tabs={[]}
        errorNotice={<div role="alert">Failure</div>}
      />,
    );
    expect(screen.getByTestId("terminal-stage")).toHaveClass(
      "min-h-0",
      "min-w-0",
      "flex-1",
    );
  });
});
