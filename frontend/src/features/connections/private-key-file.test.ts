// @vitest-environment jsdom

import { afterEach, describe, expect, it } from "vitest";

import { selectPrivateKeyText } from "./private-key-file";

const choose = (input: HTMLInputElement, file: File) => {
  Object.defineProperty(input, "files", {
    configurable: true,
    value: { length: 1, item: () => file, 0: file },
  });
  input.dispatchEvent(new Event("change"));
};

describe("selectPrivateKeyText", () => {
  afterEach(() => document.querySelectorAll("input[type=file]").forEach((value) => value.remove()));

  it("reads one selected private key as strict UTF-8 in React", async () => {
    const bytes = new TextEncoder().encode("PRIVATE_KEY_MARKER");
    const file = {
      name: "id_ed25519",
      size: bytes.byteLength,
      arrayBuffer: async () => bytes.buffer,
    } as File;

    const pending = selectPrivateKeyText();
    const input = document.querySelector<HTMLInputElement>("input[type=file]");
    expect(input).not.toBeNull();
    choose(input!, file);

    await expect(pending).resolves.toBe("PRIVATE_KEY_MARKER");
    expect(document.querySelector("input[type=file]")).toBeNull();
  });

  it("returns null when the user cancels the picker", async () => {
    const pending = selectPrivateKeyText();
    const input = document.querySelector<HTMLInputElement>("input[type=file]");
    input!.dispatchEvent(new Event("cancel"));

    await expect(pending).resolves.toBeNull();
  });

  it("rejects invalid UTF-8 without sending bytes to Python", async () => {
    const bytes = new Uint8Array([0xff]);
    const file = {
      name: "invalid-key",
      size: bytes.byteLength,
      arrayBuffer: async () => bytes.buffer,
    } as File;

    const pending = selectPrivateKeyText();
    choose(document.querySelector<HTMLInputElement>("input[type=file]")!, file);

    await expect(pending).rejects.toMatchObject({
      code: "PRIVATE_KEY_FILE_INVALID",
    });
  });
});
