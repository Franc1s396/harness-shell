const MAX_PRIVATE_KEY_BYTES = 1_048_576;

export class PrivateKeyFileError extends Error {
  readonly code: string;

  constructor() {
    super("Private key file is invalid");
    this.name = "PrivateKeyFileError";
    this.code = "PRIVATE_KEY_FILE_INVALID";
  }
}

export const selectPrivateKeyText = (): Promise<string | null> => {
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = false;
  input.hidden = true;
  document.body.append(input);

  return new Promise<string | null>((resolve, reject) => {
    let settled = false;
    const finish = (value: string | null) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(value);
    };
    const fail = () => {
      if (settled) return;
      settled = true;
      input.remove();
      reject(new PrivateKeyFileError());
    };

    input.addEventListener("change", async () => {
      try {
        const files = input.files;
        if (files === null || files.length === 0) {
          finish(null);
          return;
        }
        if (files.length !== 1) {
          fail();
          return;
        }
        const file = files.item(0);
        if (
          file === null ||
          !Number.isSafeInteger(file.size) ||
          file.size <= 0 ||
          file.size > MAX_PRIVATE_KEY_BYTES
        ) {
          fail();
          return;
        }
        const bytes = new Uint8Array(await file.arrayBuffer());
        if (bytes.byteLength !== file.size) {
          fail();
          return;
        }
        const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
        if (text.length === 0) {
          fail();
          return;
        }
        finish(text);
      } catch {
        fail();
      }
    }, { once: true });
    input.addEventListener("cancel", () => finish(null), { once: true });
    input.click();
  });
};
