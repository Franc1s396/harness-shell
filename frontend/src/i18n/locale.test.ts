// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { i18n, i18nReady } from "./index";
import { resolveLocale } from "./locale";
import { flattenResourceKeys, resources } from "./resources";

describe("locale resolution", () => {
  const requiredUiContract = {
    en: {
      "topbar.localEnvironment": "Local",
      "activity.approval": "Approval",
      "activity.settings": "Settings",
      "activity.filesUnavailable": "Files are planned for M3",
    },
    "zh-CN": {
      "topbar.localEnvironment": "本地",
      "activity.approval": "审批",
      "activity.settings": "设置",
      "activity.filesUnavailable": "文件功能计划在 M3 提供",
    },
    "zh-TW": {
      "topbar.localEnvironment": "本機",
      "activity.approval": "審批",
      "activity.settings": "設定",
      "activity.filesUnavailable": "檔案功能預計於 M3 提供",
    },
  } as const;

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

  it.each(Object.entries(requiredUiContract))(
    "provides the exact new UI contract for %s",
    (locale, expected) => {
      const translation = resources[locale as keyof typeof resources].translation;
      for (const [path, value] of Object.entries(expected)) {
        const actual = path.split(".").reduce<unknown>(
          (current, key) =>
            typeof current === "object" && current !== null
              ? (current as Record<string, unknown>)[key]
              : undefined,
          translation,
        );
        expect(actual).toBe(value);
      }
    },
  );

  it("crashes on a missing production key instead of rendering the key", async () => {
    await i18nReady;
    expect(() => i18n.t("connections.keyThatDoesNotExist")).toThrow(
      "Missing translation key: connections.keyThatDoesNotExist",
    );
  });
});
