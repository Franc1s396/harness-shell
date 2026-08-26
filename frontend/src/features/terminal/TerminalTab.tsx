import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useLayoutEffect, useRef } from "react";

import { createXtermTheme } from "./xterm-theme";

type Props = {
  active: boolean;
  enabled: boolean;
  fitRequestKey: number;
  focusRequestKey: number;
  output: readonly Uint8Array[];
  onInput: (data: Uint8Array) => Promise<void>;
  onResize: (cols: number, rows: number) => void;
  onFocusChange: (focused: boolean) => void;
};

export function TerminalTab({
  active,
  enabled,
  fitRequestKey,
  focusRequestKey,
  output,
  onInput,
  onResize,
  onFocusChange,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const writtenChunks = useRef(0);
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
    fitRef.current = fit;

    const resize = () => {
      if (
        !container.isConnected ||
        container.clientWidth === 0 ||
        container.clientHeight === 0
      ) return;
      fit.fit();
      if (!resizeEnabled.current) return;
      if (terminal.cols >= 20 && terminal.cols <= 500 && terminal.rows >= 5 && terminal.rows <= 300) {
        resizeHandler.current(terminal.cols, terminal.rows);
      }
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    resize();
    const focusIn = () => focusHandler.current(true);
    const focusOut = () => focusHandler.current(false);
    container.addEventListener("focusin", focusIn);
    container.addEventListener("focusout", focusOut);
    return () => {
      observer.disconnect();
      container.removeEventListener("focusin", focusIn);
      container.removeEventListener("focusout", focusOut);
      terminal.dispose();
      terminalRef.current = null;
      fitRef.current = null;
      writtenChunks.current = 0;
      inputGeneration.current += 1;
      inputInteractive.current = false;
      focusHandler.current(false);
    };
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    const terminal = terminalRef.current;
    const fit = fitRef.current;
    if (
      !active ||
      !container ||
      !terminal ||
      !fit ||
      !container.isConnected ||
      container.clientWidth === 0 ||
      container.clientHeight === 0
    ) return;
    fit.fit();
    if (!enabled) return;
    if (
      terminal.cols >= 20 && terminal.cols <= 500 &&
      terminal.rows >= 5 && terminal.rows <= 300
    ) {
      resizeHandler.current(terminal.cols, terminal.rows);
    }
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

  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    for (let index = writtenChunks.current; index < output.length; index += 1) {
      terminal.write(output[index]);
    }
    writtenChunks.current = output.length;
  }, [output]);

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
