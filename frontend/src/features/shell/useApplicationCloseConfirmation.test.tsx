// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  useApplicationCloseConfirmation,
  type ApplicationWindowAdapter,
  type CloseRequestedLike,
} from "./useApplicationCloseConfirmation";

const createWindowAdapter = () => {
  let handler:
    | ((event: CloseRequestedLike) => void | Promise<void>)
    | undefined;
  const adapter: ApplicationWindowAdapter & {
    emitCloseRequested: (event: CloseRequestedLike) => Promise<void>;
  } = {
    onCloseRequested: vi.fn(async (nextHandler) => {
      handler = nextHandler;
      return () => {
        handler = undefined;
      };
    }),
    close: vi.fn(async () => undefined),
    emitCloseRequested: async (event) => {
      if (handler === undefined) throw new Error("close handler is not ready");
      await handler(event);
    },
  };
  return adapter;
};

describe("useApplicationCloseConfirmation", () => {
  it("always blocks repeated close requests and opens one confirmation", async () => {
    const adapter = createWindowAdapter();
    const view = renderHook(() => useApplicationCloseConfirmation(adapter));
    const event = { preventDefault: vi.fn() };

    await act(() => adapter.emitCloseRequested(event));
    await act(() => adapter.emitCloseRequested(event));

    expect(event.preventDefault).toHaveBeenCalledTimes(2);
    expect(view.result.current.closeConfirmationOpen).toBe(true);
    expect(adapter.close).not.toHaveBeenCalled();
  });

  it("cancels the requested close without closing the window", async () => {
    const adapter = createWindowAdapter();
    const view = renderHook(() => useApplicationCloseConfirmation(adapter));

    await act(() =>
      adapter.emitCloseRequested({ preventDefault: vi.fn() }),
    );
    act(() => view.result.current.cancelApplicationClose());

    expect(view.result.current.closeConfirmationOpen).toBe(false);
    expect(adapter.close).not.toHaveBeenCalled();
  });

  it("confirms once and allows the resulting close request through", async () => {
    const adapter = createWindowAdapter();
    const view = renderHook(() => useApplicationCloseConfirmation(adapter));
    const firstEvent = { preventDefault: vi.fn() };
    const resultingEvent = { preventDefault: vi.fn() };

    await act(() => adapter.emitCloseRequested(firstEvent));
    await act(() => view.result.current.confirmApplicationClose());
    await act(() => adapter.emitCloseRequested(resultingEvent));

    expect(firstEvent.preventDefault).toHaveBeenCalledTimes(1);
    expect(adapter.close).toHaveBeenCalledTimes(1);
    expect(resultingEvent.preventDefault).not.toHaveBeenCalled();
    expect(view.result.current.closeConfirmationOpen).toBe(false);
  });
});
