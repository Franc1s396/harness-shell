import { describe, expect, it } from "vitest";

import { resolveResponsiveWorkspace } from "./workspace-layout";

describe("responsive workspace", () => {
  it("collapses only the Agent explanation at medium width", () => {
    expect(resolveResponsiveWorkspace(1200, true, true)).toEqual({
      sidebarVisible: true,
      agentRailExpanded: false,
    });
  });

  it("collapses the explorer at narrow width but never the shell", () => {
    expect(resolveResponsiveWorkspace(1000, true, true)).toEqual({
      sidebarVisible: false,
      agentRailExpanded: false,
    });
  });
});
