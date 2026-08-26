import { useTranslation } from "react-i18next";

import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
import { ShellIcon } from "./icons";

export function AgentRail({ expanded }: { expanded: boolean }) {
  const { t } = useTranslation();

  return (
    <aside
      aria-label={t("agent.title")}
      className={`shrink-0 border-l border-line bg-panel transition-[width] ${expanded ? "w-64" : "w-10"}`}
    >
      <button
        type="button"
        aria-expanded={expanded}
        aria-label={expanded ? t("agent.collapse") : t("agent.expand")}
        className="flex h-10 w-full items-center justify-center text-ink-muted hover:bg-raised hover:text-ink"
        onClick={() =>
          useWorkspaceUiStore.getState().setAgentRailExpanded(!expanded)
        }
      >
        <ShellIcon name="agent" />
      </button>
      {expanded ? (
        <div className="grid gap-3 p-4 text-sm">
          <div className="flex items-center justify-between">
            <strong>{t("agent.title")}</strong>
            <span className="rounded bg-raised px-2 py-0.5 font-mono text-xs text-warning">
              M3
            </span>
          </div>
          <p className="m-0 text-ink-muted">{t("agent.unavailable")}</p>
          <p className="m-0 text-xs text-ink-dim">{t("agent.boundary")}</p>
        </div>
      ) : (
        <span className="sr-only">
          {t("agent.title")}: M3. {t("agent.unavailable")}
        </span>
      )}
    </aside>
  );
}
