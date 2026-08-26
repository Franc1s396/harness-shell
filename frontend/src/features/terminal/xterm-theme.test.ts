// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { createXtermTheme } from "./xterm-theme";

describe("xterm theme", () => {
  it("reads CSS tokens and crashes when a token is missing", () => {
    const root = document.createElement("div");
    root.style.setProperty("--color-app", "#0b1017");
    root.style.setProperty("--color-ink", "#e5edf5");
    root.style.setProperty("--color-accent", "#4fd1bb");
    root.style.setProperty("--color-accent-soft", "#173631");
    expect(createXtermTheme(root)).toEqual({
      background: "#0b1017",
      foreground: "#e5edf5",
      cursor: "#4fd1bb",
      selectionBackground: "#173631",
    });
    root.style.removeProperty("--color-accent");
    expect(() => createXtermTheme(root)).toThrow(
      "Missing xterm theme token: --color-accent",
    );
  });
});
