import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { getApprovalContext, type ApprovalContext } from "../api/approval";

export function ApprovalApp() {
  const { t } = useTranslation();
  const [context, setContext] = useState<ApprovalContext | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    void getApprovalContext()
      .then(setContext)
      .catch(() => setLoadError(true));
  }, []);

  return (
    <main className="grid min-h-screen place-items-center bg-app p-6 text-ink">
      <section className="w-full max-w-xl rounded-xl border border-line-strong bg-panel p-6 shadow-2xl" aria-labelledby="approval-heading">
        <div className="text-xs font-semibold uppercase tracking-widest text-accent">{t("approval.eyebrow")}</div>
        <h1 id="approval-heading" className="mt-2 text-2xl font-semibold">{t("approval.title")}</h1>
        <p className="mt-2 text-sm text-ink-muted">{t("approval.subtitle")}</p>

        {loadError ? (
          <p className="mt-5 rounded-md border border-danger/60 bg-danger/15 px-3 py-2 text-sm" role="alert">{t("approval.unavailable")}</p>
        ) : context?.pending === false ? (
          <div className="mt-6 grid place-items-center gap-2 rounded-lg border border-line bg-app p-8 text-center">
            <span className="text-2xl text-success" aria-hidden="true">✓</span>
            <p>{t("approval.empty")}</p>
            <small className="font-mono text-ink-muted">pending=false</small>
          </div>
        ) : (
          <div className="mt-6 rounded-lg border border-line bg-app p-8 text-center text-ink-muted" aria-label={t("approval.loading")}>{t("approval.loading")}</div>
        )}

      </section>
    </main>
  );
}
