// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { ConnectionProfile } from "../../api/ssh";
import { i18n, i18nReady } from "../../i18n";
import { ConnectionExplorer } from "./ConnectionExplorer";

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

describe("ConnectionExplorer", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(cleanup);
  afterAll(async () => i18n.changeLanguage("en"));

  it("filters, keeps raw state, and separates row and action buttons", () => {
    const onSelect = vi.fn();
    const onEdit = vi.fn();
    const onConnect = vi.fn();
    const onDisconnect = vi.fn();
    const prod = profile("prod", "Production", "Servers", true);
    const lab = profile("lab", "Lab", null);

    render(
      <ConnectionExplorer
        connections={[prod, lab]}
        selectedId={null}
        statuses={{
          prod: {
            connection_id: "prod",
            state: "READY",
            session_id: "ssh-1",
            error_code: null,
            recoverable: false,
            correlation_id: "c1",
            host_key_candidate: null,
            trusted_fingerprint_sha256: "SHA256:trusted",
          },
          lab: {
            connection_id: "lab",
            state: "FAILED",
            session_id: null,
            error_code: "AUTH_FAILED",
            recoverable: true,
            correlation_id: "c2",
            host_key_candidate: null,
            trusted_fingerprint_sha256: null,
          },
        }}
        disabled={false}
        onSelect={onSelect}
        onCreate={vi.fn()}
        onEdit={onEdit}
        onConnect={onConnect}
        onDisconnect={onDisconnect}
      />,
    );

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "prod.example" },
    });
    expect(screen.getByText("READY")).toBeInTheDocument();
    expect(screen.queryByText("Lab")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    expect(onSelect).toHaveBeenCalledWith("prod");
    fireEvent.click(screen.getByRole("button", { name: /编辑连接/ }));
    expect(onEdit).toHaveBeenCalledWith("prod");
    expect(onSelect).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /断开/ }));
    expect(onDisconnect).toHaveBeenCalledWith("ssh-1");
  });
});
