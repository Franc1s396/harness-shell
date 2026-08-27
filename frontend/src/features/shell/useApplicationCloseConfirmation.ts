import { getCurrentWindow } from "@tauri-apps/api/window";
import { useEffect, useRef, useState } from "react";

export type CloseRequestedLike = {
  preventDefault: () => void;
};

export type ApplicationWindowAdapter = {
  onCloseRequested: (
    handler: (event: CloseRequestedLike) => void | Promise<void>,
  ) => Promise<() => void>;
  close: () => Promise<void>;
};

const tauriWindowAdapter: ApplicationWindowAdapter = {
  onCloseRequested: (handler) => getCurrentWindow().onCloseRequested(handler),
  close: () => getCurrentWindow().close(),
};

export function useApplicationCloseConfirmation(
  adapter: ApplicationWindowAdapter = tauriWindowAdapter,
) {
  const [closeConfirmationOpen, setCloseConfirmationOpen] = useState(false);
  const allowClose = useRef(false);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    void adapter.onCloseRequested((event) => {
      if (allowClose.current) return;
      event.preventDefault();
      setCloseConfirmationOpen(true);
    }).then((stop) => {
      if (disposed) {
        stop();
      } else {
        unlisten = stop;
      }
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [adapter]);

  return {
    closeConfirmationOpen,
    cancelApplicationClose: () => setCloseConfirmationOpen(false),
    confirmApplicationClose: async () => {
      if (allowClose.current) return;
      allowClose.current = true;
      setCloseConfirmationOpen(false);
      await adapter.close();
    },
  };
}
