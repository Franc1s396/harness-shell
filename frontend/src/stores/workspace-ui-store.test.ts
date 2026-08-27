// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import {
  clampAgentWidth,
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

  it("clamps user-requested panel widths to design bounds", () => {
    expect(clampSidebarWidth(100)).toBe(240);
    expect(clampSidebarWidth(900)).toBe(380);
    expect(clampAgentWidth(100)).toBe(320);
    expect(clampAgentWidth(900)).toBe(640);
  });

  it("persists only the five approved workspace preferences", () => {
    const state = {
      ...useWorkspaceUiStore.getState(),
      password: "SECRET_MARKER",
      ptyOutput: "SECRET_MARKER",
      sessionId: "session-1",
    };
    expect(persistedWorkspaceState(state)).toEqual({
      sidebarVisible: true,
      sidebarWidth: 280,
      agentVisible: false,
      agentWidth: 480,
      activeActivity: "connections",
    });
    expect(JSON.stringify(persistedWorkspaceState(state))).not.toContain(
      "SECRET_MARKER",
    );
  });

  it("keeps Drawer, dialog, and layout revisions transient", () => {
    useWorkspaceUiStore.getState().setMediumViewportDrawerOpen(true);
    useWorkspaceUiStore.getState().openCreateConnection();
    useWorkspaceUiStore.getState().bumpLayoutRevision();
    const persisted = persistedWorkspaceState(useWorkspaceUiStore.getState());
    expect(persisted).not.toHaveProperty("mediumViewportDrawerOpen");
    expect(persisted).not.toHaveProperty("connectionDialog");
    expect(persisted).not.toHaveProperty("layoutRevision");
  });

  it("migrates v1 sidebar preferences and supplies M2 Agent defaults", () => {
    expect(
      migrateWorkspaceState(
        {
          sidebarVisible: false,
          sidebarWidth: 360,
          activeActivity: "terminal",
        },
        1,
      ),
    ).toEqual({
      sidebarVisible: false,
      sidebarWidth: 360,
      agentVisible: false,
      agentWidth: 480,
      activeActivity: "connections",
    });
  });

  it("restores explicit defaults for invalid persisted widths", () => {
    expect(
      migrateWorkspaceState(
        {
          sidebarVisible: true,
          sidebarWidth: Number.POSITIVE_INFINITY,
          agentVisible: true,
          agentWidth: 900,
          activeActivity: "settings",
        },
        2,
      ),
    ).toEqual({
      sidebarVisible: true,
      sidebarWidth: 280,
      agentVisible: true,
      agentWidth: 480,
      activeActivity: "settings",
    });
  });

  it("resets an unknown schema version to UI defaults only", () => {
    expect(
      migrateWorkspaceState(
        {
          sidebarVisible: false,
          sidebarWidth: 360,
          agentVisible: true,
          agentWidth: 600,
          activeActivity: "approval",
        },
        99,
      ),
    ).toEqual({
      sidebarVisible: true,
      sidebarWidth: 280,
      agentVisible: false,
      agentWidth: 480,
      activeActivity: "connections",
    });
  });

  it("does not overwrite requested preferences for a responsive Drawer", () => {
    useWorkspaceUiStore.getState().setSidebarVisible(true);
    useWorkspaceUiStore.getState().setSidebarWidth(360);
    useWorkspaceUiStore.getState().setAgentVisible(true);
    useWorkspaceUiStore.getState().setAgentWidth(520);
    useWorkspaceUiStore.getState().setMediumViewportDrawerOpen(true);

    expect(persistedWorkspaceState(useWorkspaceUiStore.getState())).toEqual({
      sidebarVisible: true,
      sidebarWidth: 360,
      agentVisible: true,
      agentWidth: 520,
      activeActivity: "connections",
    });
  });
});
