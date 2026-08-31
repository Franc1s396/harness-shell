// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

import {
  diagnosticsApi,
  normalizeDiagnosticsCommandError,
} from "./diagnostics";

describe("diagnosticsApi", () => {
  beforeEach(() => invoke.mockReset());

  it("uses only the fixed diagnostics commands without path arguments", async () => {
    invoke
      .mockResolvedValueOnce(
        "C:\\Users\\test\\AppData\\Local\\com.harnessshell.app\\logs",
      )
      .mockResolvedValueOnce(undefined);

    await expect(diagnosticsApi.getLogDirectory()).resolves.toContain("logs");
    await expect(diagnosticsApi.openLogDirectory()).resolves.toBeUndefined();

    expect(invoke).toHaveBeenNthCalledWith(1, "get_log_directory");
    expect(invoke).toHaveBeenNthCalledWith(2, "open_log_directory");
  });

  it("normalizes structured errors and hides unknown rejection details", () => {
    expect(
      normalizeDiagnosticsCommandError({
        code: "LOG_DIRECTORY_UNAVAILABLE",
        message: "The application log directory is not available.",
        secret: "must-not-leak",
      }),
    ).toEqual({
      code: "LOG_DIRECTORY_UNAVAILABLE",
      message: "The application log directory is not available.",
    });
    expect(
      normalizeDiagnosticsCommandError(new Error("sensitive detail")),
    ).toEqual({
      code: "DIAGNOSTICS_COMMAND_FAILED",
      message: "Diagnostics operation failed.",
    });
  });
});
