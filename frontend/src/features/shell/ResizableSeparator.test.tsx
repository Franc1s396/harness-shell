// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ResizableSeparator } from "./ResizableSeparator";

const renderSeparator = (
  overrides: Partial<React.ComponentProps<typeof ResizableSeparator>> = {},
) => {
  const onChange = vi.fn();
  const onCommit = vi.fn();
  render(
    <ResizableSeparator
      label="Resize connection navigator"
      value={280}
      min={240}
      max={380}
      defaultValue={280}
      direction="increase-right"
      onChange={onChange}
      onCommit={onCommit}
      {...overrides}
    />,
  );
  return {
    separator: screen.getByRole("separator"),
    onChange,
    onCommit,
  };
};

describe("ResizableSeparator", () => {
  afterEach(cleanup);

  beforeEach(() => {
    class TestPointerEvent extends MouseEvent {
      readonly pointerId: number;

      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerId = init.pointerId ?? 0;
      }
    }
    Object.defineProperty(window, "PointerEvent", {
      configurable: true,
      value: TestPointerEvent,
    });
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("exposes the vertical separator value contract", () => {
    const { separator } = renderSeparator();
    expect(separator).toHaveAttribute("aria-orientation", "vertical");
    expect(separator).toHaveAttribute("aria-valuemin", "240");
    expect(separator).toHaveAttribute("aria-valuemax", "380");
    expect(separator).toHaveAttribute("aria-valuenow", "280");
    expect(separator).toHaveAttribute("tabindex", "0");
  });

  it("uses 8px arrows, 32px shifted arrows, and commits keyboard changes", () => {
    const { separator, onChange, onCommit } = renderSeparator();
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    fireEvent.keyDown(separator, { key: "ArrowLeft", shiftKey: true });
    expect(onChange).toHaveBeenNthCalledWith(1, 288);
    expect(onChange).toHaveBeenNthCalledWith(2, 248);
    expect(onCommit).toHaveBeenCalledTimes(2);
  });

  it("supports Home, End, and double-click reset", () => {
    const { separator, onChange, onCommit } = renderSeparator({ value: 320 });
    fireEvent.keyDown(separator, { key: "Home" });
    fireEvent.keyDown(separator, { key: "End" });
    fireEvent.doubleClick(separator);
    expect(onChange.mock.calls).toEqual([[240], [380], [280]]);
    expect(onCommit).toHaveBeenCalledTimes(3);
  });

  it("reverses horizontal meaning for a panel that grows left", () => {
    const { separator, onChange } = renderSeparator({
      direction: "increase-left",
      value: 480,
      min: 320,
      max: 640,
      defaultValue: 480,
    });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(onChange.mock.calls).toEqual([[488], [472]]);
  });

  it("clamps pointer drag, disables selection, and commits on pointer up", () => {
    const { separator, onChange, onCommit } = renderSeparator();
    fireEvent.pointerDown(separator, { clientX: 100, pointerId: 7 });
    expect(document.body.style.userSelect).toBe("none");
    fireEvent.pointerMove(separator, { clientX: 250, pointerId: 7 });
    expect(onChange).toHaveBeenLastCalledWith(380);
    fireEvent.pointerUp(separator, { clientX: 250, pointerId: 7 });
    expect(document.body.style.userSelect).toBe("");
    expect(onCommit).toHaveBeenCalledTimes(1);
  });
});
