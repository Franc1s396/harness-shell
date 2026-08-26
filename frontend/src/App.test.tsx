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
}));

vi.mock("./api/runtime", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/runtime")>()),
  ...runtimeApi,
}));

vi.mock("./api/ssh", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/ssh")>()),
  ...sshApi,
}));

vi.mock("./features/terminal/TerminalTab", () => ({
  TerminalTab: ({
    enabled,
    onInput,
    onResize,
  }: {
    enabled: boolean;
    onInput: (data: Uint8Array) => Promise<void>;
    onResize: (cols: number, rows: number) => void;
  }) => {
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
    Object.defineProperty(globalThis.crypto, "randomUUID", {
      configurable: true,
      value: vi.fn(() => "tab-test"),
    });
    useWorkspaceUiStore.getState().reset();
    Object.values(runtimeApi).forEach((mock) => mock.mockReset());
    Object.values(sshApi).forEach((mock) => mock.mockReset());
    terminalTabMock.onInput = null;
    terminalTabMock.onResize = null;
    runtimeApi.getRuntimeStatus.mockResolvedValue(runtimeReadyStatus);
    runtimeApi.openApprovalWindow.mockResolvedValue(undefined);
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
  afterEach(cleanup);

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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );

    await waitFor(() =>
      expect(sshApi.disconnectSsh).toHaveBeenCalledWith("ssh-session-test"),
    );
    expect(await screen.findByText("PTY_OPEN_FAILED")).toBeInTheDocument();
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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Disconnect: Test profile/i }),
    );
    await act(async () =>
      rejectOpenPty({ code: "PTY_OPEN_FAILED", message: "open failed" }),
    );

    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(1);
    await act(async () => resolveDisconnect(status({ state: "DISCONNECTED" })));
    expect(await screen.findByText("PTY_OPEN_FAILED")).toBeInTheDocument();
  });

  it("keeps a terminal tab retryable until PTY and SSH cleanup succeed", async () => {
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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    const closeButton = await screen.findByRole("button", {
      name: /Close Test profile/i,
    });
    fireEvent.click(closeButton);

    await waitFor(() => expect(sshApi.closePty).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: /Close Test profile/i })).toBeVisible();
    await expect(
      terminalTabMock.onInput!(new Uint8Array([65])),
    ).rejects.toMatchObject({ code: "PTY_INPUT_BLOCKED" });
    expect(sshApi.writePty).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Close Test profile/i }));
    await waitFor(() => expect(sshApi.closePty).toHaveBeenCalledTimes(2));
    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(2);
  });

  it("does not close the PTY twice when only SSH disconnect needs retry", async () => {
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
    sshApi.disconnectSsh
      .mockRejectedValueOnce({
        code: "SSH_DISCONNECT_FAILED",
        message: "disconnect failed",
      })
      .mockResolvedValueOnce(status({ state: "DISCONNECTED" }));

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Close Test profile/i }),
    );
    await screen.findByText("SSH_DISCONNECT_FAILED");
    fireEvent.click(screen.getByRole("button", { name: /Close Test profile/i }));

    await waitFor(() => expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(2));
    expect(sshApi.closePty).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("button", { name: /Close Test profile/i }),
    ).not.toBeInTheDocument();
  });

  it("disables terminal input immediately while cleanup is pending", async () => {
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
    sshApi.closePty.mockImplementation(() => new Promise(() => undefined));

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Close Test profile/i }),
    );

    expect(screen.getByTestId("terminal-tab")).toHaveAttribute(
      "data-enabled",
      "false",
    );
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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Disconnect: Test profile/i }),
    );

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

  it("coalesces stream-failure and user-close cleanup for the same PTY", async () => {
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
    sshApi.closePty.mockImplementation(() => new Promise(() => undefined));

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    const closeButton = await screen.findByRole("button", {
      name: /Close Test profile/i,
    });
    await waitFor(() => expect(emitSshEvent).toBeTypeOf("function"));
    act(() => {
      emitSshEvent({
        event: "ssh.pty.output",
        pty_session_id: "pty-session-test",
        stream_sequence: 2,
        data_b64: "QQ==",
      });
      emitSshEvent({
        event: "ssh.pty.output",
        pty_session_id: "pty-session-test",
        stream_sequence: 2,
        data_b64: "QQ==",
      });
      fireEvent.click(closeButton);
    });

    await waitFor(() => expect(sshApi.closePty).toHaveBeenCalled());
    expect(sshApi.closePty).toHaveBeenCalledTimes(1);
    expect(sshApi.disconnectSsh).not.toHaveBeenCalled();
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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Close Test profile/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Disconnect: Test profile/i }),
    );
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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Disconnect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Close Test profile/i }),
    );

    await waitFor(() => expect(sshApi.closePty).toHaveBeenCalled());
    expect(sshApi.disconnectSsh).toHaveBeenCalledTimes(1);
    await act(async () => resolveDisconnect(status({ state: "DISCONNECTED" })));
  });

  it("preserves both PTY and SSH cleanup errors and prioritizes SSH", async () => {
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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Close Test profile/i }),
    );

    expect(await screen.findByText("SSH_DISCONNECT_FAILED")).toBeInTheDocument();
    expect(screen.getByText(/PTY_CLOSE_FAILED/)).toBeInTheDocument();
  });

  it("coalesces rapid connect requests for the same profile", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    let resolveInspection!: (value: ConnectionStatus) => void;
    sshApi.inspectHostKey.mockImplementation(
      () => new Promise((resolve) => { resolveInspection = resolve; }),
    );

    render(<App />);
    const connectButton = await screen.findByRole("button", {
      name: /Connect: Test profile/i,
    });
    fireEvent.click(connectButton);
    fireEvent.click(connectButton);

    expect(sshApi.inspectHostKey).toHaveBeenCalledTimes(1);
    resolveInspection(failedInspection("AUTH_FAILED"));
    expect(await screen.findByText("AUTH_FAILED")).toBeInTheDocument();
  });

  it("rejects a different Save & Connect context while a normal connect is active", async () => {
    sshApi.listConnections.mockReset().mockResolvedValue([savedProfile]);
    sshApi.updateConnection.mockResolvedValue({
      ...savedProfile,
      updated_at: "2026-08-26T01:00:00Z",
    });
    let resolveInspection!: (value: ConnectionStatus) => void;
    sshApi.inspectHostKey.mockImplementation(
      () => new Promise((resolve) => { resolveInspection = resolve; }),
    );

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Edit connection: Test profile/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Save & Connect" }),
    );

    expect(
      await screen.findByText("CONNECTION_OPERATION_IN_PROGRESS"),
    ).toBeInTheDocument();
    expect(screen.getByText(/profile saved/i)).toBeInTheDocument();
    expect(sshApi.inspectHostKey).toHaveBeenCalledTimes(1);
    resolveInspection(failedInspection("AUTH_FAILED"));
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
    fireEvent.click(
      await screen.findByRole("button", { name: /Connect: Test profile/i }),
    );
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
