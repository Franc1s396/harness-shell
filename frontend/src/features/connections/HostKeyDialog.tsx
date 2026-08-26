import { useTranslation } from "react-i18next";

import type { HostKeyCandidate, SshCommandError } from "../../api/ssh";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import { connectionErrorView } from "./connection-state";

type Props = {
  candidate: HostKeyCandidate;
  trustedFingerprint: string | null;
  error: SshCommandError | null;
  busy: boolean;
  onConfirm: () => void;
  onReplace: () => void;
  onClose: () => void;
};

export function HostKeyDialog({
  candidate,
  trustedFingerprint,
  error,
  busy,
  onConfirm,
  onReplace,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const changed = trustedFingerprint !== null;
  const errorView = error ? connectionErrorView(error) : null;

  return (
    <Dialog
      open
      busy={busy}
      title={changed ? t("hostKey.changed") : t("hostKey.trust")}
      onClose={onClose}
    >
      <p className={`mt-3 text-sm ${changed ? "text-warning" : "text-ink-muted"}`}>
        {changed ? t("hostKey.changedBody") : t("hostKey.trustBody")}
      </p>
      <dl className="mt-4 grid gap-3 rounded-lg border border-line bg-app p-4 text-sm">
        <div>
          <dt className="text-ink-muted">{t("connections.host")}</dt>
          <dd className="mt-1 break-all font-mono text-ink">{candidate.host}:{candidate.port}</dd>
        </div>
        <div>
          <dt className="text-ink-muted">{t("hostKey.algorithm")}</dt>
          <dd className="mt-1 break-all font-mono text-ink">{candidate.key_algorithm}</dd>
        </div>
        {trustedFingerprint ? (
          <div>
            <dt className="text-ink-muted">{t("hostKey.trustedFingerprint")}</dt>
            <dd className="mt-1 break-all font-mono text-danger">{trustedFingerprint}</dd>
          </div>
        ) : null}
        <div>
          <dt className="text-ink-muted">
            {changed ? t("hostKey.newFingerprint") : t("hostKey.fingerprint")}
          </dt>
          <dd className="mt-1 break-all font-mono text-ink">{candidate.fingerprint_sha256}</dd>
        </div>
      </dl>
      {error ? (
        <section className="mt-4 rounded-md border border-danger/60 bg-danger/15 px-3 py-2 text-sm text-ink" role="alert">
          <strong className="text-danger">
            {t(errorView!.summaryKey)}
          </strong>
          <details className="mt-2 text-ink-muted">
            <summary className="cursor-pointer font-medium text-ink">
              {t("errors.technicalDetails")}
            </summary>
            <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-xs">
              <dt>error_code</dt>
              <dd>{error.code}</dd>
              <dt>message</dt>
              <dd>{error.message}</dd>
              <dt>node</dt>
              <dd>{errorView!.node}</dd>
              <dt>recoverable</dt>
              <dd>{String(errorView!.recoverable)}</dd>
              <dt>correlation_id</dt>
              <dd>{errorView!.correlationId}</dd>
              <dt>remote_state</dt>
              <dd>{errorView!.remoteState}</dd>
            </dl>
          </details>
        </section>
      ) : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="secondary" disabled={busy} onClick={onClose}>
          {t("common.cancel")}
        </Button>
        {changed ? (
          <Button variant="danger" disabled={busy} onClick={onReplace}>
            {t("hostKey.replace")}
          </Button>
        ) : (
          <Button disabled={busy} onClick={onConfirm}>
            {t("hostKey.trustConnect")}
          </Button>
        )}
      </div>
    </Dialog>
  );
}
