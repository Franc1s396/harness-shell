import { useEffect, useRef, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";

import { type LanguageMode } from "../../i18n/locale";
import { useLocaleStore } from "../../stores/locale-store";
import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";

export type SettingsPopoverProps = {
  open: boolean;
  anchor: HTMLElement | null;
  onClose: () => void;
};

export function SettingsPopover({
  open,
  anchor,
  onClose,
}: SettingsPopoverProps) {
  const { t } = useTranslation();
  const rootRef = useRef<HTMLDivElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  const languageMode = useLocaleStore((state) => state.languageMode);

  useEffect(() => {
    if (!open) return;
    selectRef.current?.focus();
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      anchor?.focus();
    };
  }, [anchor, onClose, open]);

  if (!open || !anchor) return null;

  const rect = anchor.getBoundingClientRect();
  const position: CSSProperties = {
    left: rect.right + 8,
    top: rect.top,
  };

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-label={t("nav.settings")}
      style={position}
      className="fixed z-50 grid w-64 gap-3 rounded-md border border-line-strong bg-raised p-4 shadow-2xl"
    >
      <strong className="text-sm">{t("nav.settings")}</strong>
      <label className="grid gap-1 text-xs text-ink-muted">
        <span>{t("settings.language")}</span>
        <select
          ref={selectRef}
          aria-label={t("settings.language")}
          className="rounded border border-line bg-input px-2 py-2 text-sm text-ink"
          value={languageMode}
          onChange={(event) => {
            void useLocaleStore
              .getState()
              .setLanguageMode(event.target.value as LanguageMode)
              .then(() =>
                useWorkspaceUiStore.getState().bumpLayoutRevision(),
              );
          }}
        >
          <option value="system">{t("language.system")}</option>
          <option value="zh-CN">{t("language.zhCN")}</option>
          <option value="zh-TW">{t("language.zhTW")}</option>
          <option value="en">{t("language.en")}</option>
        </select>
      </label>
    </div>
  );
}
