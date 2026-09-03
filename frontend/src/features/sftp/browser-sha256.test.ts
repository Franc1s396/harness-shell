import { describe, expect, it, vi } from "vitest";

import { sha256File } from "./browser-sha256";
import { SFTP_CHUNK_BYTES, readFileChunks } from "./browser-file-gateway";


describe("browser SHA-256", () => {
  it("hashes the empty and abc known vectors", async () => {
    await expect(sha256File(new File([], "empty"))).resolves.toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
    await expect(sha256File(new File(["abc"], "abc"))).resolves.toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });

  it("supports two independent bounded passes over one file", async () => {
    const file = new File(
      [new Uint8Array(SFTP_CHUNK_BYTES), new Uint8Array([7])],
      "two-pass.bin",
    );
    const sliceSpy = vi.spyOn(file, "slice");

    const first = await sha256File(file);
    const secondChunks: Uint8Array[] = [];
    for await (const chunk of readFileChunks(file)) secondChunks.push(chunk);

    expect(first).toMatch(/^[0-9a-f]{64}$/);
    expect(secondChunks.map((chunk) => chunk.byteLength)).toEqual([
      SFTP_CHUNK_BYTES,
      1,
    ]);
    expect(sliceSpy).toHaveBeenCalledTimes(4);
  });
});
