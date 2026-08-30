// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { SftpTransferStrip } from "./SftpTransferStrip";

it("renders a localized transfer phase instead of the wire enum", () => {
  render(
    <SftpTransferStrip
      progress={{
        operation_id: "00000000-0000-4000-8000-000000000001",
        direction: "upload",
        phase: "committing",
        display_name: "payload.bin",
        remote_path: "/home/demo/payload.bin",
        host_label: "demo-host",
        bytes_completed: 7,
        bytes_total: 7,
        cancellable: false,
      }}
      onCancel={vi.fn()}
    />,
  );

  expect(screen.getByText("Committing")).toBeVisible();
  expect(screen.queryByText("committing")).not.toBeInTheDocument();
});
