// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RemoteEntry } from "../../api/manual-sftp";
import { SftpFileTable } from "./SftpFileTable";

const entries: RemoteEntry[] = [
  { name: "file.txt", path: "/file.txt", entry_type: "file", size: 4, mode: 420, mtime_ns: "1", link_target: null },
  { name: "folder", path: "/folder", entry_type: "directory", size: null, mode: 493, mtime_ns: "2", link_target: null },
  { name: "shortcut", path: "/shortcut", entry_type: "symlink", size: null, mode: 511, mtime_ns: "3", link_target: "folder" },
  { name: "socket", path: "/socket", entry_type: "other", size: null, mode: 0, mtime_ns: null, link_target: null },
];

describe("SftpFileTable", () => {
  it("renders the approved single-entry action matrix", () => {
    const onMove = vi.fn();
    const onProperties = vi.fn();
    const onHash = vi.fn();
    const onReadLinkTarget = vi.fn();
    render(
      <SftpFileTable
        entries={entries}
        selectedPath={null}
        onSelect={vi.fn()}
        onOpen={vi.fn()}
        onDownload={vi.fn()}
        onRename={vi.fn()}
        onMove={onMove}
        onDelete={vi.fn()}
        onProperties={onProperties}
        onHash={onHash}
        onReadLinkTarget={onReadLinkTarget}
        onRefresh={vi.fn()}
        onParent={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("button", { name: "Move" })).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: "Properties" })).toHaveLength(4);
    expect(screen.getByRole("button", { name: "SHA-256" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Read link target" })).toBeVisible();
    expect(screen.getByText("File")).toBeVisible();
    expect(screen.getByText("Directory")).toBeVisible();
    expect(screen.getByText("Symbolic link")).toBeVisible();
    expect(screen.getByText("Other")).toBeVisible();

    fireEvent.click(screen.getAllByRole("button", { name: "Move" })[2]);
    expect(onMove).toHaveBeenCalledWith(entries[2]);
    fireEvent.click(screen.getByRole("button", { name: "SHA-256" }));
    expect(onHash).toHaveBeenCalledWith(entries[0]);
    fireEvent.click(screen.getByRole("button", { name: "Read link target" }));
    expect(onReadLinkTarget).toHaveBeenCalledWith(entries[2]);
  });
});
