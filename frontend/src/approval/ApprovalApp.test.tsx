// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../i18n";

const approvalApi = vi.hoisted(() => ({ getApprovalContext: vi.fn() }));
vi.mock("../api/approval", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/approval")>()),
  ...approvalApi,
}));

import { ApprovalApp } from "./ApprovalApp";

describe("ApprovalApp", () => {
  beforeEach(async () => {
    await i18nReady;
    await i18n.changeLanguage("zh-TW");
    approvalApi.getApprovalContext.mockReset().mockResolvedValue({ pending: false });
  });
  afterEach(cleanup);

  it("localizes surrounding copy and keeps raw pending state", async () => {
    render(<ApprovalApp />);
    expect(
      await screen.findByText("目前沒有待處理的審批請求。"),
    ).toBeInTheDocument();
    expect(screen.getByText("pending=false")).toBeInTheDocument();
  });
});
