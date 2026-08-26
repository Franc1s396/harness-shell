import { describe, expect, it } from "vitest";

import { base64ToBytes, bytesToBase64 } from "./base64";

describe("binary base64 conversion", () => {
  it("round-trips arbitrary bytes without text transcoding", () => {
    const bytes = new Uint8Array([0, 1, 127, 128, 255, 0xe4, 0xb8, 0xad]);
    expect(base64ToBytes(bytesToBase64(bytes))).toEqual(bytes);
  });

  it("rejects non-canonical base64", () => {
    expect(() => base64ToBytes("%%%")) .toThrow("Invalid base64");
  });
});
