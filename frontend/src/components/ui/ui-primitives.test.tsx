// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Button, IconButton, Tooltip } from "./controls";
import { Dialog } from "./Dialog";
import { FormField } from "./fields";
import { StatusIndicator } from "./feedback";

describe("UI primitives", () => {
  afterEach(cleanup);

  it("exposes an accessible icon-button name and tooltip", () => {
    render(
      <IconButton label="Open connections">
        <span aria-hidden>+</span>
      </IconButton>,
    );

    expect(
      screen.getByRole("button", { name: "Open connections" }),
    ).toBeEnabled();
    expect(screen.getByRole("tooltip")).toHaveTextContent("Open connections");
  });

  it("positions bottom-start tooltips from the trigger start edge", () => {
    render(
      <Tooltip text="Open menu" placement="bottom-start">
        <button>Menu</button>
      </Tooltip>,
    );

    expect(screen.getByRole("tooltip", { name: "Open menu" })).toHaveClass(
      "top-full",
      "left-0",
      "mt-2",
    );
    expect(screen.getByRole("tooltip", { name: "Open menu" })).not.toHaveClass(
      "-translate-x-1/2",
    );
  });

  it("positions right tooltips beside and centered on the trigger", () => {
    render(
      <Tooltip text="Unavailable" placement="right">
        <button>Unavailable</button>
      </Tooltip>,
    );

    expect(screen.getByRole("tooltip", { name: "Unavailable" })).toHaveClass(
      "left-full",
      "top-1/2",
      "ml-2",
      "-translate-y-1/2",
    );
  });

  it("passes IconButton tooltip placement to its tooltip", () => {
    render(
      <IconButton label="Open activity" tooltipPlacement="right">
        <span aria-hidden>+</span>
      </IconButton>,
    );

    expect(screen.getByRole("tooltip", { name: "Open activity" })).toHaveClass(
      "left-full",
    );
  });

  it("closes a dialog with Escape and restores trigger focus", () => {
    const closed = vi.fn();
    const Fixture = () => {
      const [open, setOpen] = useState(false);
      return (
        <>
          <Button onClick={() => setOpen(true)}>Trigger</Button>
          <Dialog
            open={open}
            title="Connection"
            onClose={() => {
              closed();
              setOpen(false);
            }}
          >
            <input aria-label="Name" />
          </Dialog>
        </>
      );
    };

    render(<Fixture />);
    const trigger = screen.getByRole("button", { name: "Trigger" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(closed).toHaveBeenCalledOnce();
    expect(trigger).toHaveFocus();
  });

  it("does not close a busy dialog with Escape", () => {
    const onClose = vi.fn();
    render(
      <Dialog open busy title="Saving" onClose={onClose}>
        <button>Save</button>
      </Dialog>,
    );

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes only the topmost stacked dialog and restores its trigger", () => {
    const closeSettings = vi.fn();
    const closeProvider = vi.fn();
    const Harness = () => {
      const [providerOpen, setProviderOpen] = useState(false);
      return (
        <Dialog open title="Settings" onClose={closeSettings}>
          <button type="button" onClick={() => setProviderOpen(true)}>
            Edit provider
          </button>
          <Dialog
            open={providerOpen}
            title="Edit provider"
            onClose={() => {
              closeProvider();
              setProviderOpen(false);
            }}
          >
            <button type="button">Save</button>
          </Dialog>
        </Dialog>
      );
    };

    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Edit provider" }));
    fireEvent.keyDown(document, { key: "Escape" });

    expect(closeProvider).toHaveBeenCalledOnce();
    expect(closeSettings).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Edit provider" }),
    ).toHaveFocus();
  });

  it("links field errors to the form control", () => {
    render(
      <FormField id="host" label="Host" error="Host is required">
        <input />
      </FormField>,
    );

    const input = screen.getByRole("textbox", { name: "Host" });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Host is required");
  });

  it("renders unknown status values verbatim", () => {
    render(<StatusIndicator value="VENDOR_EXTENSION" />);
    expect(screen.getByText("VENDOR_EXTENSION")).toBeVisible();
  });
});
