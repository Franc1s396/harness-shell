import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { ConnectionProfile, ConnectionStatus } from "../../api/ssh";
import { Button } from "../../components/ui/controls";
import { EmptyState, StatusIndicator } from "../../components/ui/feedback";
import { hostKeyTrustLabel } from "./connection-state";
import { groupVisibleConnections } from "./connection-groups";

export type ConnectionExplorerProps = {
  connections: ConnectionProfile[];
  selectedId: string | null;
  statuses: Record<string, ConnectionStatus>;
  disabled: boolean;
  onSelect: (connectionId: string) => void;
  onCreate: () => void;
  onEdit: (connectionId: string) => void;
  onConnect: (connectionId: string) => void;
  onDisconnect: (sshSessionId: string) => void;
};

export function ConnectionExplorer({
  connections,
  selectedId,
  statuses,
  disabled,
  onSelect,
  onCreate,
  onEdit,
  onConnect,
  onDisconnect,
}: ConnectionExplorerProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const groups = groupVisibleConnections(connections, query);

  return (
    <section className="grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)]">
      <header className="flex items-center justify-between gap-2 border-b border-line p-3">
        <h2 className="m-0 text-sm font-semibold">{t("connections.title")}</h2>
        <Button
          className="px-2 py-1 text-xs"
          disabled={disabled}
          onClick={onCreate}
        >
          {t("connections.new")}
        </Button>
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
          <EmptyState
            title={t("connections.noResults")}
            body={t("connections.search")}
          />
        ) : (
          <div className="grid gap-4">
            {groups.map((group) => (
              <section key={group.name ?? "__ungrouped__"} className="grid gap-1">
                <h3 className="m-0 px-1 text-xs font-semibold tracking-wide text-ink-dim uppercase">
                  {group.name ?? t("connections.ungrouped")}
                </h3>
                {group.connections.map((connection) => {
                  const status = statuses[connection.connection_id];
                  const state = status?.state ?? "DISCONNECTED";
                  const sessionId = status?.session_id ?? null;
                  const isReady = state === "READY" && sessionId !== null;
                  const isPending = [
                    "CONNECTING",
                    "HOST_KEY_REQUIRED",
                    "CLOSING",
                  ].includes(state);
                  const actionLabel = isReady
                    ? t("connections.disconnect")
                    : state === "FAILED"
                      ? t("connections.reconnect")
                      : t("connections.connect");

                  return (
                    <article
                      key={connection.connection_id}
                      className={`grid gap-2 rounded-md border p-2 ${selectedId === connection.connection_id ? "border-accent bg-accent-soft" : "border-line bg-app"}`}
                    >
                      <button
                        type="button"
                        aria-label={connection.display_name}
                        className="grid min-w-0 gap-1 text-left"
                        disabled={disabled}
                        onClick={() => onSelect(connection.connection_id)}
                      >
                        <span className="flex min-w-0 items-center justify-between gap-2">
                          <strong className="truncate text-sm text-ink">
                            {connection.favorite ? "★ " : ""}
                            {connection.display_name}
                          </strong>
                          <StatusIndicator value={state} />
                        </span>
                        <span className="truncate font-mono text-xs text-ink-muted">
                          {connection.username}@{connection.host}:{connection.port}
                        </span>
                        <span className="flex flex-wrap gap-x-3 text-xs text-ink-dim">
                          <span>
                            {connection.proxy_jump_id
                              ? `ProxyJump: ${connection.proxy_jump_id}`
                              : t("connections.direct")}
                          </span>
                          <span>
                            {t("connections.hostKeyStatus")}: {hostKeyTrustLabel(status)}
                          </span>
                        </span>
                      </button>
                      <div className="flex justify-end gap-2">
                        <Button
                          variant="ghost"
                          className="px-2 py-1 text-xs"
                          aria-label={`${t("connections.edit")}: ${connection.display_name}`}
                          disabled={disabled}
                          onClick={() => onEdit(connection.connection_id)}
                        >
                          {t("connections.edit")}
                        </Button>
                        <Button
                          variant="secondary"
                          className="px-2 py-1 text-xs"
                          aria-label={`${actionLabel}: ${connection.display_name}`}
                          disabled={disabled || isPending}
                          onClick={() => {
                            if (isReady) onDisconnect(sessionId);
                            else onConnect(connection.connection_id);
                          }}
                        >
                          {actionLabel}
                        </Button>
                      </div>
                    </article>
                  );
                })}
              </section>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
