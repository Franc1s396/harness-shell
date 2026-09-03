import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { IconButton } from "../../components/ui/controls";
import { ShellIcon } from "./icons";

export type ContextBarProps = {
  environmentLabel: string;
  connectionName: string | null;
  targetSummary: string | null;
  sidebarOpen: boolean;
  activeTerminalAvailable: boolean;
  actionsDisabled?: boolean;
  onToggleSidebar: () => void;
  onCreateConnection: () => void;
  onEditConnection: () => void;
  onFocusTerminal: () => void;
};

export function ContextBar({
  environmentLabel,
  connectionName,
  targetSummary,
  sidebarOpen,
  activeTerminalAvailable,
  actionsDisabled = false,
  onToggleSidebar,
  onCreateConnection,
  onEditConnection,
  onFocusTerminal,
}: ContextBarProps) {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRootRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!menuRootRef.current?.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setMenuOpen(false);
      menuButtonRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  const run = (action: () => void) => {
    setMenuOpen(false);
    action();
  };

  return (
    <header className="flex h-12 min-w-0 items-center gap-3 border-b border-line bg-top px-2">
      <IconButton
        label={t("topbar.toggleSidebar")}
        tooltipPlacement="bottom-start"
        aria-pressed={sidebarOpen}
        onClick={onToggleSidebar}
      >
        <ShellIcon name="menu" />
      </IconButton>
      <strong className="shrink-0 text-sm">{t("common.appName")}</strong>
      <span className="shrink-0 rounded border border-line bg-raised px-2 py-0.5 font-mono text-[11px] text-ink-muted">
        {environmentLabel}
      </span>
      <div className="min-w-0 flex-1 truncate text-sm text-ink-muted">
        <span className="text-ink">
          {connectionName ?? t("topbar.noConnection")}
        </span>
        {targetSummary ? (
          <>
            <span aria-hidden> · </span>
            <span>{targetSummary}</span>
          </>
        ) : null}
      </div>
      <div ref={menuRootRef} className="relative shrink-0">
        <button
          ref={menuButtonRef}
          type="button"
          aria-label={t("topbar.quickActions")}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          className="grid size-8 place-items-center rounded-md text-lg leading-none text-ink-muted hover:bg-raised hover:text-ink"
          onClick={() => setMenuOpen((open) => !open)}
        >
          •••
        </button>
        {menuOpen ? (
          <div
            role="menu"
            aria-label={t("topbar.quickActions")}
            className="absolute right-0 top-full z-40 mt-2 grid min-w-48 rounded-md border border-line-strong bg-raised p-1 shadow-2xl"
          >
            <button type="button" role="menuitem" disabled={actionsDisabled} className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft disabled:opacity-40" onClick={() => run(onCreateConnection)}>
              {t("topbar.newConnection")}
            </button>
            <button type="button" role="menuitem" disabled={actionsDisabled || !connectionName} className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft disabled:opacity-40" onClick={() => run(onEditConnection)}>
              {t("topbar.editConnection")}
            </button>
            <button type="button" role="menuitem" disabled={actionsDisabled || !activeTerminalAvailable} className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft disabled:opacity-40" onClick={() => run(onFocusTerminal)}>
              {t("topbar.focusTerminal")}
            </button>
          </div>
        ) : null}
      </div>
    </header>
  );
}
