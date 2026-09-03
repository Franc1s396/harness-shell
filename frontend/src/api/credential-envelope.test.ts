import { beforeEach, expect, it, vi } from "vitest";

import {
  createCredentialEnvelope,
  resetCredentialPublicKeyCacheForTest,
} from "./credential-envelope";


const toBase64 = (bytes: ArrayBuffer): string =>
  btoa(String.fromCharCode(...new Uint8Array(bytes)));

const fromBase64 = (value: string): Uint8Array =>
  Uint8Array.from(atob(value), (character) => character.charCodeAt(0));

beforeEach(() => resetCredentialPublicKeyCacheForTest());

it("creates the exact RSA-OAEP and AES-GCM v1 envelope", async () => {
  const pair = await crypto.subtle.generateKey(
    {
      name: "RSA-OAEP",
      modulusLength: 3072,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["encrypt", "decrypt"],
  );
  const keyId = "10000000-0000-4000-8000-000000000001";
  const loadPublicKey = vi.fn().mockResolvedValue({
    version: 1,
    scheme: "RSA-OAEP-256+A256GCM",
    key_id: keyId,
    public_key_spki_b64: toBase64(await crypto.subtle.exportKey("spki", pair.publicKey)),
  });

  const envelope = await createCredentialEnvelope("secret-value", loadPublicKey);
  const aesKey = await crypto.subtle.decrypt(
    { name: "RSA-OAEP" },
    pair.privateKey,
    fromBase64(envelope.wrapped_key_b64),
  );
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: fromBase64(envelope.iv_b64),
      additionalData: new TextEncoder().encode(
        `harness-shell-credential-v1\0${keyId}`,
      ),
    },
    await crypto.subtle.importKey("raw", aesKey, "AES-GCM", false, ["decrypt"]),
    fromBase64(envelope.ciphertext_b64),
  );

  expect(new TextDecoder().decode(plaintext)).toBe("secret-value");
  expect(envelope).toMatchObject({ version: 1, key_id: keyId });
});

it("reuses the current page public key without hidden refetch", async () => {
  const pair = await crypto.subtle.generateKey(
    {
      name: "RSA-OAEP",
      modulusLength: 3072,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["encrypt", "decrypt"],
  );
  const loadPublicKey = vi.fn().mockResolvedValue({
    version: 1,
    scheme: "RSA-OAEP-256+A256GCM",
    key_id: "10000000-0000-4000-8000-000000000001",
    public_key_spki_b64: toBase64(await crypto.subtle.exportKey("spki", pair.publicKey)),
  });

  await createCredentialEnvelope("first", loadPublicKey);
  await createCredentialEnvelope("second", loadPublicKey);

  expect(loadPublicKey).toHaveBeenCalledOnce();
});

it("fetches the restarted Backend public key after a full page cache reset", async () => {
  const firstPair = await crypto.subtle.generateKey(
    {
      name: "RSA-OAEP",
      modulusLength: 3072,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["encrypt", "decrypt"],
  );
  const secondPair = await crypto.subtle.generateKey(
    {
      name: "RSA-OAEP",
      modulusLength: 3072,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["encrypt", "decrypt"],
  );
  const loadFirst = vi.fn().mockResolvedValue({
    version: 1,
    scheme: "RSA-OAEP-256+A256GCM",
    key_id: "10000000-0000-4000-8000-000000000001",
    public_key_spki_b64: toBase64(
      await crypto.subtle.exportKey("spki", firstPair.publicKey),
    ),
  });
  const loadSecond = vi.fn().mockResolvedValue({
    version: 1,
    scheme: "RSA-OAEP-256+A256GCM",
    key_id: "20000000-0000-4000-8000-000000000002",
    public_key_spki_b64: toBase64(
      await crypto.subtle.exportKey("spki", secondPair.publicKey),
    ),
  });

  expect((await createCredentialEnvelope("first", loadFirst)).key_id).toBe(
    "10000000-0000-4000-8000-000000000001",
  );
  resetCredentialPublicKeyCacheForTest();
  expect((await createCredentialEnvelope("second", loadSecond)).key_id).toBe(
    "20000000-0000-4000-8000-000000000002",
  );
  expect(loadFirst).toHaveBeenCalledOnce();
  expect(loadSecond).toHaveBeenCalledOnce();
});
