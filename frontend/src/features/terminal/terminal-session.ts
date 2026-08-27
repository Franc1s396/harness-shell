export type TerminalSessionState =
  | "CONNECTING"
  | "HOST_KEY_REQUIRED"
  | "CONNECTED"
  | "DISCONNECTING"
  | "DISCONNECTED"
  | "FAILED";

export type TerminalSessionModel = {
  tabId: string;
  connectionId: string;
  title: string;
  state: TerminalSessionState;
  sshSessionId: string | null;
  ptySessionId: string | null;
  generation: number;
};

export type SessionBinding = { tabId: string; generation: number };

export type SessionStatusTone =
  | "accent"
  | "warning"
  | "success"
  | "disconnected"
  | "danger";

const keys = {
  CONNECTING: "terminal.states.connecting",
  HOST_KEY_REQUIRED: "terminal.states.hostKeyRequired",
  CONNECTED: "terminal.states.connected",
  DISCONNECTING: "terminal.states.disconnecting",
  DISCONNECTED: "terminal.states.disconnected",
  FAILED: "terminal.states.failed",
} as const satisfies Record<TerminalSessionState, string>;

const tones = {
  CONNECTING: "accent",
  HOST_KEY_REQUIRED: "warning",
  CONNECTED: "success",
  DISCONNECTING: "warning",
  DISCONNECTED: "disconnected",
  FAILED: "danger",
} as const satisfies Record<TerminalSessionState, SessionStatusTone>;

export const sessionActions = (state: TerminalSessionState) => ({
  reconnect: state === "DISCONNECTED" || state === "FAILED",
  disconnect: state === "CONNECTED",
});

export const sessionStatusKey = (state: TerminalSessionState) => keys[state];

export const sessionStatusTone = (state: TerminalSessionState) => tones[state];

export const isCurrentBinding = (
  session: Pick<TerminalSessionModel, "tabId" | "generation">,
  binding: SessionBinding,
) => session.tabId === binding.tabId && session.generation === binding.generation;
