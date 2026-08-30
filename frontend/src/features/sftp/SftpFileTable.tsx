import { useTranslation } from "react-i18next";

import type { EntryType, RemoteEntry } from "../../api/manual-sftp";
import { SftpLoadingIndicator } from "./SftpLoadingIndicator";

const ENTRY_TYPE_KEYS = {
  file: "sftp.entryTypes.file",
  directory: "sftp.entryTypes.directory",
  symlink: "sftp.entryTypes.symlink",
  other: "sftp.entryTypes.other",
} as const satisfies Record<EntryType, string>;

type SftpFileTableProps = {
  entries: readonly RemoteEntry[];
  selectedPath: string | null;
  onSelect: (path: string | null) => void;
  onOpen: (entry: RemoteEntry) => void;
  onDownload: (entry: RemoteEntry) => void;
  onRename: (entry: RemoteEntry) => void;
  onMove: (entry: RemoteEntry) => void;
  onDelete: (entry: RemoteEntry) => void;
  onProperties: (entry: RemoteEntry) => void;
  onHash: (entry: RemoteEntry) => void;
  onReadLinkTarget: (entry: RemoteEntry) => void;
  onRefresh: () => void;
  onParent: () => void;
  loading?: boolean;
  loadingLabel?: string;
};

export function SftpFileTable({
  entries,
  selectedPath,
  onSelect,
  onOpen,
  onDownload,
  onRename,
  onMove,
  onDelete,
  onProperties,
  onHash,
  onReadLinkTarget,
  onRefresh,
  onParent,
  loading = false,
  loadingLabel = "",
}: SftpFileTableProps) {
  const { t } = useTranslation();
  const selectedIndex = entries.findIndex(
    (entry) => entry.path === selectedPath,
  );
  const selected = selectedIndex >= 0 ? entries[selectedIndex] : null;

  return (
    <div
      role="grid"
      aria-label={t("sftp.title")}
      aria-busy={loading}
      tabIndex={0}
      className="min-h-0 flex-1 overflow-auto outline-none focus-visible:ring-2 focus-visible:ring-accent"
      onKeyDown={(event) => {
        if (event.ctrlKey && event.key.toLowerCase() === "r") {
          event.preventDefault();
          onRefresh();
          return;
        }
        if (event.key === "Backspace") {
          event.preventDefault();
          onParent();
          return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          if (entries.length === 0) return;
          const delta = event.key === "ArrowDown" ? 1 : -1;
          const next = Math.min(
            entries.length - 1,
            Math.max(0, selectedIndex < 0 ? 0 : selectedIndex + delta),
          );
          onSelect(entries[next].path);
          return;
        }
        if (!selected) return;
        if (event.key === "Enter") {
          event.preventDefault();
          onOpen(selected);
        } else if (event.key === "F2") {
          event.preventDefault();
          onRename(selected);
        } else if (event.key === "Delete") {
          event.preventDefault();
          onDelete(selected);
        }
      }}
    >
      <div role="row" className="sticky top-0 grid grid-cols-[minmax(180px,1fr)_90px_90px_150px_minmax(300px,auto)] border-b border-line bg-panel px-3 py-2 text-xs font-semibold text-ink-muted">
        <span role="columnheader">{t("sftp.name")}</span>
        <span role="columnheader">{t("sftp.size")}</span>
        <span role="columnheader">{t("sftp.type")}</span>
        <span role="columnheader">{t("sftp.modified")}</span>
        <span role="columnheader">{t("sftp.actions")}</span>
      </div>
      {loading ? (
        <SftpLoadingIndicator label={loadingLabel} />
      ) : entries.length === 0 ? (
        <p className="p-6 text-center text-sm text-ink-muted">{t("sftp.empty")}</p>
      ) : (
        entries.map((entry) => {
          const entryTypeLabel = t(ENTRY_TYPE_KEYS[entry.entry_type]);
          return (
          <div
            key={entry.path}
            role="row"
            aria-label={`${entry.name}, ${entryTypeLabel}`}
            aria-selected={entry.path === selectedPath}
            className="grid cursor-default grid-cols-[minmax(180px,1fr)_90px_90px_150px_minmax(300px,auto)] items-center border-b border-line/60 px-3 py-2 text-sm hover:bg-raised aria-selected:bg-accent-soft"
            onClick={() => onSelect(entry.path)}
            onDoubleClick={() => onOpen(entry)}
          >
            <span role="gridcell" className="truncate text-ink">{entry.name}</span>
            <span role="gridcell" className="font-mono text-xs text-ink-muted">{entry.size ?? "—"}</span>
            <span role="gridcell" className="text-ink-muted">{entryTypeLabel}</span>
            <span role="gridcell" className="font-mono text-xs text-ink-muted">{entry.mtime_ns ?? "—"}</span>
            <span role="gridcell" className="flex flex-wrap gap-1">
              {entry.entry_type === "file" ? (
                <RowAction label={t("sftp.download")} entry={entry} onAction={onDownload} onSelect={onSelect} />
              ) : entry.entry_type === "directory" ? (
                <RowAction label={t("sftp.open")} entry={entry} onAction={onOpen} onSelect={onSelect} />
              ) : entry.entry_type === "symlink" ? (
                <RowAction label={t("sftp.openTarget")} entry={entry} onAction={onOpen} onSelect={onSelect} />
              ) : null}
              {entry.entry_type !== "other" ? (
                <>
                  <RowAction label={t("sftp.rename")} entry={entry} onAction={onRename} onSelect={onSelect} />
                  <RowAction label={t("sftp.move")} entry={entry} onAction={onMove} onSelect={onSelect} />
                  <RowAction label={t("sftp.delete")} entry={entry} onAction={onDelete} onSelect={onSelect} />
                </>
              ) : null}
              <RowAction label={t("sftp.properties")} entry={entry} onAction={onProperties} onSelect={onSelect} />
              {entry.entry_type === "file" ? (
                <RowAction label="SHA-256" entry={entry} onAction={onHash} onSelect={onSelect} />
              ) : null}
              {entry.entry_type === "symlink" ? (
                <RowAction label={t("sftp.readLinkTarget")} entry={entry} onAction={onReadLinkTarget} onSelect={onSelect} />
              ) : null}
            </span>
          </div>
          );
        })
      )}
    </div>
  );
}

function RowAction({
  label,
  entry,
  onAction,
  onSelect,
}: {
  label: string;
  entry: RemoteEntry;
  onAction: (entry: RemoteEntry) => void;
  onSelect: (path: string | null) => void;
}) {
  return (
    <button
      type="button"
      className="rounded border border-line px-1.5 py-1 text-xs text-ink-muted hover:border-line-strong hover:text-ink"
      onClick={(event) => {
        event.stopPropagation();
        onSelect(entry.path);
        onAction(entry);
      }}
    >
      {label}
    </button>
  );
}
