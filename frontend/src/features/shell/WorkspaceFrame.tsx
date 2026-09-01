import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import type {
  OperationTerminalProjection,
  TransferProgressProjection,
} from "../../api/manual-sftp";
import { normalizeManualSftpError } from "../../api/manual-sftp";
import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";
import type { AgentBackgroundState } from "../agent/agent-state";
import { AgentStatusDot } from "../terminal/TerminalWorkspace";
import { ActivityBar } from "./ActivityBar";
import { ContextBar } from "./ContextBar";
import { ResizableSeparator } from "./ResizableSeparator";
import { SettingsDialog } from "./SettingsDialog";
import { ShellIcon } from "./icons";
import { StatusBar } from "./StatusBar";
import { useApplicationCloseConfirmation } from "./useApplicationCloseConfirmation";
import {
  DEFAULT_AGENT_WIDTH,
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  agentWidthBounds,
  resolveEffectiveAgentWidth,
  resolveResponsiveWorkspace,
} from "./workspace-layout";

export type WorkspaceFrameProps = {
  connectionNavigator: ReactNode;
  primaryWorkspace: ReactNode;
  agentWorkspace: ReactNode;
  modelProviders: ReactNode;
  workspaceOverlay?: ReactNode;
  runtimeState: string;
  hostKeyState: string;
  ptySize: { cols: number; rows: number } | null;
  route: "Direct" | "ProxyJump" | "unknown";
  environmentLabel: string;
  connectionName: string | null;
  targetSummary: string | null;
  agentWidth: number | null;
  activeTerminalAvailable: boolean;
  activeAgentRunCount: number;
  agentBadge: AgentBackgroundState;
  providerSettingsRequestKey?: number;
  connectionActionsDisabled?: boolean;
  activeSftpTransfer?: TransferProgressProjection | null;
  activeSftpTerminal?: OperationTerminalProjection | null;
  onCancelActiveSftpTransfer?: (operationId: string) => Promise<void>;
  onCreateConnection: () => void;
  onEditConnection: () => void;
  onFocusTerminal: () => void;
  onOpenApproval: () => void;
  onSettingsOpening: () => void;
};

export function WorkspaceFrame({
  connectionNavigator,
  primaryWorkspace,
  agentWorkspace,
  modelProviders,
  workspaceOverlay,
  runtimeState,
  hostKeyState,
  ptySize,
  route,
  environmentLabel,
  connectionName,
  targetSummary,
  activeTerminalAvailable,
  activeAgentRunCount,
  agentBadge,
  providerSettingsRequestKey = 0,
  connectionActionsDisabled = false,
  activeSftpTransfer = null,
  activeSftpTerminal = null,
  onCancelActiveSftpTransfer,
  onCreateConnection,
  onEditConnection,
  onFocusTerminal,
  onOpenApproval,
  onSettingsOpening,
}: WorkspaceFrameProps) {
  const { t } = useTranslation();
  const requestedSidebarVisible = useWorkspaceUiStore(
    (state) => state.sidebarVisible,
  );
  const requestedSidebarWidth = useWorkspaceUiStore(
    (state) => state.sidebarWidth,
  );
  const requestedAgentVisible = useWorkspaceUiStore(
    (state) => state.agentVisible,
  );
  const activeActivity = useWorkspaceUiStore((state) => state.activeActivity);
  const requestedAgentWidth = useWorkspaceUiStore((state) => state.agentWidth);
  const drawerOpen = useWorkspaceUiStore(
    (state) => state.mediumViewportDrawerOpen,
  );
  const [viewportWidth, setViewportWidth] = useState(window.innerWidth);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialCategory, setSettingsInitialCategory] =
    useState<"general" | "modelProviders">("general");
  const [closeTransferDecision, setCloseTransferDecision] = useState<
    "idle" | "cancelling"
  >("idle");
  const [closeTransferError, setCloseTransferError] = useState<
    ReturnType<typeof normalizeManualSftpError> | null
  >(null);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const {
    closeConfirmationOpen,
    cancelApplicationClose,
    confirmApplicationClose,
  } = useApplicationCloseConfirmation();

  useEffect(() => {
    if (providerSettingsRequestKey <= 0) return;
    setSettingsInitialCategory("modelProviders");
    setSettingsOpen(true);
  }, [providerSettingsRequestKey]);

  useEffect(() => {
    if (
      !closeConfirmationOpen ||
      closeTransferDecision !== "cancelling" ||
      activeSftpTransfer
    ) {
      return;
    }
    if (
      activeSftpTerminal?.state === "cleanup_required" ||
      activeSftpTerminal?.state === "outcome_unknown"
    ) {
      return;
    }
    void confirmApplicationClose();
  }, [
    activeSftpTerminal?.state,
    activeSftpTransfer,
    closeConfirmationOpen,
    closeTransferDecision,
    confirmApplicationClose,
  ]);

  const continueWaitingForClose = () => {
    setCloseTransferDecision("idle");
    setCloseTransferError(null);
    cancelApplicationClose();
  };

  const cancelTransferForClose = async () => {
    if (!activeSftpTransfer?.cancellable || !onCancelActiveSftpTransfer) return;
    setCloseTransferDecision("cancelling");
    setCloseTransferError(null);
    try {
      await onCancelActiveSftpTransfer(activeSftpTransfer.operation_id);
    } catch (error) {
      // Keep the lifecycle decision visible. A failed cancel is not equivalent to cleanup.
      setCloseTransferDecision("idle");
      setCloseTransferError(normalizeManualSftpError(error));
    }
  };

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const responsive = resolveResponsiveWorkspace(
    viewportWidth,
    requestedSidebarVisible,
    requestedAgentVisible,
  );
  const agentAllowed = activeActivity !== "sftp";
  const effectiveAgentWidth = responsive.agentVisible && agentAllowed
    ? resolveEffectiveAgentWidth(
        requestedAgentWidth,
        agentWidthBounds({
          viewportWidth,
          sidebarInline: responsive.sidebarInline,
          sidebarWidth: requestedSidebarWidth,
        }),
      )
    : null;
  const sftpOwnsCloseDecision =
    activeSftpTransfer !== null || closeTransferDecision === "cancelling";
  const agentOwnsCloseDecision =
    !sftpOwnsCloseDecision && activeAgentRunCount > 0;

  const toggleConnections = () => {
    const store = useWorkspaceUiStore.getState();
    store.setActiveActivity("connections");
    if (responsive.sidebarDrawerAvailable) {
      store.setMediumViewportDrawerOpen(!store.mediumViewportDrawerOpen);
      return;
    }
    store.setSidebarVisible(!store.sidebarVisible);
  };

  const closeDrawer = () =>
    useWorkspaceUiStore.getState().setMediumViewportDrawerOpen(false);

  return (
    <main className="relative grid h-dvh min-h-0 grid-rows-[48px_minmax(0,1fr)_23px] overflow-hidden bg-app text-ink">
      <ContextBar
        environmentLabel={environmentLabel}
        connectionName={connectionName}
        targetSummary={targetSummary}
        sidebarOpen={responsive.sidebarInline || drawerOpen}
        activeTerminalAvailable={activeTerminalAvailable}
        actionsDisabled={connectionActionsDisabled}
        onToggleSidebar={toggleConnections}
        onCreateConnection={onCreateConnection}
        onEditConnection={onEditConnection}
        onFocusTerminal={onFocusTerminal}
        onOpenApproval={onOpenApproval}
      />

      <div className="grid min-h-0 min-w-0 grid-cols-[44px_minmax(0,1fr)]">
        <ActivityBar
          onToggleConnections={toggleConnections}
          onOpenApproval={onOpenApproval}
          onOpenSettings={() => {
            onSettingsOpening();
            setSettingsInitialCategory("general");
            setSettingsOpen(true);
          }}
        />
        <div className="flex min-h-0 min-w-0">
          {responsive.sidebarInline ? (
            <>
              <aside
                aria-label={t("nav.connections")}
                style={{ width: requestedSidebarWidth }}
                className="min-h-0 shrink-0 overflow-hidden bg-panel"
              >
                {connectionNavigator}
              </aside>
              <ResizableSeparator
                label={t("shell.resizeSidebar")}
                value={requestedSidebarWidth}
                min={MIN_SIDEBAR_WIDTH}
                max={MAX_SIDEBAR_WIDTH}
                defaultValue={DEFAULT_SIDEBAR_WIDTH}
                direction="increase-right"
                onChange={(width) =>
                  useWorkspaceUiStore.getState().setSidebarWidth(width)
                }
                onCommit={() =>
                  useWorkspaceUiStore.getState().bumpLayoutRevision()
                }
              />
            </>
          ) : null}

          <section
            data-testid="terminal-region"
            style={{ minWidth: 560 }}
            className="min-h-0 min-w-0 flex-1"
          >
            {primaryWorkspace}
          </section>

          {effectiveAgentWidth !== null ? (
            <>
              <ResizableSeparator
                label={t("shell.resizeAgent")}
                value={effectiveAgentWidth}
                min={agentWidthBounds({
                  viewportWidth,
                  sidebarInline: responsive.sidebarInline,
                  sidebarWidth: requestedSidebarWidth,
                }).min}
                max={agentWidthBounds({
                  viewportWidth,
                  sidebarInline: responsive.sidebarInline,
                  sidebarWidth: requestedSidebarWidth,
                }).max}
                defaultValue={DEFAULT_AGENT_WIDTH}
                direction="increase-left"
                onChange={(width) =>
                  useWorkspaceUiStore.getState().setAgentWidth(width)
                }
                onCommit={() =>
                  useWorkspaceUiStore.getState().bumpLayoutRevision()
                }
              />
              <aside
                data-testid="agent-region"
                aria-label={t("agent.title")}
                style={{ width: effectiveAgentWidth }}
                className="min-h-0 shrink-0 overflow-hidden bg-panel"
              >
                {agentWorkspace}
              </aside>
            </>
          ) : agentAllowed ? (
            <button
              type="button"
              aria-label={t("agent.expand")}
              className="grid w-10 shrink-0 place-items-start border-l border-line bg-panel pt-3 text-ink-muted hover:bg-raised hover:text-ink"
              onClick={() =>
                useWorkspaceUiStore.getState().setAgentVisible(true)
              }
            >
              <ShellIcon name="agent" />
              <AgentStatusDot
                state={agentBadge}
                label={t(
                  agentBadge === "RUNNING"
                    ? "agent.tabRunning"
                    : agentBadge === "COMPLETED_UNREAD"
                      ? "agent.tabCompleted"
                      : "agent.tabFailed",
                  { name: t("agent.title") },
                )}
              />
            </button>
          ) : null}
        </div>
      </div>

      <StatusBar
        runtimeState={runtimeState}
        hostKeyState={hostKeyState}
        ptySize={ptySize}
        agentWidth={effectiveAgentWidth}
        route={route}
      />

      <Dialog
        open={responsive.sidebarDrawerAvailable && drawerOpen}
        title={t("nav.connections")}
        placement="left"
        onClose={closeDrawer}
      >
        <div className="mt-3 min-h-0">{connectionNavigator}</div>
        <div className="mt-4 flex justify-end">
          <Button variant="secondary" onClick={closeDrawer}>
            {t("common.close")}
          </Button>
        </div>
      </Dialog>
      <SettingsDialog
        open={settingsOpen}
        initialCategory={settingsInitialCategory}
        onClose={closeSettings}
        modelProviders={modelProviders}
      />
      <Dialog
        open={closeConfirmationOpen}
        title={t("applicationClose.title")}
        onClose={continueWaitingForClose}
      >
        <p className="mt-3 text-sm text-ink-muted">
          {activeSftpTransfer
            ? activeSftpTransfer.cancellable
              ? t("applicationClose.activeTransferBody")
              : t("applicationClose.committingBody")
            : closeTransferDecision === "cancelling" &&
                (activeSftpTerminal?.state === "cleanup_required" ||
                  activeSftpTerminal?.state === "outcome_unknown")
              ? t("applicationClose.recoveryBody")
              : agentOwnsCloseDecision
                ? t("applicationClose.activeAgentBody", {
                    count: activeAgentRunCount,
                  })
                : t("applicationClose.body")}
        </p>
        {closeTransferError ? (
          <p role="alert" className="mt-3 text-sm text-danger">
            <strong>{closeTransferError.code}</strong>: {closeTransferError.message}
          </p>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          {sftpOwnsCloseDecision || agentOwnsCloseDecision ? (
            <Button variant="secondary" onClick={continueWaitingForClose}>
              {t("applicationClose.continueWaiting")}
            </Button>
          ) : (
            <Button variant="secondary" onClick={cancelApplicationClose}>
              {t("common.cancel")}
            </Button>
          )}
          {activeSftpTransfer?.cancellable ? (
            <Button
              variant="danger"
              disabled={closeTransferDecision === "cancelling"}
              onClick={() => void cancelTransferForClose()}
            >
              {t("applicationClose.cancelAndCleanUp")}
            </Button>
          ) : !activeSftpTransfer &&
            closeTransferDecision === "cancelling" &&
            (activeSftpTerminal?.state === "cleanup_required" ||
              activeSftpTerminal?.state === "outcome_unknown") ? (
            <Button onClick={() => void confirmApplicationClose()}>
              {t("applicationClose.keepRecoveryAndClose")}
            </Button>
          ) : agentOwnsCloseDecision ? (
            <Button onClick={() => void confirmApplicationClose()}>
              {t("applicationClose.forceExit")}
            </Button>
          ) : !activeSftpTransfer && closeTransferDecision === "idle" ? (
            <Button onClick={() => void confirmApplicationClose()}>
              {t("applicationClose.confirm")}
            </Button>
          ) : null}
        </div>
      </Dialog>
      {workspaceOverlay}
    </main>
  );
}
