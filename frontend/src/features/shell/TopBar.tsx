import { useTranslation } from "react-i18next";

import { IconButton } from "../../components/ui/controls";
import { StatusIndicator } from "../../components/ui/feedback";
import { type LanguageMode } from "../../i18n/locale";
import { useLocaleStore } from "../../stores/locale-store";
import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
import { ShellIcon } from "./icons";

export type TopBarProps = {
  connectionName: string | null;
  targetSummary: string | null;
  connectionState: string;
  runtimeState: string;
  onOpenApproval: () => void;
  onFocusTerminal: () => void;
};

export function TopBar({
  connectionName,
  targetSummary,
  connectionState,
  runtimeState,
  onOpenApproval,
  onFocusTerminal,
}: TopBarProps) {
  const { t } = useTranslation();
  const languageMode = useLocaleStore((state) => state.languageMode);
  const sidebarVisible = useWorkspaceUiStore((state) => state.sidebarVisible);

  return (
    <header className="flex min-w-0 items-center gap-3 border-b border-line bg-panel px-2">
      <IconButton
        label={t("topbar.toggleSidebar")}
        tooltipPlacement="bottom-start"
        aria-pressed={sidebarVisible}
        onClick={() =>
          useWorkspaceUiStore
            .getState()
            .setSidebarVisible(!useWorkspaceUiStore.getState().sidebarVisible)
        }
      >
        <ShellIcon name="menu" />
      </IconButton>
      <strong className="shrink-0 text-sm">{t("common.appName")}</strong>
      <div className="min-w-0 flex-1 truncate text-sm text-ink-muted">
        <span className="text-ink">
          {connectionName ?? t("topbar.noConnection")}
        </span>
        {targetSummary ? <span> · {targetSummary}</span> : null}
      </div>
      <StatusIndicator value={runtimeState} />
      <StatusIndicator value={connectionState} />
      <button
        type="button"
        className="inline-flex items-center gap-2 rounded px-2 py-1 text-xs text-ink-muted hover:bg-raised hover:text-ink"
        onClick={() => {
          useWorkspaceUiStore.getState().setActiveActivity("terminal");
          onFocusTerminal();
        }}
      >
        <ShellIcon name="terminal" />
        {t("topbar.terminal")}
      </button>
      <button
        type="button"
        className="rounded px-2 py-1 text-xs text-ink-muted hover:bg-raised hover:text-ink"
        onClick={onOpenApproval}
      >
        {t("common.approval")}
      </button>
      <label className="sr-only" htmlFor="shell-language">
        {t("topbar.language")}
      </label>
      <select
        id="shell-language"
        aria-label={t("topbar.language")}
        className="rounded border border-line bg-input px-2 py-1 text-xs text-ink"
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
    </header>
  );
}
