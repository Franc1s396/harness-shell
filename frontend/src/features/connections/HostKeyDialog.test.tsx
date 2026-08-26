// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { HostKeyDialog } from "./HostKeyDialog";

const candidate = {
  connection_id: "c1",
  host: "test.invalid",
  port: 22,
  key_algorithm: "ssh-ed25519",
  fingerprint_sha256: "SHA256:new/RAW=",
  public_key_openssh_b64: "AAAATEST",
};

describe("HostKeyDialog", () => {
  beforeAll(() => i18nReady);
  afterEach(cleanup);

  for (const locale of ["zh-CN", "zh-TW", "en"] as const) {
    it(`keeps full raw fingerprints in ${locale}`, async () => {
      await i18n.changeLanguage(locale);
      render(
        <HostKeyDialog
          candidate={candidate}
          trustedFingerprint="SHA256:old/RAW="
          error={null}
          busy={false}
          onConfirm={vi.fn()}
          onReplace={vi.fn()}
          onClose={vi.fn()}
        />,
      );
      expect(screen.getByText("SHA256:new/RAW=")).toBeInTheDocument();
      expect(screen.getByText("SHA256:old/RAW=")).toBeInTheDocument();
    });
  }

  it("shows the raw structured conflict inside the modal", async () => {
    await i18n.changeLanguage("en");
    render(
      <HostKeyDialog
        candidate={candidate}
        trustedFingerprint="SHA256:old/RAW="
        error={{
          code: "HOST_KEY_REPLACE_CONFLICT",
          message: "active host key changed",
          details: {
            correlation_id: "host-key-correlation",
            recoverable: true,
          },
        }}
        busy={false}
        onConfirm={vi.fn()}
        onReplace={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Trusted Host Key changed again",
    );
    expect(screen.getByText("HOST_KEY_REPLACE_CONFLICT")).toBeInTheDocument();
    expect(screen.getByText("active host key changed")).toBeInTheDocument();
    expect(screen.getByText("host-key-correlation")).toBeInTheDocument();
  });
});
