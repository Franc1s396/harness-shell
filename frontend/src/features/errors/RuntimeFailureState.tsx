import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";

type RuntimeFailureStateProps = {
  errorCode: string;
  correlationId: string;
  onRetryStatus: () => void;
};

export function RuntimeFailureState({
  errorCode,
  correlationId,
  onRetryStatus,
}: RuntimeFailureStateProps) {
  const { t } = useTranslation();

  return (
    <section
      role="alert"
      className="absolute inset-0 z-30 grid place-items-center bg-app/90 p-6"
    >
      <div className="grid max-w-md gap-4 rounded-lg border border-danger bg-panel p-5 shadow-2xl">
        <div>
          <h2 className="m-0 text-base font-semibold text-danger">
            {t("runtime.failedTitle")}
          </h2>
          <p className="mb-0 mt-2 text-sm text-ink-muted">
            {t("runtime.failedBody")}
          </p>
        </div>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-xs text-ink-muted">
          <dt>error_code</dt>
          <dd className="break-all text-ink">{errorCode}</dd>
          <dt>correlation_id</dt>
          <dd className="break-all text-ink">{correlationId}</dd>
        </dl>
        <Button className="w-fit" onClick={onRetryStatus}>
          {t("runtime.retryStatus")}
        </Button>
      </div>
    </section>
  );
}
