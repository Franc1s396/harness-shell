// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BrowserFileGateway,
  SFTP_CHUNK_BYTES,
  readFileChunks,
} from "./browser-file-gateway";


describe("BrowserFileGateway", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("fails explicitly when save-file access is unavailable", async () => {
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: false,
    });
    const gateway = new BrowserFileGateway();

    await expect(gateway.selectDownloadTarget("report.txt")).rejects.toMatchObject({
      code: "SFTP_LOCAL_FILE_API_UNAVAILABLE",
    });
  });

  it("treats picker AbortError as user cancellation", async () => {
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    const savePicker = vi
      .fn()
      .mockRejectedValueOnce(new DOMException("cancelled", "AbortError"));
    Object.defineProperty(window, "showSaveFilePicker", {
      configurable: true,
      value: savePicker,
    });
    const gateway = new BrowserFileGateway();

    await expect(gateway.selectDownloadTarget("report.txt")).resolves.toBeNull();
    expect(savePicker).toHaveBeenCalledOnce();
  });

  it("maps non-cancellation picker failures without leaking their message", async () => {
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    Object.defineProperty(window, "showSaveFilePicker", {
      configurable: true,
      value: vi.fn().mockRejectedValueOnce(
        new DOMException("C:\\Users\\secret\\report.txt", "SecurityError"),
      ),
    });

    await expect(
      new BrowserFileGateway().selectDownloadTarget("report.txt"),
    ).rejects.toMatchObject({
      code: "SFTP_LOCAL_FILE_SELECTION_FAILED",
      message: "Local file selection failed",
    });
  });

  it("rejects a save picker result without file-write capability", async () => {
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    Object.defineProperty(window, "showSaveFilePicker", {
      configurable: true,
      value: vi.fn().mockResolvedValueOnce({
        kind: "directory",
        name: "report.txt",
      }),
    });

    await expect(
      new BrowserFileGateway().selectDownloadTarget("report.txt"),
    ).rejects.toMatchObject({ code: "SFTP_LOCAL_FILE_API_UNAVAILABLE" });
  });

  it("reads only fixed-size slices and keeps the final short chunk", async () => {
    const bytes = new Uint8Array(SFTP_CHUNK_BYTES + 3);
    bytes.set([1, 2, 3], SFTP_CHUNK_BYTES);
    const file = {
      name: "payload.bin",
      size: bytes.byteLength,
      lastModified: 1,
      slice: (start: number, end: number) => ({
        arrayBuffer: async () => bytes.slice(start, end).buffer,
      }),
    } as unknown as File;
    const chunks: Uint8Array[] = [];

    for await (const chunk of readFileChunks(file)) chunks.push(chunk);

    expect(chunks.map((chunk) => chunk.byteLength)).toEqual([
      SFTP_CHUNK_BYTES,
      3,
    ]);
    expect([...chunks[1]]).toEqual([1, 2, 3]);
  });

  it("rejects a file with an unsafe byte size before reading", async () => {
    const unsafeFile = {
      name: "unsafe.bin",
      size: Number.MAX_SAFE_INTEGER + 1,
      slice: vi.fn(),
    } as unknown as File;

    const consume = async () => {
      for await (const _chunk of readFileChunks(unsafeFile)) {
        // The invalid size must fail before iteration produces any data.
      }
    };

    await expect(consume()).rejects.toMatchObject({
      code: "SFTP_LOCAL_FILE_INVALID",
    });
    expect(unsafeFile.slice).not.toHaveBeenCalled();
  });
});
