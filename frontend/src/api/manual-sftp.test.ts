import { beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.hoisted(() => vi.fn());
const listenMock = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock }));

import {
  getManualSftpContext,
  parseMutationProgress,
  parseTransferProgress,
  prepareManualSftpUpload,
  removeManualSftpEntry,
  renameManualSftpEntry,
  subscribeManualSftpEvents,
} from "./manual-sftp";

describe("manual SFTP API", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    listenMock.mockReset();
  });

  it("passes only the active tab session id to context lookup", async () => {
    invokeMock.mockResolvedValue({ ssh_session_id: "ssh-session-active" });
    await getManualSftpContext("ssh-session-active");
    expect(invokeMock).toHaveBeenCalledWith("get_manual_sftp_context", {
      sshSessionId: "ssh-session-active",
    });
  });

  it("does not expose a local path in upload preparation input", async () => {
    invokeMock.mockResolvedValue(null);
    await prepareManualSftpUpload(
      "ssh-session-active",
      "/srv/data",
      "report.csv",
    );
    expect(invokeMock).toHaveBeenCalledWith("prepare_manual_sftp_upload", {
      sshSessionId: "ssh-session-active",
      remoteDirectory: "/srv/data",
      targetName: "report.csv",
    });
    expect(JSON.stringify(invokeMock.mock.calls)).not.toMatch(/localPath/i);
  });

  it("sends rename and remove intent without WebView-controlled snapshots", async () => {
    invokeMock.mockResolvedValue({ state: "succeeded" });

    await (renameManualSftpEntry as (...args: unknown[]) => Promise<unknown>)(
      "ssh-session-active",
      "/srv/data/source.txt",
      "/srv/data/target.txt",
      true,
      { path: "/forbidden-source", exists: true },
      { path: "/forbidden-target", exists: true },
    );
    await (removeManualSftpEntry as (...args: unknown[]) => Promise<unknown>)(
      "ssh-session-active",
      "/srv/data/source.txt",
      { path: "/forbidden-remove", exists: true },
    );

    expect(invokeMock).toHaveBeenNthCalledWith(1, "rename_manual_sftp_entry", {
      sshSessionId: "ssh-session-active",
      sourcePath: "/srv/data/source.txt",
      targetPath: "/srv/data/target.txt",
      overwrite: true,
    });
    expect(invokeMock).toHaveBeenNthCalledWith(2, "remove_manual_sftp_entry", {
      sshSessionId: "ssh-session-active",
      remotePath: "/srv/data/source.txt",
    });
    expect(JSON.stringify(invokeMock.mock.calls)).not.toMatch(/snapshot/i);
  });

  it("rejects malformed or extra event fields", () => {
    expect(() =>
      parseTransferProgress({
        operation_id: "operation",
        direction: "upload",
        phase: "transferring",
        display_name: "report.csv",
        remote_path: "/report.csv",
        host_label: "host",
        bytes_completed: 1,
        bytes_total: 2,
        cancellable: true,
        local_path: "C:\\secret\\report.csv",
      }),
    ).toThrow(/invalid/i);
    expect(() => parseMutationProgress({ cancellable: true })).toThrow(/invalid/i);
  });

  it("cleans up both deterministic event subscriptions", async () => {
    const unlistenTransfer = vi.fn();
    const unlistenOperation = vi.fn();
    listenMock
      .mockResolvedValueOnce(unlistenTransfer)
      .mockResolvedValueOnce(unlistenOperation);
    const unlisten = await subscribeManualSftpEvents(
      () => undefined,
      () => undefined,
      () => undefined,
    );
    expect(listenMock.mock.calls.map(([name]) => name)).toEqual([
      "manual-sftp://transfer-state",
      "manual-sftp://operation-state",
    ]);
    unlisten();
    expect(unlistenTransfer).toHaveBeenCalledOnce();
    expect(unlistenOperation).toHaveBeenCalledOnce();
  });
});
