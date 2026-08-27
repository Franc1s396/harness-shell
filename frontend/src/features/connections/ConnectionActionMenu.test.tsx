// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectionProfile, ConnectionStatus } from "../../api/ssh";
import { i18n, i18nReady } from "../../i18n";
import { ConnectionActionMenu } from "./ConnectionActionMenu";

const connection: ConnectionProfile = {
  connection_id: "prod",
  display_name: "Production",
  group_name: "Servers",
  host: "prod.example",
  port: 22,
  username: "root",
  auth_kind: "password",
  credential_id: "cred-prod",
  passphrase_credential_id: null,
  proxy_jump_id: null,
  favorite: true,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
};

const ready: ConnectionStatus = {
  connection_id: "prod",
  state: "READY",
  session_id: "ssh-1",
  error_code: null,
  recoverable: false,
  correlation_id: "c1",
  host_key_candidate: null,
  trusted_fingerprint_sha256: "SHA256:trusted",
};

describe("ConnectionActionMenu", () => {
  afterEach(cleanup);

  it("offers one keyboard-navigable action contract and restores focus", async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
    const anchor = document.createElement("button");
    document.body.append(anchor);
    anchor.focus();
    const callbacks = {
      onClose: vi.fn(),
      onOpen: vi.fn(),
      onEdit: vi.fn(),
      onDelete: vi.fn(),
      onDisconnect: vi.fn(),
    };
    const { unmount } = render(
      <ConnectionActionMenu
        connection={connection}
        status={ready}
        anchor={anchor}
        disabled={false}
        {...callbacks}
      />,
    );

    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "Open connection",
      "Edit connection",
      "Disconnect",
      "Delete",
    ]);
    expect(items[0]).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "End" });
    expect(items[3]).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(items[0]).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(callbacks.onClose).toHaveBeenCalledTimes(1);

    unmount();
    expect(anchor).toHaveFocus();
    anchor.remove();
  });

  it("disables Open when runtime interaction is unavailable", async () => {
    await i18nReady;
    const anchor = document.createElement("button");
    document.body.append(anchor);
    render(
      <ConnectionActionMenu
        connection={connection}
        status={undefined}
        anchor={anchor}
        disabled
        onClose={vi.fn()}
        onOpen={vi.fn()}
        onEdit={vi.fn()}
        onDelete={vi.fn()}
        onDisconnect={vi.fn()}
      />,
    );
    expect(screen.getByRole("menuitem", { name: "Open connection" })).toBeDisabled();
    expect(screen.getByRole("menuitem", { name: "Edit connection" })).toBeEnabled();
    expect(screen.queryByRole("menuitem", { name: "Disconnect" })).not.toBeInTheDocument();
    anchor.remove();
  });
});
