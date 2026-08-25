import { useEffect, useState } from "react";

import { getApprovalContext, type ApprovalContext } from "../api/approval";
import "../App.css";

export function ApprovalApp() {
  const [context, setContext] = useState<ApprovalContext | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    void getApprovalContext()
      .then(setContext)
      .catch(() => setLoadError(true));
  }, []);

  return (
    <main className="app-shell approval-shell">
      <section className="status-card approval-card" aria-labelledby="approval-heading">
        <div className="eyebrow">Security &amp; approval</div>
        <h1 id="approval-heading">Approval requests</h1>
        <p className="subtitle">Sensitive actions require an explicit decision in this window.</p>

        {loadError ? (
          <p className="error-banner" role="alert">Approval context is unavailable.</p>
        ) : context?.pending === false ? (
          <div className="empty-state">
            <span className="empty-mark" aria-hidden="true">✓</span>
            <p>No approval request is pending.</p>
            <small>pending=false</small>
          </div>
        ) : (
          <div className="empty-state" aria-label="Loading approval context">Loading…</div>
        )}

      </section>
    </main>
  );
}
