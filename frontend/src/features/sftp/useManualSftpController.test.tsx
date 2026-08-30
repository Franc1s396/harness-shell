// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getManualSftpContext: vi.fn(),
  listManualSftpDirectory: vi.fn(),
  nextManualSftpDirectoryBatch: vi.fn(),
  closeManualSftpListing: vi.fn(),
  prepareManualSftpUpload: vi.fn(),
  discardManualSftpPreparation: vi.fn(),
  listManualSftpRecoveries: vi.fn(),
  inspectManualSftpRecovery: vi.fn(),
  executeManualSftpRecovery: vi.fn(),
  subscribeManualSftpEvents: vi.fn(),
}));

vi.mock("../../api/manual-sftp", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/manual-sftp")>()),
  ...api,
}));

import { useManualSftpController } from "./useManualSftpController";

const sessions = [
  {
    tabId: "tab-first",
    connectionId: "connection-first",
    title: "First",
    state: "CONNECTED" as const,
    sshSessionId: "ssh-session-first",
    ptySessionId: "pty-first",
    generation: 1,
  },
  {
    tabId: "tab-second",
    connectionId: "connection-second",
    title: "Second",
    state: "CONNECTED" as const,
    sshSessionId: "ssh-session-second",
    ptySessionId: "pty-second",
    generation: 1,
  },
];

const context = {
  ssh_session_id: "ssh-session-first",
  connection_id: "connection-first",
  home: "/home/first",
  host_label: "first.example",
  sftp_version: 3,
};

const batch = {
  listing_id: "listing-1",
  path: "/home/first",
  entries: [],
  next_sequence: 1,
  done: true,
  observed_entry_count: 0,
  complete: true,
};

const recovery = {
  recovery_id: "recovery-1",
  operation_id: "operation-1",
  kind: "download_part" as const,
  host_label: "first.example",
  remote_path: "/home/first/payload.bin",
  display_name: "payload.bin",
  state: "cleanup_required" as const,
  created_at: "2026-08-30T00:00:00Z",
  available_actions: ["verify", "open_local_folder", "keep"] as const,
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

describe("useManualSftpController", () => {
  beforeEach(() => {
    vi.useRealTimers();
    Object.values(api).forEach((mock) => mock.mockReset());
    api.subscribeManualSftpEvents.mockResolvedValue(() => undefined);
    api.getManualSftpContext.mockResolvedValue(context);
    api.listManualSftpDirectory.mockResolvedValue(batch);
    api.closeManualSftpListing.mockResolvedValue(true);
    api.prepareManualSftpUpload.mockResolvedValue(null);
    api.discardManualSftpPreparation.mockResolvedValue(undefined);
    api.listManualSftpRecoveries.mockResolvedValue([]);
    api.inspectManualSftpRecovery.mockResolvedValue(recovery);
    api.executeManualSftpRecovery.mockResolvedValue(recovery);
  });

  it("binds only the explicitly active connected terminal tab", async () => {
    renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-first",
      }),
    );
    await waitFor(() =>
      expect(api.getManualSftpContext).toHaveBeenCalledWith(
        "ssh-session-first",
      ),
    );
    expect(api.getManualSftpContext).not.toHaveBeenCalledWith(
      "ssh-session-second",
    );
  });

  it("keeps context and directory loading visible until the complete listing arrives", async () => {
    const contextRequest = deferred<typeof context>();
    const listingRequest = deferred<typeof batch>();
    api.getManualSftpContext.mockReturnValue(contextRequest.promise);
    api.listManualSftpDirectory.mockReturnValue(listingRequest.promise);

    const { result } = renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-first",
      }),
    );

    expect(result.current.state.contextLoading).toBe(true);
    await act(async () => contextRequest.resolve(context));
    await waitFor(() => expect(result.current.state.listingLoading).toBe(true));
    expect(result.current.state.contextLoading).toBe(false);

    await act(async () => listingRequest.resolve(batch));
    await waitFor(() => expect(result.current.state.listingLoading).toBe(false));
  });

  it("ends context loading when opening the SFTP context fails", async () => {
    api.getManualSftpContext.mockRejectedValue({
      code: "NO_SESSION",
      message: "No session is connected.",
    });

    const { result } = renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-first",
      }),
    );

    await waitFor(() => expect(result.current.state.error?.code).toBe("NO_SESSION"));
    expect(result.current.state.contextLoading).toBe(false);
  });

  it("marks an explicit recovery refresh as loading until it finishes", async () => {
    const { result } = renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-first",
      }),
    );
    await waitFor(() => expect(result.current.state.recoveriesLoading).toBe(false));

    const recoveryRequest = deferred<(typeof recovery)[]>();
    api.listManualSftpRecoveries.mockReturnValueOnce(recoveryRequest.promise);
    let refresh!: Promise<unknown>;
    act(() => {
      refresh = result.current.loadRecoveries();
    });
    expect(result.current.state.recoveriesLoading).toBe(true);

    await act(async () => recoveryRequest.resolve([recovery]));
    await refresh;
    expect(result.current.state.recoveriesLoading).toBe(false);
  });

  it("does not fall back to the latest session when activeTabId is absent", async () => {
    renderHook(() =>
      useManualSftpController({ enabled: true, sessions, activeTabId: null }),
    );
    await waitFor(() =>
      expect(api.getManualSftpContext).toHaveBeenCalledWith(null),
    );
    expect(api.getManualSftpContext).not.toHaveBeenCalledWith(
      "ssh-session-second",
    );
  });

  it("does not close a completed listing before refresh, parent, or child navigation", async () => {
    const { result } = renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-first",
      }),
    );
    await waitFor(() => expect(result.current.state.listing).not.toBeNull());
    api.listManualSftpDirectory
      .mockResolvedValueOnce({
        ...batch,
        listing_id: "listing-2",
        path: "/home/first",
      })
      .mockResolvedValueOnce({
        ...batch,
        listing_id: "listing-3",
        path: "/home",
      })
      .mockResolvedValueOnce({
        ...batch,
        listing_id: "listing-4",
        path: "/home/first/data",
      });
    await act(() => result.current.refresh());
    await act(() => result.current.navigate("/home"));
    await act(() => result.current.navigate("/home/first/data"));
    expect(api.closeManualSftpListing).not.toHaveBeenCalled();
    expect(api.listManualSftpDirectory.mock.calls.slice(-3)).toEqual([
      ["ssh-session-first", "/home/first"],
      ["ssh-session-first", "/home"],
      ["ssh-session-first", "/home/first/data"],
    ]);
  });

  it("keeps the entry session binding until unmount and re-entry", async () => {
    const { result, rerender, unmount } = renderHook(
      ({ activeTabId }: { activeTabId: string }) =>
        useManualSftpController({ enabled: true, sessions, activeTabId }),
      { initialProps: { activeTabId: "tab-first" } },
    );
    await waitFor(() => expect(result.current.state.listing).not.toBeNull());

    rerender({ activeTabId: "tab-second" });
    api.listManualSftpDirectory.mockResolvedValue({
      ...batch,
      listing_id: "listing-fixed-binding",
      path: "/home/first/data",
    });
    await act(() => result.current.navigate("/home/first/data"));
    expect(api.getManualSftpContext).not.toHaveBeenCalledWith(
      "ssh-session-second",
    );
    expect(api.listManualSftpDirectory).toHaveBeenLastCalledWith(
      "ssh-session-first",
      "/home/first/data",
    );

    unmount();
    api.getManualSftpContext.mockResolvedValue({
      ...context,
      ssh_session_id: "ssh-session-second",
      connection_id: "connection-second",
      home: "/home/second",
      host_label: "second.example",
    });
    api.listManualSftpDirectory.mockResolvedValue({
      ...batch,
      listing_id: "listing-second",
      path: "/home/second",
    });
    renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-second",
      }),
    );
    await waitFor(() =>
      expect(api.getManualSftpContext).toHaveBeenCalledWith(
        "ssh-session-second",
      ),
    );
  });

  it("discards an expired one-shot preparation", async () => {
    api.prepareManualSftpUpload.mockResolvedValue({
      preparation_id: "preparation-1",
      operation_id: "operation-1",
      direction: "upload",
      display_name: "report.csv",
      remote_path: "/home/first/report.csv",
      host_label: "first.example",
      source_sha256: "a".repeat(64),
      source_byte_count: 4,
      target_snapshot: {
        path: "/home/first/report.csv",
        exists: false,
        entry_type: null,
        size: null,
        mtime_ns: null,
        sha256: null,
      },
      overwrite_required: false,
      expires_at: new Date(Date.now() - 1).toISOString(),
    });
    const { result } = renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-first",
      }),
    );
    await waitFor(() => expect(result.current.state.listing).not.toBeNull());
    await act(() => result.current.prepareUpload("report.csv"));
    await waitFor(() =>
      expect(api.discardManualSftpPreparation).toHaveBeenCalledWith(
        "preparation-1",
      ),
    );
    expect(result.current.state.terminal?.state).toBe("cancelled");
  });

  it("loads recovery records even when no SSH context can be opened", async () => {
    api.getManualSftpContext.mockRejectedValue({
      code: "NO_SESSION",
      message: "No session is connected.",
    });
    api.listManualSftpRecoveries.mockResolvedValue([recovery]);

    const { result } = renderHook(() =>
      useManualSftpController({ enabled: true, sessions: [], activeTabId: null }),
    );

    await waitFor(() => expect(result.current.state.recoveries).toEqual([recovery]));
    expect(api.listManualSftpRecoveries).toHaveBeenCalledOnce();
  });

  it.each(["inspect", "execute"] as const)(
    "refreshes the complete recovery list after %s",
    async (operation) => {
      api.listManualSftpRecoveries
        .mockResolvedValueOnce([recovery])
        .mockResolvedValueOnce([]);
      const { result } = renderHook(() =>
        useManualSftpController({
          enabled: true,
          sessions,
          activeTabId: "tab-first",
        }),
      );
      await waitFor(() => expect(result.current.state.recoveries).toEqual([recovery]));

      if (operation === "inspect") {
        await act(() => result.current.inspectRecovery(recovery.recovery_id));
      } else {
        await act(() =>
          result.current.executeRecovery(recovery.recovery_id, "keep"),
        );
      }

      expect(result.current.state.recoveries).toEqual([]);
      expect(api.listManualSftpRecoveries).toHaveBeenCalledTimes(2);
    },
  );

  it("keeps recovery loading visible while Verify is inspecting remote state", async () => {
    const inspection = deferred<typeof recovery>();
    api.listManualSftpRecoveries
      .mockResolvedValueOnce([recovery])
      .mockResolvedValueOnce([recovery]);
    api.inspectManualSftpRecovery.mockReturnValueOnce(inspection.promise);
    const { result } = renderHook(() =>
      useManualSftpController({
        enabled: true,
        sessions,
        activeTabId: "tab-first",
      }),
    );
    await waitFor(() => expect(result.current.state.recoveriesLoading).toBe(false));

    let verifying!: Promise<unknown>;
    act(() => {
      verifying = result.current.inspectRecovery(recovery.recovery_id);
    });
    expect(result.current.state.recoveriesLoading).toBe(true);

    await act(async () => inspection.resolve(recovery));
    await verifying;
  });
});
