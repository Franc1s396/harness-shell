import { describe, expect, it, vi } from "vitest";

import { submitConnectionProfile } from "./connection-submit";

describe("connection submission", () => {
  it("closes after persistence and before starting Save & Connect", async () => {
    const calls: string[] = [];
    const saved = { connection_id: "c1" };

    await submitConnectionProfile({
      intent: "save-and-connect",
      persist: async () => {
        calls.push("persist");
        return saved;
      },
      closeDialog: () => calls.push("close"),
      connect: async (profile) => {
        calls.push(`connect:${profile.connection_id}`);
        return "HOST_KEY_REQUIRED" as const;
      },
    });

    expect(calls).toEqual(["persist", "close", "connect:c1"]);
  });

  it("returns saved-with-connect-error without rolling back the profile", async () => {
    const saved = { connection_id: "c1" };
    const persist = vi.fn().mockResolvedValue(saved);
    const connect = vi
      .fn()
      .mockRejectedValue({ code: "AUTH_FAILED", message: "denied" });

    await expect(
      submitConnectionProfile({
        intent: "save-and-connect",
        persist,
        closeDialog: vi.fn(),
        connect,
      }),
    ).resolves.toEqual({
      kind: "saved-connect-failed",
      saved,
      error: { code: "AUTH_FAILED", message: "denied" },
    });
    expect(persist).toHaveBeenCalledOnce();
  });

  it("does not call connect for Save", async () => {
    const connect = vi.fn();
    const closeDialog = vi.fn();
    await submitConnectionProfile({
      intent: "save",
      persist: vi.fn().mockResolvedValue({ connection_id: "c1" }),
      closeDialog,
      connect,
    });

    expect(connect).not.toHaveBeenCalled();
    expect(closeDialog).toHaveBeenCalledOnce();
  });
});
