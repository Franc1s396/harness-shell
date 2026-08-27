import {
  useEffect,
  useRef,
  type KeyboardEvent,
  type PointerEvent,
} from "react";

export type ResizableSeparatorProps = {
  label: string;
  value: number;
  min: number;
  max: number;
  defaultValue: number;
  direction: "increase-right" | "increase-left";
  onChange: (value: number) => void;
  onCommit: () => void;
};

type DragState = {
  pointerId: number;
  startX: number;
  startValue: number;
  previousUserSelect: string;
};

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, Math.round(value)));

export function ResizableSeparator({
  label,
  value,
  min,
  max,
  defaultValue,
  direction,
  onChange,
  onCommit,
}: ResizableSeparatorProps) {
  const separatorRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const commitRef = useRef(onCommit);
  commitRef.current = onCommit;

  const finishDrag = (pointerId: number) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== pointerId) return;
    const separator = separatorRef.current;
    if (separator?.hasPointerCapture?.(pointerId)) {
      separator.releasePointerCapture(pointerId);
    }
    document.body.style.userSelect = drag.previousUserSelect;
    dragRef.current = null;
    commitRef.current();
  };

  useEffect(
    () => () => {
      const drag = dragRef.current;
      if (!drag) return;
      document.body.style.userSelect = drag.previousUserSelect;
      dragRef.current = null;
      commitRef.current();
    },
    [],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 32 : 8;
    let next: number | null = null;
    if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    else if (event.key === "ArrowLeft") {
      next = value + (direction === "increase-left" ? step : -step);
    } else if (event.key === "ArrowRight") {
      next = value + (direction === "increase-right" ? step : -step);
    }
    if (next === null) return;
    event.preventDefault();
    onChange(clamp(next, min, max));
    onCommit();
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startValue: value,
      previousUserSelect: document.body.style.userSelect,
    };
    document.body.style.userSelect = "none";
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const pointerDelta = event.clientX - drag.startX;
    const signedDelta =
      direction === "increase-right" ? pointerDelta : -pointerDelta;
    onChange(clamp(drag.startValue + signedDelta, min, max));
  };

  return (
    <div
      ref={separatorRef}
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      tabIndex={0}
      className="relative z-20 w-1 shrink-0 cursor-col-resize bg-line hover:bg-accent focus-visible:bg-accent"
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={(event) => finishDrag(event.pointerId)}
      onPointerCancel={(event) => finishDrag(event.pointerId)}
      onDoubleClick={(event) => {
        event.preventDefault();
        onChange(clamp(defaultValue, min, max));
        onCommit();
      }}
    />
  );
}
