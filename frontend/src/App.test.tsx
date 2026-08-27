// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeStatus } from "./api/runtime";
import type { ConnectionProfile, ConnectionStatus, SshEvent } from "./api/ssh";
import { i18n, i18nReady } from "./i18n";
import { useWorkspaceUiStore } from "./stores/workspace-ui-store";

const runtimeApi = vi.hoisted(() => ({
  getRuntimeStatus: vi.fn(),
  openApprovalWindow: vi.fn(),
}));

const tauriWindow = vi.hoisted(() => ({
  close: vi.fn(),
  onCloseRequested: vi.fn(),
}));

const sshApi = vi.hoisted(() => ({
  closePty: vi.fn(),
  confirmHostKey: vi.fn(),
  connectSsh: vi.fn(),
  createConnection: vi.fn(),
  deleteConnection: vi.fn(),
  disconnectSsh: vi.fn(),
  importPrivateKey: vi.fn(),
  inspectHostKey: vi.fn(),
  listConnections: vi.fn(),
  openPty: vi.fn(),
  replaceHostKey: vi.fn(),
  resizePty: vi.fn(),
  storePrivateKeyPassphrase: vi.fn(),
  storeSshPassword: vi.fn(),
  subscribeSshEvents: vi.fn(),
  updateConnection: vi.fn(),
  writePty: vi.fn(),
}));

const terminalTabMock = vi.hoisted(() => ({
  onInput: null as null | ((data: Uint8Array) => Promise<void>),
  onResize: null as null | ((cols: number, rows: number) => void),
  tabId: null as string | null,
  outputBuffer: null as null | {
    subscribe: (
      tabId: string,
      listener: (data: Uint8Array) => void,
    ) => () => void;
  },
  renderCount: 0,
}));

vi.mock("./api/runtime", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/runtime")>()),
  ...runtimeApi,
}));

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => tauriWindow,
}));

vi.mock("./api/ssh", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/ssh")>()),
  ...sshApi,
}));

vi.mock("./features/terminal/TerminalTab", () => ({
  TerminalTab: ({
    tabId,
    outputBuffer,
    enabled,
    onInput,
    onResize,
  }: {
    tabId: string;
    outputBuffer: {
      subscribe: (
        tabId: string,
        listener: (data: Uint8Array) => void,
      ) => () => void;
    };
    enabled: boolean;
    onInput: (data: Uint8Array) => Promise<void>;
    onResize: (cols: number, rows: number) => void;
  }) => {
    terminalTabMock.renderCount += 1;
    terminalTabMock.tabId = tabId;
    terminalTabMock.outputBuffer = outputBuffer;
    terminalTabMock.onInput = onInput;
    terminalTabMock.onResize = onResize;
    return <div data-testid="terminal-tab" data-enabled={String(enabled)} />;
  },
}));

import App from "./App";

const runtimeReadyStatus: RuntimeStatus = {
  state: "READY",
  error_code: null,
  node: "core",
  recoverable: false,
  correlation_id: "runtime-correlation-test",
  last_sequence: 1,
  last_heartbeat_at: "2026-08-26T00:00:00Z",
};

const savedProfile: ConnectionProfile = {
  connection_id: "connection-test",
  display_name: "Test profile",
  group_name: "Lab",
  host: "test.invalid",
  port: 22,
  username: "tester",
  auth_kind: "password",
  credential_id: "credential-test",
  passphrase_credential_id: null,
  proxy_jump_id: null,
  favorite: false,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
};

const status = (overrides: Partial<ConnectionStatus>): ConnectionStatus => ({
  connection_id: savedProfile.connection_id,
  state: "DISCONNECTED",
  session_id: null,
  error_code: null,
  recoverable: false,
  correlation_id: "correlation-test",
  host_key_candidate: null,
  trusted_fingerprint_sha256: null,
  ...overrides,
});

const failedInspection = (errorCode: string) =>
  status({ state: "FAILED", error_code: errorCode });

const openSavedProfile = async () => {
  fireEvent.doubleClick(
    await screen.findByRole("option", { name: /Test profile/i }),
  );
};

const disconnectSavedProfile = async () => {
  fireEvent.contextMenu(
    await screen.findByRole("tab", { name: /Test profile/i }),
    { clientX: 20, clientY: 40 },
  );
  fireEvent.click(
    await screen.findByRole("menuitem", { name: /Disconnect/i }),
  );
};

const closeSavedSession = async () => {
  fireEvent.click(
    await screen.findByRole("button", { name: /Close Test profile/i }),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Close session" }),
  );
};

const editSavedProfile = async () => {
  fireEvent.click(
    await screen.findByRole("button", {
      name: /Connection actions: Test profile/i,
    }),
  );
  fireEvent.click(
    await screen.findByRole("menuitem", { name: /Edit connection/i }),
  );
};
const hostKeyRequired = status({
  state: "HOST_KEY_REQUIRED",
  host_key_candidate: {
    connection_id: savedProfile.connection_id,
    host: savedProfile.host,
    port: 22,
    key_algorithm: "ssh-ed25519",
    fingerprint_sha256: "SHA256:new",
    public_key_openssh_b64: "AAAATEST",
  },
});
const trustedInspection = status({
  state: "DISCONNECTED",
  trusted_fingerprint_sha256: "SHA256:new",
});

const openAndFillNewConnection = async () => {
  fireEvent.click(
    await screen.findByRole("button", { name: /New connection/i }),
  );
  fireEvent.change(screen.getByLabelText("Connection name"), {
    target: { value: savedProfile.display_name },
  });
  fireEvent.change(screen.getByLabelText("Host"), {
    target: { value: savedProfile.host },
  });
  fireEvent.click(screen.getByRole("tab", { name: "Authentication" }));
  fireEvent.change(screen.getByLabelText("Username"), {
    target: { value: savedProfile.username },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "DUMMY_TEST_SECRET" },
  });
};

describe("App connection orchestration", () => {
  beforeEach(async () => {
    await i18nReady;
    await i18n.changeLanguage("en");
    localStorage.clear();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1440,
    });
    let nextUuid = 0;
    Object.defineProperty(globalThis.crypto, "randomUUID", {
      configurable: true,
      value: vi.fn(() => `uuid-${++nextUuid}`),
    });
    useWorkspaceUiStore.getState().reset();
    Object.values(runtimeApi).forEach((mock) => mock.mockReset());
    Object.values(sshApi).forEach((mock) => mock.mockReset());
    Object.values(tauriWindow).forEach((mock) => mock.mockReset());
    terminalTabMock.onInput = null;
    terminalTabMock.onResize = null;
    terminalTabMock.tabId = null;
    terminalTabMock.outputBuffer = null;
    terminalTabMock.renderCount = 0;
    runtimeApi.getRuntimeStatus.mockResolvedValue(runtimeReadyStatus);
    runtimeApi.openApprovalWindow.mockResolvedValue(undefined);
    tauriWindow.close.mockResolvedValue(undefined);
    tauriWindow.onCloseRequested.mockResolvedValue(() => undefined);
    sshApi.subscribeSshEvents.mockResolvedValue(() => undefined);
    sshApi.storeSshPassword.mockResolvedValue({
      credential_id: savedProfile.credential_id,
      kind: "ssh_password",
    });
    sshApi.createConnection.mockResolvedValue(savedProfile);
    sshApi.listConnections
      .mockResolvedValueOnce([])
      .mockResolvedValue([savedProfile]);
    sshApi.confirmHostKey.mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("persists, closes, then inspects Host Key for Save & Connect", async () => {
    sshApi.inspectHostKey.mockResolvedValue(failedInspection("AUTH_FAILED"));
    render(<App />);
    await openAndFillNewConnection();
    fireEvent.click(screen.getByRole("button", { name: "Save & Connect" }));

    await waitFor(() =>
      expect(sshApi.inspectHostKey).toHaveBeenCalledWith(
        savedProfile.connection_id,
      ),
    );
    expect(sshApi.createConnection.mock.invocationCallOrder[0]).toBeLessThan(
      sshApi.inspectHostKey.mock.invocationCallOrder[0],
    );
    expect(
      screen.queryByRole("dialog", { name: /New connection/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/profile saved/i)).toBeInTheDocument();
    expect(screen.getByText("AUTH_FAILED")).toBeInTheDocument();
    expect(screen.getAllByText(savedProfile.display_name).length).toBeGreaterThan(0);
  });

  it("carries partial-success context through Host Key confirmation", async () => {
    sshApi.inspectHostKey
      .mockResolvedValueOnce(hostKeyRequired)
      .mockResolvedValueOnce(trustedInspection);
    sshApi.connectSsh.mockRejectedValue({
      code: "AUTH_FAILED",
      message: "denied",
    });
    render(<App />);
    await openAndFillNewConnection();
    fireEvent.click(screen.getByRole("button", { name: "Save & Connect" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Trust and connect" }),
    );

    expect(await screen.findByText(/profile saved/i)).toBeInTheDocument();
    expect(screen.getByText("AUTH_FAILED")).toBeInTheDocument();
  });

  it("disconnects SSH when opening the PTY fails", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockRejectedValue({
      code: "PTY_OPEN_FAILED",
      message: "open failed",
    });
    sshApi.disconnectSsh.mockResolvedValue(status({ state: "DISCONNECTED" }));

    render(<App />);
    await openSavedProfile();

    await waitFor(() =>
      expect(sshApi.disconnectSsh).toHaveBeenCalledWith("ssh-session-test"),
    );
    expect(await screen.findByText("PTY_OPEN_FAILED")).toBeInTheDocument();
  });

  it("creates independent sessions from the same connection profile", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh
      .mockResolvedValueOnce(status({ state: "READY", session_id: "ssh-1" }))
      .mockResolvedValueOnce(status({ state: "READY", session_id: "ssh-2" }));
    sshApi.openPty
      .mockResolvedValueOnce({
        pty_session_id: "pty-1",
        ssh_session_id: "ssh-1",
        connection_id: savedProfile.connection_id,
        cols: 80,
        rows: 24,
        state: "OPEN",
      })
      .mockResolvedValueOnce({
        pty_session_id: "pty-2",
        ssh_session_id: "ssh-2",
        connection_id: savedProfile.connection_id,
        cols: 80,
        rows: 24,
        state: "OPEN",
      });

    render(<App />);
    await openSavedProfile();
    await openSavedProfile();

    await waitFor(() => expect(sshApi.connectSsh).toHaveBeenCalledTimes(2));
    expect(
      screen.getAllByRole("tab", { name: /Test profile/i }),
    ).toHaveLength(2);
  });

  it("reconnects one tab in place and rejects output from its old PTY", async () => {
    let emitSshEvent!: (event: SshEvent) => void;
    sshApi.subscribeSshEvents.mockImplementation(async (onEvent) => {
      emitSshEvent = onEvent;
      return () => undefined;
    });
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh
      .mockResolvedValueOnce(
        status({ state: "READY", session_id: "ssh-before" }),
      )
      .mockResolvedValueOnce(
        status({ state: "READY", session_id: "ssh-after" }),
      );
    sshApi.openPty
      .mockResolvedValueOnce({
        pty_session_id: "pty-before",
        ssh_session_id: "ssh-before",
        connection_id: savedProfile.connection_id,
        cols: 80,
        rows: 24,
        state: "OPEN",
      })
      .mockResolvedValueOnce({
        pty_session_id: "pty-after",
        ssh_session_id: "ssh-after",
        connection_id: savedProfile.connection_id,
        cols: 80,
        rows: 24,
        state: "OPEN",
      });
    sshApi.closePty.mockResolvedValue({ state: "CLOSED" });
    sshApi.disconnectSsh.mockResolvedValue(status({ state: "DISCONNECTED" }));

    render(<App />);
    await openSavedProfile();
    const tab = await screen.findByRole("tab", { name: /Test profile/i });
    await waitFor(() => expect(emitSshEvent).toBeTypeOf("function"));
    act(() => {
      emitSshEvent({
        event: "ssh.pty.output",
        pty_session_id: "pty-before",
        stream_sequence: 1,
        data_b64: "YmVmb3Jl",
      });
    });

    fireEvent.contextMenu(tab, { clientX: 20, clientY: 40 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Disconnect" }));
    await waitFor(() =>
      expect(screen.getByTestId("active-session-status")).toHaveTextContent(
        "Disconnected",
      ),
    );
    fireEvent.contextMenu(tab, { clientX: 20, clientY: 40 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Reconnect" }));
    await waitFor(() => expect(sshApi.connectSsh).toHaveBeenCalledTimes(2));

    act(() => {
      emitSshEvent({
        event: "ssh.pty.output",
        pty_session_id: "pty-before",
        stream_sequence: 2,
        data_b64: "bGF0ZS1vbGQ=",
      });
      emitSshEvent({
        event: "ssh.pty.output",
        pty_session_id: "pty-after",
        stream_sequence: 1,
        data_b64: "YWZ0ZXI=",
      });
    });

    const output: string[] = [];
    const unsubscribe = terminalTabMock.outputBuffer!.subscribe(
      terminalTabMock.tabId!,
      (data) => output.push(new TextDecoder().decode(data)),
    );
    expect(output).toEqual([
      "before",
      "\r\n── Reconnected ──\r\n",
      "after",
    ]);
    expect(
      screen.getAllByRole("tab", { name: /Test profile/i }),
    ).toHaveLength(1);
    unsubscribe();
  });

  it("removes a confirmed tab before its background cleanup resolves", async () => {
    let resolveClose!: () => void;
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });
    sshApi.closePty.mockImplementation(
      () => new Promise<void>((resolve) => { resolveClose = resolve; }),
    );
    sshApi.disconnectSsh.mockResolvedValue(status({ state: "DISCONNECTED" }));

    render(<App />);
    await openSavedProfile();
    await screen.findByRole("tab", { name: /Test profile/i });
    await closeSavedSession();

    expect(
      screen.queryByRole("tab", { name: /Test profile/i }),
    ).not.toBeInTheDocument();
    expect(sshApi.disconnectSsh).not.toHaveBeenCalled();
    await act(async () => resolveClose());
    await waitFor(() =>
      expect(sshApi.disconnectSsh).toHaveBeenCalledWith("ssh-session-test"),
    );
  });

  it("keeps final cleanup failures visible and retries unfinished steps", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });
    sshApi.closePty.mockRejectedValue({
      code: "PTY_CLOSE_FAILED",
      message: "close failed",
    });
    sshApi.disconnectSsh.mockRejectedValue({
      code: "SSH_DISCONNECT_FAILED",
      message: "disconnect failed",
    });

    render(<App />);
    await openSavedProfile();
    await screen.findByRole("tab", { name: /Test profile/i });
    vi.useFakeTimers();
    fireEvent.click(
      screen.getByRole("button", { name: /Close Test profile/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close session" }));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));

    expect(screen.getByText(/Could not finish cleaning up Test profile/)).toBeVisible();
    expect(screen.getByText(/PTY_CLOSE_FAILED/)).toBeVisible();
    expect(screen.getByText(/SSH_DISCONNECT_FAILED/)).toBeVisible();
    expect(sshApi.closePty).toHaveBeenCalledTimes(3);
    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(3);

    sshApi.closePty.mockResolvedValue({ state: "CLOSED" });
    sshApi.disconnectSsh.mockResolvedValue(status({ state: "DISCONNECTED" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry cleanup" }));
    await act(async () => Promise.resolve());
    expect(
      screen.queryByText(/Could not finish cleaning up Test profile/),
    ).not.toBeInTheDocument();
    expect(sshApi.closePty).toHaveBeenCalledTimes(4);
    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(4);
  });

  it("cleans late PTY results without restoring a closed connecting tab", async () => {
    let resolveOpenPty!: (value: {
      pty_session_id: string;
      ssh_session_id: string;
      connection_id: string;
      cols: number;
      rows: number;
      state: "OPEN";
    }) => void;
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockImplementation(
      () => new Promise((resolve) => { resolveOpenPty = resolve; }),
    );
    sshApi.closePty.mockResolvedValue({ state: "CLOSED" });
    sshApi.disconnectSsh.mockResolvedValue(status({ state: "DISCONNECTED" }));

    render(<App />);
    await openSavedProfile();
    await waitFor(() => expect(sshApi.openPty).toHaveBeenCalled());
    await closeSavedSession();
    expect(
      screen.queryByRole("tab", { name: /Test profile/i }),
    ).not.toBeInTheDocument();

    await act(async () =>
      resolveOpenPty({
        pty_session_id: "pty-session-test",
        ssh_session_id: "ssh-session-test",
        connection_id: savedProfile.connection_id,
        cols: 80,
        rows: 24,
        state: "OPEN",
      }),
    );
    await waitFor(() =>
      expect(sshApi.closePty).toHaveBeenCalledWith("pty-session-test"),
    );
    expect(sshApi.disconnectSsh).toHaveBeenCalledWith("ssh-session-test");
    expect(
      screen.queryByRole("tab", { name: /Test profile/i }),
    ).not.toBeInTheDocument();
  });

  it("shares one SSH disconnect between PTY-open cleanup and Explorer", async () => {
    let rejectOpenPty!: (error: unknown) => void;
    let resolveDisconnect!: (value: ConnectionStatus) => void;
    const openTask = new Promise((_, reject) => {
      rejectOpenPty = reject;
    });
    const disconnectTask = new Promise<ConnectionStatus>((resolve) => {
      resolveDisconnect = resolve;
    });
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockImplementation(() => openTask);
    sshApi.disconnectSsh.mockImplementation(() => disconnectTask);

    render(<App />);
    await openSavedProfile();
    await disconnectSavedProfile();
    await act(async () =>
      rejectOpenPty({ code: "PTY_OPEN_FAILED", message: "open failed" }),
    );

    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(1);
    await act(async () => resolveDisconnect(status({ state: "DISCONNECTED" })));
    expect(await screen.findByText("PTY_OPEN_FAILED")).toBeInTheDocument();
  });

  it("blocks terminal input and resize immediately while Explorer disconnect is pending", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });
    sshApi.writePty.mockResolvedValue(1);
    sshApi.resizePty.mockResolvedValue({ state: "OPEN" });
    sshApi.disconnectSsh.mockImplementation(() => new Promise(() => undefined));

    render(<App />);
    await openSavedProfile();
    await disconnectSavedProfile();

    expect(screen.getByTestId("terminal-tab")).toHaveAttribute(
      "data-enabled",
      "false",
    );
    await expect(
      terminalTabMock.onInput!(new Uint8Array([65])),
    ).rejects.toMatchObject({ code: "PTY_INPUT_BLOCKED" });
    terminalTabMock.onResize!(100, 30);
    expect(sshApi.writePty).not.toHaveBeenCalled();
    expect(sshApi.resizePty).not.toHaveBeenCalled();
  });

  it("does not rerender the React terminal wrapper for PTY output", async () => {
    let emitSshEvent!: (event: SshEvent) => void;
    sshApi.subscribeSshEvents.mockImplementation(async (onEvent) => {
      emitSshEvent = onEvent;
      return () => undefined;
    });
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });

    render(<App />);
    await openSavedProfile();
    await screen.findByTestId("terminal-tab");
    await waitFor(() => expect(emitSshEvent).toBeTypeOf("function"));
    const renderCountBeforeOutput = terminalTabMock.renderCount;

    act(() => {
      emitSshEvent({
        event: "ssh.pty.output",
        pty_session_id: "pty-session-test",
        stream_sequence: 1,
        data_b64: "QQ==",
      });
    });

    expect(terminalTabMock.renderCount).toBe(renderCountBeforeOutput);
  });

  it("shares one SSH disconnect when tab cleanup starts before Explorer disconnect", async () => {
    let resolveClose!: (value: { state: "CLOSED" }) => void;
    let resolveDisconnect!: (value: ConnectionStatus) => void;
    const disconnectTask = new Promise<ConnectionStatus>((resolve) => {
      resolveDisconnect = resolve;
    });
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });
    sshApi.closePty.mockImplementation(
      () => new Promise((resolve) => { resolveClose = resolve; }),
    );
    sshApi.disconnectSsh.mockImplementation(() => disconnectTask);

    render(<App />);
    await openSavedProfile();
    fireEvent.click(
      await screen.findByRole("button", { name: /Close Test profile/i }),
    );
    await disconnectSavedProfile();
    await act(async () => resolveClose({ state: "CLOSED" }));

    await waitFor(() => expect(sshApi.disconnectSsh).toHaveBeenCalled());
    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(1);
    await act(async () => resolveDisconnect(status({ state: "DISCONNECTED" })));
  });

  it("shares one SSH disconnect when Explorer disconnect starts before tab cleanup", async () => {
    let resolveDisconnect!: (value: ConnectionStatus) => void;
    const disconnectTask = new Promise<ConnectionStatus>((resolve) => {
      resolveDisconnect = resolve;
    });
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });
    sshApi.closePty.mockResolvedValue({ state: "CLOSED" });
    sshApi.disconnectSsh.mockImplementation(() => disconnectTask);

    render(<App />);
    await openSavedProfile();
    await disconnectSavedProfile();
    fireEvent.click(
      await screen.findByRole("button", { name: /Close Test profile/i }),
    );

    await waitFor(() => expect(sshApi.closePty).toHaveBeenCalled());
    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(1);
    await act(async () => resolveDisconnect(status({ state: "DISCONNECTED" })));
  });

  it("keeps rapid open requests independent", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    const resolveInspections: Array<(value: ConnectionStatus) => void> = [];
    sshApi.inspectHostKey.mockImplementation(
      () => new Promise((resolve) => { resolveInspections.push(resolve); }),
    );

    render(<App />);
    const connectionRow = await screen.findByRole("option", {
      name: /Test profile/i,
    });
    fireEvent.doubleClick(connectionRow);
    fireEvent.doubleClick(connectionRow);

    await waitFor(() => expect(sshApi.inspectHostKey).toHaveBeenCalledTimes(2));
    expect(
      screen.getAllByRole("tab", { name: /Test profile/i }),
    ).toHaveLength(2);
    await act(async () => {
      for (const resolve of resolveInspections) {
        resolve(failedInspection("AUTH_FAILED"));
      }
    });
  });

  it("allows Save & Connect to create another session while a normal connect is active", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.updateConnection.mockResolvedValue({
      ...savedProfile,
      updated_at: "2026-08-26T01:00:00Z",
    });
    let resolveFirstInspection!: (value: ConnectionStatus) => void;
    sshApi.inspectHostKey
      .mockImplementationOnce(
        () => new Promise((resolve) => { resolveFirstInspection = resolve; }),
      )
      .mockResolvedValueOnce(failedInspection("AUTH_FAILED"));

    render(<App />);
    await openSavedProfile();
    await editSavedProfile();
    fireEvent.click(
      await screen.findByRole("button", { name: "Save & Connect" }),
    );

    await waitFor(() => expect(sshApi.inspectHostKey).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/profile saved/i)).toBeInTheDocument();
    expect(
      screen.queryByText("CONNECTION_OPERATION_IN_PROGRESS"),
    ).not.toBeInTheDocument();
    expect(
      screen.getAllByRole("tab", { name: /Test profile/i }),
    ).toHaveLength(2);
    await act(async () => {
      resolveFirstInspection(failedInspection("AUTH_FAILED"));
    });
  });

  it("surfaces approval-window launch failures", async () => {
    runtimeApi.openApprovalWindow.mockRejectedValue({
      code: "APPROVAL_WINDOW_FAILED",
      message: "window failed",
    });
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: /Approval/i }))[0]);

    expect(await screen.findByText("APPROVAL_WINDOW_FAILED")).toBeInTheDocument();
  });

  it("places connection failures beside the selected navigator row with explicit recovery actions", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(failedInspection("AUTH_FAILED"));
    render(<App />);

    await openSavedProfile();

    const errorCode = await screen.findByText("AUTH_FAILED");
    const alert = errorCode.closest('[role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert?.closest("aside")).not.toBeNull();
    expect(alert?.closest('[data-testid="terminal-region"]')).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Edit connection" }),
    ).toBeEnabled();
  });

  it("places terminal write failures inside the terminal workspace without removing xterm", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });
    sshApi.writePty.mockRejectedValue({
      code: "PTY_WRITE_FAILED",
      message: "write failed",
    });
    render(<App />);
    await openSavedProfile();
    await screen.findByTestId("terminal-tab");

    await act(async () => {
      await terminalTabMock.onInput!(new Uint8Array([65]));
    });

    const errorCode = await screen.findByText("PTY_WRITE_FAILED");
    expect(
      errorCode.closest('[data-testid="terminal-region"]'),
    ).not.toBeNull();
    expect(screen.getByTestId("terminal-tab")).toBeInTheDocument();
  });

  it("blocks runtime interactions while preserving rendered terminal UI", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey.mockResolvedValue(trustedInspection);
    sshApi.connectSsh.mockResolvedValue(
      status({ state: "READY", session_id: "ssh-session-test" }),
    );
    sshApi.openPty.mockResolvedValue({
      pty_session_id: "pty-session-test",
      ssh_session_id: "ssh-session-test",
      connection_id: savedProfile.connection_id,
      cols: 80,
      rows: 24,
      state: "OPEN",
    });
    runtimeApi.openApprovalWindow.mockRejectedValue({
      code: "APPROVAL_WINDOW_FAILED",
      message: "window failed",
      details: { correlation_id: "corr-approval" },
    });
    render(<App />);
    await openSavedProfile();
    await screen.findByTestId("terminal-tab");

    fireEvent.click(
      (await screen.findAllByRole("button", { name: /Approval/i }))[0],
    );

    const runtimeTitle = await screen.findByText("Runtime unavailable");
    const runtimeAlert = runtimeTitle.closest('[role="alert"]');
    expect(runtimeAlert).not.toBeNull();
    expect(runtimeAlert).toHaveTextContent("Runtime unavailable");
    expect(runtimeAlert).toHaveTextContent("APPROVAL_WINDOW_FAILED");
    expect(runtimeAlert).toHaveTextContent("corr-approval");
    expect(screen.getByTestId("terminal-tab")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "New connection" }),
    ).toBeDisabled();
  });

  it("re-inspects and refreshes a stale Host Key replacement prompt", async () => {
    const changed = status({
      state: "HOST_KEY_REQUIRED",
      trusted_fingerprint_sha256: "SHA256:old",
      host_key_candidate: {
        ...hostKeyRequired.host_key_candidate!,
        fingerprint_sha256: "SHA256:first-new",
      },
    });
    const refreshed = status({
      state: "HOST_KEY_REQUIRED",
      trusted_fingerprint_sha256: "SHA256:middle",
      host_key_candidate: {
        ...hostKeyRequired.host_key_candidate!,
        fingerprint_sha256: "SHA256:latest",
      },
    });
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.inspectHostKey
      .mockResolvedValueOnce(changed)
      .mockResolvedValueOnce(refreshed);
    sshApi.replaceHostKey.mockRejectedValue({
      code: "HOST_KEY_REPLACE_CONFLICT",
      message: "active host key changed",
      details: { correlation_id: "host-key-correlation", recoverable: true },
    });

    render(<App />);
    await openSavedProfile();
    fireEvent.click(
      await screen.findByRole("button", { name: "Replace trusted key" }),
    );

    expect(await screen.findByText("SHA256:latest")).toBeInTheDocument();
    expect(screen.getByText("SHA256:middle")).toBeInTheDocument();
    expect(
      screen.getAllByText("HOST_KEY_REPLACE_CONFLICT").length,
    ).toBeGreaterThanOrEqual(1);
    expect(sshApi.inspectHostKey).toHaveBeenCalledTimes(2);

    sshApi.replaceHostKey.mockResolvedValue(undefined);
    sshApi.inspectHostKey.mockResolvedValueOnce(failedInspection("AUTH_FAILED"));
    fireEvent.click(screen.getByRole("button", { name: "Replace trusted key" }));
    await waitFor(() =>
      expect(sshApi.replaceHostKey).toHaveBeenLastCalledWith(
        refreshed.host_key_candidate,
        "SHA256:middle",
      ),
    );
  });
});
