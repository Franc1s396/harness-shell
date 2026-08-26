import { useId } from "react";
import { useTranslation } from "react-i18next";

import { MilestonePlaceholder } from "../../components/ui/feedback";
import { Tooltip } from "../../components/ui/controls";
import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
import { ShellIcon, type ShellIconName } from "./icons";

export const activities = [
  { id: "connections", labelKey: "nav.connections", enabled: true, milestone: "M2" },
  { id: "files", labelKey: "nav.files", enabled: false, milestone: "M3+" },
  { id: "sftp", labelKey: "nav.sftp", enabled: false, milestone: "M3+" },
  { id: "settings", labelKey: "nav.settings", enabled: false, milestone: "M3+" },
  { id: "security", labelKey: "common.approval", enabled: true, milestone: "M2" },
] as const;

export function ActivityBar({ onOpenApproval }: { onOpenApproval: () => void }) {
  const { t } = useTranslation();
  const descriptionPrefix = useId();
  const activeActivity = useWorkspaceUiStore((state) => state.activeActivity);

  return (
    <nav
      aria-label={t("shell.primaryActivities")}
      className="flex min-h-0 flex-col items-center gap-2 border-r border-line bg-panel py-2"
    >
      {activities.map((activity) => {
        const label = t(activity.labelKey);
        const descriptionId = `${descriptionPrefix}-${activity.id}`;
        const selected =
          activity.id === "connections" && activeActivity === "connections";
        const button = (
          <button
            type="button"
            aria-label={`${label} (${activity.milestone})`}
            aria-current={selected ? "page" : undefined}
            aria-describedby={!activity.enabled ? descriptionId : undefined}
            disabled={!activity.enabled}
            className="grid size-9 place-items-center rounded text-ink-muted hover:bg-raised hover:text-ink aria-[current=page]:bg-accent-soft aria-[current=page]:text-accent disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => {
              if (activity.id === "connections") {
                const store = useWorkspaceUiStore.getState();
                store.setActiveActivity("connections");
                store.setSidebarVisible(true);
              } else if (activity.id === "security") {
                onOpenApproval();
              }
            }}
          >
            <ShellIcon name={activity.id as ShellIconName} />
          </button>
        );

        if (activity.enabled) return <span key={activity.id}>{button}</span>;

        const milestoneText = t("nav.milestone", {
          milestone: activity.milestone,
        });
        return (
          <Tooltip key={activity.id} text={milestoneText} placement="right">
            {button}
            <span id={descriptionId}>
              <MilestonePlaceholder
                label={label}
                milestone={activity.milestone}
              />
            </span>
          </Tooltip>
        );
      })}
    </nav>
  );
}
