// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SftpDirectoryTree } from "./SftpDirectoryTree";

describe("SftpDirectoryTree", () => {
  afterEach(cleanup);

  it("lazily lists, expands, collapses, and navigates remote directories", async () => {
    const onListDirectories = vi.fn(async () => [
      { name: "alpha", path: "/alpha" },
      { name: "beta", path: "/beta" },
    ]);
    const onNavigate = vi.fn();
    render(
      <SftpDirectoryTree
        path="/"
        onListDirectories={onListDirectories}
        onNavigate={onNavigate}
      />,
    );

    expect(screen.getByRole("tree")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Remote directory tree /" }));
    await waitFor(() => expect(onListDirectories).toHaveBeenCalledWith("/"));
    fireEvent.click(screen.getByRole("button", { name: "alpha" }));
    expect(onNavigate).toHaveBeenCalledWith("/alpha");

    fireEvent.click(screen.getByRole("button", { name: "Remote directory tree /" }));
    expect(screen.queryByRole("button", { name: "alpha" })).not.toBeInTheDocument();
  });

  it("shows an inline status while a directory node is loading", async () => {
    let resolveDirectories!: (value: { name: string; path: string }[]) => void;
    const onListDirectories = vi.fn(
      () =>
        new Promise<{ name: string; path: string }[]>((resolve) => {
          resolveDirectories = resolve;
        }),
    );
    render(
      <SftpDirectoryTree
        path="/"
        onListDirectories={onListDirectories}
        onNavigate={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Remote directory tree /" }));

    expect(
      screen.getByRole("status", { name: "Loading directories in /…" }),
    ).toBeVisible();
    resolveDirectories([]);
    await waitFor(() =>
      expect(
        screen.queryByRole("status", { name: "Loading directories in /…" }),
      ).not.toBeInTheDocument(),
    );
  });
});
