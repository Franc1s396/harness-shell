import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import { i18n, i18nReady } from "../i18n";
import {
  resolveLocale,
  type LanguageMode,
  type SupportedLocale,
} from "../i18n/locale";

const STORAGE_KEY = "harness-shell.locale";

const isLanguageMode = (value: unknown): value is LanguageMode =>
  value === "system" ||
  value === "zh-CN" ||
  value === "zh-TW" ||
  value === "en";

type LocaleState = {
  languageMode: LanguageMode;
  resolvedLocale: SupportedLocale;
  setLanguageMode: (
    mode: LanguageMode,
    systemLanguages?: readonly string[],
  ) => Promise<void>;
  reset: () => void;
};

const defaults = {
  languageMode: "system" as LanguageMode,
  resolvedLocale: resolveLocale("system"),
};

export const persistedLocaleState = (state: { languageMode?: unknown }) => ({
  languageMode: isLanguageMode(state.languageMode)
    ? state.languageMode
    : ("system" as const),
});

export const migrateLocaleState = (persisted: unknown, version: number) =>
  version === 1 && typeof persisted === "object" && persisted !== null
    ? persistedLocaleState(persisted as { languageMode?: unknown })
    : { languageMode: "system" as const };

export const useLocaleStore = create<LocaleState>()(
  persist(
    (set) => ({
      ...defaults,
      setLanguageMode: async (
        languageMode,
        systemLanguages = navigator.languages,
      ) => {
        const resolvedLocale = resolveLocale(languageMode, systemLanguages);
        await i18n.changeLanguage(resolvedLocale);
        set({ languageMode, resolvedLocale });
      },
      reset: () => set(defaults),
    }),
    {
      name: STORAGE_KEY,
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: (state: LocaleState) => persistedLocaleState(state),
      migrate: migrateLocaleState,
      merge: (persisted, current) => ({
        ...current,
        ...migrateLocaleState(persisted, 1),
      }),
      onRehydrateStorage: () => (_state, error) => {
        if (error) localStorage.removeItem(STORAGE_KEY);
      },
    },
  ),
);

export const initializeLocale = async () => {
  await i18nReady;
  const { languageMode, setLanguageMode } = useLocaleStore.getState();
  await setLanguageMode(languageMode, navigator.languages);
};
