// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { StrictMode } from "react";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const terminalMock = vi.hoisted(() => ({
  instances: [] as Array<{
    writes: Uint8Array[];
    emitData: (data: string) => void;
  }>,
  fitInstances: [] as Array<{ fitCalls: number }>,
  resizeCallbacks: [] as ResizeObserverCallback[],
  frames: new Map<number, FrameRequestCallback>(),
  nextFrame: 1,
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fitCalls = 0;

    constructor() {
      terminalMock.fitInstances.push(this);
    }

    fit() {
      this.fitCalls += 1;
    }
  },
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    readonly writes: Uint8Array[] = [];
    cols = 80;
    rows = 24;
    private dataHandler: ((data: string) => void) | undefined;

    constructor() {
      terminalMock.instances.push(this);
    }

    loadAddon() {}
    open() {}
    focus() {}
    blur() {}
    dispose() {}

    onData(handler: (data: string) => void) {
      this.dataHandler = handler;
      return {
        dispose: () => {
          this.dataHandler = undefined;
        },
      };
    }

    write(data: Uint8Array) {
      this.writes.push(data);
    }

    emitData(data: string) {
      this.dataHandler?.(data);
    }
  },
}));

import { TerminalOutputBuffer } from "./terminal-output-buffer";
import { TerminalTab } from "./TerminalTab";

const makeBuffer = (initial?: Uint8Array) => {
  const outputBuffer = new TerminalOutputBuffer();
  outputBuffer.registerTab("tab-1", 1);
  if (initial) {
    outputBuffer.ingest({
      ptySessionId: "pty-1",
      streamSequence: 1,
      data: initial,
    });
  }
  outputBuffer.bindPty({
    ptySessionId: "pty-1",
    tabId: "tab-1",
    generation: 1,
  });
  return outputBuffer;
};

const renderTab = ({
  outputBuffer = makeBuffer(),
  active = true,
  enabled = true,
  fitRequestKey = 0,
  onInput = () => Promise.resolve(),
  onResize = () => undefined,
}: {
  outputBuffer?: TerminalOutputBuffer;
  active?: boolean;
  enabled?: boolean;
  fitRequestKey?: number;
  onInput?: (data: Uint8Array) => Promise<void>;
  onResize?: (cols: number, rows: number) => void;
} = {}) =>
  render(
    <TerminalTab
      tabId="tab-1"
      outputBuffer={outputBuffer}
      active={active}
      enabled={enabled}
      fitRequestKey={fitRequestKey}
      focusRequestKey={0}
      onInput={onInput}
      onResize={onResize}
      onFocusChange={() => undefined}
    />,
  );

const flushNextFrame = () => {
  const entry = terminalMock.frames.entries().next().value as
    | [number, FrameRequestCallback]
    | undefined;
  if (!entry) throw new Error("No animation frame is pending.");
  terminalMock.frames.delete(entry[0]);
  act(() => entry[1](performance.now()));
};

describe("TerminalTab", () => {
  beforeEach(() => {
    terminalMock.instances.length = 0;
    terminalMock.fitInstances.length = 0;
    terminalMock.resizeCallbacks.length = 0;
    terminalMock.frames.clear();
    terminalMock.nextFrame = 1;
    document.documentElement.style.setProperty("--color-app", "#0b1017");
    document.documentElement.style.setProperty("--color-ink", "#dce6ee");
    document.documentElement.style.setProperty("--color-accent", "#5fa8ff");
    document.documentElement.style.setProperty("--color-accent-soft", "#152a42");
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => 800,
    });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get: () => 600,
    });
    globalThis.ResizeObserver = class {
      constructor(callback: ResizeObserverCallback) {
        terminalMock.resizeCallbacks.push(callback);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    } as typeof ResizeObserver;
    window.requestAnimationFrame = vi.fn((callback: FrameRequestCallback) => {
      const handle = terminalMock.nextFrame++;
      terminalMock.frames.set(handle, callback);
      return handle;
    });
    window.cancelAnimationFrame = vi.fn((handle: number) => {
      terminalMock.frames.delete(handle);
    });
  });

  it("replays retained history when StrictMode recreates xterm", () => {
    const banner = new TextEncoder().encode("Debian GNU/Linux\r\nroot@host:~# ");
    const outputBuffer = makeBuffer(banner);
    render(
      <StrictMode>
        <TerminalTab
          tabId="tab-1"
          outputBuffer={outputBuffer}
          active
          enabled
          fitRequestKey={0}
          focusRequestKey={0}
          onInput={() => Promise.resolve()}
          onResize={() => undefined}
          onFocusChange={() => undefined}
        />
      </StrictMode>,
    );
    expect(terminalMock.instances).toHaveLength(2);
    expect(terminalMock.instances[1].writes).toEqual([banner]);
  });

  it("shows a visible focus boundary only while terminal input is focused", () => {
    const { container } = renderTab();
    const terminalSurface = container.firstElementChild;
    expect(terminalSurface).not.toBeNull();
    expect(terminalSurface).not.toHaveClass("ring-1");

    fireEvent.focusIn(terminalSurface!);
    expect(terminalSurface).toHaveClass("ring-1", "ring-inset", "ring-accent");

    fireEvent.focusOut(terminalSurface!);
    expect(terminalSurface).not.toHaveClass("ring-1");
  });

  it("keeps one xterm instance when a replacement PTY is bound", () => {
    const outputBuffer = makeBuffer(new TextEncoder().encode("old prompt"));
    const view = renderTab({ outputBuffer });
    outputBuffer.advanceGeneration("tab-1", 2);
    outputBuffer.bindPty({
      ptySessionId: "pty-2",
      tabId: "tab-1",
      generation: 2,
      separator: new TextEncoder().encode("\r\n── Reconnected ──\r\n"),
    });
    act(() => {
      outputBuffer.ingest({
        ptySessionId: "pty-2",
        streamSequence: 1,
        data: new TextEncoder().encode("new prompt"),
      });
    });
    view.rerender(
      <TerminalTab
        tabId="tab-1"
        outputBuffer={outputBuffer}
        active
        enabled
        fitRequestKey={1}
        focusRequestKey={0}
        onInput={() => Promise.resolve()}
        onResize={() => undefined}
        onFocusChange={() => undefined}
      />,
    );

    expect(terminalMock.instances).toHaveLength(1);
    expect(
      terminalMock.instances[0].writes.map((data) =>
        new TextDecoder().decode(data),
      ),
    ).toEqual([
      "old prompt",
      "\r\n── Reconnected ──\r\n",
      "new prompt",
    ]);
  });

  it("forwards input unchanged and receives output without a React rerender", async () => {
    const encoder = new TextEncoder();
    const prompt = encoder.encode("root@host:~# ");
    const response = encoder.encode("echo OK\r\nOK\r\nroot@host:~# ");
    const outputBuffer = makeBuffer(prompt);
    const onInput = vi.fn(() => Promise.resolve());
    renderTab({ outputBuffer, onInput });

    act(() => terminalMock.instances[0].emitData("echo OK\r"));
    await waitFor(() =>
      expect(onInput).toHaveBeenCalledWith(encoder.encode("echo OK\r")),
    );
    act(() => {
      outputBuffer.ingest({
        ptySessionId: "pty-1",
        streamSequence: 2,
        data: response,
      });
    });
    expect(terminalMock.instances).toHaveLength(1);
    expect(terminalMock.instances[0].writes).toEqual([prompt, response]);
  });

  it("coalesces ten observer notifications into one local fit and remote resize", () => {
    const onResize = vi.fn();
    renderTab({ onResize });
    flushNextFrame();
    terminalMock.fitInstances[0].fitCalls = 0;
    onResize.mockClear();
    (terminalMock.instances[0] as unknown as { cols: number }).cols = 100;
    for (let index = 0; index < 10; index += 1) {
      act(() => terminalMock.resizeCallbacks[0]([], {} as ResizeObserver));
    }
    expect(terminalMock.frames.size).toBe(1);
    flushNextFrame();
    expect(terminalMock.fitInstances[0].fitCalls).toBe(1);
    expect(onResize).toHaveBeenCalledTimes(1);
    expect(onResize).toHaveBeenCalledWith(100, 24);
  });

  it("fits locally without resizing the remote PTY while disabled", () => {
    const onResize = vi.fn();
    renderTab({ enabled: false, onResize });
    flushNextFrame();
    expect(terminalMock.fitInstances[0].fitCalls).toBe(1);
    expect(onResize).not.toHaveBeenCalled();
  });

  it("routes fitRequestKey through the same coalesced controller", () => {
    const onResize = vi.fn();
    const outputBuffer = makeBuffer();
    const view = renderTab({ outputBuffer, onResize });
    flushNextFrame();
    onResize.mockClear();
    view.rerender(
      <TerminalTab
        tabId="tab-1"
        outputBuffer={outputBuffer}
        active
        enabled
        fitRequestKey={1}
        focusRequestKey={0}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );
    flushNextFrame();
    expect(terminalMock.fitInstances[0].fitCalls).toBe(2);
    expect(onResize).not.toHaveBeenCalled();
  });

  it("does not resize after the tab becomes disabled", () => {
    const onResize = vi.fn();
    const outputBuffer = makeBuffer();
    const view = renderTab({ outputBuffer, onResize });
    flushNextFrame();
    onResize.mockClear();
    view.rerender(
      <TerminalTab
        tabId="tab-1"
        outputBuffer={outputBuffer}
        active
        enabled={false}
        fitRequestKey={1}
        focusRequestKey={0}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );
    flushNextFrame();
    expect(onResize).not.toHaveBeenCalled();
  });

  it("cancels queued input chunks when the tab becomes disabled", async () => {
    let resolveFirst!: () => void;
    const onInput = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<void>((resolve) => { resolveFirst = resolve; }),
      )
      .mockResolvedValue(undefined);
    const outputBuffer = makeBuffer();
    const view = renderTab({ outputBuffer, onInput });
    act(() => terminalMock.instances[0].emitData("x".repeat(32_769)));
    await waitFor(() => expect(onInput).toHaveBeenCalledTimes(1));
    view.rerender(
      <TerminalTab
        tabId="tab-1"
        outputBuffer={outputBuffer}
        active
        enabled={false}
        fitRequestKey={0}
        focusRequestKey={0}
        onInput={onInput}
        onResize={() => undefined}
        onFocusChange={() => undefined}
      />,
    );
    await act(async () => resolveFirst());
    expect(onInput).toHaveBeenCalledTimes(1);
  });

  it("forwards control sequences through xterm onData", async () => {
    const onInput = vi.fn((_data: Uint8Array) => Promise.resolve());
    renderTab({ onInput });
    act(() => {
      terminalMock.instances[0].emitData("\u000b");
      terminalMock.instances[0].emitData("\u0010");
      terminalMock.instances[0].emitData("\t");
    });
    await waitFor(() => expect(onInput).toHaveBeenCalledTimes(3));
    expect(onInput.mock.calls.map(([data]) => [...data])).toEqual([
      [11],
      [16],
      [9],
    ]);
  });
});
