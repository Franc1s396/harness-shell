import { useEffect, useLayoutEffect, useRef, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";

import type { ConnectionProfile, ConnectionStatus } from "../../api/ssh";

export type ConnectionMenuProfile = ConnectionProfile;
export type ConnectionMenuStatus = ConnectionStatus;

export type ConnectionActionMenuProps = {
  connection: ConnectionMenuProfile;
  status: ConnectionMenuStatus | undefined;
  anchor: { x: number; y: number } | HTMLElement;
  disabled: boolean;
  onClose: () => void;
  onOpen: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onDisconnect: (sshSessionId: string) => void;
};

export function ConnectionActionMenu({
  connection,
  status,
  anchor,
  disabled,
  onClose,
  onOpen,
  onEdit,
  onDelete,
  onDisconnect,
}: ConnectionActionMenuProps) {
  const { t } = useTranslation();
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<HTMLButtonElement[]>([]);
  const anchorElement = anchor instanceof HTMLElement ? anchor : null;

  const position: CSSProperties = anchor instanceof HTMLElement
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

  const readySessionId =
    status?.state === "READY" ? status.session_id : null;

  return (
    <div
      ref={menuRef}
      role="menu"
      aria-label={t("connections.actions")}
      style={position}
      className="fixed z-50 grid min-w-52 rounded-md border border-line-strong bg-raised p-1 shadow-2xl"
      onKeyDown={(event) => {
        const enabled = itemRefs.current.filter((item) => !item.disabled);
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
          return;
        }
        if (enabled.length === 0) return;
        const current = enabled.indexOf(document.activeElement as HTMLButtonElement);
        let next: number | null = null;
        if (event.key === "ArrowDown") next = (current + 1) % enabled.length;
        if (event.key === "ArrowUp") next = (current - 1 + enabled.length) % enabled.length;
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
        disabled={disabled}
        className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft disabled:opacity-40"
        onClick={() => run(onOpen)}
      >
        {t("connections.open")}
      </button>
      <button
        ref={setItemRef(1)}
        type="button"
        role="menuitem"
        className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft"
        onClick={() => run(onEdit)}
      >
        {t("connections.edit")}
      </button>
      {readySessionId ? (
        <button
          ref={setItemRef(2)}
          type="button"
          role="menuitem"
          className="rounded px-3 py-2 text-left text-sm hover:bg-accent-soft"
          onClick={() => run(() => onDisconnect(readySessionId))}
        >
          {t("connections.disconnect")}
        </button>
      ) : null}
      <button
        ref={setItemRef(readySessionId ? 3 : 2)}
        type="button"
        role="menuitem"
        className="rounded px-3 py-2 text-left text-sm text-danger hover:bg-danger/10"
        onClick={() => run(onDelete)}
      >
        {t("common.delete")}
      </button>
      <span className="sr-only">{connection.display_name}</span>
    </div>
  );
}
