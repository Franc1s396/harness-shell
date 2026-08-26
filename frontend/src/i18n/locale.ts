export type LanguageMode = "system" | "zh-CN" | "zh-TW" | "en";
export type SupportedLocale = Exclude<LanguageMode, "system">;

export const resolveLocale = (
  mode: LanguageMode,
  systemLanguages: readonly string[] = navigator.languages,
): SupportedLocale => {
  if (mode !== "system") return mode;
  const normalized = (systemLanguages[0] ?? "en").toLowerCase();
  if (["zh-tw", "zh-hk", "zh-mo"].some((tag) => normalized.startsWith(tag))) {
    return "zh-TW";
  }
  return normalized.startsWith("zh") ? "zh-CN" : "en";
};
