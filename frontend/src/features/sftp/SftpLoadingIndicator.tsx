export function SftpLoadingIndicator({
  label,
  compact = false,
}: {
  label: string;
  compact?: boolean;
}) {
  return (
    <div
      role="status"
      aria-label={label}
      aria-live="polite"
      className={
        compact
          ? "inline-flex items-center justify-center"
          : "grid place-content-center justify-items-center gap-3 p-6 text-center text-sm text-ink-muted"
      }
    >
      <span
        aria-hidden
        className="size-5 animate-spin rounded-full border-2 border-line-strong border-t-accent"
      />
      {compact ? null : <span>{label}</span>}
    </div>
  );
}
