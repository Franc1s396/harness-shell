// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n, i18nReady } from "../../i18n";
import { useLocaleStore } from "../../stores/locale-store";
import { SettingsPopover } from "./SettingsPopover";

describe("SettingsPopover", () => {
  afterEach(cleanup);

  beforeEach(async () => {
    localStorage.clear();
    await i18nReady;
    await i18n.changeLanguage("en");
    useLocaleStore.getState().reset();
  });

  it("contains only the existing language preference", async () => {
    const anchor = document.createElement("button");
    document.body.append(anchor);
    render(<SettingsPopover open anchor={anchor} onClose={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "Settings" });
    expect(screen.getByRole("combobox", { name: "Language" })).toBeVisible();
    expect(screen.getAllByRole("option")).toHaveLength(4);
    expect(dialog).not.toHaveTextContent(/theme|SSH|terminal|Agent/i);

    fireEvent.change(screen.getByRole("combobox", { name: "Language" }), {
      target: { value: "zh-CN" },
    });
    await waitFor(() =>
      expect(useLocaleStore.getState().languageMode).toBe("zh-CN"),
    );
    anchor.remove();
  });

  it("closes on Escape and restores focus to the Activity button", async () => {
    const anchor = document.createElement("button");
    document.body.append(anchor);
    anchor.focus();
    const onClose = vi.fn();
    const { unmount } = render(
      <SettingsPopover open anchor={anchor} onClose={onClose} />,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();
    expect(anchor).toHaveFocus();
    anchor.remove();
  });
});
