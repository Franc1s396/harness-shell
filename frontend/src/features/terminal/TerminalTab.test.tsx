// @vitest-environment jsdom

import { StrictMode } from "react";
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const terminalMock = vi.hoisted(() => ({
  instances: [] as Array<{
    writes: Uint8Array[];
    emitData: (data: string) => void;
  }>,
  fitInstances: [] as Array<{ fitCalls: number }>,
  resizeCallbacks: [] as ResizeObserverCallback[],
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
    readonly cols = 80;
    readonly rows = 24;
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

import { TerminalTab } from "./TerminalTab";

describe("TerminalTab", () => {
  beforeEach(() => {
    terminalMock.instances.length = 0;
    terminalMock.fitInstances.length = 0;
    terminalMock.resizeCallbacks.length = 0;
    document.documentElement.style.setProperty("--color-app", "#0b1017");
    document.documentElement.style.setProperty("--color-ink", "#d8e2ef");
    document.documentElement.style.setProperty("--color-accent", "#66d9c8");
    document.documentElement.style.setProperty("--color-accent-soft", "#27445a");
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
  });

  it("replays existing output when StrictMode recreates the xterm instance", () => {
    const banner = new TextEncoder().encode("Debian GNU/Linux\r\nroot@host:~# ");

    render(
      <StrictMode>
        <TerminalTab
          active
          enabled
          fitRequestKey={0}
          focusRequestKey={0}
          output={[banner]}
          onInput={() => Promise.resolve()}
          onResize={() => undefined}
          onFocusChange={() => undefined}
        />
      </StrictMode>,
    );

    expect(terminalMock.instances).toHaveLength(2);
    expect(terminalMock.instances[1].writes).toEqual([banner]);
  });

  it("forwards user input and appends the resulting remote output", async () => {
    const encoder = new TextEncoder();
    const prompt = encoder.encode("root@host:~# ");
    const response = encoder.encode(
      "echo HARNESS_UI_OK\r\nHARNESS_UI_OK\r\nroot@host:~# ",
    );
    const onInput = vi.fn(() => Promise.resolve());
    const view = render(
      <StrictMode>
        <TerminalTab
          active
          enabled
          fitRequestKey={0}
          focusRequestKey={0}
          output={[prompt]}
          onInput={onInput}
          onResize={() => undefined}
          onFocusChange={() => undefined}
        />
      </StrictMode>,
    );

    act(() => {
      terminalMock.instances[1].emitData("echo HARNESS_UI_OK\r");
    });

    await waitFor(() => {
      expect(onInput).toHaveBeenCalledWith(
        encoder.encode("echo HARNESS_UI_OK\r"),
      );
    });

    view.rerender(
      <StrictMode>
        <TerminalTab
          active
          enabled
          fitRequestKey={0}
          focusRequestKey={0}
          output={[prompt, response]}
          onInput={onInput}
          onResize={() => undefined}
          onFocusChange={() => undefined}
        />
      </StrictMode>,
    );

    expect(terminalMock.instances[1].writes).toEqual([prompt, response]);
  });

  it("does not resize the remote PTY while the tab is disabled", () => {
    const onResize = vi.fn();
    render(
      <TerminalTab
        active
        enabled={false}
        fitRequestKey={1}
        focusRequestKey={0}
        output={[]}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );

    expect(onResize).not.toHaveBeenCalled();
  });

  it("fits locally when a disabled tab's container is resized", () => {
    const onResize = vi.fn();
    render(
      <TerminalTab
        active
        enabled={false}
        fitRequestKey={0}
        focusRequestKey={0}
        output={[]}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );

    expect(terminalMock.fitInstances).toHaveLength(1);
    const baselineFitCalls = terminalMock.fitInstances[0].fitCalls;

    act(() => {
      terminalMock.resizeCallbacks[0]([], {} as ResizeObserver);
    });

    expect(terminalMock.fitInstances[0].fitCalls).toBe(baselineFitCalls + 1);
    expect(onResize).not.toHaveBeenCalled();
  });

  it("fits locally when a disabled tab receives a fit request", () => {
    const onResize = vi.fn();
    const view = render(
      <TerminalTab
        active
        enabled={false}
        fitRequestKey={0}
        focusRequestKey={0}
        output={[]}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );

    const baselineFitCalls = terminalMock.fitInstances[0].fitCalls;

    view.rerender(
      <TerminalTab
        active
        enabled={false}
        fitRequestKey={1}
        focusRequestKey={0}
        output={[]}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );

    expect(terminalMock.fitInstances[0].fitCalls).toBe(baselineFitCalls + 1);
    expect(onResize).not.toHaveBeenCalled();
  });

  it("does not resize remote PTY after the tab becomes disabled", () => {
    const onResize = vi.fn();
    const view = render(
      <TerminalTab
        active
        enabled
        fitRequestKey={0}
        focusRequestKey={0}
        output={[]}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );
    expect(onResize).toHaveBeenCalled();
    onResize.mockClear();

    view.rerender(
      <TerminalTab
        active
        enabled={false}
        fitRequestKey={0}
        focusRequestKey={0}
        output={[]}
        onInput={() => Promise.resolve()}
        onResize={onResize}
        onFocusChange={() => undefined}
      />,
    );
    const baselineFitCalls = terminalMock.fitInstances[0].fitCalls;
    act(() => {
      terminalMock.resizeCallbacks[0]([], {} as ResizeObserver);
    });

    expect(terminalMock.fitInstances[0].fitCalls).toBe(baselineFitCalls + 1);
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
    const view = render(
      <TerminalTab
        active
        enabled
        fitRequestKey={0}
        focusRequestKey={0}
        output={[]}
        onInput={onInput}
        onResize={() => undefined}
        onFocusChange={() => undefined}
      />,
    );
    act(() => {
      terminalMock.instances[0].emitData("x".repeat(32_769));
    });
    await waitFor(() => expect(onInput).toHaveBeenCalledTimes(1));

    view.rerender(
      <TerminalTab
        active
        enabled={false}
        fitRequestKey={0}
        focusRequestKey={0}
        output={[]}
        onInput={onInput}
        onResize={() => undefined}
        onFocusChange={() => undefined}
      />,
    );
    await act(async () => resolveFirst());

    expect(onInput).toHaveBeenCalledTimes(1);
  });
});
