import { useEffect, useRef, useState } from "react";

import {
  subscribeManualSftpEvents,
  type ManualSftpProtocolError,
  type MutationProgressProjection,
  type TransferProgressProjection,
} from "../../api/manual-sftp";

export type ManualSftpEventListenerState =
  | "SUBSCRIBING"
  | "READY"
  | "FAILED";

export type ManualSftpEventListenerError = {
  code: "SFTP_EVENT_PROTOCOL_ERROR" | "SFTP_EVENT_SUBSCRIPTION_FAILED";
  message: string;
};

export const useManualSftpEvents = (
  onTransfer: (progress: TransferProgressProjection) => void,
  onOperation: (progress: MutationProgressProjection) => void,
  onError: (error: ManualSftpEventListenerError) => void,
  enabled = true,
) => {
  const [state, setState] =
    useState<ManualSftpEventListenerState>("SUBSCRIBING");
  const transferHandler = useRef(onTransfer);
  const operationHandler = useRef(onOperation);
  const errorHandler = useRef(onError);
  transferHandler.current = onTransfer;
  operationHandler.current = onOperation;
  errorHandler.current = onError;

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    const failSubscription = (error: unknown) => {
      if (disposed) return;
      setState("FAILED");
      const detail =
        error instanceof Error
          ? error.message
          : typeof error === "string"
            ? error
            : "Unknown Tauri event listener error.";
      errorHandler.current({
        code: "SFTP_EVENT_SUBSCRIPTION_FAILED",
        message: `Manual SFTP event subscription failed: ${detail}`,
      });
    };
    let subscription: Promise<() => void>;
    try {
      subscription = Promise.resolve(
        subscribeManualSftpEvents(
          (progress) => transferHandler.current(progress),
          (progress) => operationHandler.current(progress),
          (error: ManualSftpProtocolError) =>
            errorHandler.current({
              code: "SFTP_EVENT_PROTOCOL_ERROR",
              message: error.message,
            }),
        ),
      );
    } catch (error) {
      failSubscription(error);
      return () => {
        disposed = true;
      };
    }
    void subscription.then(
      (nextUnlisten) => {
        if (disposed) nextUnlisten();
        else {
          unlisten = nextUnlisten;
          setState("READY");
        }
      },
      failSubscription,
    );
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [enabled]);

  return state;
};
