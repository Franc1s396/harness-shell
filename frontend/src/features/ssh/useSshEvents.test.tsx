// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const subscribeSshEventsMock = vi.hoisted(() => vi.fn());

vi.mock("../../api/ssh", () => ({
  subscribeSshEvents: subscribeSshEventsMock,
}));

import { useSshEvents } from "./useSshEvents";

const EventSubscriber = ({ onError }: { onError: (error: unknown) => void }) => {
  const state = useSshEvents(() => undefined, onError);
  return <span>{state}</span>;
};

describe("useSshEvents", () => {
  beforeEach(() => {
    subscribeSshEventsMock.mockReset();
  });

  it("surfaces a rejected Tauri event subscription", async () => {
    subscribeSshEventsMock.mockRejectedValue(
      new Error("event.listen is not allowed for window main"),
    );
    const onError = vi.fn();

    render(<EventSubscriber onError={onError} />);

    await waitFor(() => {
      expect(onError).toHaveBeenCalledWith({
        code: "SSH_EVENT_SUBSCRIPTION_FAILED",
        message:
          "SSH event subscription failed: event.listen is not allowed for window main",
      });
    });
    expect(screen.getByText("FAILED")).toBeTruthy();
  });

  it("reports READY only after the Tauri listener is registered", async () => {
    subscribeSshEventsMock.mockResolvedValue(() => undefined);

    render(<EventSubscriber onError={() => undefined} />);

    expect(screen.getByText("SUBSCRIBING")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByText("READY")).toBeTruthy();
    });
  });
});
