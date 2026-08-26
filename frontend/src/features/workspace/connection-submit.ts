import type { SshCommandError } from "../../api/ssh";

export type ConnectionSubmitOutcome<T> =
  | { kind: "saved"; saved: T }
  | {
      kind: "saved-connect-started";
      saved: T;
      connectionState: "CONNECTED" | "HOST_KEY_REQUIRED";
    }
  | { kind: "saved-connect-failed"; saved: T; error: SshCommandError };

const normalize = (error: unknown): SshCommandError =>
  typeof error === "object" && error !== null && "code" in error
    ? (error as SshCommandError)
    : {
        code: "SSH_OPERATION_FAILED",
        message:
          error instanceof Error ? error.message : "SSH operation failed.",
      };

export async function submitConnectionProfile<
  T extends { connection_id: string },
>({
  intent,
  persist,
  closeDialog,
  connect,
}: {
  intent: "save" | "save-and-connect";
  persist: () => Promise<T>;
  closeDialog: () => void;
  connect: (saved: T) => Promise<"CONNECTED" | "HOST_KEY_REQUIRED">;
}): Promise<ConnectionSubmitOutcome<T>> {
  const saved = await persist();
  closeDialog();
  if (intent === "save") return { kind: "saved", saved };

  try {
    const connectionState = await connect(saved);
    return { kind: "saved-connect-started", saved, connectionState };
  } catch (error) {
    return { kind: "saved-connect-failed", saved, error: normalize(error) };
  }
}
