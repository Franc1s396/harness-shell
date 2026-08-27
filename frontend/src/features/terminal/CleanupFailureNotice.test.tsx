// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { CleanupFailureNotice } from "./CleanupFailureNotice";
import type { SessionCleanupJob } from "./session-cleanup";

const job: SessionCleanupJob = {
  cleanupJobId: "cleanup-1",
  tabId: "tab-1",
  sessionTitle: "Production",
  connectionId: "connection-1",
  generation: 1,
  ptySessionId: "pty-1",
  sshSessionId: "ssh-1",
  ptyClosed: false,
  sshDisconnected: false,
  lastPtyError: {
    code: "PTY_CLOSE_FAILED",
    message: "PTY close failed",
    details: { remote_state: "channel_dispatched" },
  },
  lastSshError: {
    code: "SSH_CLOSE_FAILED",
    message: "SSH disconnect failed",
  },
};

describe("CleanupFailureNotice", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
  });
  afterEach(cleanup);

  it("keeps both final errors visible and offers manual retry", () => {
    const onRetry = vi.fn();
    render(
      <CleanupFailureNotice job={job} retrying={false} onRetry={onRetry} />,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not finish cleaning up Production");
    expect(alert).toHaveTextContent(
      "PTY_CLOSE_FAILED: PTY close failed · remote_state: channel_dispatched",
    );
    expect(alert).toHaveTextContent(
      "SSH_CLOSE_FAILED: SSH disconnect failed · remote_state: unknown",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry cleanup" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("disables retry while the same cleanup job is running", () => {
    render(
      <CleanupFailureNotice job={job} retrying onRetry={vi.fn()} />,
    );
    expect(
      screen.getByRole("button", { name: "Retrying cleanup…" }),
    ).toBeDisabled();
  });
});
