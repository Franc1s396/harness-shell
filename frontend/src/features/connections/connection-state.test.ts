import { describe, expect, it } from "vitest";

import { parseSshEvent, type SshCommandError } from "../../api/ssh";
import {
  connectionErrorView,
  findTabByPtySessionId,
  hostKeyTrustLabel,
  isInteractiveSshReady,
  resolveResponsivePanels,
} from "./connection-state";

describe("SSH UI state contracts", () => {
  it("rejects unknown runtime events", () => {
    expect(() => parseSshEvent({ event: "ssh.unknown" })).toThrow(
      "Unknown SSH event",
    );
  });

  it("surfaces stale Host Key replacement conflicts", () => {
    const error: SshCommandError = {
      code: "HOST_KEY_REPLACE_CONFLICT",
      message: "rejected",
      details: {
        node: "host_key",
        recoverable: false,
        correlation_id: "corr-1",
        remote_state: "pre_auth",
      },
    };
    expect(connectionErrorView(error)).toEqual({
      summaryKey: "errors.hostKeyConflict",
      errorCode: "HOST_KEY_REPLACE_CONFLICT",
      node: "host_key",
      recoverable: false,
      correlationId: "corr-1",
      remoteState: "pre_auth",
    });
  });

  it("maps terminal events only to the matching PTY tab", () => {
    const tabs = [
      { tabId: "a", ptySessionId: "pty-a" },
      { tabId: "b", ptySessionId: "pty-b" },
    ];
    expect(findTabByPtySessionId(tabs, "pty-b")?.tabId).toBe("b");
    expect(findTabByPtySessionId(tabs, "pty-missing")).toBeUndefined();
  });

  it("keeps interactive SSH disabled until both runtime and event listener are ready", () => {
    expect(isInteractiveSshReady(true, "SUBSCRIBING")).toBe(false);
    expect(isInteractiveSshReady(true, "FAILED")).toBe(false);
    expect(isInteractiveSshReady(false, "READY")).toBe(false);
    expect(isInteractiveSshReady(true, "READY")).toBe(true);
  });

  it("keeps a focused center terminal visible below minimum width", () => {
    expect(
      resolveResponsivePanels({
        viewportWidth: 900,
        terminalOwnsFocus: true,
        requestedCenterVisible: false,
      }).centerVisible,
    ).toBe(true);
    expect(
      resolveResponsivePanels({
        viewportWidth: 900,
        terminalOwnsFocus: false,
        requestedCenterVisible: false,
      }).centerVisible,
    ).toBe(false);
  });

  it("derives an explicit Host Key trust label from live status", () => {
    expect(hostKeyTrustLabel(undefined)).toBe("unknown");
    expect(hostKeyTrustLabel({ state: "HOST_KEY_REQUIRED", host_key_candidate: {} })).toBe("untrusted");
    expect(hostKeyTrustLabel({ state: "FAILED", host_key_candidate: {}, trusted_fingerprint_sha256: "SHA256:old" })).toBe("changed");
    expect(hostKeyTrustLabel({ state: "READY", host_key_candidate: null })).toBe("trusted");
  });
});
