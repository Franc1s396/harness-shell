import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const config = JSON.parse(
  readFileSync(new URL("../../src-tauri/tauri.conf.json", import.meta.url), "utf8"),
);

describe("desktop UI bounds", () => {
  it("prevents the main window from shrinking below the supported terminal layout", () => {
    const main = config.app.windows.find((window) => window.label === "main");
    expect(main).toMatchObject({
      width: 1440,
      height: 900,
      minWidth: 960,
      minHeight: 640,
    });
  });
});
