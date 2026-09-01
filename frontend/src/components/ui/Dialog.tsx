import { useEffect, useId, useRef, type ReactNode } from "react";

import { isTopDialog, registerDialog } from "./dialog-stack";

const FOCUSABLE =
  "button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[href],[tabindex]:not([tabindex='-1'])";

export type DialogProps = {
  open: boolean;
  title: string;
  busy?: boolean;
  placement?: "center" | "left";
  onClose: () => void;
  children: ReactNode;
};

export function Dialog({
  open,
  title,
  busy = false,
  placement = "center",
  onClose,
  children,
}: DialogProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const busyRef = useRef(busy);
  const closeRef = useRef(onClose);
  busyRef.current = busy;
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;

    const unregister = registerDialog(titleId);

    triggerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const panel = panelRef.current;
    const focusable = () => [
      ...(panel?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []),
    ];
    focusable()[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (!isTopDialog(titleId)) return;
      if (event.key === "Escape" && !busyRef.current) {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const items = focusable();
      if (items.length === 0) {
        event.preventDefault();
        panel?.focus();
        return;
      }

      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      unregister();
      triggerRef.current?.focus();
    };
  }, [open, titleId]);

  if (!open) return null;

  return (
    <div
      className={`fixed inset-0 z-50 grid bg-black/70 ${
        placement === "left" ? "place-items-stretch justify-items-start" : "place-items-center p-6"
      }`}
      onMouseDown={(event) => {
        if (
          event.target === event.currentTarget &&
          !busy &&
          isTopDialog(titleId)
        ) {
          onClose();
        }
      }}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={
          placement === "left"
            ? "h-full w-[min(380px,calc(100vw-44px))] overflow-auto border-r border-line-strong bg-panel p-4 shadow-2xl"
            : "max-h-full w-full max-w-2xl overflow-auto rounded-xl border border-line-strong bg-panel p-5 shadow-2xl"
        }
      >
        <h2 id={titleId} className="m-0 text-lg font-semibold text-ink">
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}
