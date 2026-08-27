// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { ConnectionProfile } from "../../api/ssh";
import { i18n, i18nReady } from "../../i18n";
import { ConnectionNavigator } from "./ConnectionNavigator";

const profile = (
  id: string,
  name: string,
  group: string | null,
  favorite = false,
): ConnectionProfile => ({
  connection_id: id,
  display_name: name,
  group_name: group,
  host: `${id}.example`,
  port: 22,
  username: "root",
  auth_kind: "password",
  credential_id: `cred-${id}`,
  passphrase_credential_id: null,
  proxy_jump_id: null,
  favorite,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
});

const setup = (disabled = false) => {
  const callbacks = {
    onSelect: vi.fn(),
    onOpen: vi.fn(),
    onCreate: vi.fn(),
    onEdit: vi.fn(),
    onDelete: vi.fn(),
  };
  render(
    <ConnectionNavigator
      connections={[
        profile("lab", "Lab", null),
        profile("staging", "Staging", "Servers"),
        profile("prod", "Production", "Servers", true),
      ]}
      selectedId="prod"
      disabled={disabled}
      {...callbacks}
    />,
  );
  return callbacks;
};

describe("ConnectionNavigator", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
  });
  afterEach(cleanup);
  afterAll(async () => i18n.changeLanguage("en"));

  it("keeps search, groups, favorites-first ordering, and compact row content", () => {
    setup();
    const options = screen.getAllByRole("option");
    expect(options.map((option) => option.getAttribute("aria-label"))).toEqual([
      "Production, root@prod.example:22",
      "Staging, root@staging.example:22",
      "Lab, root@lab.example:22",
    ]);
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(options[0]).toHaveClass("h-10");

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "lab.example" },
    });
    expect(screen.getByRole("option", { name: /Lab/ })).toBeVisible();
    expect(screen.queryByRole("option", { name: /Production/ })).not.toBeInTheDocument();
  });

  it("uses a compact plus icon for creating connections", () => {
    setup();

    const createButton = screen.getByRole("button", { name: "New connection" });
    expect(createButton).toHaveTextContent("+");
    expect(createButton).not.toHaveTextContent("New connection");
    expect(createButton).toHaveClass("size-8");
    expect(screen.getByRole("tooltip", { name: "New connection" })).toBeVisible();
  });

  it("selects on one click and opens on every double click or Enter", () => {
    const callbacks = setup();
    const lab = screen.getByRole("option", { name: /Lab/ });
    fireEvent.click(lab);
    expect(callbacks.onSelect).toHaveBeenCalledWith("lab");
    expect(callbacks.onOpen).not.toHaveBeenCalled();
    fireEvent.doubleClick(lab);
    fireEvent.doubleClick(lab);
    expect(callbacks.onOpen).toHaveBeenNthCalledWith(1, "lab");
    expect(callbacks.onOpen).toHaveBeenNthCalledWith(2, "lab");
    fireEvent.keyDown(lab, { key: "Enter" });
    expect(callbacks.onOpen).toHaveBeenCalledTimes(3);
  });

  it("uses the same menu for the visible button, context menu, and Shift+F10", () => {
    const callbacks = setup();
    const prod = screen.getByRole("option", { name: /Production/ });

    fireEvent.click(screen.getByRole("button", { name: /Connection actions: Production/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit connection" }));
    expect(callbacks.onEdit).toHaveBeenCalledWith("prod");

    fireEvent.contextMenu(prod, { clientX: 20, clientY: 40 });
    expect(
      screen.queryByRole("menuitem", { name: "Disconnect" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open connection" }));
    expect(callbacks.onOpen).toHaveBeenCalledWith("prod");

    fireEvent.keyDown(prod, { key: "F10", shiftKey: true });
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(callbacks.onDelete).toHaveBeenCalledWith("prod");
  });

  it("blocks New/Open when disabled while preserving selection and editing", () => {
    const callbacks = setup(true);
    expect(screen.getByRole("button", { name: "New connection" })).toBeDisabled();
    const lab = screen.getByRole("option", { name: /Lab/ });
    fireEvent.click(lab);
    fireEvent.doubleClick(lab);
    expect(callbacks.onSelect).toHaveBeenCalledWith("lab");
    expect(callbacks.onOpen).not.toHaveBeenCalled();

    fireEvent.keyDown(lab, { key: "F10", shiftKey: true });
    expect(screen.getByRole("menuitem", { name: "Open connection" })).toBeDisabled();
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit connection" }));
    expect(callbacks.onEdit).toHaveBeenCalledWith("lab");
  });
});
