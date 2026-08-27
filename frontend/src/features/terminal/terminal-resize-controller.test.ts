import { describe, expect, it, vi } from "vitest";

import { TerminalResizeController } from "./terminal-resize-controller";

const setup = () => {
  const frames = new Map<number, FrameRequestCallback>();
  let nextHandle = 1;
  let size = { cols: 80, rows: 24 };
  let enabled = true;
  const fit = vi.fn();
  const onRemoteResize = vi.fn();
  const cancelFrame = vi.fn((handle: number) => frames.delete(handle));
  const controller = new TerminalResizeController({
    fit,
    readSize: () => size,
    isRemoteResizeEnabled: () => enabled,
    onRemoteResize,
    requestFrame: (callback) => {
      const handle = nextHandle++;
      frames.set(handle, callback);
      return handle;
    },
    cancelFrame,
  });
  const runNextFrame = () => {
    const entry = frames.entries().next().value as
      | [number, FrameRequestCallback]
      | undefined;
    if (!entry) throw new Error("No frame was scheduled.");
    frames.delete(entry[0]);
    entry[1](performance.now());
  };
  return {
    controller,
    fit,
    onRemoteResize,
    cancelFrame,
    frames,
    runNextFrame,
    setSize: (next: { cols: number; rows: number }) => {
      size = next;
    },
    setEnabled: (next: boolean) => {
      enabled = next;
    },
  };
};

describe("TerminalResizeController", () => {
  it("coalesces repeated requests into one fit and one remote resize", () => {
    const harness = setup();
    for (let index = 0; index < 10; index += 1) harness.controller.request();
    expect(harness.frames.size).toBe(1);
    harness.runNextFrame();
    expect(harness.fit).toHaveBeenCalledTimes(1);
    expect(harness.onRemoteResize).toHaveBeenCalledWith({ cols: 80, rows: 24 });
  });

  it("does not resend unchanged dimensions but sends the final changed size", () => {
    const harness = setup();
    harness.controller.request();
    harness.runNextFrame();
    harness.controller.request();
    harness.runNextFrame();
    expect(harness.onRemoteResize).toHaveBeenCalledTimes(1);

    harness.setSize({ cols: 120, rows: 32 });
    harness.controller.request();
    harness.runNextFrame();
    expect(harness.onRemoteResize).toHaveBeenLastCalledWith({
      cols: 120,
      rows: 32,
    });
  });

  it("fits locally without remote resize while disabled", () => {
    const harness = setup();
    harness.setEnabled(false);
    harness.controller.request();
    harness.runNextFrame();
    expect(harness.fit).toHaveBeenCalledTimes(1);
    expect(harness.onRemoteResize).not.toHaveBeenCalled();
  });

  it.each([
    { cols: 19, rows: 24 },
    { cols: 501, rows: 24 },
    { cols: 80, rows: 4 },
    { cols: 80, rows: 301 },
  ])("crashes for invalid xterm size $cols×$rows", (size) => {
    const harness = setup();
    harness.setSize(size);
    harness.controller.request();
    expect(() => harness.runNextFrame()).toThrow(
      `Invalid terminal dimensions: ${size.cols}×${size.rows}`,
    );
  });

  it("cancels a pending frame on dispose", () => {
    const harness = setup();
    harness.controller.request();
    harness.controller.dispose();
    expect(harness.cancelFrame).toHaveBeenCalledTimes(1);
    expect(harness.frames.size).toBe(0);
    expect(() => harness.controller.request()).toThrow(
      "TerminalResizeController is disposed.",
    );
  });
});
