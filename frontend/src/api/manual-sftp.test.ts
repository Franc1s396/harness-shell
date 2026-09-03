import { beforeEach, describe, expect, it, vi } from "vitest";

const request = vi.hoisted(() => vi.fn());
const putBinary = vi.hoisted(() => vi.fn());
const getBinary = vi.hoisted(() => vi.fn());
const subscribe = vi.hoisted(() => vi.fn());
const coordinator = vi.hoisted(() => ({
  prepareUpload: vi.fn(),
  prepareDownloadFromUserGesture: vi.fn(),
  executeUpload: vi.fn(),
  executeDownload: vi.fn(),
  discardPreparation: vi.fn(),
  cancelActive: vi.fn(),
}));
vi.mock("./bootstrap", () => ({
  getBackendClient: () => ({
    http: { request, putBinary, getBinary },
    runtimeWebSocket: { subscribe },
  }),
}));
vi.mock("../features/sftp/browser-file-gateway", () => ({
  BrowserFileGateway: class {},
}));
vi.mock("../features/sftp/browser-transfer-coordinator", () => ({
  BrowserTransferCoordinator: class { constructor() { return coordinator; } },
}));

import {
  getManualSftpContext,
  getManualSftpDownloadChunk,
  listManualSftpRecoveries,
  parseMutationProgress,
  parseTransferProgress,
  prepareManualSftpUpload,
  putManualSftpUploadChunk,
  renameManualSftpEntry,
  subscribeManualSftpEvents,
} from "./manual-sftp";

describe("manual SFTP API", () => {
  beforeEach(() => {
    request.mockReset();
    putBinary.mockReset();
    getBinary.mockReset();
    subscribe.mockReset();
    coordinator.prepareUpload.mockReset();
  });

  it("maps strict upload and download chunks to the shared binary client", async () => {
    const signal = new AbortController().signal;
    const body = new Uint8Array([1, 2, 3]);
    putBinary.mockResolvedValue({ accepted_bytes: 3 });
    getBinary.mockResolvedValue({
      sequence: 8,
      offset: 12,
      body,
      eof: false,
    });

    await putManualSftpUploadChunk("upload-1", 7, 9, body, signal);
    const chunk = await getManualSftpDownloadChunk("download-1", 8, 12, signal);

    expect(putBinary).toHaveBeenCalledWith(
      "/v1/sftp/uploads/upload-1/chunks/7", body, 9, signal,
    );
    expect(getBinary).toHaveBeenCalledWith(
      "/v1/sftp/downloads/download-1/chunks/8", 12, signal,
    );
    expect(chunk).toEqual({
      operation_id: "download-1",
      sequence: 8,
      offset: 12,
      data: body,
      eof: false,
    });
  });

  it("maps context and recovery collections to direct HTTP", async () => {
    request
      .mockResolvedValueOnce({ request_id: "r", context: { ssh_session_id: "ssh-1" } })
      .mockResolvedValueOnce({ request_id: "r", recoveries: [] });

    await getManualSftpContext("ssh-1");
    await listManualSftpRecoveries();

    expect(request.mock.calls).toEqual([
      ["POST", "/v1/sftp/contexts", { body: { ssh_session_id: "ssh-1" } }],
      ["GET", "/v1/sftp/recoveries"],
    ]);
  });

  it("delegates upload file ownership to the browser coordinator", async () => {
    coordinator.prepareUpload.mockResolvedValue(null);

    await prepareManualSftpUpload("ssh-1", "/srv/data", "report.csv");

    expect(coordinator.prepareUpload).toHaveBeenCalledWith(
      "ssh-1", "/srv/data", "report.csv",
    );
    expect(JSON.stringify(coordinator.prepareUpload.mock.calls)).not.toMatch(/localPath/i);
  });

  it("sends rename intent without a WebView local path", async () => {
    request.mockResolvedValue({ request_id: "r", terminal: { state: "succeeded" } });

    await renameManualSftpEntry("ssh-1", "/a", "/b", true);

    expect(request).toHaveBeenCalledWith("POST", "/v1/sftp/renames", {
      body: expect.objectContaining({
        ssh_session_id: "ssh-1",
        source_path: "/a",
        target_path: "/b",
        overwrite: true,
      }),
    });
    expect(JSON.stringify(request.mock.calls)).not.toMatch(/localPath/i);
  });

  it("rejects malformed or extra event fields", () => {
    expect(() => parseTransferProgress({ cancellable: true })).toThrow(/invalid/i);
    expect(() => parseMutationProgress({ cancellable: true })).toThrow(/invalid/i);
  });

  it("subscribes once to the process-wide Runtime WebSocket", async () => {
    const unsubscribe = vi.fn();
    subscribe.mockReturnValue(unsubscribe);

    const result = await subscribeManualSftpEvents(
      () => undefined, () => undefined, () => undefined,
    );
    result();

    expect(subscribe).toHaveBeenCalledOnce();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });
});
