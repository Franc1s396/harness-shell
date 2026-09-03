import { getBackendClient } from "./bootstrap";

export type DiagnosticsCommandError = {
  code: string;
  message: string;
};

export const normalizeDiagnosticsCommandError = (
  error: unknown,
): DiagnosticsCommandError => {
  if (typeof error === "object" && error !== null && "code" in error) {
    const candidate = error as { code: unknown; message?: unknown };
    return {
      code: String(candidate.code),
      message: String(candidate.message ?? "Diagnostics operation failed."),
    };
  }
  return {
    code: "DIAGNOSTICS_COMMAND_FAILED",
    message: "Diagnostics operation failed.",
  };
};

export const diagnosticsApi = {
  getLogDirectory: () =>
    getBackendClient().http.request<{ request_id: string; available: boolean }>(
      "GET", "/v1/diagnostics/log-directory",
    ).then((value) => ({ available: value.available })),
  openLogDirectory: () =>
    getBackendClient().http.request<void>(
      "POST", "/v1/diagnostics/log-directory/open",
    ),
};
