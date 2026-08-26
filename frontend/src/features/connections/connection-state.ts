import type { ConnectionStatus, SshCommandError } from "../../api/ssh";
import type { SshEventListenerState } from "../ssh/useSshEvents";

export type TerminalTabIdentity = {
  tabId: string;
  ptySessionId: string;
};

export const findTabByPtySessionId = <T extends TerminalTabIdentity>(
  tabs: readonly T[],
  ptySessionId: string,
): T | undefined => tabs.find((tab) => tab.ptySessionId === ptySessionId);

export const isInteractiveSshReady = (
  runtimeReady: boolean,
  eventListenerState: SshEventListenerState,
): boolean => runtimeReady && eventListenerState === "READY";

export const resolveResponsivePanels = ({
  viewportWidth,
  terminalOwnsFocus,
  requestedCenterVisible,
}: {
  viewportWidth: number;
  terminalOwnsFocus: boolean;
  requestedCenterVisible: boolean;
}) => ({
  centerVisible:
    viewportWidth < 1280 && terminalOwnsFocus
      ? true
      : requestedCenterVisible,
});

export const hostKeyTrustLabel = (
  status:
    | (Pick<ConnectionStatus, "state"> & {
        host_key_candidate?: unknown;
        trusted_fingerprint_sha256?: string | null;
      })
    | undefined,
): "unknown" | "untrusted" | "trusted" | "changed" => {
  if (!status) return "unknown";
  if (status.host_key_candidate != null) {
    return status.trusted_fingerprint_sha256 ? "changed" : "untrusted";
  }
  return ["DISCONNECTED", "CONNECTING", "READY", "CLOSING"].includes(status.state)
    ? "trusted"
    : "unknown";
};

export const connectionErrorView = (error: SshCommandError) => ({
  summaryKey:
    error.code === "HOST_KEY_REPLACE_CONFLICT"
      ? ("errors.hostKeyConflict" as const)
      : ("errors.sshFailed" as const),
  errorCode: error.code,
  node: error.details?.node ?? "unknown",
  recoverable: error.details?.recoverable ?? false,
  correlationId: error.details?.correlation_id ?? "unknown",
  remoteState: error.details?.remote_state ?? "unknown",
});
