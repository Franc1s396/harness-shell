import "./App.css";
import { useEffect, useState } from "react";

import {
  getRuntimeStatus,
  openApprovalWindow,
  type RuntimeStatus,
} from "./api/runtime";

function App() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);

  useEffect(() => {
    let active = true;
    let refreshTimer: number | undefined;
    const refreshStatus = async () => {
      try {
        const nextStatus = await getRuntimeStatus();
        if (active) {
          setStatus(nextStatus);
          setRuntimeError(null);
        }
      } catch {
        if (active) {
          setStatus(null);
          setRuntimeError("Runtime status is unavailable.");
        }
      } finally {
        if (active) {
          refreshTimer = window.setTimeout(() => void refreshStatus(), 1_000);
        }
      }
    };

    void refreshStatus();
    return () => {
      active = false;
      if (refreshTimer !== undefined) {
        window.clearTimeout(refreshTimer);
      }
    };
  }, []);

  const openApproval = async () => {
    setOpening(true);
    setApprovalError(null);
    try {
      await openApprovalWindow();
    } catch {
      setApprovalError("The approval window could not be opened.");
    } finally {
      setOpening(false);
    }
  };

  return (
    <main className="app-shell">
      <section className="status-card" aria-labelledby="runtime-heading">
        <div className="eyebrow">Local runtime</div>
        <div className="title-row">
          <div>
            <h1 id="runtime-heading">Harness Shell</h1>
            <p className="subtitle">Secure desktop supervisor</p>
          </div>
          <span className={`state-badge state-${status?.state.toLowerCase() ?? "loading"}`}>
            {status?.state ?? "LOADING"}
          </span>
        </div>

        {runtimeError ? <p className="error-banner" role="alert">{runtimeError}</p> : null}
        {approvalError ? <p className="error-banner" role="alert">{approvalError}</p> : null}

        <dl className="status-grid">
          <div>
            <dt>Node</dt>
            <dd>{status?.node ?? "—"}</dd>
          </div>
          <div>
            <dt>Error code</dt>
            <dd>{status?.error_code ?? "None"}</dd>
          </div>
          <div>
            <dt>Recoverable</dt>
            <dd>{status ? (status.recoverable ? "Yes" : "No") : "—"}</dd>
          </div>
          <div>
            <dt>Correlation ID</dt>
            <dd className="monospace">{status?.correlation_id ?? "—"}</dd>
          </div>
        </dl>

        <button type="button" onClick={() => void openApproval()} disabled={opening}>
          {opening ? "Opening…" : "Open approval window"}
        </button>
      </section>
    </main>
  );
}

export default App;
