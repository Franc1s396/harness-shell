// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RemoteEntry } from "../../api/manual-sftp";
import type { ManualSftpController } from "./useManualSftpController";
import { ManualSftpWorkspace } from "./ManualSftpWorkspace";

const removeEntry = vi.fn();
const preflightDelete = vi.fn();
const renameEntry = vi.fn();
const inspectEntry = vi.fn();
const hashFile = vi.fn();

const controller = {
  state: {
    context: {
      ssh_session_id: "ssh-session",
      connection_id: "connection",
      home: "/home/tester",
      host_label: "test.example",
      sftp_version: 3,
    },
    contextLoading: false,
    requestedPath: "/home/tester",
    listing: {
      listingId: "listing",
      path: "/home/tester",
      entries: [
        {
          name: "data.txt",
          path: "/home/tester/data.txt",
          entry_type: "file" as const,
          size: 4,
          mode: 420,
          mtime_ns: "1",
          link_target: null,
        },
      ],
      nextSequence: 1,
      done: true,
      observedEntryCount: 1,
      complete: true,
    },
    listingLoading: false,
    selectedPath: "/home/tester/data.txt",
    preparation: null,
    operationProgress: null,
    transferProgress: null,
    terminal: null,
    recoveries: [],
    recoveriesLoading: false,
    error: null,
  },
  activeSession: null,
  navigate: vi.fn(),
  refresh: vi.fn(),
  select: vi.fn(),
  prepareUpload: vi.fn(),
  prepareDownload: vi.fn(),
  executePrepared: vi.fn(),
  discardPreparation: vi.fn(),
  createDirectory: vi.fn(),
  renameEntry,
  removeEntry,
  preflightDelete,
  executeDelete: vi.fn(),
  listTreeDirectories: vi.fn(),
  inspectEntry,
  hashFile,
  openLink: vi.fn(),
  cancelOperation: vi.fn(),
  loadRecoveries: vi.fn(),
  inspectRecovery: vi.fn(),
  executeRecovery: vi.fn(),
} as unknown as ManualSftpController;

describe("ManualSftpWorkspace", () => {
  afterEach(cleanup);
  beforeEach(() => {
    removeEntry.mockReset();
    removeEntry.mockResolvedValue(undefined);
    preflightDelete.mockReset();
    renameEntry.mockReset();
    renameEntry.mockResolvedValue(undefined);
    inspectEntry.mockReset();
    inspectEntry.mockImplementation(async (entry: RemoteEntry) => entry);
    hashFile.mockReset();
    hashFile.mockResolvedValue({
      path: "/home/tester/data.txt",
      snapshot: {
        path: "/home/tester/data.txt",
        exists: true,
        entry_type: "file",
        size: 4,
        mtime_ns: "1",
        sha256: "a".repeat(64),
      },
      sha256: "a".repeat(64),
      byte_count: 4,
    });
    vi.mocked(controller.openLink).mockReset();
    vi.mocked(controller.select).mockReset();
    vi.mocked(controller.refresh).mockReset();
    vi.mocked(controller.refresh).mockResolvedValue(undefined);
  });

  it("keeps Delete as a confirmation action", () => {
    render(<ManualSftpWorkspace controller={controller} />);
    fireEvent.keyDown(screen.getByRole("grid"), { key: "Delete" });
    expect(
      screen.getByRole("dialog", { name: "Delete data.txt?" }),
    ).toBeVisible();
    expect(removeEntry).not.toHaveBeenCalled();
    expect(preflightDelete).not.toHaveBeenCalled();
  });

  it("shows remote-file loading instead of a false no-session state", () => {
    const loadingController = {
      ...controller,
      activeSession: {
        tabId: "tab-first",
        state: "CONNECTED",
        sshSessionId: "ssh-session",
      },
      state: {
        ...controller.state,
        context: null,
        contextLoading: true,
        requestedPath: null,
        listing: null,
        listingLoading: false,
      },
    } as ManualSftpController;

    render(<ManualSftpWorkspace controller={loadingController} />);

    expect(
      screen.getByRole("status", { name: "Loading remote files…" }),
    ).toBeVisible();
    expect(
      screen.queryByText("No connected terminal selected"),
    ).not.toBeInTheDocument();
  });

  it("shows the requested path while a directory listing is incomplete", () => {
    const loadingController = {
      ...controller,
      state: {
        ...controller.state,
        requestedPath: "/var/log",
        listing: null,
        listingLoading: true,
      },
    } as ManualSftpController;

    render(<ManualSftpWorkspace controller={loadingController} />);

    expect(
      screen.getByRole("status", { name: "Loading /var/log…" }),
    ).toBeVisible();
    expect(screen.queryByText("This directory is empty.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload file" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "New folder" })).toBeDisabled();
  });

  it("opens a loading dialog immediately while properties are read", async () => {
    let resolveInspection!: (entry: RemoteEntry) => void;
    inspectEntry.mockReturnValueOnce(
      new Promise<RemoteEntry>((resolve) => {
        resolveInspection = resolve;
      }),
    );
    render(<ManualSftpWorkspace controller={controller} />);

    fireEvent.click(
      within(screen.getByRole("row", { name: /data\.txt/ })).getByRole(
        "button",
        { name: "Properties" },
      ),
    );

    expect(
      screen.getByRole("status", {
        name: "Loading properties for data.txt…",
      }),
    ).toBeVisible();

    await act(async () => resolveInspection(controller.state.listing!.entries[0]));
    expect(
      await screen.findByRole("dialog", { name: "Properties — data.txt" }),
    ).toBeVisible();
  });

  it("names SHA-256 calculation while file content is being read", async () => {
    let resolveHash!: (value: Awaited<ReturnType<typeof controller.hashFile>>) => void;
    hashFile.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveHash = resolve;
      }),
    );
    render(<ManualSftpWorkspace controller={controller} />);

    fireEvent.click(screen.getByRole("button", { name: "SHA-256" }));

    expect(
      screen.getByRole("status", {
        name: "Calculating SHA-256 for data.txt…",
      }),
    ).toBeVisible();

    await act(async () =>
      resolveHash({
        path: "/home/tester/data.txt",
        snapshot: {
          path: "/home/tester/data.txt",
          exists: true,
          entry_type: "file",
          size: 4,
          mtime_ns: "1",
          sha256: "a".repeat(64),
        },
        sha256: "a".repeat(64),
        byte_count: 4,
      }),
    );
  });

  it("names symlink resolution while the target is being inspected", async () => {
    const link: RemoteEntry = {
      name: "shortcut",
      path: "/home/tester/shortcut",
      entry_type: "symlink",
      size: null,
      mode: 511,
      mtime_ns: "2",
      link_target: "archive",
    };
    let resolveTarget!: (entry: RemoteEntry) => void;
    vi.mocked(controller.openLink).mockReturnValueOnce(
      new Promise<RemoteEntry>((resolve) => {
        resolveTarget = resolve;
      }),
    );
    const linkController = {
      ...controller,
      state: {
        ...controller.state,
        listing: {
          ...controller.state.listing!,
          entries: [link],
        },
        selectedPath: link.path,
      },
    } as ManualSftpController;
    render(<ManualSftpWorkspace controller={linkController} />);

    fireEvent.click(screen.getByRole("button", { name: "Open target" }));

    expect(
      screen.getByRole("status", { name: "Resolving shortcut…" }),
    ).toBeVisible();
    expect(screen.getByRole("dialog", { name: "Open target" })).toBeVisible();

    await act(async () => resolveTarget({ ...link, entry_type: "directory" }));
  });

  it("supports grid selection keys and never renders local path markers", () => {
    render(<ManualSftpWorkspace controller={controller} />);
    const grid = screen.getByRole("grid");
    fireEvent.keyDown(grid, { key: "ArrowDown" });
    expect(controller.select).toHaveBeenCalledWith(
      "/home/tester/data.txt",
    );
    expect(document.body.textContent).not.toMatch(/C:\\|local_path|localPath/);
  });

  it("restores grid focus after cancelling a confirmation", () => {
    render(<ManualSftpWorkspace controller={controller} />);
    const grid = screen.getByRole("grid");
    grid.focus();
    fireEvent.keyDown(grid, { key: "Delete" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(grid).toHaveFocus();
  });

  it("closes the confirmation after a file deletion succeeds", async () => {
    render(<ManualSftpWorkspace controller={controller} />);
    fireEvent.keyDown(screen.getByRole("grid"), { key: "Delete" });
    const dialog = screen.getByRole("dialog", { name: "Delete data.txt?" });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Confirm delete" }),
    );

    await waitFor(() => expect(removeEntry).toHaveBeenCalledOnce());
    expect(removeEntry).toHaveBeenCalledWith("/home/tester/data.txt");
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Delete data.txt?" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("does not dispatch a rename after cancelling the explicit overwrite confirmation", async () => {
    renameEntry.mockRejectedValueOnce({
      code: "SFTP_TARGET_EXISTS",
      message: "The remote rename target already exists.",
    });
    render(<ManualSftpWorkspace controller={controller} />);

    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    fireEvent.change(screen.getByLabelText("New name"), {
      target: { value: "target.txt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() =>
      expect(renameEntry).toHaveBeenCalledWith(
        "/home/tester/data.txt",
        "/home/tester/target.txt",
        false,
      ),
    );
    expect(
      screen.getByText(
        "The destination already exists and will be atomically replaced.",
      ),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(renameEntry).toHaveBeenCalledOnce();
  });

  it("retries a rename with overwrite only after explicit confirmation", async () => {
    renameEntry.mockRejectedValueOnce({
      code: "SFTP_TARGET_EXISTS",
      message: "The remote rename target already exists.",
    });
    render(<ManualSftpWorkspace controller={controller} />);

    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    fireEvent.change(screen.getByLabelText("New name"), {
      target: { value: "target.txt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(renameEntry).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: "Confirm overwrite" }));
    await waitFor(() =>
      expect(renameEntry).toHaveBeenLastCalledWith(
        "/home/tester/data.txt",
        "/home/tester/target.txt",
        true,
      ),
    );
    expect(renameEntry).toHaveBeenCalledTimes(2);
  });

  it("moves one selected entry through the existing atomic rename contract", async () => {
    render(<ManualSftpWorkspace controller={controller} />);

    fireEvent.click(screen.getByRole("button", { name: "Move" }));
    fireEvent.change(screen.getByLabelText("Target path"), {
      target: { value: "/home/archive/data.txt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() =>
      expect(renameEntry).toHaveBeenCalledWith(
        "/home/tester/data.txt",
        "/home/archive/data.txt",
        false,
      ),
    );
  });

  it("shows properties and SHA-256 from explicit read-only inspection", async () => {
    render(<ManualSftpWorkspace controller={controller} />);

    fireEvent.click(screen.getByRole("button", { name: "SHA-256" }));

    await waitFor(() => expect(inspectEntry).toHaveBeenCalledOnce());
    expect(hashFile).toHaveBeenCalledOnce();
    const dialog = await screen.findByRole("dialog", {
      name: "Properties — data.txt",
    });
    expect(within(dialog).getByText("a".repeat(64))).toBeVisible();
    expect(within(dialog).getByText("/home/tester/data.txt")).toBeVisible();
  });

  it("reads a symlink target without following it", async () => {
    const link = {
      name: "shortcut",
      path: "/home/tester/shortcut",
      entry_type: "symlink" as const,
      size: null,
      mode: 511,
      mtime_ns: "2",
      link_target: "archive/data.txt",
    };
    const linkController = {
      ...controller,
      state: {
        ...controller.state,
        listing: {
          ...controller.state.listing,
          entries: [link],
        },
        selectedPath: link.path,
      },
    } as ManualSftpController;
    inspectEntry.mockResolvedValueOnce(link);
    render(<ManualSftpWorkspace controller={linkController} />);

    fireEvent.click(screen.getByRole("button", { name: "Read link target" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Properties — shortcut",
    });
    expect(within(dialog).getByText("archive/data.txt")).toBeVisible();
    expect(hashFile).not.toHaveBeenCalled();
  });

  it.each(["cleanup_required", "outcome_unknown"] as const)(
    "opens recovery center for a %s terminal state",
    (terminalState) => {
      const recoveryController = {
        ...controller,
        state: {
          ...controller.state,
          terminal: {
            operation_id: "operation-id",
            state: terminalState,
            error_code: "SFTP_RECOVERY_REQUIRED",
            message: "Recovery action is required.",
            sha256: null,
            byte_count: null,
            recovery_id: "recovery-id",
          },
        },
      } as ManualSftpController;

      render(<ManualSftpWorkspace controller={recoveryController} />);

      expect(
        screen.getByRole("dialog", { name: /Recovery center/i }),
      ).toBeVisible();
    },
  );

  it("opens recovery center on Activity startup without an SSH context", () => {
    const recoveryController = {
      ...controller,
      state: {
        ...controller.state,
        context: null,
        listing: null,
        selectedPath: null,
        recoveries: [
          {
            recovery_id: "recovery-id",
            operation_id: "operation-id",
            kind: "download_part",
            host_label: "test.example",
            remote_path: "/home/tester/data.txt",
            display_name: "data.txt",
            state: "cleanup_required",
            created_at: "2026-08-30T00:00:00Z",
            available_actions: ["verify", "open_local_folder", "keep"],
          },
        ],
      },
    } as ManualSftpController;

    render(
      <ManualSftpWorkspace
        controller={recoveryController}
        onSelectConnection={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("dialog", { name: /Recovery center/i }),
    ).toBeVisible();
    expect(screen.getByText("data.txt")).toBeVisible();
    expect(screen.getByRole("button", { name: /Select connection/i })).toBeVisible();
  });

  it("retains path and table controls at 900 by 600 without scaling", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 900 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
    render(<ManualSftpWorkspace controller={controller} />);
    expect(screen.getByLabelText("Remote path")).toBeVisible();
    expect(screen.getByRole("grid")).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Remote directory tree" })).toHaveClass("hidden");
    expect(document.body.innerHTML).not.toMatch(/scale\(|transform:/);
  });
});
