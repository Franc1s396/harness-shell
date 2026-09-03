export type CredentialEnvelope = Readonly<{
  version: 1;
  key_id: string;
  wrapped_key_b64: string;
  iv_b64: string;
  ciphertext_b64: string;
}>;

export type CredentialPublicKey = Readonly<{
  version: 1;
  scheme: "RSA-OAEP-256+A256GCM";
  key_id: string;
  public_key_spki_b64: string;
}>;

let publicKeyPromise: Promise<CredentialPublicKey> | null = null;

export const createCredentialEnvelope = async (
  secret: string,
  loadPublicKey: () => Promise<CredentialPublicKey>,
): Promise<CredentialEnvelope> => {
  publicKeyPromise ??= loadPublicKey();
  const publicKey = await publicKeyPromise;
  requirePublicKey(publicKey);
  let plaintext: Uint8Array<ArrayBuffer> | null = new TextEncoder().encode(secret);
  let aesKey: CryptoKey | null = null;
  let rsaKey: CryptoKey | null = null;
  try {
    rsaKey = await crypto.subtle.importKey(
      "spki",
      decodeBase64(publicKey.public_key_spki_b64),
      { name: "RSA-OAEP", hash: "SHA-256" },
      false,
      ["wrapKey"],
    );
    aesKey = await crypto.subtle.generateKey(
      { name: "AES-GCM", length: 256 },
      true,
      ["encrypt"],
    );
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const aad = new TextEncoder().encode(
      `harness-shell-credential-v1\0${publicKey.key_id}`,
    );
    const ciphertext = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: aad },
      aesKey,
      plaintext,
    );
    const wrappedKey = await crypto.subtle.wrapKey(
      "raw",
      aesKey,
      rsaKey,
      { name: "RSA-OAEP" },
    );
    return {
      version: 1,
      key_id: publicKey.key_id,
      wrapped_key_b64: encodeBase64(new Uint8Array(wrappedKey)),
      iv_b64: encodeBase64(iv),
      ciphertext_b64: encodeBase64(new Uint8Array(ciphertext)),
    };
  } finally {
    plaintext?.fill(0);
    plaintext = null;
    aesKey = null;
    rsaKey = null;
  }
};

export const resetCredentialPublicKeyCacheForTest = (): void => {
  publicKeyPromise = null;
};

const requirePublicKey = (value: CredentialPublicKey): void => {
  if (
    value.version !== 1 ||
    value.scheme !== "RSA-OAEP-256+A256GCM" ||
    typeof value.key_id !== "string" ||
    typeof value.public_key_spki_b64 !== "string"
  ) {
    throw new Error("CREDENTIAL_PUBLIC_KEY_INVALID");
  }
};

const encodeBase64 = (bytes: Uint8Array): string => {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
};

const decodeBase64 = (value: string): Uint8Array<ArrayBuffer> => {
  let binary: string;
  try {
    binary = atob(value);
  } catch {
    throw new Error("CREDENTIAL_PUBLIC_KEY_INVALID");
  }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (encodeBase64(bytes) !== value) {
    throw new Error("CREDENTIAL_PUBLIC_KEY_INVALID");
  }
  return bytes;
};
