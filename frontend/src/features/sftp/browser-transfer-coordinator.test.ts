import { describe, expect, it, vi } from "vitest";

import { SFTP_CHUNK_BYTES, type BrowserFileGateway } from "./browser-file-gateway";
import { sha256File } from "./browser-sha256";
import {
  BrowserTransferCoordinator,
  type ManualSftpTransport,
} from "./browser-transfer-coordinator";


const SESSION_ID = "00000000-0000-4000-8000-000000000901";
const OPERATION_ID = "00000000-0000-4000-8000-000000000902";
const PREPARATION_ID = "00000000-0000-4000-8000-000000000903";
const ABSENT_SNAPSHOT = {
  path: "/srv/payload.bin",
  exists: false,
  entry_type: null,
  size: null,
  mtime_ns: null,
  sha256: null,
};


describe("BrowserTransferCoordinator", () => {
  it("hashes twice and uploads only strict sequential chunks", async () => {
    const bytes = new Uint8Array(SFTP_CHUNK_BYTES + 1);
    bytes[SFTP_CHUNK_BYTES] = 7;
    const file = memoryFile("payload.bin", bytes);
    const sourceHash = await sha256File(file);
    const gateway = uploadGateway(file);
    const transport = transportMock();
    transport.preflightUpload.mockResolvedValue(ABSENT_SNAPSHOT);
    transport.beginUpload.mockResolvedValue({
      operation_id: OPERATION_ID,
      temp_path: "/srv/.payload.part",
      next_sequence: 0,
      next_offset: 0,
    });
    transport.putUploadChunk.mockImplementation(
      async (_operationId, sequence, offset, chunk) => ({
        operation_id: OPERATION_ID,
        sequence,
        offset,
        accepted_bytes: chunk.byteLength,
      }),
    );
    transport.finishUpload.mockResolvedValue(terminal("succeeded"));
    const coordinator = coordinatorWith({
      gateway,
      transport,
      hashFile: vi.fn().mockResolvedValue(sourceHash),
    });

    const preparation = await coordinator.prepareUpload(
      SESSION_ID,
      "/srv",
      "payload.bin",
    );
    const result = await coordinator.executeUpload(
      preparation!.preparation_id,
      true,
    );

    expect(
      transport.putUploadChunk.mock.calls.map((call) => call.slice(1, 3)),
    ).toEqual([
      [0, 0],
      [1, SFTP_CHUNK_BYTES],
    ]);
    expect(result.state).toBe("succeeded");
    expect(transport.abortUpload).not.toHaveBeenCalled();
  });

  it("aborts when the second upload digest differs", async () => {
    const file = memoryFile("payload.bin", new Uint8Array([1, 2, 3]));
    const gateway = uploadGateway(file);
    const transport = transportMock();
    transport.preflightUpload.mockResolvedValue(ABSENT_SNAPSHOT);
    transport.beginUpload.mockResolvedValue({
      operation_id: OPERATION_ID,
      temp_path: "/srv/.payload.part",
      next_sequence: 0,
      next_offset: 0,
    });
    transport.putUploadChunk.mockResolvedValue({
      operation_id: OPERATION_ID,
      sequence: 0,
      offset: 0,
      accepted_bytes: 3,
    });
    transport.abortUpload.mockResolvedValue(terminal("cancelled"));
    const coordinator = coordinatorWith({
      gateway,
      transport,
      hashFile: vi.fn().mockResolvedValue("0".repeat(64)),
    });
    const preparation = await coordinator.prepareUpload(
      SESSION_ID,
      "/srv",
      "payload.bin",
    );

    await expect(
      coordinator.executeUpload(preparation!.preparation_id, true),
    ).rejects.toMatchObject({ code: "SFTP_LOCAL_SOURCE_CHANGED" });
    expect(transport.abortUpload).toHaveBeenCalledOnce();
    expect(transport.finishUpload).not.toHaveBeenCalled();
  });

  it("selects a download target before remote work and closes after finish", async () => {
    const order: string[] = [];
    const writable = {
      write: vi.fn(async () => order.push("write")),
      close: vi.fn(async () => order.push("close")),
      abort: vi.fn(async () => order.push("abort-local")),
    };
    const handle = {
      kind: "file" as const,
      name: "report.txt",
      createWritable: vi.fn(async () => writable),
    } as unknown as FileSystemFileHandle;
    const gateway = {
      selectUploadSource: vi.fn(),
      selectDownloadTarget: vi.fn(() => {
        order.push("picker");
        return Promise.resolve({ handle, displayName: "report.txt" });
      }),
    } as unknown as BrowserFileGateway;
    const transport = transportMock();
    transport.preflightDownload.mockImplementation(async () => {
      order.push("preflight");
      return {
        path: "/srv/report.txt",
        snapshot: {
          ...ABSENT_SNAPSHOT,
          path: "/srv/report.txt",
          exists: true,
          entry_type: "file",
          size: 3,
        },
        sha256: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        byte_count: 3,
      };
    });
    transport.beginDownload.mockResolvedValue({
      operation_id: OPERATION_ID,
      path: "/srv/report.txt",
      snapshot: {
        ...ABSENT_SNAPSHOT,
        path: "/srv/report.txt",
        exists: true,
        entry_type: "file",
        size: 3,
      },
      sha256: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
      byte_count: 3,
      next_sequence: 0,
      next_offset: 0,
    });
    transport.getDownloadChunk.mockResolvedValue({
      operation_id: OPERATION_ID,
      sequence: 0,
      offset: 0,
      data: new Uint8Array([97, 98, 99]),
      eof: true,
    });
    transport.finishDownload.mockImplementation(async () => {
      order.push("finish-remote");
      return terminal("succeeded");
    });
    const coordinator = coordinatorWith({ gateway, transport });

    const preparing = coordinator.prepareDownloadFromUserGesture(
      SESSION_ID,
      "/srv/report.txt",
      "report.txt",
    );
    expect(order).toEqual(["picker"]);
    const preparation = await preparing;
    expect(order).toEqual(["picker", "preflight"]);
    await coordinator.executeDownload(preparation!.preparation_id, true);

    expect(order).toEqual([
      "picker",
      "preflight",
      "write",
      "finish-remote",
      "close",
    ]);
    expect(writable.abort).not.toHaveBeenCalled();
  });

  it("does no remote work when the save picker is cancelled", async () => {
    const gateway = {
      selectUploadSource: vi.fn(),
      selectDownloadTarget: vi.fn().mockResolvedValue(null),
    } as unknown as BrowserFileGateway;
    const transport = transportMock();
    const coordinator = coordinatorWith({ gateway, transport });

    await expect(
      coordinator.prepareDownloadFromUserGesture(
        SESSION_ID,
        "/srv/report.txt",
        "report.txt",
      ),
    ).resolves.toBeNull();
    expect(transport.preflightDownload).not.toHaveBeenCalled();
  });

  it("aborts both owners when downloaded bytes do not match preflight", async () => {
    const writable = {
      write: vi.fn().mockResolvedValue(undefined),
      close: vi.fn().mockResolvedValue(undefined),
      abort: vi.fn().mockResolvedValue(undefined),
    };
    const handle = {
      kind: "file" as const,
      name: "report.txt",
      createWritable: vi.fn().mockResolvedValue(writable),
    } as unknown as FileSystemFileHandle;
    const gateway = {
      selectUploadSource: vi.fn(),
      selectDownloadTarget: vi.fn().mockResolvedValue({
        handle,
        displayName: "report.txt",
      }),
    } as unknown as BrowserFileGateway;
    const transport = transportMock();
    const remote = {
      path: "/srv/report.txt",
      snapshot: {
        ...ABSENT_SNAPSHOT,
        path: "/srv/report.txt",
        exists: true,
        entry_type: "file" as const,
        size: 3,
      },
      sha256: "0".repeat(64),
      byte_count: 3,
    };
    transport.preflightDownload.mockResolvedValue(remote);
    transport.beginDownload.mockResolvedValue({
      operation_id: OPERATION_ID,
      ...remote,
      next_sequence: 0,
      next_offset: 0,
    });
    transport.getDownloadChunk.mockResolvedValue({
      operation_id: OPERATION_ID,
      sequence: 0,
      offset: 0,
      data: new Uint8Array([97, 98, 99]),
      eof: true,
    });
    transport.abortDownload.mockResolvedValue(terminal("cancelled"));
    const coordinator = coordinatorWith({ gateway, transport });
    const preparation = await coordinator.prepareDownload(
      SESSION_ID,
      "/srv/report.txt",
      "report.txt",
    );

    await expect(
      coordinator.executeDownload(preparation!.preparation_id, true),
    ).rejects.toMatchObject({ code: "SFTP_REMOTE_SOURCE_CHANGED" });
    expect(writable.abort).toHaveBeenCalledOnce();
    expect(transport.abortDownload).toHaveBeenCalledOnce();
    expect(transport.finishDownload).not.toHaveBeenCalled();
    expect(writable.close).not.toHaveBeenCalled();
  });

  it("reports local close failure after remote finish without remote abort", async () => {
    const writable = {
      write: vi.fn().mockResolvedValue(undefined),
      close: vi.fn().mockRejectedValue(new Error("disk failure")),
      abort: vi.fn().mockResolvedValue(undefined),
    };
    const handle = {
      kind: "file" as const,
      name: "empty.txt",
      createWritable: vi.fn().mockResolvedValue(writable),
    } as unknown as FileSystemFileHandle;
    const gateway = {
      selectUploadSource: vi.fn(),
      selectDownloadTarget: vi.fn().mockResolvedValue({
        handle,
        displayName: "empty.txt",
      }),
    } as unknown as BrowserFileGateway;
    const transport = transportMock();
    const remote = {
      path: "/srv/empty.txt",
      snapshot: {
        ...ABSENT_SNAPSHOT,
        path: "/srv/empty.txt",
        exists: true,
        entry_type: "file" as const,
        size: 0,
      },
      sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      byte_count: 0,
    };
    transport.preflightDownload.mockResolvedValue(remote);
    transport.beginDownload.mockResolvedValue({
      operation_id: OPERATION_ID,
      ...remote,
      next_sequence: 0,
      next_offset: 0,
    });
    transport.getDownloadChunk.mockResolvedValue({
      operation_id: OPERATION_ID,
      sequence: 0,
      offset: 0,
      data: new Uint8Array(),
      eof: true,
    });
    transport.finishDownload.mockResolvedValue(terminal("succeeded"));
    const coordinator = coordinatorWith({ gateway, transport });
    const preparation = await coordinator.prepareDownload(
      SESSION_ID,
      "/srv/empty.txt",
      "empty.txt",
    );

    await expect(
      coordinator.executeDownload(preparation!.preparation_id, true),
    ).rejects.toMatchObject({ code: "SFTP_LOCAL_WRITE_FAILED" });
    expect(transport.finishDownload).toHaveBeenCalledOnce();
    expect(transport.abortDownload).not.toHaveBeenCalled();
  });

  it("does not consume a preparation when another transfer is busy", async () => {
    const file = memoryFile("payload.bin", new Uint8Array([1]));
    const hash = await sha256File(file);
    const gateway = uploadGateway(file);
    const transport = transportMock();
    transport.preflightUpload.mockResolvedValue(ABSENT_SNAPSHOT);
    transport.beginUpload.mockImplementation(async (request) => ({
      operation_id: request.operation_id,
      temp_path: "/srv/.payload.part",
      next_sequence: 0,
      next_offset: 0,
    }));
    let releaseFirstChunk!: () => void;
    const firstChunkBlocked = new Promise<void>((resolve) => {
      releaseFirstChunk = resolve;
    });
    transport.putUploadChunk
      .mockImplementationOnce(async (operationId, sequence, offset, chunk) => {
        await firstChunkBlocked;
        return {
          operation_id: operationId,
          sequence,
          offset,
          accepted_bytes: chunk.byteLength,
        };
      })
      .mockImplementation(async (operationId, sequence, offset, chunk) => ({
        operation_id: operationId,
        sequence,
        offset,
        accepted_bytes: chunk.byteLength,
      }));
    transport.finishUpload.mockImplementation(async (operationId) => ({
      ...terminal("succeeded"),
      operation_id: operationId,
    }));
    const ids = [
      "00000000-0000-4000-8000-000000000911",
      "00000000-0000-4000-8000-000000000912",
      "00000000-0000-4000-8000-000000000913",
      "00000000-0000-4000-8000-000000000914",
    ];
    const coordinator = new BrowserTransferCoordinator({
      gateway,
      transport: transport as unknown as ManualSftpTransport,
      hashFile: vi.fn().mockResolvedValue(hash),
      randomUUID: () => ids.shift()!,
    });
    const first = await coordinator.prepareUpload(SESSION_ID, "/srv", "one.bin");
    const second = await coordinator.prepareUpload(SESSION_ID, "/srv", "two.bin");

    const firstExecution = coordinator.executeUpload(first!.preparation_id, true);
    await vi.waitFor(() => expect(transport.putUploadChunk).toHaveBeenCalledOnce());
    await expect(
      coordinator.executeUpload(second!.preparation_id, true),
    ).rejects.toMatchObject({ code: "SFTP_TRANSFER_BUSY" });
    releaseFirstChunk();
    await firstExecution;

    await expect(
      coordinator.executeUpload(second!.preparation_id, true),
    ).resolves.toMatchObject({ state: "succeeded" });
  });
});


function memoryFile(name: string, bytes: Uint8Array): File {
  return {
    name,
    size: bytes.byteLength,
    lastModified: 1,
    slice: (start: number, end: number) => ({
      arrayBuffer: async () => bytes.slice(start, end).buffer,
    }),
  } as unknown as File;
}

function uploadGateway(file: File): BrowserFileGateway {
  return {
    selectUploadSource: vi.fn().mockResolvedValue({
      file,
      displayName: file.name,
      byteCount: file.size,
      lastModified: file.lastModified,
    }),
    selectDownloadTarget: vi.fn(),
  } as unknown as BrowserFileGateway;
}

function terminal(state: "succeeded" | "cancelled") {
  return {
    operation_id: OPERATION_ID,
    state,
    error_code: null,
    message: "done",
    sha256: null,
    byte_count: null,
    recovery_id: null,
  } as const;
}

function transportMock() {
  return {
    preflightUpload: vi.fn(),
    beginUpload: vi.fn(),
    putUploadChunk: vi.fn(),
    finishUpload: vi.fn(),
    abortUpload: vi.fn(),
    preflightDownload: vi.fn(),
    beginDownload: vi.fn(),
    getDownloadChunk: vi.fn(),
    finishDownload: vi.fn(),
    abortDownload: vi.fn(),
  } as unknown as {
    [Key in keyof ManualSftpTransport]: ReturnType<typeof vi.fn>;
  };
}

function coordinatorWith({
  gateway,
  transport,
  hashFile = sha256File,
}: {
  gateway: BrowserFileGateway;
  transport: ReturnType<typeof transportMock>;
  hashFile?: typeof sha256File;
}) {
  const ids = [PREPARATION_ID, OPERATION_ID];
  return new BrowserTransferCoordinator({
    gateway,
    transport: transport as unknown as ManualSftpTransport,
    hashFile,
    randomUUID: () => ids.shift() ?? crypto.randomUUID(),
  });
}
