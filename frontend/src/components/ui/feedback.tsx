import type { ReactNode } from "react";

const readyStatuses = new Set(["READY", "OPEN"]);
const pendingStatuses = new Set([
  "CONNECTING",
  "STARTING",
  "HANDSHAKING",
  "HOST_KEY_REQUIRED",
  "CLOSING",
  "PAUSED",
]);
const failedStatuses = new Set(["FAILED", "CLOSED", "DISCONNECTED"]);

export function StatusIndicator({ value }: { value: string }) {
  const tone = readyStatuses.has(value)
    ? "bg-success"
    : pendingStatuses.has(value)
      ? "bg-warning"
      : failedStatuses.has(value)
        ? "bg-danger"
        : "bg-ink-dim";

  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden className={`size-2 rounded-full ${tone}`} />
      <span className="font-mono text-xs">{value}</span>
    </span>
  );
}

export function EmptyState({
  title,
  body,
  actions,
}: {
  title: string;
  body: string;
  actions?: ReactNode;
}) {
  return (
    <div className="grid h-full place-content-center gap-2 p-8 text-center">
      <strong>{title}</strong>
      <p className="m-0 text-sm text-ink-muted">{body}</p>
      {actions}
    </div>
  );
}

export function MilestonePlaceholder({
  label,
  milestone,
}: {
  label: string;
  milestone: string;
}) {
  return (
    <span className="sr-only">
      {label}: {milestone}
    </span>
  );
}
