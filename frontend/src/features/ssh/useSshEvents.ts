import { useEffect, useRef, useState } from "react";

import {
  subscribeSshEvents,
  type SshEvent,
} from "../../api/ssh";

export type SshEventListenerError = {
  code: "SSH_EVENT_PROTOCOL_ERROR" | "SSH_EVENT_SUBSCRIPTION_FAILED";
  message: string;
};

export type SshEventListenerState = "SUBSCRIBING" | "READY" | "FAILED";

export const useSshEvents = (
  onEvent: (event: SshEvent) => void,
  onError: (error: SshEventListenerError) => void,
) => {
  const [state, setState] = useState<SshEventListenerState>("SUBSCRIBING");
  const eventHandler = useRef(onEvent);
  const errorHandler = useRef(onError);
  eventHandler.current = onEvent;
  errorHandler.current = onError;

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void subscribeSshEvents(
      (event) => eventHandler.current(event),
      (error) =>
        errorHandler.current({
          code: "SSH_EVENT_PROTOCOL_ERROR",
          message: error.message,
        }),
    ).then(
      (nextUnlisten) => {
        if (disposed) nextUnlisten();
        else {
          unlisten = nextUnlisten;
          setState("READY");
        }
      },
      (error: unknown) => {
        if (disposed) return;
        setState("FAILED");
        const detail =
          error instanceof Error
            ? error.message
            : typeof error === "string"
              ? error
              : "Unknown Tauri event listener error.";
        errorHandler.current({
          code: "SSH_EVENT_SUBSCRIPTION_FAILED",
          message: `SSH event subscription failed: ${detail}`,
        });
      },
    );
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  return state;
};
