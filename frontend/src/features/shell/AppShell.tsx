import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

import { useTerminalUiStore } from "../../stores/terminal-ui-store";
import {
  clampSidebarWidth,
  sidebarWidthBounds,
  useWorkspaceUiStore,
} from "../../stores/workspace-ui-store";
import { useTranslation } from "react-i18next";
import { ActivityBar } from "./ActivityBar";
import { AgentRail } from "./AgentRail";
import { StatusBar, type StatusBarProps } from "./StatusBar";
import { TopBar } from "./TopBar";
import { resolveResponsiveWorkspace } from "./workspace-layout";

export type AppShellProps = Partial<
  Pick<
    StatusBarProps,
    "ptySize" | "route"
  >
> & {
  explorer: ReactNode;
  terminal: ReactNode;
  runtimeState: string;
  sshState: string;
  hostKeyState: string;
  connectionName?: string | null;
  targetSummary?: string | null;
  onOpenApproval?: () => void;
  onFocusTerminal?: () => void;
};

function ResizeHandle() {
  const { t } = useTranslation();
  const finishRef = useRef<(() => void) | null>(null);
  const sidebarWidth = useWorkspaceUiStore((state) => state.sidebarWidth);
  const bounds = sidebarWidthBounds(window.innerWidth);

  useEffect(() => () => finishRef.current?.(), []);

  const beginResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    finishRef.current?.();
    const startX = event.clientX;
    const startWidth = useWorkspaceUiStore.getState().sidebarWidth;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = "none";
    const move = (next: PointerEvent) => {
      useWorkspaceUiStore
        .getState()
        .setSidebarWidth(
          startWidth + next.clientX - startX,
          window.innerWidth,
        );
    };
    const finish = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", finish);
      document.body.style.userSelect = previousUserSelect;
      useWorkspaceUiStore.getState().bumpLayoutRevision();
      finishRef.current = null;
    };
    finishRef.current = finish;
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", finish, { once: true });
  };

  const resizeWithKeyboard = (nextWidth: number) => {
    const store = useWorkspaceUiStore.getState();
    store.setSidebarWidth(nextWidth, window.innerWidth);
    store.bumpLayoutRevision();
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t("shell.resizeSidebar")}
      aria-valuemin={bounds.min}
      aria-valuemax={bounds.max}
      aria-valuenow={sidebarWidth}
      tabIndex={0}
      className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-accent"
      onPointerDown={beginResize}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") resizeWithKeyboard(sidebarWidth - 8);
        else if (event.key === "ArrowRight") resizeWithKeyboard(sidebarWidth + 8);
        else if (event.key === "Home") resizeWithKeyboard(bounds.min);
        else if (event.key === "End") resizeWithKeyboard(bounds.max);
        else return;
        event.preventDefault();
      }}
    />
  );
}

export function AppShell({
  explorer,
  terminal,
  runtimeState,
  sshState,
  hostKeyState,
  ptySize = null,
  route = "unknown",
  connectionName = null,
  targetSummary = null,
  onOpenApproval = () => undefined,
  onFocusTerminal = () => useTerminalUiStore.getState().requestFocus(),
}: AppShellProps) {
  const requestedSidebarVisible = useWorkspaceUiStore(
    (state) => state.sidebarVisible,
  );
  const sidebarWidth = useWorkspaceUiStore((state) => state.sidebarWidth);
  const requestedAgentExpanded = useWorkspaceUiStore(
    (state) => state.agentRailExpanded,
  );
  const [viewportWidth, setViewportWidth] = useState(window.innerWidth);

  useEffect(() => {
    const onResize = () => {
      const width = window.innerWidth;
      const store = useWorkspaceUiStore.getState();
      store.setSidebarWidth(clampSidebarWidth(store.sidebarWidth, width), width);
      setViewportWidth(width);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const responsive = resolveResponsiveWorkspace(
    viewportWidth,
    requestedSidebarVisible,
    requestedAgentExpanded,
  );

  return (
    <main className="grid h-dvh min-h-0 grid-rows-[42px_minmax(0,1fr)_24px] overflow-hidden bg-app text-ink">
      <TopBar
        connectionName={connectionName}
        targetSummary={targetSummary}
        connectionState={sshState}
        runtimeState={runtimeState}
        onOpenApproval={onOpenApproval}
        onFocusTerminal={onFocusTerminal}
      />
      <div className="grid min-h-0 grid-cols-[44px_minmax(0,1fr)]">
        <ActivityBar onOpenApproval={onOpenApproval} />
        <div className="flex min-h-0 min-w-0">
          {responsive.sidebarVisible ? (
            <div
              style={{ width: sidebarWidth }}
              className="relative shrink-0 border-r border-line bg-panel"
            >
              {explorer}
              <ResizeHandle />
            </div>
          ) : null}
          <div className="min-h-0 min-w-0 flex-1">{terminal}</div>
          <AgentRail expanded={responsive.agentRailExpanded} />
        </div>
      </div>
      <StatusBar
        runtimeState={runtimeState}
        sshState={sshState}
        hostKeyState={hostKeyState}
        ptySize={ptySize}
        route={route}
      />
    </main>
  );
}
