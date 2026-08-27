import { useTranslation } from "react-i18next";

import type { SshCommandError } from "../../api/ssh";
import { Button } from "../../components/ui/controls";
import type { SessionCleanupJob } from "./session-cleanup";

export function CleanupFailureNotice({
  job,
  retrying,
  onRetry,
}: {
  job: SessionCleanupJob;
  retrying: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const errors = [job.lastPtyError, job.lastSshError].filter(
    (error): error is SshCommandError => error !== null,
  );

  return (
    <section
      role="alert"
      className="grid gap-2 border-b border-danger bg-panel p-3 text-sm"
    >
      <strong className="text-danger">
        {t("terminal.cleanupFailed", { name: job.sessionTitle })}
      </strong>
      {errors.map((error, index) => (
        <p
          key={`${index}:${error.code}:${error.message}`}
          className="m-0 font-mono text-xs text-ink-muted"
        >
          {error.code}: {error.message} · remote_state:{" "}
          {error.details?.remote_state ?? "unknown"}
        </p>
      ))}
      <Button disabled={retrying} onClick={onRetry}>
        {retrying
          ? t("terminal.cleanupRetrying")
          : t("terminal.retryCleanup")}
      </Button>
    </section>
  );
}
