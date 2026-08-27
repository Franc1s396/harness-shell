import { describe, expect, it } from "vitest";

import {
  DEFAULT_AGENT_WIDTH,
  DEFAULT_SIDEBAR_WIDTH,
  MAX_AGENT_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_AGENT_WIDTH,
  MIN_SIDEBAR_WIDTH,
  MIN_TERMINAL_WIDTH,
  agentWidthBounds,
  resolveEffectiveAgentWidth,
  resolveResponsiveWorkspace,
  resolveTerminalWidth,
} from "./workspace-layout";

describe("responsive workspace", () => {
  it("keeps the sidebar inline only at the wide breakpoint", () => {
    expect(resolveResponsiveWorkspace(1440, true, true)).toEqual({
      sidebarInline: true,
      sidebarDrawerAvailable: false,
      agentVisible: true,
    });
    expect(resolveResponsiveWorkspace(1200, true, true)).toEqual({
      sidebarInline: false,
      sidebarDrawerAvailable: true,
      agentVisible: true,
    });
    expect(resolveResponsiveWorkspace(960, false, false)).toEqual({
      sidebarInline: false,
      sidebarDrawerAvailable: true,
      agentVisible: false,
    });
  });

  it("crashes below the supported 960px viewport", () => {
    expect(() => resolveResponsiveWorkspace(959, true, true)).toThrow(
      "Unsupported workspace width: 959",
    );
  });

  it("exposes the approved requested-width constants", () => {
    expect({
      sidebar: [MIN_SIDEBAR_WIDTH, DEFAULT_SIDEBAR_WIDTH, MAX_SIDEBAR_WIDTH],
      agent: [MIN_AGENT_WIDTH, DEFAULT_AGENT_WIDTH, MAX_AGENT_WIDTH],
      terminal: MIN_TERMINAL_WIDTH,
    }).toEqual({
      sidebar: [240, 280, 380],
      agent: [320, 480, 640],
      terminal: 560,
    });
  });

  it.each([
    { viewportWidth: 960, sidebarInline: false, max: 352 },
    { viewportWidth: 1200, sidebarInline: false, max: 592 },
    { viewportWidth: 1440, sidebarInline: true, max: 548 },
    { viewportWidth: 1920, sidebarInline: true, max: 640 },
  ])(
    "preserves the terminal budget at $viewportWidth px",
    ({ viewportWidth, sidebarInline, max }) => {
      const bounds = agentWidthBounds({
        viewportWidth,
        sidebarInline,
        sidebarWidth: DEFAULT_SIDEBAR_WIDTH,
      });
      expect(bounds).toEqual({ min: MIN_AGENT_WIDTH, max });
      const agentWidth = resolveEffectiveAgentWidth(DEFAULT_AGENT_WIDTH, bounds);
      expect(
        resolveTerminalWidth({
          viewportWidth,
          sidebarInline,
          sidebarWidth: DEFAULT_SIDEBAR_WIDTH,
          agentVisible: true,
          agentWidth,
        }),
      ).toBeGreaterThanOrEqual(MIN_TERMINAL_WIDTH);
    },
  );
});
