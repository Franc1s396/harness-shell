import { Fragment, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { IconButton } from "../../components/ui/controls";
import { EmptyState, StatusIndicator } from "../../components/ui/feedback";
import {
  ConnectionActionMenu,
  type ConnectionMenuProfile,
  type ConnectionMenuStatus,
} from "./ConnectionActionMenu";
import { groupVisibleConnections } from "./connection-groups";

export type ConnectionNavigatorProps = {
  connections: ConnectionMenuProfile[];
  selectedId: string | null;
  statuses: Record<string, ConnectionMenuStatus>;
  disabled: boolean;
  selectedErrorNotice?: ReactNode;
  onSelect: (connectionId: string) => void;
  onOpen: (connectionId: string) => void;
  onCreate: () => void;
  onEdit: (connectionId: string) => void;
  onDelete: (connectionId: string) => void;
  onDisconnect: (sshSessionId: string) => void;
};

type MenuState = {
  connection: ConnectionMenuProfile;
  anchor: { x: number; y: number } | HTMLElement;
};

export function ConnectionNavigator({
  connections,
  selectedId,
  statuses,
  disabled,
  selectedErrorNotice,
  onSelect,
  onOpen,
  onCreate,
  onEdit,
  onDelete,
  onDisconnect,
}: ConnectionNavigatorProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState<MenuState | null>(null);
  const groups = groupVisibleConnections(connections, query);

  const openMenu = (
    connection: ConnectionMenuProfile,
    anchor: MenuState["anchor"],
  ) => setMenu({ connection, anchor });

  const onRowKeyDown = (
    event: KeyboardEvent<HTMLDivElement>,
    connection: ConnectionMenuProfile,
  ) => {
    if (event.key === "Enter" && !disabled) {
      event.preventDefault();
      onOpen(connection.connection_id);
      return;
    }
    if (event.key === "F10" && event.shiftKey) {
      event.preventDefault();
      openMenu(connection, event.currentTarget);
    }
  };

  const onContextMenu = (
    event: MouseEvent<HTMLDivElement>,
    connection: ConnectionMenuProfile,
  ) => {
    event.preventDefault();
    openMenu(connection, { x: event.clientX, y: event.clientY });
  };

  return (
    <section className="grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] bg-panel">
      <header className="flex h-11 items-center justify-between gap-2 border-b border-line px-3">
        <h2 className="m-0 text-sm font-semibold">{t("connections.title")}</h2>
        <IconButton label={t("connections.new")} disabled={disabled} onClick={onCreate}>
          <span aria-hidden className="text-lg leading-none">+</span>
        </IconButton>
      </header>
      <div className="border-b border-line p-2">
        <input
          type="search"
          aria-label={t("connections.search")}
          placeholder={t("connections.search")}
          className="w-full rounded border border-line bg-input px-2 py-1.5 text-sm text-ink placeholder:text-ink-dim"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div className="min-h-0 overflow-y-auto p-2">
        {groups.length === 0 ? (
          <EmptyState title={t("connections.noResults")} body={t("connections.search")} />
        ) : (
          <div className="grid gap-3" role="listbox" aria-label={t("connections.title")}>
            {groups.map((group) => (
              <section key={group.name ?? "__ungrouped__"} className="grid gap-1">
                <h3 className="m-0 px-1 text-[11px] font-semibold tracking-wide text-ink-dim uppercase">
                  {group.name ?? t("connections.ungrouped")}
                </h3>
                {group.connections.map((connection) => {
                  const currentStatus = statuses[connection.connection_id];
                  const state = currentStatus?.state ?? "DISCONNECTED";
                  const target = `${connection.username}@${connection.host}:${connection.port}`;
                  const selected = selectedId === connection.connection_id;
                  return (
                    <Fragment key={connection.connection_id}>
                    <div
                      role="option"
                      tabIndex={0}
                      aria-label={`${connection.display_name}, ${target}`}
                      aria-selected={selected}
                      className={`group flex h-10 min-w-0 items-center gap-2 rounded border px-2 outline-none ${
                        selected
                          ? "border-accent bg-accent-soft"
                          : "border-transparent bg-app hover:border-line hover:bg-raised"
                      }`}
                      onClick={() => onSelect(connection.connection_id)}
                      onDoubleClick={() => {
                        if (!disabled) onOpen(connection.connection_id);
                      }}
                      onKeyDown={(event) => onRowKeyDown(event, connection)}
                      onContextMenu={(event) => onContextMenu(event, connection)}
                    >
                      <span aria-hidden className="shrink-0">
                        <StatusIndicator value={state} />
                      </span>
                      <span className="grid min-w-0 flex-1 text-left leading-tight">
                        <strong className="truncate text-xs font-medium text-ink">
                          {connection.favorite ? "★ " : ""}{connection.display_name}
                        </strong>
                        <span className="truncate font-mono text-[10px] text-ink-muted">
                          {target}
                        </span>
                      </span>
                      <button
                        type="button"
                        aria-label={`${t("connections.actions")}: ${connection.display_name}`}
                        className="grid size-7 shrink-0 place-items-center rounded text-sm text-ink-muted hover:bg-line hover:text-ink"
                        onClick={(event) => {
                          event.stopPropagation();
                          openMenu(connection, event.currentTarget);
                        }}
                        onDoubleClick={(event) => event.stopPropagation()}
                      >
                        •••
                      </button>
                    </div>
                    {selected && selectedErrorNotice ? (
                      <div className="ml-3 overflow-hidden rounded border border-danger/60">
                        {selectedErrorNotice}
                      </div>
                    ) : null}
                    </Fragment>
                  );
                })}
              </section>
            ))}
          </div>
        )}
      </div>

      {menu ? (
        <ConnectionActionMenu
          connection={menu.connection}
          status={statuses[menu.connection.connection_id]}
          anchor={menu.anchor}
          disabled={disabled}
          onClose={() => setMenu(null)}
          onOpen={() => onOpen(menu.connection.connection_id)}
          onEdit={() => onEdit(menu.connection.connection_id)}
          onDelete={() => onDelete(menu.connection.connection_id)}
          onDisconnect={onDisconnect}
        />
      ) : null}
    </section>
  );
}
