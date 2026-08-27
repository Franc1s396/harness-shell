import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useLayoutEffect, useRef } from "react";

import { PtyOutputBuffer } from "./terminal-output-buffer";
import { TerminalResizeController } from "./terminal-resize-controller";
import { createXtermTheme } from "./xterm-theme";

type Props = {
  ptySessionId: string;
  outputBuffer: PtyOutputBuffer;
  active: boolean;
  enabled: boolean;
  fitRequestKey: number;
  focusRequestKey: number;
  onInput: (data: Uint8Array) => Promise<void>;
  onResize: (cols: number, rows: number) => void;
  onFocusChange: (focused: boolean) => void;
};

export function TerminalTab({
  ptySessionId,
  outputBuffer,
  active,
  enabled,
  fitRequestKey,
  focusRequestKey,
  onInput,
  onResize,
  onFocusChange,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const resizeControllerRef = useRef<TerminalResizeController | null>(null);
  const inputHandler = useRef(onInput);
  const resizeHandler = useRef(onResize);
  const resizeEnabled = useRef(enabled);
  const inputGeneration = useRef(0);
  const inputInteractive = useRef(active && enabled);
  const focusHandler = useRef(onFocusChange);
  const inputQueue = useRef(Promise.resolve());
  inputHandler.current = onInput;
  resizeHandler.current = onResize;
  resizeEnabled.current = enabled;
  focusHandler.current = onFocusChange;

  useLayoutEffect(() => {
    const interactive = active && enabled;
    if (inputInteractive.current && !interactive) {
      inputGeneration.current += 1;
    }
    inputInteractive.current = interactive;
  }, [active, enabled]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const terminal = new Terminal({
      convertEol: false,
      allowProposedApi: false,
      scrollback: 5000,
      screenReaderMode: true,
      cursorBlink: true,
      fontFamily: '"Cascadia Mono", Consolas, monospace',
      fontSize: 14,
      theme: createXtermTheme(),
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(container);
    terminalRef.current = terminal;

    const resizeController = new TerminalResizeController({
      fit: () => fit.fit(),
      readSize: () => ({ cols: terminal.cols, rows: terminal.rows }),
      isRemoteResizeEnabled: () => resizeEnabled.current,
      onRemoteResize: ({ cols, rows }) => resizeHandler.current(cols, rows),
      requestFrame: (callback) => window.requestAnimationFrame(callback),
      cancelFrame: (handle) => window.cancelAnimationFrame(handle),
    });
    resizeControllerRef.current = resizeController;
    const unsubscribeOutput = outputBuffer.subscribe(
      ptySessionId,
      (data) => terminal.write(data),
    );
    const requestResize = () => {
      if (
        !container.isConnected ||
        container.clientWidth === 0 ||
        container.clientHeight === 0
      ) return;
      resizeController.request();
    };
    const observer = new ResizeObserver(requestResize);
    observer.observe(container);
    requestResize();
    const focusIn = () => focusHandler.current(true);
    const focusOut = () => focusHandler.current(false);
    container.addEventListener("focusin", focusIn);
    container.addEventListener("focusout", focusOut);
    return () => {
      observer.disconnect();
      container.removeEventListener("focusin", focusIn);
      container.removeEventListener("focusout", focusOut);
      unsubscribeOutput();
      resizeController.dispose();
      terminal.dispose();
      terminalRef.current = null;
      resizeControllerRef.current = null;
      inputGeneration.current += 1;
      inputInteractive.current = false;
      focusHandler.current(false);
    };
  }, [outputBuffer, ptySessionId]);

  useEffect(() => {
    const container = containerRef.current;
    if (
      !active ||
      !container ||
      !container.isConnected ||
      container.clientWidth === 0 ||
      container.clientHeight === 0
    ) return;
    resizeControllerRef.current?.request();
  }, [active, enabled, fitRequestKey]);

  useEffect(() => {
    if (active && enabled) terminalRef.current?.focus();
  }, [active, enabled, focusRequestKey]);

  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal || !active || !enabled) {
      terminal?.blur();
      return;
    }
    const subscription = terminal.onData((data) => {
      const generation = inputGeneration.current;
      const bytes = new TextEncoder().encode(data);
      for (let offset = 0; offset < bytes.length; offset += 32_768) {
        const chunk = bytes.slice(offset, offset + 32_768);
        inputQueue.current = inputQueue.current.then(() => {
          if (
            !inputInteractive.current ||
            inputGeneration.current !== generation
          ) return;
          return inputHandler.current(chunk);
        });
      }
    });
    terminal.focus();
    return () => {
      subscription.dispose();
      terminal.blur();
    };
  }, [active, enabled]);

  return (
    <div
      ref={containerRef}
      className={`absolute inset-0 min-h-0 min-w-0 ${active ? "block" : "hidden"}`}
      aria-hidden={!active}
      onMouseDown={() => {
        if (active && enabled) terminalRef.current?.focus();
      }}
    />
  );
}
