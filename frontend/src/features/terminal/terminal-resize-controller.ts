export type TerminalSize = {
  cols: number;
  rows: number;
};

export type TerminalResizeControllerOptions = {
  fit: () => void;
  readSize: () => TerminalSize;
  isRemoteResizeEnabled: () => boolean;
  onRemoteResize: (size: TerminalSize) => void;
  requestFrame: (callback: FrameRequestCallback) => number;
  cancelFrame: (handle: number) => void;
};

const validateSize = ({ cols, rows }: TerminalSize) => {
  if (
    !Number.isInteger(cols) ||
    cols < 20 ||
    cols > 500 ||
    !Number.isInteger(rows) ||
    rows < 5 ||
    rows > 300
  ) {
    throw new Error(`Invalid terminal dimensions: ${cols}×${rows}`);
  }
};

export class TerminalResizeController {
  private frameHandle: number | null = null;
  private lastSent: TerminalSize | null = null;
  private disposed = false;

  constructor(private readonly options: TerminalResizeControllerOptions) {}

  request(): void {
    if (this.disposed) {
      throw new Error("TerminalResizeController is disposed.");
    }
    if (this.frameHandle !== null) return;
    this.frameHandle = this.options.requestFrame(() => {
      this.frameHandle = null;
      this.options.fit();
      const size = this.options.readSize();
      validateSize(size);
      if (!this.options.isRemoteResizeEnabled()) return;
      if (
        this.lastSent?.cols === size.cols &&
        this.lastSent.rows === size.rows
      ) {
        return;
      }
      this.options.onRemoteResize(size);
      this.lastSent = { ...size };
    });
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.frameHandle !== null) {
      this.options.cancelFrame(this.frameHandle);
      this.frameHandle = null;
    }
  }
}
