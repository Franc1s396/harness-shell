// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import {
  clampSidebarWidth,
  migrateWorkspaceState,
  persistedWorkspaceState,
  useWorkspaceUiStore,
} from "./workspace-ui-store";

describe("workspace UI store", () => {
  beforeEach(() => {
    localStorage.clear();
    useWorkspaceUiStore.getState().reset();
  });

  it("clamps against both design bounds and the terminal budget", () => {
    expect(clampSidebarWidth(100, 1280)).toBe(240);
    expect(clampSidebarWidth(900, 1280)).toBe(420);
    expect(clampSidebarWidth(420, 900)).toBe(248);
  });

  it("persists only approved workspace preferences", () => {
    const state = {
      ...useWorkspaceUiStore.getState(),
      password: "SECRET_MARKER",
      ptyOutput: "SECRET_MARKER",
      sessionId: "session-1",
    };
    expect(persistedWorkspaceState(state)).toEqual({
      sidebarVisible: true,
      sidebarWidth: 280,
      activeActivity: "connections",
    });
    expect(JSON.stringify(persistedWorkspaceState(state))).not.toContain("SECRET_MARKER");
  });

  it("bumps layout revision when closing a dialog or changing the sidebar", () => {
    const before = useWorkspaceUiStore.getState().layoutRevision;
    useWorkspaceUiStore.getState().openCreateConnection();
    useWorkspaceUiStore.getState().closeConnectionDialog();
    useWorkspaceUiStore.getState().setSidebarVisible(false);
    expect(useWorkspaceUiStore.getState().layoutRevision).toBe(before + 2);
  });

  it("resets an unknown schema version to UI defaults only", () => {
    expect(
      migrateWorkspaceState(
        { sidebarVisible: false, sidebarWidth: 410, activeActivity: "terminal" },
        99,
      ),
    ).toEqual({ sidebarVisible: true, sidebarWidth: 280, activeActivity: "connections" });
  });
});
