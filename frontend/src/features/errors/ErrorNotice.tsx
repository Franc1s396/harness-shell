import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { SshCommandError } from "../../api/ssh";
import { Button } from "../../components/ui/controls";
import { connectionErrorView } from "../connections/connection-state";

export type ErrorNoticeProps = {
  error: SshCommandError;
  partialSuccess?: boolean;
  onRetry?: () => void;
  onEdit?: () => void;
  onDismiss?: () => void;
};

export function ErrorNotice({
  error,
  partialSuccess = false,
  onRetry,
  onEdit,
  onDismiss,
}: ErrorNoticeProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const view = connectionErrorView(error);
  const rawLines = [
    `error_code: ${view.errorCode}`,
    `message: ${error.message}`,
    `node: ${view.node}`,
    `recoverable: ${String(view.recoverable)}`,
    `correlation_id: ${view.correlationId}`,
    `remote_state: ${view.remoteState}`,
  ];

  return (
    <section
      role="alert"
      className="grid gap-2 border-b border-danger bg-panel p-3 text-sm"
    >
      <strong className="text-danger">
        {t(
          partialSuccess
            ? "errors.profileSavedConnectFailed"
            : view.summaryKey,
        )}
      </strong>
      <p className="m-0 text-ink-muted">{t("errors.whatNext")}</p>
      {onRetry || onEdit || onDismiss ? (
        <div className="flex flex-wrap gap-2">
          {onRetry ? <Button onClick={onRetry}>{t("errors.retry")}</Button> : null}
          {onEdit ? (
            <Button variant="secondary" onClick={onEdit}>
              {t("errors.edit")}
            </Button>
          ) : null}
          {onDismiss ? (
            <Button variant="secondary" onClick={onDismiss}>
              {t("common.close")}
            </Button>
          ) : null}
        </div>
      ) : null}
      <details className="grid gap-2 text-ink-muted">
        <summary className="cursor-pointer font-medium text-ink">
          {t("errors.technicalDetails")}
        </summary>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-xs">
          <dt>error_code</dt>
          <dd>{view.errorCode}</dd>
          <dt>message</dt>
          <dd>{error.message}</dd>
          <dt>node</dt>
          <dd>{view.node}</dd>
          <dt>recoverable</dt>
          <dd>{String(view.recoverable)}</dd>
          <dt>correlation_id</dt>
          <dd>{view.correlationId}</dd>
          <dt>remote_state</dt>
          <dd>{view.remoteState}</dd>
        </dl>
        <Button
          variant="secondary"
          className="w-fit px-2 py-1 text-xs"
          onClick={async () => {
            await navigator.clipboard.writeText(rawLines.join("\n"));
            setCopied(true);
          }}
        >
          {copied ? t("common.copied") : t("common.copyDetails")}
        </Button>
      </details>
    </section>
  );
}
