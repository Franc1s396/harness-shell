import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";
import type { OperationPhase, TransferProgressProjection } from "../../api/manual-sftp";

const TRANSFER_PHASE_KEYS = {
  preparing: "sftp.transferPhases.preparing",
  transferring: "sftp.transferPhases.transferring",
  verifying: "sftp.transferPhases.verifying",
  committing: "sftp.transferPhases.committing",
} as const satisfies Record<OperationPhase, string>;

export function SftpTransferStrip({
  progress,
  onCancel,
}: {
  progress: TransferProgressProjection | null;
  onCancel: (operationId: string) => void;
}) {
  const { t } = useTranslation();
  if (!progress) return null;
  return (
    <section aria-label={t("sftp.transfer")} className="flex items-center gap-3 border-t border-line bg-panel px-3 py-2 text-xs">
      <strong>{progress.host_label}</strong>
      <span className="min-w-0 flex-1 truncate font-mono">{progress.remote_path}</span>
      <span>{progress.bytes_completed}/{progress.bytes_total}</span>
      <span>{t(TRANSFER_PHASE_KEYS[progress.phase])}</span>
      {progress.cancellable ? (
        <Button variant="secondary" onClick={() => onCancel(progress.operation_id)}>
          {t("sftp.cancelOperation")}
        </Button>
      ) : null}
    </section>
  );
}
