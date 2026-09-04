import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import type { AgentCommandError, ModelApiConfig } from "../../api/agent";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import { ShellIcon } from "../shell/icons";
import { AssistantMarkdown } from "./AssistantMarkdown";
import type { AgentTabState } from "./agent-state";

export type AgentWorkspaceProps = {
  width: number;
  tabTitle: string | null;
  tab: AgentTabState | null;
  configs: ModelApiConfig[];
  configsLoading: boolean;
  onCollapse: () => void;
  onDraftChange: (value: string) => void;
  onProviderSelect: (apiConfigId: string | null) => void;
  onOpenProviderSettings: () => void;
  onRequestSend: () => void;
  onConfirmRiskAndSend: () => void;
  onCancelRisk: () => void;
  onResetConversation: () => void;
  onMarkRead: () => void;
};

export function AgentWorkspace({
  width,
  tabTitle,
  tab,
  configs,
  configsLoading,
  onCollapse,
  onDraftChange,
  onProviderSelect,
  onOpenProviderSettings,
  onRequestSend,
  onConfirmRiskAndSend,
  onCancelRisk,
  onResetConversation,
  onMarkRead,
}: AgentWorkspaceProps) {
  const { t, i18n } = useTranslation();
  const [providerOpen, setProviderOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const messageListRef = useRef<HTMLDivElement>(null);
  const lastMessageId = tab?.messages[tab.messages.length - 1]?.id ?? null;
  const streamedText = tab?.activeRun?.streamedText ?? "";
  const streamSequence = tab?.activeRun?.nextSequence ?? null;
  const errorMessage = (error: AgentCommandError): string => {
    const key = `agent.errors.${error.code}`;
    return i18n.exists(key) ? t(key) : error.message;
  };
  const openProviderSettings = () => {
    setProviderOpen(false);
    onOpenProviderSettings();
  };

  useEffect(() => {
    if (
      tab?.backgroundState === "COMPLETED_UNREAD" ||
      tab?.backgroundState === "FAILED_UNREAD"
    ) {
      onMarkRead();
    }
  }, [onMarkRead, tab?.backgroundState]);

  useEffect(() => {
    const messageList = messageListRef.current;
    if (!messageList) return;
    messageList.scrollTop = messageList.scrollHeight;
  }, [lastMessageId, streamSequence]);

  if (!tab) {
    return (
      <section
        role="region"
        aria-label={t("agent.title")}
        style={{ width: `min(${width}px, 100%)` }}
        className="flex h-full max-w-full flex-col bg-panel"
      >
        <AgentHeader
          tabTitle={tabTitle}
          resetDisabled
          onReset={() => undefined}
          onCollapse={onCollapse}
        />
        <div className="grid flex-1 place-content-center gap-2 p-6 text-center text-sm text-ink-muted">
          <strong className="text-ink">{t("agent.emptySession")}</strong>
        </div>
      </section>
    );
  }

  const enabledConfigs = configs.filter((config) => config.enabled);
  const selectedConfig = enabledConfigs.find(
    (config) => config.api_config_id === tab.selectedApiConfigId,
  );
  const messageLength = [...tab.draft].length;
  const sendDisabled =
    tab.phase !== "IDLE" ||
    selectedConfig === undefined ||
    messageLength < 1 ||
    messageLength > 65_536;

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key !== "Enter" ||
      event.shiftKey ||
      event.nativeEvent.isComposing
    ) {
      return;
    }
    event.preventDefault();
    if (!sendDisabled) onRequestSend();
  };

  return (
    <section
      role="region"
      aria-label={t("agent.title")}
      style={{ width: `min(${width}px, 100%)` }}
      className="flex h-full max-w-full flex-col bg-panel"
    >
      <AgentHeader
        tabTitle={tabTitle}
        resetDisabled={tab.phase === "RUNNING"}
        onReset={() => setResetOpen(true)}
        onCollapse={onCollapse}
      />

      <p className="mx-3 mt-3 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
        {t("agent.compactRisk")}
      </p>

      <div
        ref={messageListRef}
        className="agent-scrollbar min-h-0 flex-1 space-y-3 overflow-y-auto p-3 text-sm"
      >
        {tab.messages.length === 0 ? (
          <p className="grid min-h-32 place-content-center text-center text-ink-dim">
            {t("agent.noMessages")}
          </p>
        ) : null}
        {tab.messages.map((message) => {
          if (message.kind === "user") {
            return (
              <article key={message.id} className="ml-auto w-fit max-w-[88%] whitespace-pre-wrap break-words rounded-xl bg-raised px-3 py-2">
                {message.text}
              </article>
            );
          }
          if (message.kind === "error") {
            return (
              <article key={message.id} role="alert" className="w-fit max-w-[88%] break-words rounded-xl border border-danger/40 px-3 py-2 text-danger">
                <strong>{message.error.code}</strong>: {errorMessage(message.error)}
              </article>
            );
          }
          return (
            <article key={message.id} className="w-fit max-w-[88%] space-y-2 rounded-xl border border-line px-3 py-2">
              <AssistantMarkdown text={message.text} />
              <details>
                <summary className="cursor-pointer text-xs text-ink-muted">
                  {t("agent.runDetails")} · {t("agent.sentSnapshot")}
                </summary>
                <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 break-all text-xs">
                  <dt>{t("agent.runId")}</dt><dd>{message.run.agentRunId}</dd>
                  <dt>{t("agent.runStatus")}</dt><dd>{message.run.status}</dd>
                  <dt>{t("agent.iteration")}</dt><dd>{message.run.reactIteration}</dd>
                  <dt>{t("agent.session")}</dt><dd>{tabTitle}</dd>
                  <dt>{t("agent.apiType")}</dt><dd>{message.run.provider.apiType}</dd>
                  <dt>{t("agent.provider")}</dt><dd>{message.run.provider.displayName}</dd>
                  <dt>{t("agent.model")}</dt><dd>{message.run.provider.model}</dd>
                </dl>
              </details>
            </article>
          );
        })}
        {tab.phase === "RUNNING" && streamedText.length === 0 ? (
          <article
            role="status"
            className="flex w-fit max-w-[88%] items-center gap-2 rounded-xl border border-line px-3 py-2 text-ink-muted"
          >
            <span
              aria-hidden="true"
              className="size-3 shrink-0 animate-spin rounded-full border-2 border-line-strong border-t-accent motion-reduce:animate-none"
            />
            <span>{t("agent.thinking")}</span>
          </article>
        ) : tab.phase === "RUNNING" ? (
          <article
            data-provisional="true"
            role="status"
            className="w-fit max-w-[88%] rounded-xl border border-line px-3 py-2"
          >
            <AssistantMarkdown text={streamedText} />
          </article>
        ) : null}
        {tab.lastError &&
        tab.messages[tab.messages.length - 1]?.kind !== "error" ? (
          <p role="alert" className="text-sm text-danger">
            <strong>{tab.lastError.code}</strong>: {errorMessage(tab.lastError)}
          </p>
        ) : null}
      </div>

      <div className="shrink-0 p-3 pt-0">
        <div className="rounded-xl border border-line-strong bg-app focus-within:border-accent focus-within:ring-1 focus-within:ring-accent/50">
          <textarea
            aria-label={t("agent.message")}
            placeholder={t("agent.messagePlaceholder")}
            value={tab.draft}
            disabled={tab.phase !== "IDLE"}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={onComposerKeyDown}
            className="min-h-16 w-full resize-none bg-transparent px-3 pt-3 text-sm text-ink outline-none focus-visible:outline-hidden"
          />
          <div className="relative flex items-center gap-2 px-2 pb-2">
            <div className="relative">
              <button
                type="button"
                role="combobox"
                aria-haspopup="listbox"
                aria-expanded={providerOpen}
                aria-label={t("agent.provider")}
                disabled={tab.phase !== "IDLE" || configsLoading}
                onClick={() => setProviderOpen((open) => !open)}
                className="rounded-full border border-line bg-raised px-2 py-1 text-xs"
              >
                {selectedConfig
                  ? `${selectedConfig.display_name} · ${selectedConfig.model}`
                  : t("agent.chooseProvider")}
              </button>
              {providerOpen ? (
                <div role="listbox" aria-label={t("agent.chooseProvider")} className="absolute bottom-full left-0 z-20 mb-2 min-w-56 rounded-md border border-line-strong bg-raised p-1 shadow-xl">
                  {enabledConfigs.map((config) => (
                    <button
                      key={config.api_config_id}
                      type="button"
                      role="option"
                      aria-selected={config.api_config_id === tab.selectedApiConfigId}
                      className="block w-full rounded px-2 py-1.5 text-left text-xs hover:bg-accent-soft"
                      onClick={() => {
                        onProviderSelect(config.api_config_id);
                        setProviderOpen(false);
                      }}
                    >
                      {config.display_name} · {config.model}
                    </button>
                  ))}
                  {enabledConfigs.length === 0 ? (
                    <button
                      type="button"
                      className="block w-full rounded px-2 py-1.5 text-left text-xs"
                      onClick={openProviderSettings}
                    >
                      {t("agent.providerSettings")}
                    </button>
                  ) : null}
                </div>
              ) : null}
            </div>
            <button
              type="button"
              aria-label={t("agent.openProviderSettings")}
              onClick={openProviderSettings}
              className="grid size-7 place-items-center rounded-md"
            >
              <ShellIcon name="settings" className="size-4" />
            </button>
            <span className="ml-auto text-[11px] text-ink-dim">
              {tab.phase === "RUNNING"
                ? t("agent.running")
                : t("agent.enterToSend")}
            </span>
            <button
              type="button"
              aria-label={t("agent.send")}
              disabled={sendDisabled}
              onClick={onRequestSend}
              className="grid size-[26px] place-items-center rounded-full bg-white text-black transition hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent active:scale-95 disabled:cursor-not-allowed disabled:bg-line disabled:text-ink-dim disabled:opacity-70"
            >
              <svg aria-hidden viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="size-4">
                <path d="M12 19V5" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M6.5 10.5 12 5l5.5 5.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
        </div>
      </div>

      <div aria-live="polite" aria-atomic="true" className="sr-only">
        {tab.backgroundState === "COMPLETED_UNREAD"
          ? t("agent.completedAnnouncement", { name: tabTitle })
          : tab.backgroundState === "FAILED_UNREAD"
            ? t("agent.failedAnnouncement", { name: tabTitle })
            : ""}
      </div>

      <Dialog open={tab.phase === "AWAITING_RISK_CONFIRMATION"} title={t("agent.riskTitle")} onClose={onCancelRisk}>
        <p className="mt-3 text-sm text-ink-muted">{t("agent.riskBody")}</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancelRisk}>{t("common.cancel")}</Button>
          <Button variant="danger" onClick={onConfirmRiskAndSend}>{t("agent.confirmRisk")}</Button>
        </div>
      </Dialog>

      <Dialog open={resetOpen} title={t("agent.resetTitle")} onClose={() => setResetOpen(false)}>
        <p className="mt-3 text-sm text-ink-muted">{t("agent.resetBody")}</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setResetOpen(false)}>{t("common.cancel")}</Button>
          <Button onClick={() => { setResetOpen(false); onResetConversation(); }}>{t("agent.confirmReset")}</Button>
        </div>
      </Dialog>
    </section>
  );
}

function AgentHeader({
  tabTitle,
  resetDisabled,
  onReset,
  onCollapse,
}: {
  tabTitle: string | null;
  resetDisabled: boolean;
  onReset: () => void;
  onCollapse: () => void;
}) {
  const { t } = useTranslation();
  return (
    <header className="flex min-h-11 shrink-0 items-center gap-2 border-b border-line px-3">
      <div className="min-w-0 flex-1">
        <h2 className="m-0 text-sm font-semibold">{t("agent.title")}</h2>
        {tabTitle ? <p className="truncate text-[11px] text-ink-dim">{tabTitle}</p> : null}
      </div>
      <button type="button" disabled={resetDisabled} onClick={onReset} className="rounded px-2 py-1 text-xs text-ink-muted hover:bg-raised disabled:opacity-40">
        {t("agent.newConversation")}
      </button>
      <button type="button" aria-label={t("agent.collapse")} className="grid size-7 place-items-center rounded text-ink-muted hover:bg-raised hover:text-ink" onClick={onCollapse}>
        <ShellIcon name="agent" />
      </button>
    </header>
  );
}
