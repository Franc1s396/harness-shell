export const SFTP_CHUNK_BYTES = 262_144;

export type BrowserUploadSource = Readonly<{
  file: File;
  displayName: string;
  byteCount: number;
  lastModified: number;
}>;

export type BrowserDownloadTarget = Readonly<{
  handle: FileSystemFileHandle;
  displayName: string;
}>;

export class BrowserFileError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "BrowserFileError";
    this.code = code;
  }
}

export class BrowserFileGateway {
  async selectUploadSource(): Promise<BrowserUploadSource | null> {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = false;
    input.hidden = true;
    document.body.append(input);

    return new Promise<BrowserUploadSource | null>((resolve, reject) => {
      let settled = false;
      const finish = (value: BrowserUploadSource | null) => {
        if (settled) return;
        settled = true;
        input.remove();
        resolve(value);
      };
      input.addEventListener(
        "change",
        () => {
          try {
            const files = input.files;
            if (files === null || files.length === 0) {
              finish(null);
              return;
            }
            if (files.length !== 1) throw invalidLocalFile();
            const file = files.item(0);
            if (file === null) throw invalidLocalFile();
            validateFile(file);
            finish({
              file,
              displayName: file.name,
              byteCount: file.size,
              lastModified: file.lastModified,
            });
          } catch (error) {
            settled = true;
            input.remove();
            reject(error);
          }
        },
        { once: true },
      );
      input.addEventListener("cancel", () => finish(null), { once: true });
      input.click();
    });
  }

  async selectDownloadTarget(
    suggestedName: string,
  ): Promise<BrowserDownloadTarget | null> {
    validateBasename(suggestedName);
    const picker = window.showSaveFilePicker;
    if (window.isSecureContext !== true || typeof picker !== "function") {
      throw new BrowserFileError(
        "SFTP_LOCAL_FILE_API_UNAVAILABLE",
        "Local file access is unavailable",
      );
    }

    // Invoke before the first await so the browser still observes user activation.
    const selection = picker.call(window, { suggestedName });
    let handle: FileSystemFileHandle;
    try {
      handle = await selection;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return null;
      throw new BrowserFileError(
        "SFTP_LOCAL_FILE_SELECTION_FAILED",
        "Local file selection failed",
      );
    }
    if (
      handle.kind !== "file" ||
      typeof handle.createWritable !== "function"
    ) {
      throw new BrowserFileError(
        "SFTP_LOCAL_FILE_API_UNAVAILABLE",
        "Local file access is unavailable",
      );
    }
    validateBasename(handle.name);
    return { handle, displayName: handle.name };
  }
}

export async function* readFileChunks(
  file: File,
): AsyncGenerator<Uint8Array> {
  validateFile(file);
  for (let offset = 0; offset < file.size; offset += SFTP_CHUNK_BYTES) {
    const end = Math.min(offset + SFTP_CHUNK_BYTES, file.size);
    yield new Uint8Array(await file.slice(offset, end).arrayBuffer());
  }
}

function validateFile(file: File): void {
  validateBasename(file.name);
  if (!Number.isSafeInteger(file.size) || file.size < 0) throw invalidLocalFile();
  if (!Number.isSafeInteger(file.lastModified) || file.lastModified < 0) {
    throw invalidLocalFile();
  }
}

function validateBasename(value: string): void {
  if (value.length === 0 || value.includes("/") || value.includes("\\")) {
    throw invalidLocalFile();
  }
}

function invalidLocalFile(): BrowserFileError {
  return new BrowserFileError(
    "SFTP_LOCAL_FILE_INVALID",
    "Local file metadata is invalid",
  );
}
