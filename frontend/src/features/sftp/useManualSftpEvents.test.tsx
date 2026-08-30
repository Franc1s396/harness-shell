// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const subscribeMock = vi.hoisted(() => vi.fn());

vi.mock("../../api/manual-sftp", () => ({
  subscribeManualSftpEvents: subscribeMock,
}));

import { useManualSftpEvents } from "./useManualSftpEvents";

const EventSubscriber = ({
  onError,
}: {
  onError: (error: { code: string; message: string }) => void;
}) => {
  const state = useManualSftpEvents(
    () => undefined,
    () => undefined,
    onError,
  );
  return <span>{state}</span>;
};

describe("useManualSftpEvents", () => {
  beforeEach(() => subscribeMock.mockReset());

  it("reports ready only after registration succeeds", async () => {
    subscribeMock.mockResolvedValue(() => undefined);
    const onError = vi.fn();
    render(<EventSubscriber onError={onError} />);
    expect(screen.getByText("SUBSCRIBING")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("READY")).toBeTruthy());
    expect(subscribeMock).toHaveBeenCalledOnce();
    expect(onError).not.toHaveBeenCalled();
  });

  it("unlistens when registration completes after unmount", async () => {
    let resolveSubscription: ((unlisten: () => void) => void) | undefined;
    const unlisten = vi.fn();
    subscribeMock.mockReturnValue(
      new Promise<() => void>((resolve) => {
        resolveSubscription = resolve;
      }),
    );
    const { unmount } = render(
      <EventSubscriber onError={() => undefined} />,
    );
    unmount();
    resolveSubscription?.(unlisten);
    await waitFor(() => expect(unlisten).toHaveBeenCalledOnce());
  });

});
