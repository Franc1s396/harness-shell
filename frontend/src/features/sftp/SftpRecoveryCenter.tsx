import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import type { RecoveryAction, RecoverySummary } from "../../api/manual-sftp";
import { SftpLoadingIndicator } from "./SftpLoadingIndicator";

export function SftpRecoveryCenter({
  open,
  loading = false,
  recoveries,
  onClose,
  onInspect,
  onExecute,
}: {
  open: boolean;
  loading?: boolean;
  recoveries: readonly RecoverySummary[];
  onClose: () => void;
  onInspect: (recoveryId: string) => void;
  onExecute: (recoveryId: string, action: RecoveryAction) => void;
}) {
  const { t } = useTranslation();
  const [pending, setPending] = useState<{
    recoveryId: string;
    action: RecoveryAction;
  } | null>(null);
  return (
    <>
      <Dialog
        open={open}
        title={t("sftp.recoveries")}
        onClose={() => {
          setPending(null);
          onClose();
        }}
      >
        <div className="mt-4 grid gap-3">
        {loading ? (
          <SftpLoadingIndicator label={t("sftp.loadingRecoveries")} />
        ) : recoveries.length === 0 ? (
          <p className="text-sm text-ink-muted">{t("sftp.noRecoveries")}</p>
        ) : (
          recoveries.map((recovery) => (
            <article key={recovery.recovery_id} className="rounded border border-line bg-raised p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <strong>{recovery.display_name}</strong>
                <span className="text-xs text-ink-muted">{recovery.host_label}</span>
              </div>
              <p className="my-1 break-all font-mono text-xs text-ink-muted">{recovery.remote_path ?? recovery.kind}</p>
              <p className="mb-2 text-xs text-ink-muted">{t(`sftp.recoveryStates.${recovery.state}`)}</p>
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" onClick={() => onInspect(recovery.recovery_id)}>{t("sftp.verify")}</Button>
                {(recovery.state === "outcome_unknown"
                  ? []
                  : recovery.available_actions.filter((action) => action !== "verify"))
                  .map((action) => (
                    <Button
                      key={action}
                      variant={isMutating(action) ? "danger" : "secondary"}
                      onClick={() => {
                        if (isMutating(action)) {
                          setPending({ recoveryId: recovery.recovery_id, action });
                          return;
                        }
                        onExecute(recovery.recovery_id, action);
                      }}
                    >
                      {recoveryActionLabel(t, action)}
                    </Button>
                  ))}
              </div>
            </article>
          ))
        )}
        </div>
        <div className="mt-4 flex justify-end"><Button variant="secondary" onClick={onClose}>{t("common.close")}</Button></div>
      </Dialog>
      <Dialog
        open={pending !== null}
        title={t("sftp.recoveryConfirmTitle")}
        onClose={() => setPending(null)}
      >
        <p className="mt-4 text-sm text-ink-muted">
          {pending
            ? t("sftp.recoveryConfirmBody", {
                action: recoveryActionLabel(t, pending.action),
              })
            : null}
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setPending(null)}>{t("common.cancel")}</Button>
          <Button
            variant="danger"
            onClick={() => {
              if (!pending) return;
              const confirmed = pending;
              setPending(null);
              onExecute(confirmed.recoveryId, confirmed.action);
            }}
          >
            {t("sftp.confirmRecoveryAction")}
          </Button>
        </div>
      </Dialog>
    </>
  );
}

const isMutating = (action: RecoveryAction) =>
  action === "delete_temp" || action === "continue_delete" || action === "restore_tombstone";

const recoveryActionLabel = (
  t: (key: string) => string,
  action: RecoveryAction,
) => {
  const labels: Record<RecoveryAction, string> = {
    verify: t("sftp.verify"),
    delete_temp: t("sftp.recoveryActions.deleteTemp"),
    continue_delete: t("sftp.recoveryActions.continueDelete"),
    restore_tombstone: t("sftp.recoveryActions.restoreTombstone"),
    keep: t("sftp.recoveryActions.keep"),
  };
  return labels[action];
};
