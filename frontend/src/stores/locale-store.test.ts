// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import { i18nReady } from "../i18n";
import {
  migrateLocaleState,
  persistedLocaleState,
  useLocaleStore,
} from "./locale-store";

describe("locale store", () => {
  beforeEach(async () => {
    await i18nReady;
    localStorage.clear();
    useLocaleStore.getState().reset();
  });

  it("resolves system and explicit language modes", async () => {
    await useLocaleStore.getState().setLanguageMode("system", ["zh-HK"]);
    expect(useLocaleStore.getState().resolvedLocale).toBe("zh-TW");
    await useLocaleStore.getState().setLanguageMode("en", ["zh-CN"]);
    expect(useLocaleStore.getState()).toMatchObject({ languageMode: "en", resolvedLocale: "en" });
  });

  it("persists languageMode but not resolved or runtime data", () => {
    const candidate = {
      ...useLocaleStore.getState(),
      password: "SECRET_MARKER",
    };

    expect(persistedLocaleState(candidate)).toEqual({ languageMode: "system" });
    expect(migrateLocaleState({ languageMode: "zh-TW" }, 99)).toEqual({
      languageMode: "system",
    });
  });
});
