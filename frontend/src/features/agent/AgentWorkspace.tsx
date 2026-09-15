import { AgentAttachmentStrip, AgentSentImage } from "./AgentAttachmentStrip";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import type { AgentCommandError, ModelApiConfig } from "../../api/agent";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import { ShellIcon } from "../shell/icons";
import { AgentApprovalBubble } from "./AgentApprovalBubble";
import { AssistantMarkdown } from "./AssistantMarkdown";
import { AgentMessageActions } from "./AgentMessageActions";
import { lastRetryableUser } from "./agent-state";
import type { AgentApprovalMessage, AgentTabState } from "./agent-state";

export type AgentWorkspaceProps = {
  onAddImages?: (files: readonly File[]) => void;
  onRemoveImage?: (id: string) => void;
  onRetryImage?: (id: string) => void;
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
  onRetry: () => void;
  onCancelTurn: () => void;
  onApprovalDecision: (approvalId: string, decision: "approve" | "reject") => void;
  onResetConversation: () => void;
  onMarkRead: () => void;
};

function AgentErrorDetails({ error }: { error: AgentCommandError }) {
  return (
    <>
      <div>
        <strong>error_code:</strong> {error.code}
      </div>
      <div>
        <strong>error_message:</strong> {error.message}
      </div>
    </>
  );
}

/** 审核记录按终态消息绑定的 ID 展示；历史视图不提供重新授权按钮。 */
function ApprovalHistory({ ids, approvals }: { ids?: string[]; approvals: Map<string, AgentApprovalMessage> }) {
  const { t } = useTranslation();
  if (!ids?.length) return null;
  return <details>
    <summary className="cursor-pointer text-xs text-ink-muted">{t("agent.approvalHistory", { count: ids.length })}</summary>
    <ol className="mt-2 space-y-2 text-xs">
      {ids.map(id => <li key={id}><AgentApprovalBubble message={approvals.get(id)!} /></li>)}
    </ol>
  </details>;
}

export function AgentWorkspace({
  onAddImages, onRemoveImage, onRetryImage,
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
  onRetry,
  onCancelTurn,
  onApprovalDecision,
  onResetConversation,
  onMarkRead,
}: AgentWorkspaceProps) {
  const { t } = useTranslation();
  const fileInput = useRef<HTMLInputElement>(null);
  const imageMenuRef = useRef<HTMLDivElement>(null);
  const [imageMenuOpen, setImageMenuOpen] = useState(false);
  useEffect(() => {
    if (!imageMenuOpen) return;
    const closeOutside = (event: MouseEvent) => {
      if (event.target instanceof Node && !imageMenuRef.current?.contains(event.target)) setImageMenuOpen(false);
    };
    const closeEscape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape") setImageMenuOpen(false); };
    document.addEventListener("click", closeOutside);
    document.addEventListener("keydown", closeEscape);
    return () => { document.removeEventListener("click", closeOutside); document.removeEventListener("keydown", closeEscape); };
  }, [imageMenuOpen]);
  const [providerOpen, setProviderOpen] = useState(false);
  const providerMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!providerOpen) return;
    const closeOutside = (event: MouseEvent) => {
      if (event.target instanceof Node && !providerMenuRef.current?.contains(event.target)) {
        setProviderOpen(false);
      }
    };
    document.addEventListener("click", closeOutside, true);
    return () => document.removeEventListener("click", closeOutside, true);
  }, [providerOpen]);
  const [resetOpen, setResetOpen] = useState(false);
  const messageListRef = useRef<HTMLDivElement>(null);
  const lastMessageId = tab?.messages[tab.messages.length - 1]?.id ?? null;
  const awaitingApproval = tab?.phase === "RUNNING" && tab.messages.some(message =>
    message.kind === "approval" && message.request.agent_run_id === tab.activeRun?.agentRunId &&
    (message.status === "PENDING" || message.status === "UNKNOWN"));
  const approvals = new Map((tab?.messages ?? []).filter((message): message is AgentApprovalMessage =>
    message.kind === "approval").map(message => [message.id, message]));
  // 同轮可能依次触发多个审核；只让最后一项占据固定区域，避免旧错误重新浮现。
  const currentApproval = tab?.phase === "RUNNING" ? [...approvals.values()].reverse().find(message =>
    message.request.agent_run_id === tab.activeRun?.agentRunId) : undefined;
  const pendingApproval = currentApproval && !currentApproval.submitting &&
    (currentApproval.status === "PENDING" || currentApproval.status === "UNKNOWN" || currentApproval.status === "INVALIDATED")
    ? currentApproval : undefined;
  const streamedText = tab?.activeRun?.streamedText ?? "";
  const streamSequence = tab?.activeRun?.nextSequence ?? null;
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
    (!tab.draft.trim() && !(tab.attachments?.length)) ||
    tab.attachmentBusy === true ||
    (tab.attachments?.some(item => item.status !== "ready") ?? false) ||
    messageLength > 65_536;
  const lastReply = [...tab.messages].reverse().find(message => message.kind !== "approval");
  const retryUser = lastRetryableUser(tab);

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

      <div
        ref={messageListRef}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-panel p-3 text-sm"
      >
        {tab.messages.length === 0 ? (
          <p className="grid min-h-32 place-content-center text-center text-ink-dim">
            {t("agent.noMessages")}
          </p>
        ) : null}
        {tab.messages.map((message) => {
          if (message.kind === "approval") return null;
          const actions = retryUser && message.id === lastReply?.id ? <AgentMessageActions
            text={message.kind === "error" ? `error_code: ${message.error.code}\nerror_message: ${message.error.message}`
              : message.kind === "cancelled" ? t("agent.cancelled") : message.text}
            retryDisabled={selectedConfig === undefined} onRetry={onRetry} /> : null;
          if (message.kind === "user") {
            return (
              <article key={message.id} className="ml-auto w-fit max-w-[88%] whitespace-pre-wrap break-words rounded-xl bg-raised px-3 py-2">
                {!!message.attachments?.length && <div className="mb-2 flex flex-wrap gap-2">{message.attachments.map(image => <AgentSentImage key={image.attachment_id} image={image} />)}</div>}
                {message.text}
              </article>
            );
          }
          if (message.kind === "error") {
            return (
              <div key={message.id}>
              <article role="alert" className="w-fit max-w-[88%] break-words rounded-xl border border-danger/40 px-3 py-2 text-danger">
                <AgentErrorDetails error={message.error} />
                <ApprovalHistory ids={message.approvalIds} approvals={approvals} />
              </article>
              {actions}
              </div>
            );
          }
          if (message.kind === "cancelled") {
            return (
              <div key={message.id}>
              <article role="status" className="text-xs text-ink-muted">
                {t("agent.cancelled")}
                <ApprovalHistory ids={message.approvalIds} approvals={approvals} />
              </article>
              {actions}
              </div>
            );
          }
          return (
            <div key={message.id}>
            <article className="w-fit max-w-[88%] space-y-2 rounded-xl border border-line px-3 py-2">
              <AssistantMarkdown text={message.text} />
              {message.tools.length > 0 && (
                <details>
                  <summary className="cursor-pointer text-xs text-ink-muted">
                    {t("agent.executedTools", { count: message.tools.length })}
                  </summary>
                  <ol className="mt-2 list-inside list-decimal space-y-3 text-xs">
                    {message.tools.map((tool, index) => (
                      <li key={index}>
                        <span className="font-mono">{tool.tool_name}</span>
                        <pre className="mt-1 max-w-full overflow-x-auto rounded bg-raised p-2">{JSON.stringify(tool.arguments, null, 2)}</pre>
                      </li>
                    ))}
                  </ol>
                </details>
              )}
              <ApprovalHistory ids={message.approvalIds} approvals={approvals} />
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
            {actions}
            </div>
          );
        })}
        {tab.phase === "RUNNING" && !awaitingApproval && streamedText.length === 0 ? (
          <article
            role="status"
            className="flex w-fit max-w-[88%] items-center gap-2 rounded-xl border border-line px-3 py-2 text-ink-muted"
          >
            <span
              aria-hidden="true"
              className="size-3 shrink-0 animate-spin rounded-full border-2 border-line-strong border-t-accent motion-reduce:animate-none"
            />
            <span>{t(tab.activeRun?.toolExecuting ? "agent.toolExecuting" : "agent.thinking")}</span>
          </article>
        ) : tab.phase === "RUNNING" && streamedText.length > 0 ? (
          <article
            data-provisional="true"
            role="status"
            aria-busy={!awaitingApproval}
            className="w-fit max-w-[88%] rounded-xl border border-line px-3 py-2"
          >
            <AssistantMarkdown text={streamedText} />
            {!awaitingApproval && <div className="mt-2 flex items-center gap-2 text-ink-muted">
              <span
                aria-hidden="true"
                className="size-3 shrink-0 animate-spin rounded-full border-2 border-line-strong border-t-accent motion-reduce:animate-none"
              />
              <span className={tab.activeRun?.toolExecuting ? undefined : "sr-only"}>
                {t(tab.activeRun?.toolExecuting ? "agent.toolExecuting" : "agent.running")}
              </span>
            </div>}
          </article>
        ) : null}
        {tab.lastError &&
        tab.messages[tab.messages.length - 1]?.kind !== "error" ? (
          <div role="alert" className="text-sm text-danger">
            <AgentErrorDetails error={tab.lastError} />
          </div>
        ) : null}
      </div>

      <div className="shrink-0 p-3 pt-0">
        <div className="rounded-xl border border-line-strong bg-app focus-within:border-accent focus-within:ring-1 focus-within:ring-accent/50">
          {pendingApproval && <section aria-label={t("agent.pendingApproval")} className="rounded-t-xl border-b border-line-strong bg-raised text-sm">
            <AgentApprovalBubble message={pendingApproval} docked
              onDecision={pendingApproval.status === "PENDING" ? decision => onApprovalDecision(pendingApproval.id, decision) : undefined} />
          </section>}
          {!!tab.attachments?.length && <AgentAttachmentStrip items={tab.attachments} onRemove={id => onRemoveImage?.(id)} onRetry={id => onRetryImage?.(id)} />}
          <textarea
            aria-label={t("agent.message")}
            placeholder={t("agent.messagePlaceholder")}
            value={tab.draft}
            disabled={tab.phase !== "IDLE" || tab.attachmentBusy}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={onComposerKeyDown}
            onPaste={event => {
              const files = Array.from(event.clipboardData.items).filter(item => item.kind === "file").map(item => item.getAsFile()).filter((file): file is File => file !== null);
              if (!files.length) return;
              event.preventDefault();
              const text = event.clipboardData.getData("text/plain");
              if (text) { const input = event.currentTarget; onDraftChange(tab.draft.slice(0, input.selectionStart) + text + tab.draft.slice(input.selectionEnd)); }
              onAddImages?.(files);
            }}
            className="min-h-16 w-full resize-none bg-transparent px-3 pt-3 text-sm text-ink outline-none focus-visible:outline-hidden!"
          />
          <div className="relative flex items-center gap-2 px-2 pb-2">
            <div ref={imageMenuRef} className="relative mr-auto shrink-0">
              <input ref={fileInput} type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif" className="hidden" onChange={event => {
                const files = Array.from(event.target.files ?? []); event.target.value = ""; if (files.length) onAddImages?.(files);
              }} />
              <button type="button" aria-label={t("agent.imageAdd")} aria-expanded={imageMenuOpen}
                disabled={tab.phase !== "IDLE" || tab.attachmentBusy} onClick={() => setImageMenuOpen(value => !value)}
                className="grid size-8 place-items-center rounded-full text-ink-muted hover:bg-raised focus-visible:outline-2 focus-visible:outline-accent">
                <svg aria-hidden viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="size-5"><path d="M12 5v14M5 12h14" strokeLinecap="round" /></svg>
              </button>
              {imageMenuOpen && <div className="absolute bottom-full left-0 z-20 mb-2 min-w-36 rounded-xl border border-line-strong bg-raised p-1 shadow-xl">
                <button type="button" className="w-full rounded-lg px-3 py-2 text-left text-sm hover:bg-accent-soft" onClick={() => {setImageMenuOpen(false); fileInput.current?.click();}}>{t("agent.imageAdd")}</button>
              </div>}
            </div>
            <div ref={providerMenuRef} className="relative min-w-0">
              <button
                type="button"
                role="combobox"
                aria-haspopup="listbox"
                aria-expanded={providerOpen}
                aria-label={t("agent.provider")}
                disabled={tab.phase !== "IDLE" || configsLoading}
                onClick={() => setProviderOpen((open) => !open)}
                className="block max-w-full truncate rounded-full border border-line bg-raised px-2 py-1 text-xs"
              >
                {selectedConfig
                  ? `${selectedConfig.display_name} · ${selectedConfig.model}`
                  : t("agent.chooseProvider")}
              </button>
              {providerOpen ? (
                <div role="listbox" aria-label={t("agent.chooseProvider")} className="absolute bottom-full right-0 z-20 mb-2 w-56 max-w-[calc(100vw-6rem)] break-words rounded-md border border-line-strong bg-raised p-1 shadow-xl">
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
              aria-label={t(tab.phase === "RUNNING" ? "agent.cancelResponse" : "agent.send")}
              disabled={tab.phase === "RUNNING" ? false : sendDisabled}
              onClick={tab.phase === "RUNNING" ? onCancelTurn : onRequestSend}
              className="grid size-[26px] shrink-0 place-items-center rounded-full bg-white text-black transition hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent active:scale-95 disabled:cursor-not-allowed disabled:bg-line disabled:text-ink-dim disabled:opacity-70"
            >
              {tab.phase === "RUNNING" ? (
                <svg aria-hidden viewBox="0 0 24 24" fill="currentColor" className="size-4">
                  <rect x="6" y="6" width="12" height="12" rx="2" />
                </svg>
              ) : <svg aria-hidden viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="size-4">
                <path d="M12 19V5" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M6.5 10.5 12 5l5.5 5.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>}
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
