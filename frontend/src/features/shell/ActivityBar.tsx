import { useId, type MouseEvent } from "react";
import { useTranslation } from "react-i18next";

import { Tooltip } from "../../components/ui/controls";
import { MilestonePlaceholder } from "../../components/ui/feedback";
import { useWorkspaceUiStore, type WorkspaceActivity } from "../../stores/workspace-ui-store";
import { ShellIcon, type ShellIconName } from "./icons";

type Activity = {
  id: "connections" | "files" | "sftp" | "settings" | "approval";
  labelKey: string;
  unavailableKey?: string;
  icon: ShellIconName;
  enabled: boolean;
  milestone: "M2" | "M3+";
  activity?: WorkspaceActivity;
};

export const activities: readonly Activity[] = [
  { id: "connections", labelKey: "nav.connections", icon: "connections", enabled: true, milestone: "M2", activity: "connections" },
  { id: "files", labelKey: "nav.files", unavailableKey: "activity.filesUnavailable", icon: "files", enabled: false, milestone: "M3+" },
  { id: "sftp", labelKey: "nav.sftp", unavailableKey: "activity.sftpUnavailable", icon: "sftp", enabled: false, milestone: "M3+" },
  { id: "settings", labelKey: "activity.settings", icon: "settings", enabled: true, milestone: "M2", activity: "settings" },
  { id: "approval", labelKey: "activity.approval", icon: "security", enabled: true, milestone: "M2", activity: "approval" },
];

type ActivityBarProps = {
  onToggleConnections: () => void;
  onOpenApproval: () => void;
  onOpenSettings: (anchor: HTMLButtonElement) => void;
};

export function ActivityBar({
  onToggleConnections,
  onOpenApproval,
  onOpenSettings,
}: ActivityBarProps) {
  const { t } = useTranslation();
  const descriptionPrefix = useId();
  const activeActivity = useWorkspaceUiStore((state) => state.activeActivity);

  const activate = (
    activity: Activity,
    event: MouseEvent<HTMLButtonElement>,
  ) => {
    if (!activity.enabled || !activity.activity) return;
    useWorkspaceUiStore.getState().setActiveActivity(activity.activity);
    if (activity.id === "connections") onToggleConnections();
    if (activity.id === "settings") onOpenSettings(event.currentTarget);
    if (activity.id === "approval") onOpenApproval();
  };

  return (
    <nav
      aria-label={t("shell.primaryActivities")}
      className="flex min-h-0 w-11 flex-col items-center gap-2 border-r border-line bg-panel py-2"
    >
      {activities.map((activity) => {
        const label = t(activity.labelKey);
        const descriptionId = `${descriptionPrefix}-${activity.id}`;
        const selected = activity.activity === activeActivity;
        const button = (
          <button
            type="button"
            aria-label={`${label} (${activity.milestone})`}
            aria-current={selected ? "page" : undefined}
            aria-describedby={!activity.enabled ? descriptionId : undefined}
            disabled={!activity.enabled}
            className="relative grid size-9 place-items-center rounded text-ink-muted transition hover:bg-raised hover:text-ink aria-[current=page]:bg-accent-soft aria-[current=page]:text-accent aria-[current=page]:after:absolute aria-[current=page]:after:inset-y-1 aria-[current=page]:after:left-0 aria-[current=page]:after:w-0.5 aria-[current=page]:after:bg-accent disabled:cursor-not-allowed disabled:opacity-40"
            onClick={(event) => activate(activity, event)}
          >
            <ShellIcon name={activity.icon} />
          </button>
        );

        if (activity.enabled) return <span key={activity.id}>{button}</span>;

        const milestoneText = t(activity.unavailableKey!);
        return (
          <Tooltip key={activity.id} text={milestoneText} placement="right">
            {button}
            <span id={descriptionId}>
              <MilestonePlaceholder label={label} milestone={activity.milestone} />
            </span>
          </Tooltip>
        );
      })}
    </nav>
  );
}
