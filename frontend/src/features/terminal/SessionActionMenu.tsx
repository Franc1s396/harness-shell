import {
  useEffect,
  useLayoutEffect,
  useRef,
  type CSSProperties,
} from "react";
import { useTranslation } from "react-i18next";

import { sessionActions, type TerminalSessionModel } from "./terminal-session";

export type SessionActionMenuProps = {
  session: TerminalSessionModel;
  anchor: { x: number; y: number } | HTMLElement;
  onClose: () => void;
  onReconnect: () => void;
  onDisconnect: () => void;
};

export function SessionActionMenu({
  session,
  anchor,
  onClose,
  onReconnect,
  onDisconnect,
}: SessionActionMenuProps) {
  const { t } = useTranslation();
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<HTMLButtonElement[]>([]);
  const anchorElement = anchor instanceof HTMLElement ? anchor : null;
  const actions = sessionActions(session.state);

  const position: CSSProperties =
    anchor instanceof HTMLElement
      ? {
          left: anchor.getBoundingClientRect().right,
          top: anchor.getBoundingClientRect().bottom,
        }
      : { left: anchor.x, top: anchor.y };

  useLayoutEffect(() => {
    itemRefs.current.find((item) => !item.disabled)?.focus();
  }, []);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) onClose();
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      anchorElement?.focus();
    };
  }, [anchorElement, onClose]);

  const run = (action: () => void) => {
    action();
    onClose();
  };

  const setItemRef = (index: number) => (node: HTMLButtonElement | null) => {
    if (node) itemRefs.current[index] = node;
  };

  return (
    <div
      ref={menuRef}
      role="menu"
      aria-label={t("terminal.actions")}
      style={position}
      className="fixed z-50 grid min-w-44 rounded-md border border-line-strong bg-raised p-1 shadow-2xl"
      onKeyDown={(event) => {
        const enabled = itemRefs.current.filter((item) => !item.disabled);
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
          return;
        }
        if (enabled.length === 0) return;
        const current = enabled.indexOf(
          document.activeElement as HTMLButtonElement,
        );
        let next: number | null = null;
        if (event.key === "ArrowDown") next = (current + 1) % enabled.length;
        if (event.key === "ArrowUp") {
          next = (current - 1 + enabled.length) % enabled.length;
        }
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = enabled.length - 1;
        if (next === null) return;
        event.preventDefault();
        enabled[next].focus();
      }}
    >
      <button
        ref={setItemRef(0)}
        type="button"
        role="menuitem"
        disabled={!actions.reconnect}
        className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft disabled:opacity-40"
        onClick={() => run(onReconnect)}
      >
        {t("terminal.reconnect")}
      </button>
      <button
        ref={setItemRef(1)}
        type="button"
        role="menuitem"
        disabled={!actions.disconnect}
        className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft disabled:opacity-40"
        onClick={() => run(onDisconnect)}
      >
        {t("terminal.disconnect")}
      </button>
    </div>
  );
}
