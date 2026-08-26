// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { ErrorNotice } from "./ErrorNotice";

describe("ErrorNotice", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("zh-CN");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });
  afterEach(cleanup);

  it("localizes the summary but preserves and copies raw details", () => {
    render(
      <ErrorNotice
        partialSuccess
        error={{
          code: "REQUEST_HANDLER_FAILED",
          message: "raw message",
          details: {
            node: "target",
            recoverable: false,
            correlation_id: "corr-raw",
            remote_state: "pre_auth",
          },
        }}
      />,
    );

    expect(screen.getByText("配置已保存；连接失败")).toBeInTheDocument();
    expect(screen.getByText("技术详情").closest("details")).not.toHaveAttribute(
      "open",
    );
    for (const raw of [
      "REQUEST_HANDLER_FAILED",
      "raw message",
      "target",
      "false",
      "corr-raw",
      "pre_auth",
    ]) {
      expect(screen.getByText(raw)).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole("button", { name: "复制详情" }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      [
        "error_code: REQUEST_HANDLER_FAILED",
        "message: raw message",
        "node: target",
        "recoverable: false",
        "correlation_id: corr-raw",
        "remote_state: pre_auth",
      ].join("\n"),
    );
  });
});
