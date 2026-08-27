import type { SshCommandError } from "../../api/ssh";

export const CLEANUP_RETRY_DELAYS_MS = [500, 1500] as const;

export type SessionCleanupJob = {
  cleanupJobId: string;
  tabId: string;
  sessionTitle: string;
  connectionId: string;
  generation: number;
  ptySessionId: string | null;
  sshSessionId: string | null;
  ptyClosed: boolean;
  sshDisconnected: boolean;
  lastPtyError: SshCommandError | null;
  lastSshError: SshCommandError | null;
};

export const createCleanupJob = (input: {
  tabId: string;
  sessionTitle: string;
  connectionId: string;
  generation: number;
  ptySessionId: string | null;
  sshSessionId: string | null;
}): SessionCleanupJob => ({
  cleanupJobId: crypto.randomUUID(),
  ...input,
  ptyClosed: input.ptySessionId === null,
  sshDisconnected: input.sshSessionId === null,
  lastPtyError: null,
  lastSshError: null,
});

const defaultNormalizeError = (error: unknown): SshCommandError => {
  if (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string"
  ) {
    return error as SshCommandError;
  }
  return { code: "SESSION_CLEANUP_FAILED", message: String(error) };
};

export async function runCleanupJob(
  job: SessionCleanupJob,
  operations: {
    closePty: (id: string) => Promise<unknown>;
    disconnectSsh: (id: string) => Promise<unknown>;
    normalizeError?: (error: unknown) => SshCommandError;
    wait?: (delayMs: number) => Promise<void>;
  },
): Promise<{ complete: boolean; job: SessionCleanupJob }> {
  const normalize = operations.normalizeError ?? defaultNormalizeError;
  const wait =
    operations.wait ??
    ((delayMs: number) =>
      new Promise<void>((resolve) => globalThis.setTimeout(resolve, delayMs)));
  const current = { ...job };

  for (
    let attempt = 0;
    attempt <= CLEANUP_RETRY_DELAYS_MS.length;
    attempt += 1
  ) {
    if (!current.ptyClosed && current.ptySessionId) {
      try {
        await operations.closePty(current.ptySessionId);
        current.ptyClosed = true;
        current.lastPtyError = null;
      } catch (error) {
        const normalized = normalize(error);
        if (normalized.code === "PTY_SESSION_NOT_FOUND") {
          current.ptyClosed = true;
          current.lastPtyError = null;
        } else {
          current.lastPtyError = normalized;
        }
      }
    }

    if (!current.sshDisconnected && current.sshSessionId) {
      try {
        await operations.disconnectSsh(current.sshSessionId);
        current.sshDisconnected = true;
        current.lastSshError = null;
      } catch (error) {
        const normalized = normalize(error);
        if (normalized.code === "SSH_SESSION_NOT_FOUND") {
          current.sshDisconnected = true;
          current.lastSshError = null;
        } else {
          current.lastSshError = normalized;
        }
      }
    }

    if (current.ptyClosed && current.sshDisconnected) {
      return { complete: true, job: current };
    }
    const delay = CLEANUP_RETRY_DELAYS_MS[attempt];
    if (delay !== undefined) await wait(delay);
  }

  return { complete: false, job: current };
}
