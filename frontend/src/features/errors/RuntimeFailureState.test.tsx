// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { RuntimeFailureState } from "./RuntimeFailureState";

describe("RuntimeFailureState", () => {
  beforeAll(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
  });
  afterEach(cleanup);

  it("blocks the workspace and exposes the exact runtime failure identity", () => {
    const onRetryStatus = vi.fn();
    render(
      <RuntimeFailureState
        errorCode="SIDECAR_EXITED"
        correlationId="corr-runtime"
        onRetryStatus={onRetryStatus}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Runtime unavailable");
    expect(alert).toHaveTextContent("SIDECAR_EXITED");
    expect(alert).toHaveTextContent("corr-runtime");
    fireEvent.click(
      screen.getByRole("button", { name: "Check runtime again" }),
    );
    expect(onRetryStatus).toHaveBeenCalledTimes(1);
  });
});
