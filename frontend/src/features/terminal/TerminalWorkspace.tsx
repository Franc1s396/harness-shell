import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";

import { Dialog } from "../../components/ui/Dialog";
import { Button } from "../../components/ui/controls";
import { EmptyState } from "../../components/ui/feedback";
import { useTerminalUiStore } from "../../stores/terminal-ui-store";
import type { AgentBackgroundState } from "../agent/agent-state";
import { SessionActionMenu } from "./SessionActionMenu";
import { TerminalTab } from "./TerminalTab";
import type { TerminalOutputBuffer } from "./terminal-output-buffer";
import {
  sessionStatusKey,
  sessionStatusTone,
  type TerminalSessionModel,
} from "./terminal-session";

type Props = {
  sessions: TerminalSessionModel[];
  outputBuffer: TerminalOutputBuffer;
  runtimeReady: boolean;
  fitRequestKey: number;
  agentBackgroundByTab: Readonly<Record<string, AgentBackgroundState>>;
  activeAgentRunTabIds: ReadonlySet<string>;
  errorNotice?: ReactNode;
  cleanupNotices?: ReactNode;
  onWrite: (tabId: string, data: Uint8Array) => Promise<void>;
  onResize: (tabId: string, cols: number, rows: number) => void;
  onReconnect: (session: TerminalSessionModel) => void;
  onDisconnect: (session: TerminalSessionModel) => void;
  onCloseConfirmed: (session: TerminalSessionModel) => void;
  onFocusChange: (focused: boolean) => void;
  onSelectConnection?: () => void;
  onCreateConnection?: () => void;
};

type SessionMenuState = {
  session: TerminalSessionModel;
  anchor: { x: number; y: number };
};

const toneClass = (session: TerminalSessionModel) =>
  ({
    accent: "bg-accent",
    warning: "bg-warning",
    success: "bg-success",
    disconnected: "bg-danger/60",
    danger: "bg-danger",
  })[sessionStatusTone(session.state)];

const agentDotClass: Record<Exclude<AgentBackgroundState, "NONE">, string> = {
  RUNNING: "bg-accent",
  COMPLETED_UNREAD: "bg-success",
  FAILED_UNREAD: "bg-danger",
};

export function AgentStatusDot({
  state,
  label,
}: {
  state: AgentBackgroundState;
  label: string;
}) {
  if (state === "NONE") return null;
  return (
    <span
      role="status"
      aria-label={label}
      className={`size-2 shrink-0 rounded-full ${agentDotClass[state]}`}
    />
  );
}

export function TerminalWorkspace({
  sessions,
  outputBuffer,
  runtimeReady,
  fitRequestKey,
  agentBackgroundByTab,
  activeAgentRunTabIds,
  errorNotice,
  cleanupNotices,
  onWrite,
  onResize,
  onReconnect,
  onDisconnect,
  onCloseConfirmed,
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
  const [sessionMenu, setSessionMenu] = useState<SessionMenuState | null>(null);
  const [closeTarget, setCloseTarget] = useState<TerminalSessionModel | null>(
    null,
  );
  const [agentBlockTarget, setAgentBlockTarget] =
    useState<TerminalSessionModel | null>(null);

  useEffect(() => {
    reconcileTabs(sessions.map((session) => session.tabId));
  }, [reconcileTabs, sessions]);

  const selectTab = (tabId: string, focusButton = false) => {
    setActiveTab(tabId);
    requestFocus();
    if (focusButton) tabButtonRefs.current.get(tabId)?.focus();
  };

  const onTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % sessions.length;
    if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + sessions.length) % sessions.length;
    }
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = sessions.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    selectTab(sessions[nextIndex].tabId, true);
  };

  const activeSession =
    sessions.find((session) => session.tabId === activeTabId) ?? null;
  const closeMayReachRemote =
    closeTarget !== null &&
    closeTarget.state !== "DISCONNECTED" &&
    closeTarget.state !== "FAILED";

  return (
    <section
      className="flex h-full min-h-0 min-w-0 flex-col bg-app"
      aria-label={t("terminal.title")}
    >
      <div className="flex shrink-0 items-center border-b border-line bg-panel px-2">
        <div className="flex min-w-0 flex-1 overflow-x-auto" role="tablist">
          {sessions.map((session, index) => {
            const active = session.tabId === activeTabId;
            return (
              <div
                key={session.tabId}
                className="flex shrink-0 items-center border-r border-line"
              >
                <button
                  ref={(node) => {
                    if (node) tabButtonRefs.current.set(session.tabId, node);
                    else tabButtonRefs.current.delete(session.tabId);
                  }}
                  className={`flex h-10 items-center gap-2 px-3 text-sm ${
                    active
                      ? "bg-app text-ink"
                      : "text-ink-muted hover:bg-raised hover:text-ink"
                  }`}
                  type="button"
                  role="tab"
                  tabIndex={active ? 0 : -1}
                  aria-selected={active}
                  onClick={() => selectTab(session.tabId)}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    setSessionMenu({
                      session,
                      anchor: { x: event.clientX, y: event.clientY },
                    });
                  }}
                  onKeyDown={(event) => onTabKeyDown(event, index)}
                >
                  <span
                    aria-hidden
                    className={`size-2 rounded-full ${toneClass(session)}`}
                  />
                  <span>{session.title}</span>
                  <AgentStatusDot
                    state={agentBackgroundByTab[session.tabId] ?? "NONE"}
                    label={t(
                      agentBackgroundByTab[session.tabId] === "RUNNING"
                        ? "agent.tabRunning"
                        : agentBackgroundByTab[session.tabId] ===
                            "COMPLETED_UNREAD"
                          ? "agent.tabCompleted"
                          : "agent.tabFailed",
                      { name: session.title },
                    )}
                  />
                  <span className="sr-only">
                    {t(sessionStatusKey(session.state))}
                  </span>
                </button>
                <button
                  type="button"
                  className="mr-1 grid size-7 place-items-center rounded text-ink-muted hover:bg-raised hover:text-ink"
                  aria-label={t("terminal.closeTab", { name: session.title })}
                  onClick={() => setCloseTarget(session)}
                >
                  ×
                </button>
              </div>
            );
          })}
        </div>
        {activeSession ? (
          <span
            data-testid="active-session-status"
            aria-live="polite"
            className="flex h-10 shrink-0 items-center gap-2 border-l border-line px-3 text-xs text-ink-muted"
          >
            <span
              aria-hidden
              className={`size-2 rounded-full ${toneClass(activeSession)}`}
            />
            {t(sessionStatusKey(activeSession.state))}
          </span>
        ) : null}
      </div>

      {errorNotice}
      {cleanupNotices}

      <div
        data-testid="terminal-stage"
        className="relative min-h-0 min-w-0 flex-1"
      >
        {sessions.length === 0 ? (
          <EmptyState
            title={t("terminal.emptyTitle")}
            body={t("terminal.emptyBody")}
            actions={
              onSelectConnection || onCreateConnection ? (
                <div className="mt-3 flex justify-center gap-2">
                  {onSelectConnection ? (
                    <Button variant="secondary" onClick={onSelectConnection}>
                      {t("terminal.selectConnection")}
                    </Button>
                  ) : null}
                  {onCreateConnection ? (
                    <Button onClick={onCreateConnection}>
                      {t("terminal.createConnection")}
                    </Button>
                  ) : null}
                </div>
              ) : null
            }
          />
        ) : null}
        {sessions.map((session) => (
          <TerminalTab
            key={session.tabId}
            tabId={session.tabId}
            outputBuffer={outputBuffer}
            active={session.tabId === activeTabId}
            enabled={runtimeReady && session.state === "CONNECTED"}
            fitRequestKey={fitRequestKey}
            focusRequestKey={
              session.tabId === activeTabId ? focusRevision : 0
            }
            onInput={(data) => onWrite(session.tabId, data)}
            onResize={(cols, rows) => onResize(session.tabId, cols, rows)}
            onFocusChange={onFocusChange}
          />
        ))}
        {!runtimeReady && sessions.length > 0 ? (
          <div
            className="absolute inset-x-3 bottom-3 rounded-md border border-danger/60 bg-danger/15 px-3 py-2 text-sm text-ink"
            role="alert"
          >
            {t("terminal.sidecarUnavailable")}
          </div>
        ) : null}
      </div>

      {sessionMenu ? (
        <SessionActionMenu
          session={sessionMenu.session}
          anchor={sessionMenu.anchor}
          onClose={() => setSessionMenu(null)}
          onReconnect={() => onReconnect(sessionMenu.session)}
          onDisconnect={() => {
            const target = sessionMenu.session;
            if (activeAgentRunTabIds.has(target.tabId)) {
              setAgentBlockTarget(target);
              return;
            }
            onDisconnect(target);
          }}
        />
      ) : null}

      <Dialog
        open={closeTarget !== null}
        title={t("terminal.closeConfirmTitle")}
        onClose={() => setCloseTarget(null)}
      >
        <p className="mt-3 text-sm text-ink-muted">
          {t(
            closeMayReachRemote
              ? "terminal.closeConnectedBody"
              : "terminal.closeLocalBody",
          )}
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setCloseTarget(null)}>
            {t("common.cancel")}
          </Button>
          <Button
            onClick={() => {
              const target = closeTarget;
              setCloseTarget(null);
              if (!target) return;
              if (activeAgentRunTabIds.has(target.tabId)) {
                setAgentBlockTarget(target);
                return;
              }
              onCloseConfirmed(target);
            }}
          >
            {t("terminal.confirmClose")}
          </Button>
        </div>
      </Dialog>
      <Dialog
        open={agentBlockTarget !== null}
        title={t("agent.activeRunTitle")}
        onClose={() => setAgentBlockTarget(null)}
      >
        <p className="mt-3 text-sm text-ink-muted">
          {t("agent.activeRunBody", {
            name: agentBlockTarget?.title ?? "",
          })}
        </p>
        <div className="mt-5 flex justify-end">
          <Button
            variant="secondary"
            onClick={() => setAgentBlockTarget(null)}
          >
            {t("applicationClose.continueWaiting")}
          </Button>
        </div>
      </Dialog>
    </section>
  );
}
