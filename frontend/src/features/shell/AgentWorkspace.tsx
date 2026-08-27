import { useTranslation } from "react-i18next";

import { ShellIcon } from "./icons";

export type AgentWorkspaceProps = {
  width: number;
  onCollapse: () => void;
};

export function AgentWorkspace({ width, onCollapse }: AgentWorkspaceProps) {
  const { t } = useTranslation();

  return (
    <section
      role="region"
      aria-label={t("agent.title")}
      style={{ width: `min(${width}px, 100%)` }}
      className="flex h-full max-w-full flex-col bg-panel"
    >
      <header className="flex h-11 shrink-0 items-center justify-between border-b border-line px-3">
        <h2 className="m-0 text-sm font-semibold">{t("agent.title")}</h2>
        <button
          type="button"
          aria-label={t("agent.collapse")}
          className="grid size-7 place-items-center rounded text-ink-muted hover:bg-raised hover:text-ink"
          onClick={onCollapse}
        >
          <ShellIcon name="agent" />
        </button>
      </header>
      <div className="grid flex-1 place-content-center gap-3 p-6 text-center">
        <span className="mx-auto rounded border border-warning/50 bg-warning/10 px-2 py-0.5 font-mono text-xs text-warning">
          M3
        </span>
        <strong>{t("agent.unavailableTitle")}</strong>
        <p className="m-0 max-w-sm text-sm text-ink-muted">
          {t("agent.unavailableBody")}
        </p>
      </div>
    </section>
  );
}
