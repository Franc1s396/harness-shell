import { beforeEach, describe, expect, it } from "vitest";

import { useTerminalUiStore } from "./terminal-ui-store";

describe("terminal UI store", () => {
  beforeEach(() => useTerminalUiStore.getState().reset());

  it("selects the newest tab when the active tab disappears", () => {
    useTerminalUiStore.getState().reconcileTabs(["a", "b"]);
    expect(useTerminalUiStore.getState()).toMatchObject({
      activeTabId: "b",
      tabOrder: ["a", "b"],
    });
    useTerminalUiStore.getState().setActiveTab("a");
    useTerminalUiStore.getState().reconcileTabs(["b"]);
    expect(useTerminalUiStore.getState().activeTabId).toBe("b");
    useTerminalUiStore.getState().requestFocus();
    expect(useTerminalUiStore.getState().focusRevision).toBe(1);
  });
});
