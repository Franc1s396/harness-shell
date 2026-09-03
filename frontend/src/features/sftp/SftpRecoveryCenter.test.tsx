// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SftpRecoveryCenter } from "./SftpRecoveryCenter";
import type { RecoverySummary } from "../../api/manual-sftp";

const recovery: RecoverySummary = {
  recovery_id: "recovery-1",
  operation_id: "operation-1",
  kind: "upload_temp" as const,
  host_label: "test.example",
  remote_path: "/home/test/payload.bin",
  display_name: "payload.bin",
  state: "cleanup_required" as const,
  created_at: "2026-08-30T00:00:00Z",
  available_actions: ["verify", "delete_temp", "keep"],
};

describe("SftpRecoveryCenter", () => {
  afterEach(cleanup);

  it("renders remote recovery labels and exposes verify/keep actions", () => {
    const onInspect = vi.fn();
    const onExecute = vi.fn();
    render(
      <SftpRecoveryCenter
        open
        recoveries={[recovery]}
        onClose={vi.fn()}
        onInspect={onInspect}
        onExecute={onExecute}
      />,
    );

    expect(screen.getByText("test.example")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Verify result" }));
    expect(onInspect).toHaveBeenCalledWith("recovery-1");
    fireEvent.click(screen.getByRole("button", { name: "Keep for later" }));
    expect(onExecute).toHaveBeenCalledWith("recovery-1", "keep");
  });

  it("shows loading instead of an empty result before recovery data arrives", () => {
    render(
      <SftpRecoveryCenter
        open
        loading
        recoveries={[]}
        onClose={vi.fn()}
        onInspect={vi.fn()}
        onExecute={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("status", { name: "Loading recovery records…" }),
    ).toBeVisible();
    expect(
      screen.queryByText("No recovery work is pending."),
    ).not.toBeInTheDocument();
  });

  it("requires a second explicit confirmation for mutating recovery actions", () => {
    const onExecute = vi.fn();
    render(
      <SftpRecoveryCenter
        open
        recoveries={[
          {
            ...recovery,
            kind: "upload_temp",
            available_actions: ["verify", "delete_temp", "keep"],
          },
        ]}
        onClose={vi.fn()}
        onInspect={vi.fn()}
        onExecute={onExecute}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Delete temporary file" }));
    expect(onExecute).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "Confirm recovery action" });
    expect(dialog).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Confirm recovery action" }));
    expect(dialog).not.toBeInTheDocument();
    expect(onExecute).toHaveBeenCalledWith("recovery-1", "delete_temp");
  });
});
