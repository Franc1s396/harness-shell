import { afterEach, describe, expect, it, vi } from "vitest";

import type { SshCommandError } from "../../api/ssh";
import {
  createCleanupJob,
  runCleanupJob,
} from "./session-cleanup";

const failure = (code: string, message = code): SshCommandError => ({
  code,
  message,
  details: { remote_state: "unknown" },
});

const createJob = () =>
  createCleanupJob({
    tabId: "tab-1",
    sessionTitle: "Production",
    connectionId: "connection-1",
    generation: 1,
    ptySessionId: "pty-1",
    sshSessionId: "ssh-1",
  });

describe("session cleanup", () => {
  afterEach(() => vi.useRealTimers());

  it("retries only unfinished steps after 500ms and 1500ms", async () => {
    vi.useFakeTimers();
    const closePty = vi.fn().mockResolvedValue(undefined);
    const disconnectSsh = vi
      .fn()
      .mockRejectedValueOnce(failure("SSH_CLOSE_FAILED", "first"))
      .mockRejectedValueOnce(failure("SSH_CLOSE_FAILED", "second"))
      .mockResolvedValue(undefined);
    const task = runCleanupJob(createJob(), { closePty, disconnectSsh });

    await vi.advanceTimersByTimeAsync(500);
    await vi.advanceTimersByTimeAsync(1500);

    expect((await task).complete).toBe(true);
    expect(closePty).toHaveBeenCalledTimes(1);
    expect(disconnectSsh).toHaveBeenCalledTimes(3);
  });

  it("returns the final errors after exactly 2000ms", async () => {
    vi.useFakeTimers();
    const closePty = vi.fn().mockRejectedValue(failure("PTY_CLOSE_FAILED"));
    const disconnectSsh = vi
      .fn()
      .mockRejectedValue(failure("SSH_CLOSE_FAILED"));
    const task = runCleanupJob(createJob(), { closePty, disconnectSsh });

    await vi.advanceTimersByTimeAsync(1999);
    expect(closePty).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    const result = await task;

    expect(result.complete).toBe(false);
    expect(closePty).toHaveBeenCalledTimes(3);
    expect(disconnectSsh).toHaveBeenCalledTimes(3);
    expect(result.job.lastPtyError?.code).toBe("PTY_CLOSE_FAILED");
    expect(result.job.lastSshError?.code).toBe("SSH_CLOSE_FAILED");
  });

  it("treats only exact session-not-found postconditions as success", async () => {
    const result = await runCleanupJob(createJob(), {
      closePty: vi.fn().mockRejectedValue(failure("PTY_SESSION_NOT_FOUND")),
      disconnectSsh: vi
        .fn()
        .mockRejectedValue(failure("SSH_SESSION_NOT_FOUND")),
    });
    expect(result.complete).toBe(true);
    expect(result.job.lastPtyError).toBeNull();
    expect(result.job.lastSshError).toBeNull();
  });

  it("does not accept similar not-found codes", async () => {
    const wait = vi.fn().mockResolvedValue(undefined);
    const result = await runCleanupJob(createJob(), {
      closePty: vi.fn().mockRejectedValue(failure("PTY_NOT_FOUND")),
      disconnectSsh: vi.fn().mockRejectedValue(failure("SSH_NOT_FOUND")),
      wait,
    });
    expect(result.complete).toBe(false);
    expect(wait.mock.calls).toEqual([[500], [1500]]);
    expect(result.job.lastPtyError?.code).toBe("PTY_NOT_FOUND");
    expect(result.job.lastSshError?.code).toBe("SSH_NOT_FOUND");
  });

  it("marks missing resource IDs complete without invoking operations", async () => {
    const closePty = vi.fn();
    const disconnectSsh = vi.fn();
    const job = createCleanupJob({
      tabId: "tab-1",
      sessionTitle: "Local transcript",
      connectionId: "connection-1",
      generation: 1,
      ptySessionId: null,
      sshSessionId: null,
    });
    const result = await runCleanupJob(job, { closePty, disconnectSsh });
    expect(result.complete).toBe(true);
    expect(closePty).not.toHaveBeenCalled();
    expect(disconnectSsh).not.toHaveBeenCalled();
  });
});
