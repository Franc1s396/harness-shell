import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";
import { EmptyState, StatusIndicator } from "../../components/ui/feedback";
import { useTerminalUiStore } from "../../stores/terminal-ui-store";
import { TerminalTab } from "./TerminalTab";

export type TerminalTabModel = {
  tabId: string;
  title: string;
  ptySessionId: string;
  sshSessionId: string;
  connectionId: string;
  output: Uint8Array[];
  state: "OPEN" | "CLOSED" | "DISCONNECTED";
};

type Props = {
  tabs: TerminalTabModel[];
  runtimeReady: boolean;
  fitRequestKey: number;
  errorNotice?: ReactNode;
  onWrite: (ptySessionId: string, data: Uint8Array) => Promise<void>;
  onResize: (ptySessionId: string, cols: number, rows: number) => void;
  onClose: (tab: TerminalTabModel) => void;
  onFocusChange: (focused: boolean) => void;
  onSelectConnection?: () => void;
  onCreateConnection?: () => void;
};

export function TerminalWorkspace({
  tabs,
  runtimeReady,
  fitRequestKey,
  errorNotice,
  onWrite,
  onResize,
  onClose,
  onFocusChange,
  onSelectConnection,
  onCreateConnection,
}: Props) {
  const { t } = useTranslation();
  const activeTabId = useTerminalUiStore((state) => state.activeTabId);
  const focusRevision = useTerminalUiStore((state) => state.focusRevision);
  const reconcileTabs = useTerminalUiStore((state) => state.reconcileTabs);
  const setActiveTab = useTerminalUiStore((state) => state.setActiveTab);
  const requestFocus = useTerminalUiStore((state) => state.requestFocus);
  const tabButtonRefs = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    reconcileTabs(tabs.map((tab) => tab.tabId));
  }, [reconcileTabs, tabs]);

  const selectTab = (tabId: string, focusButton = false) => {
    setActiveTab(tabId);
    requestFocus();
    if (focusButton) tabButtonRefs.current.get(tabId)?.focus();
  };

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    selectTab(tabs[nextIndex].tabId, true);
  };

  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col bg-app" aria-label={t("terminal.title")}>
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-line bg-panel px-2">
        <div className="flex min-w-0 flex-1 overflow-x-auto" role="tablist">
          {tabs.map((tab, index) => {
            const active = tab.tabId === activeTabId;
            return (
              <div key={tab.tabId} className="flex shrink-0 items-center border-r border-line">
                <button
                  ref={(node) => {
                    if (node) tabButtonRefs.current.set(tab.tabId, node);
                    else tabButtonRefs.current.delete(tab.tabId);
                  }}
                  className={`flex h-10 items-center gap-2 px-3 text-sm ${active ? "bg-app text-ink" : "text-ink-muted hover:bg-raised hover:text-ink"}`}
                  type="button"
                  role="tab"
                  tabIndex={active ? 0 : -1}
                  aria-selected={active}
                  onClick={() => selectTab(tab.tabId)}
                  onKeyDown={(event) => onTabKeyDown(event, index)}
                >
                  <span aria-hidden className={`size-2 rounded-full ${tab.state === "OPEN" ? "bg-success" : "bg-danger"}`} />
                  <span>{tab.title}</span>
                </button>
                <button
                  type="button"
                  className="mr-1 grid size-7 place-items-center rounded text-ink-muted hover:bg-raised hover:text-ink"
                  aria-label={t("terminal.closeTab", { name: tab.title })}
                  onClick={() => onClose(tab)}
                >
                  ×
                </button>
              </div>
            );
          })}
        </div>
        <span className="shrink-0 text-xs text-ink-muted">
          <StatusIndicator value={runtimeReady ? "READY" : "DISCONNECTED"} />{" "}
          {runtimeReady ? t("terminal.runtimeReady") : t("terminal.inputDisabled")}
        </span>
      </div>

      {errorNotice}

      <div data-testid="terminal-stage" className="relative min-h-0 min-w-0 flex-1">
        {tabs.length === 0 ? (
          <EmptyState
            title={t("terminal.emptyTitle")}
            body={t("terminal.emptyBody")}
            actions={onSelectConnection || onCreateConnection ? (
              <div className="mt-3 flex justify-center gap-2">
                {onSelectConnection ? <Button variant="secondary" onClick={onSelectConnection}>{t("terminal.selectConnection")}</Button> : null}
                {onCreateConnection ? <Button onClick={onCreateConnection}>{t("terminal.createConnection")}</Button> : null}
              </div>
            ) : null}
          />
        ) : null}
        {tabs.map((tab) => (
          <TerminalTab
            key={tab.tabId}
            active={tab.tabId === activeTabId}
            enabled={runtimeReady && tab.state === "OPEN"}
            fitRequestKey={fitRequestKey}
            focusRequestKey={tab.tabId === activeTabId ? focusRevision : 0}
            output={tab.output}
            onInput={(data) => onWrite(tab.ptySessionId, data)}
            onResize={(cols, rows) => onResize(tab.ptySessionId, cols, rows)}
            onFocusChange={onFocusChange}
          />
        ))}
        {!runtimeReady && tabs.length > 0 ? (
          <div className="absolute inset-x-3 bottom-3 rounded-md border border-danger/60 bg-danger/15 px-3 py-2 text-sm text-ink" role="alert">
            {t("terminal.sidecarUnavailable")}
          </div>
        ) : null}
      </div>
    </section>
  );
}
