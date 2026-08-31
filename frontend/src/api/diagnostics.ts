import { invoke } from "@tauri-apps/api/core";

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
  getLogDirectory: () => invoke<string>("get_log_directory"),
  openLogDirectory: () => invoke<void>("open_log_directory"),
};
