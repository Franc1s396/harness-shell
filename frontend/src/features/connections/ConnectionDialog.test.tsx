// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectionProfile } from "../../api/ssh";
import { i18n, i18nReady } from "../../i18n";
import { ConnectionDialog } from "./ConnectionDialog";

const selectPrivateKeyText = vi.hoisted(() => vi.fn());
vi.mock("./private-key-file", () => ({ selectPrivateKeyText }));

const existing: ConnectionProfile = {
  connection_id: "c1",
  version: 1,
  display_name: "Prod",
  group_name: null,
  host: "prod.example",
  port: 22,
  username: "root",
  auth_kind: "password",
  credential_id: "cred-old",
  passphrase_credential_id: null,
  proxy_jump_id: null,
  favorite: false,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
};

const fillRequired = () => {
  fireEvent.change(screen.getByLabelText("Connection name"), {
    target: { value: "Prod" },
  });
  fireEvent.change(screen.getByLabelText("Host"), {
    target: { value: "prod.example" },
  });
  fireEvent.click(screen.getByRole("tab", { name: "Authentication" }));
  fireEvent.change(screen.getByLabelText("Username"), {
    target: { value: "root" },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "SECRET_MARKER" },
  });
};

describe("ConnectionDialog", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
  });
  beforeEach(() => {
    localStorage.clear();
    selectPrivateKeyText.mockReset();
  });
  afterEach(cleanup);

  it("moves to and focuses the first cross-tab error", async () => {
    render(
      <ConnectionDialog
        open
        connection={null}
        connections={[]}
        onClose={vi.fn()}
        onSubmit={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Prod" },
    });
    fireEvent.change(screen.getByLabelText("Host"), {
      target: { value: "prod.example" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: "Authentication" }),
      ).toHaveAttribute("aria-selected", "true"),
    );
    expect(screen.getByLabelText("Username")).toHaveFocus();
  });

  it.each([
    ["Save", "save"],
    ["Save & Connect", "save-and-connect"],
  ] as const)("maps %s to %s", async (button, intent) => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ConnectionDialog
        open
        connection={null}
        connections={[]}
        onClose={vi.fn()}
        onSubmit={onSubmit}
        onDelete={vi.fn()}
      />,
    );
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: button }));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          credential_secret: "SECRET_MARKER",
          passphrase_secret: null,
        }),
        intent,
      ),
    );
    expect(JSON.stringify(localStorage)).not.toContain("SECRET_MARKER");
  });

  it("requires a separate confirmation before delete", async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined);
    render(
      <ConnectionDialog
        open
        connection={existing}
        connections={[existing]}
        onClose={vi.fn()}
        onSubmit={vi.fn()}
        onDelete={onDelete}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith("c1"));
  });

  it("does not let an old submission clear a newly reopened dialog", async () => {
    let resolveSubmit!: () => void;
    const pendingSubmit = new Promise<void>((resolve) => {
      resolveSubmit = resolve;
    });

    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Reopen</button>
          <ConnectionDialog
            open={open}
            connection={null}
            connections={[]}
            onClose={() => setOpen(false)}
            onSubmit={async () => {
              setOpen(false);
              await pendingSubmit;
            }}
            onDelete={vi.fn()}
          />
        </>
      );
    }

    render(<Harness />);
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: "Save & Connect" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /New connection/i })).not.toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    fireEvent.click(screen.getByRole("tab", { name: "Authentication" }));
    const newPassword = screen.getByLabelText("Password") as HTMLInputElement;
    fireEvent.change(newPassword, { target: { value: "NEW_SECRET" } });
    await act(async () => resolveSubmit());

    expect(newPassword.value).toBe("NEW_SECRET");
  });

  it("preserves an unsaved password while switching to Advanced", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ConnectionDialog
        open
        connection={null}
        connections={[]}
        onClose={vi.fn()}
        onSubmit={onSubmit}
        onDelete={vi.fn()}
      />,
    );

    fillRequired();
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Favorite" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        credential_secret: "SECRET_MARKER",
        favorite: true,
      }),
      "save",
    );
  });

  it("preserves an unsaved private-key passphrase while switching to Advanced", async () => {
    selectPrivateKeyText.mockResolvedValue("PRIVATE_KEY_MARKER");
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    render(
      <ConnectionDialog
        open
        connection={null}
        connections={[]}
        onClose={vi.fn()}
        onSubmit={onSubmit}
        onDelete={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Key profile" },
    });
    fireEvent.change(screen.getByLabelText("Host"), {
      target: { value: "target.example" },
    });
    fireEvent.click(screen.getByRole("tab", { name: "Authentication" }));
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "targetuser" },
    });
    fireEvent.change(screen.getByLabelText("Authentication method"), {
      target: { value: "private_key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Private key" }));
    await waitFor(() => expect(selectPrivateKeyText).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("Passphrase"), {
      target: { value: "PASSPHRASE_MARKER" },
    });

    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        credential_secret: "PRIVATE_KEY_MARKER",
        passphrase_secret: "PASSPHRASE_MARKER",
      }),
      "save",
    );
  });
});
