import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import { resolveLocale } from "./locale";
import { resources } from "./resources";

export const i18nReady = i18n.use(initReactI18next).init({
  resources,
  lng: resolveLocale("system"),
  fallbackLng: false,
  interpolation: { escapeValue: false },
  returnNull: false,
  saveMissing: false,
  parseMissingKeyHandler: (key) => {
    throw new Error(`Missing translation key: ${key}`);
  },
});

export { i18n };
