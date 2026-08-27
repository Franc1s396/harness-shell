import { describe, expect, it } from "vitest";

import {
  isCurrentBinding,
  sessionActions,
  sessionStatusKey,
  sessionStatusTone,
  type TerminalSessionModel,
} from "./terminal-session";

const model = (state: TerminalSessionModel["state"]): TerminalSessionModel => ({
  tabId: "tab-1",
  connectionId: "connection-1",
  title: "Production",
  state,
  sshSessionId: state === "CONNECTED" ? "ssh-1" : null,
  ptySessionId: state === "CONNECTED" ? "pty-1" : null,
  generation: 2,
});

describe("terminal session contract", () => {
  it.each([
    ["CONNECTING", false, false, "terminal.states.connecting", "accent"],
    [
      "HOST_KEY_REQUIRED",
      false,
      false,
      "terminal.states.hostKeyRequired",
      "warning",
    ],
    ["CONNECTED", false, true, "terminal.states.connected", "success"],
    [
      "DISCONNECTING",
      false,
      false,
      "terminal.states.disconnecting",
      "warning",
    ],
    [
      "DISCONNECTED",
      true,
      false,
      "terminal.states.disconnected",
      "disconnected",
    ],
    ["FAILED", true, false, "terminal.states.failed", "danger"],
  ] as const)("maps %s", (state, reconnect, disconnect, key, tone) => {
    expect(sessionActions(state)).toEqual({ reconnect, disconnect });
    expect(sessionStatusKey(state)).toBe(key);
    expect(sessionStatusTone(state)).toBe(tone);
  });

  it("accepts only the current tab generation", () => {
    const current = model("CONNECTED");
    expect(
      isCurrentBinding(current, { tabId: "tab-1", generation: 2 }),
    ).toBe(true);
    expect(
      isCurrentBinding(current, { tabId: "tab-1", generation: 1 }),
    ).toBe(false);
    expect(
      isCurrentBinding(current, { tabId: "tab-2", generation: 2 }),
    ).toBe(false);
  });
});
