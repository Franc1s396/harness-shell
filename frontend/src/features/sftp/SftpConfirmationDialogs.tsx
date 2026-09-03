import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import type {
  DeletePlanSummary,
  RemoteFileHash,
  RemoteEntry,
  TransferPreparationSummary,
} from "../../api/manual-sftp";
import { SftpLoadingIndicator } from "./SftpLoadingIndicator";

export type SftpDialogState =
  | { kind: "closed" }
  | { kind: "upload" }
  | { kind: "mkdir" }
  | { kind: "rename"; entry: RemoteEntry }
  | { kind: "move"; entry: RemoteEntry }
  | { kind: "propertiesLoading"; entry: RemoteEntry; includeHash: boolean }
  | { kind: "linkLoading"; entry: RemoteEntry }
  | { kind: "properties"; entry: RemoteEntry; hash: RemoteFileHash | null }
  | { kind: "renameOverwrite"; entry: RemoteEntry; targetPath: string }
  | { kind: "delete"; entry: RemoteEntry };

export function SftpConfirmationDialogs({
  dialog,
  preparation,
  deletePlan,
  busy,
  onClose,
  onUpload,
  onMkdir,
  onRename,
  onMove,
  onConfirmRenameOverwrite,
  onDelete,
  onExecuteDelete,
  onExecutePrepared,
}: {
  dialog: SftpDialogState;
  preparation: TransferPreparationSummary | null;
  deletePlan: DeletePlanSummary | null;
  busy: boolean;
  onClose: () => void;
  onUpload: (name: string) => void;
  onMkdir: (name: string) => void;
  onRename: (entry: RemoteEntry, name: string) => void;
  onMove: (entry: RemoteEntry, targetPath: string) => void;
  onConfirmRenameOverwrite: (entry: RemoteEntry, targetPath: string) => void;
  onDelete: (entry: RemoteEntry) => void;
  onExecuteDelete: (plan: DeletePlanSummary) => void;
  onExecutePrepared: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  useEffect(() => {
    setName(
      dialog.kind === "rename"
        ? dialog.entry.name
        : dialog.kind === "move"
          ? dialog.entry.path
          : "",
    );
  }, [dialog]);

  if (preparation) {
    return (
      <Dialog
        open
        busy={busy}
        title={t("sftp.transferTitle", { direction: preparation.direction })}
        onClose={onClose}
      >
        <div className="mt-4 grid gap-2 text-sm">
          <strong>{preparation.display_name}</strong>
          <code className="break-all">{preparation.remote_path}</code>
          <span>{preparation.byte_count} bytes</span>
          <code className="break-all text-xs">SHA-256 {preparation.sha256}</code>
          {preparation.overwrite_required ? (
            <>
              <p className="text-warning">{t("sftp.overwriteWarning")}</p>
              {preparation.direction === "upload" ? (
                <p className="text-warning">{t("sftp.externalRace")}</p>
              ) : null}
            </>
          ) : null}
        </div>
        <Actions busy={busy} onClose={onClose} onConfirm={onExecutePrepared} confirm={t("sftp.confirmTransfer")} />
      </Dialog>
    );
  }

  if (deletePlan) {
    return (
      <Dialog open busy={busy} title={t("sftp.recursiveTitle")} onClose={onClose}>
        <p className="mt-4 text-sm text-ink-muted">
          {t("sftp.recursiveSummary", {
            files: deletePlan.file_count,
            directories: deletePlan.directory_count,
            links: deletePlan.symlink_count,
            bytes: deletePlan.total_byte_count,
            path: deletePlan.root_path,
          })}
        </p>
        <code className="block break-all text-xs">SHA-256 {deletePlan.manifest_sha256}</code>
        <Actions busy={busy} danger onClose={onClose} onConfirm={() => onExecuteDelete(deletePlan)} confirm={t("sftp.confirmRecursive")} />
      </Dialog>
    );
  }

  if (dialog.kind === "closed") return null;
  if (dialog.kind === "propertiesLoading" || dialog.kind === "linkLoading") {
    const label =
      dialog.kind === "linkLoading"
        ? t("sftp.resolvingLink", { name: dialog.entry.name })
        : dialog.includeHash
          ? t("sftp.calculatingHash", { name: dialog.entry.name })
          : t("sftp.loadingProperties", { name: dialog.entry.name });
    return (
      <Dialog
        open
        busy
        title={
          dialog.kind === "linkLoading"
            ? t("sftp.openTarget")
            : t("sftp.propertiesTitle", { name: dialog.entry.name })
        }
        onClose={onClose}
      >
        <SftpLoadingIndicator label={label} />
      </Dialog>
    );
  }
  if (dialog.kind === "properties") {
    return (
      <Dialog open title={t("sftp.propertiesTitle", { name: dialog.entry.name })} onClose={onClose}>
        <dl className="mt-4 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2 text-sm">
          <dt>{t("sftp.path")}</dt><dd className="break-all font-mono">{dialog.entry.path}</dd>
          <dt>{t("sftp.type")}</dt><dd>{dialog.entry.entry_type}</dd>
          <dt>{t("sftp.size")}</dt><dd>{dialog.entry.size ?? "—"}</dd>
          <dt>{t("sftp.mode")}</dt><dd className="font-mono">{dialog.entry.mode.toString(8)}</dd>
          <dt>{t("sftp.modified")}</dt><dd className="font-mono">{dialog.entry.mtime_ns ?? "—"}</dd>
          {dialog.entry.entry_type === "symlink" ? (
            <><dt>{t("sftp.linkTarget")}</dt><dd className="break-all font-mono">{dialog.entry.link_target ?? "—"}</dd></>
          ) : null}
          {dialog.hash ? (
            <><dt>SHA-256</dt><dd className="break-all font-mono">{dialog.hash.sha256}</dd></>
          ) : null}
        </dl>
        <div className="mt-5 flex justify-end"><Button variant="secondary" onClick={onClose}>{t("common.close")}</Button></div>
      </Dialog>
    );
  }
  if (dialog.kind === "delete") {
    return (
      <Dialog open busy={busy} title={t("sftp.deleteTitle", { name: dialog.entry.name })} onClose={onClose}>
        <p className="mt-4 text-sm text-ink-muted">{t("sftp.deleteBody")}</p>
        <code className="block break-all text-xs">{dialog.entry.path}</code>
        <Actions busy={busy} danger onClose={onClose} onConfirm={() => onDelete(dialog.entry)} confirm={t("sftp.confirmDelete")} />
      </Dialog>
    );
  }

  if (dialog.kind === "renameOverwrite") {
    return (
      <Dialog open busy={busy} title={t("sftp.renameTitle", { name: dialog.entry.name })} onClose={onClose}>
        <p className="mt-4 text-sm text-warning">{t("sftp.overwriteWarning")}</p>
        <code className="block break-all text-xs">{dialog.targetPath}</code>
        <Actions
          busy={busy}
          danger
          onClose={onClose}
          onConfirm={() => onConfirmRenameOverwrite(dialog.entry, dialog.targetPath)}
          confirm={t("sftp.confirmRenameOverwrite")}
        />
      </Dialog>
    );
  }

  const title =
    dialog.kind === "upload"
      ? t("sftp.uploadNameTitle")
      : dialog.kind === "mkdir"
        ? t("sftp.mkdirTitle")
        : dialog.kind === "move"
          ? t("sftp.moveTitle", { name: dialog.entry.name })
          : t("sftp.renameTitle", { name: dialog.entry.name });
  const label = dialog.kind === "rename"
    ? t("sftp.newName")
    : dialog.kind === "move"
      ? t("sftp.targetPath")
      : t("sftp.targetName");
  return (
    <Dialog open busy={busy} title={title} onClose={onClose}>
      <label className="mt-4 grid gap-1 text-sm">
        <span>{label}</span>
        <input
          value={name}
          className="rounded border border-line bg-app px-3 py-2 text-ink"
          onChange={(event) => setName(event.currentTarget.value)}
        />
      </label>
      <Actions
        busy={busy || name.length === 0}
        onClose={onClose}
        onConfirm={() => {
          if (dialog.kind === "upload") onUpload(name);
          else if (dialog.kind === "mkdir") onMkdir(name);
          else if (dialog.kind === "move") onMove(dialog.entry, name);
          else onRename(dialog.entry, name);
        }}
        confirm={t("sftp.continue")}
      />
    </Dialog>
  );
}

function Actions({
  busy,
  danger = false,
  confirm,
  onClose,
  onConfirm,
}: {
  busy: boolean;
  danger?: boolean;
  confirm: string;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="mt-5 flex justify-end gap-2">
      <Button variant="secondary" disabled={busy} onClick={onClose}>{t("common.cancel")}</Button>
      <Button variant={danger ? "danger" : "primary"} disabled={busy} onClick={onConfirm}>{confirm}</Button>
    </div>
  );
}
