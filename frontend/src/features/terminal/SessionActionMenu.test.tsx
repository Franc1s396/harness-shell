// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { SessionActionMenu } from "./SessionActionMenu";
import type {
  TerminalSessionModel,
  TerminalSessionState,
} from "./terminal-session";

const session = (state: TerminalSessionState): TerminalSessionModel => ({
  tabId: "tab-1",
  connectionId: "connection-1",
  title: "Production",
  state,
  sshSessionId: state === "CONNECTED" ? "ssh-1" : null,
  ptySessionId: state === "CONNECTED" ? "pty-1" : null,
  generation: 1,
});

const callbacks = () => ({
  onClose: vi.fn(),
  onReconnect: vi.fn(),
  onDisconnect: vi.fn(),
});

describe("SessionActionMenu", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
  });
  afterEach(cleanup);

  it.each([
    ["CONNECTING", false, false],
    ["HOST_KEY_REQUIRED", false, false],
    ["CONNECTED", false, true],
    ["DISCONNECTING", false, false],
    ["DISCONNECTED", true, false],
    ["FAILED", true, false],
  ] as const)(
    "uses the action matrix for %s",
    (state, reconnectEnabled, disconnectEnabled) => {
      render(
        <SessionActionMenu
          session={session(state)}
          anchor={{ x: 20, y: 40 }}
          {...callbacks()}
        />,
      );
      expect(
        screen.getByRole("menuitem", { name: "Reconnect" }),
      ).toHaveProperty("disabled", !reconnectEnabled);
      expect(
        screen.getByRole("menuitem", { name: "Disconnect" }),
      ).toHaveProperty("disabled", !disconnectEnabled);
    },
  );

  it("focuses the enabled action and handles every menu navigation key", () => {
    render(
      <SessionActionMenu
        session={session("DISCONNECTED")}
        anchor={{ x: 20, y: 40 }}
        {...callbacks()}
      />,
    );
    const menu = screen.getByRole("menu");
    const reconnect = screen.getByRole("menuitem", { name: "Reconnect" });
    expect(reconnect).toHaveFocus();
    for (const key of ["ArrowDown", "ArrowUp", "Home", "End"]) {
      fireEvent.keyDown(menu, { key });
      expect(reconnect).toHaveFocus();
    }
  });

  it("runs only the enabled action, closes, escapes and restores anchor focus", () => {
    const anchor = document.createElement("button");
    document.body.append(anchor);
    anchor.focus();
    const handlers = callbacks();
    const view = render(
      <SessionActionMenu
        session={session("CONNECTED")}
        anchor={anchor}
        {...handlers}
      />,
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Disconnect" }));
    expect(handlers.onDisconnect).toHaveBeenCalledTimes(1);
    expect(handlers.onReconnect).not.toHaveBeenCalled();
    expect(handlers.onClose).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(handlers.onClose).toHaveBeenCalledTimes(2);
    view.unmount();
    expect(anchor).toHaveFocus();
    anchor.remove();
  });
});
