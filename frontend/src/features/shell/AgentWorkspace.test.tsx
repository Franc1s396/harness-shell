// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { AgentWorkspace } from "./AgentWorkspace";

describe("AgentWorkspace", () => {
  afterEach(cleanup);

  it("shows an honest width-controlled M3 state with one collapse action", async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
    const onCollapse = vi.fn();
    render(<AgentWorkspace width={480} onCollapse={onCollapse} />);

    expect(screen.getByRole("region", { name: "Agent" })).toHaveStyle({
      width: "min(480px, 100%)",
    });
    expect(screen.getByRole("heading", { name: "Agent" })).toBeVisible();
    expect(screen.getByText("Agent arrives in M3")).toBeVisible();
    expect(
      screen.getByText("This M2 workspace does not execute Agent tasks."),
    ).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /plan|evidence/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /send|execute|pause|resume|stop/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/online/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Collapse Agent/i }));
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });
});
