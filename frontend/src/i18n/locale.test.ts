// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { i18n, i18nReady } from "./index";
import { resolveLocale } from "./locale";
import { flattenResourceKeys, resources } from "./resources";

describe("locale resolution", () => {
  it.each([
    [["zh-TW"], "zh-TW"],
    [["zh-HK"], "zh-TW"],
    [["zh-MO"], "zh-TW"],
    [["zh-CN"], "zh-CN"],
    [["zh-SG"], "zh-CN"],
    [["en-US"], "en"],
    [[], "en"],
  ] as const)("maps %j to %s", (languages, expected) => {
    expect(resolveLocale("system", languages)).toBe(expected);
  });

  it("honors an explicit language", () => {
    expect(resolveLocale("zh-TW", ["en-US"])).toBe("zh-TW");
  });

  it("keeps every locale on the same key contract", () => {
    const canonical = flattenResourceKeys(resources.en.translation);
    expect(flattenResourceKeys(resources["zh-CN"].translation)).toEqual(canonical);
    expect(flattenResourceKeys(resources["zh-TW"].translation)).toEqual(canonical);
  });

  it("crashes on a missing production key instead of rendering the key", async () => {
    await i18nReady;
    expect(() => i18n.t("connections.keyThatDoesNotExist")).toThrow(
      "Missing translation key: connections.keyThatDoesNotExist",
    );
  });
});
