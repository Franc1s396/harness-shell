// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { createXtermTheme } from "./xterm-theme";

describe("xterm theme", () => {
  it("reads CSS tokens and crashes when a token is missing", () => {
    const root = document.createElement("div");
    root.style.setProperty("--color-app", "#0b1017");
    root.style.setProperty("--color-ink", "#dce6ee");
    root.style.setProperty("--color-accent", "#5fa8ff");
    root.style.setProperty("--color-accent-soft", "#152a42");
    expect(createXtermTheme(root)).toEqual({
      background: "#0b1017",
      foreground: "#dce6ee",
      cursor: "#5fa8ff",
      selectionBackground: "#152a42",
    });
    root.style.removeProperty("--color-accent");
    expect(() => createXtermTheme(root)).toThrow(
      "Missing xterm theme token: --color-accent",
    );
  });
});
