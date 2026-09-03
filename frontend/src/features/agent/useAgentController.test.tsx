// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AgentApi,
  AgentTurnResult,
  ModelApiConfig,
} from "../../api/agent";
import { useAgentPreferencesStore } from "../../stores/agent-preferences-store";
import type { TerminalSessionModel } from "../terminal/terminal-session";
import type { ProviderDraft } from "./provider-config-actions";
import { useAgentController } from "./useAgentController";

const config: ModelApiConfig = {
  api_config_id: "10000000-0000-4000-8000-000000000001",
  display_name: "Production",
  api_type: "RESPONSES",
  base_url: "https://api.example/v1",
  model: "gpt-5",
  api_key_secret_ref: "credential-old",
  enabled: true,
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:00Z",
};

const connectedSession: TerminalSessionModel = {
  tabId: "tab-1",
  connectionId: "connection-1",
  title: "Production",
  state: "CONNECTED",
  sshSessionId: "ssh-1",
  ptySessionId: "pty-1",
  generation: 1,
};

const mockAgentApi = {
  listModelApiConfigs: vi.fn(),
  createModelApiConfig: vi.fn(),
  updateModelApiConfig: vi.fn(),
  deleteModelApiConfig: vi.fn(),
  runAgentTurn: vi.fn(),
} satisfies AgentApi;

let generatedId = 0;
const dependencies = {
  api: mockAgentApi,
  makeId: vi.fn(() => `agent-test-id-${++generatedId}`),
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
};

const providerDraft: ProviderDraft = {
  displayName: "Production",
  apiType: "RESPONSES",
  baseUrl: "https://api.example/v1",
  model: "gpt-5",
  enabled: true,
};

const completedResult = (
  conversationId: string,
  runId: string,
): AgentTurnResult => ({
  conversation_id: conversationId,
  agent_run_id: runId,
  status: "COMPLETED",
  final_text: "ok",
  react_iteration: 1,
  error_code: null,
});

const renderController = (sessions: TerminalSessionModel[]) =>
  renderHook(
    ({ currentSessions }: { currentSessions: TerminalSessionModel[] }) =>
      useAgentController(
        {
          sessions: currentSessions,
          activeTabId: currentSessions[0]?.tabId ?? null,
        },
        dependencies,
      ),
    { initialProps: { currentSessions: sessions } },
  );

const primeTab = async (
  view: ReturnType<typeof renderController>,
  tabId: string,
  message: string,
) => {
  await waitFor(() => expect(view.result.current.configs).toEqual([config]));
  act(() => view.result.current.ensureTab(tabId));
  act(() =>
    view.result.current.selectProvider(tabId, config.api_config_id),
  );
  act(() => view.result.current.changeDraft(tabId, message));
  await act(() => view.result.current.requestSend(tabId));
};

describe("useAgentController", () => {
  beforeEach(() => {
    generatedId = 0;
    vi.resetAllMocks();
    dependencies.makeId.mockImplementation(
      () => `agent-test-id-${++generatedId}`,
    );
    useAgentPreferencesStore.getState().reset();
    mockAgentApi.listModelApiConfigs.mockResolvedValue([config]);
  });

  it("refreshes and freezes Provider plus Session before a non-streaming turn", async () => {
    mockAgentApi.runAgentTurn.mockResolvedValue(
      completedResult("conversation-1", "run-1"),
    );
    const view = renderController([connectedSession]);

    await primeTab(view, "tab-1", "inspect service");
    expect(view.result.current.state.tabs["tab-1"].phase).toBe(
      "AWAITING_RISK_CONFIRMATION",
    );
    await act(() => view.result.current.confirmRiskAndSend("tab-1"));

    expect(mockAgentApi.runAgentTurn).toHaveBeenCalledWith({
      conversationId: null,
      sshSessionId: "ssh-1",
      apiConfigId: config.api_config_id,
      userMessage: "inspect service",
    });
    expect(
      view.result.current.state.tabs["tab-1"].messages[
        view.result.current.state.tabs["tab-1"].messages.length - 1
      ],
    ).toMatchObject({ kind: "assistant", text: "ok" });
  });

  it("allows different tabs to own concurrent non-streaming Runs", async () => {
    const first = deferred<AgentTurnResult>();
    const second = deferred<AgentTurnResult>();
    mockAgentApi.runAgentTurn
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const secondSession = {
      ...connectedSession,
      tabId: "tab-2",
      sshSessionId: "ssh-2",
    };
    const view = renderController([connectedSession, secondSession]);
    await primeTab(view, "tab-1", "inspect one");
    act(() => view.result.current.ensureTab("tab-2"));
    act(() =>
      view.result.current.selectProvider("tab-2", config.api_config_id),
    );
    act(() => view.result.current.changeDraft("tab-2", "inspect two"));
    await act(() => view.result.current.requestSend("tab-2"));

    let sendOne!: Promise<void>;
    let sendTwo!: Promise<void>;
    act(() => {
      sendOne = view.result.current.confirmRiskAndSend("tab-1");
      sendTwo = view.result.current.confirmRiskAndSend("tab-2");
    });
    await waitFor(() =>
      expect(mockAgentApi.runAgentTurn).toHaveBeenCalledTimes(2),
    );
    expect(view.result.current.activeAgentRunCount).toBe(2);
    first.resolve(completedResult("conversation-1", "run-1"));
    second.resolve(completedResult("conversation-2", "run-2"));
    await act(async () => Promise.all([sendOne, sendTwo]));
  });

  it("stops before run_agent_turn when the selected config became disabled", async () => {
    mockAgentApi.listModelApiConfigs
      .mockResolvedValueOnce([config])
      .mockResolvedValueOnce([{ ...config, enabled: false }]);
    const view = renderController([connectedSession]);

    await primeTab(view, "tab-1", "inspect");
    await act(() => view.result.current.confirmRiskAndSend("tab-1"));

    expect(mockAgentApi.runAgentTurn).not.toHaveBeenCalled();
    expect(view.result.current.state.tabs["tab-1"].lastError?.code).toBe(
      "UI_AGENT_PROVIDER_UNAVAILABLE",
    );
  });

  it("drops a late completion after an unexpected tab removal", async () => {
    const pending = deferred<AgentTurnResult>();
    mockAgentApi.runAgentTurn.mockReturnValue(pending.promise);
    const view = renderController([connectedSession]);
    await primeTab(view, "tab-1", "inspect");
    let send!: Promise<void>;
    act(() => {
      send = view.result.current.confirmRiskAndSend("tab-1");
    });
    await waitFor(() =>
      expect(view.result.current.state.tabs["tab-1"].phase).toBe("RUNNING"),
    );
    act(() => view.result.current.removeTab("tab-1"));
    pending.resolve(completedResult("conversation-1", "run-1"));
    await act(() => send);
    expect(view.result.current.state.tabs["tab-1"]).toBeUndefined();
  });

  it("clears a disabled preferred config instead of selecting a fallback", async () => {
    useAgentPreferencesStore
      .getState()
      .setPreferredApiConfigId(config.api_config_id);
    mockAgentApi.listModelApiConfigs.mockResolvedValue([
      { ...config, enabled: false },
    ]);
    const view = renderController([connectedSession]);

    await waitFor(() =>
      expect(view.result.current.configsLoading).toBe(false),
    );
    act(() => view.result.current.ensureTab("tab-1"));
    expect(
      view.result.current.state.tabs["tab-1"].selectedApiConfigId,
    ).toBeNull();
    expect(
      useAgentPreferencesStore.getState().preferredApiConfigId,
    ).toBeNull();
  });

  it("submits a replacement Key through the Provider aggregate and refreshes", async () => {
    mockAgentApi.updateModelApiConfig.mockResolvedValue({
      ...config,
      api_key_secret_ref: "credential-new",
    });
    const view = renderController([connectedSession]);

    await act(() =>
      view.result.current.updateProvider(config, providerDraft, "replacement"),
    );

    expect(mockAgentApi.updateModelApiConfig).toHaveBeenCalledWith(
      config.api_config_id,
      expect.any(Object),
      "replacement",
    );
    expect(mockAgentApi.listModelApiConfigs).toHaveBeenCalled();
  });

  it("does not retry a rejected turn and keeps the prior conversation identity", async () => {
    mockAgentApi.runAgentTurn.mockRejectedValue({
      code: "MODEL_NETWORK_TIMEOUT",
    });
    const view = renderController([connectedSession]);
    await primeTab(view, "tab-1", "inspect");

    await act(() => view.result.current.confirmRiskAndSend("tab-1"));

    expect(mockAgentApi.runAgentTurn).toHaveBeenCalledOnce();
    expect(
      view.result.current.state.tabs["tab-1"].conversationId,
    ).toBeNull();
    expect(view.result.current.state.tabs["tab-1"].lastError?.code).toBe(
      "MODEL_NETWORK_TIMEOUT",
    );
  });

  it("requires risk confirmation again after reconnect changes the SSH Session ID", async () => {
    mockAgentApi.runAgentTurn.mockResolvedValue(
      completedResult("conversation-1", "run-1"),
    );
    const view = renderController([connectedSession]);
    await primeTab(view, "tab-1", "first turn");
    await act(() => view.result.current.confirmRiskAndSend("tab-1"));

    view.rerender({
      currentSessions: [
        { ...connectedSession, sshSessionId: "ssh-reconnected" },
      ],
    });
    act(() => view.result.current.changeDraft("tab-1", "second turn"));
    await act(() => view.result.current.requestSend("tab-1"));

    expect(view.result.current.state.tabs["tab-1"]).toMatchObject({
      phase: "AWAITING_RISK_CONFIRMATION",
      pendingRiskSshSessionId: "ssh-reconnected",
      riskAcknowledgedSshSessionId: "ssh-1",
    });
    expect(mockAgentApi.runAgentTurn).toHaveBeenCalledOnce();
  });
});
