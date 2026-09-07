// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import "../../i18n";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelApiConfig } from "../../api/agent";
import { agentReducer, type AgentTabState, type AgentState } from "./agent-state";
import { AgentWorkspace, type AgentWorkspaceProps } from "./AgentWorkspace";

const config: ModelApiConfig = {
  api_config_id: "config-1",
  display_name: "Production",
  api_type: "RESPONSES",
  context_window_size: 128000,
  context_compaction_threshold_ratio: 0.75,
  max_output_tokens: 8192,
  base_url: "https://api.example/v1",
  model: "gpt-5",
  api_key_secret_ref: "credential-ref",
  enabled: true,
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:00Z",
};

const idleTab: AgentTabState = {
  conversationId: null,
  messages: [],
  draft: "inspect the service",
  phase: "IDLE",
  selectedApiConfigId: "config-1",
  activeRun: null,
  pendingRiskSshSessionId: null,
  riskAcknowledgedSshSessionId: null,
  lastError: null,
  backgroundState: "NONE",
};

const runningTab: AgentTabState = {
  ...idleTab,
  draft: "",
  phase: "RUNNING",
  activeRun: {
    requestToken: "request-1",
    sshSessionId: "ssh-1",
    provider: {
      apiConfigId: "config-1",
      displayName: "Production",
      apiType: "RESPONSES",
      baseUrl: "https://api.example/v1",
      model: "gpt-5",
      updatedAt: "2026-08-31T00:00:00Z",
    },
    conversationId: "conversation-1",
    agentRunId: "run-1",
    nextSequence: 1,
    streamedText: "",
    toolExecuting: false,
    tools: [],
    reactIteration: 0,
  },
  backgroundState: "RUNNING",
};

const renderWorkspace = (
  overrides: Partial<AgentWorkspaceProps> & {
    tab?: AgentTabState | null;
    configs?: ModelApiConfig[];
  } = {},
) => {
  const props: AgentWorkspaceProps = {
    width: 480,
    tabTitle: "Tab production",
    tab: idleTab,
    configs: [config],
    configsLoading: false,
    onCollapse: vi.fn(),
    onDraftChange: vi.fn(),
    onProviderSelect: vi.fn(),
    onOpenProviderSettings: vi.fn(),
    onRequestSend: vi.fn(),
    onCancelTurn: vi.fn(),
    onConfirmRiskAndSend: vi.fn(),
    onCancelRisk: vi.fn(),
    onResetConversation: vi.fn(),
    onMarkRead: vi.fn(),
    ...overrides,
  };
  return { props, view: render(<AgentWorkspace {...props} />) };
};

describe("AgentWorkspace", () => {
  afterEach(cleanup);

  it("renders the single-box composer with the confirmed send button", () => {
    renderWorkspace();

    const send = screen.getByRole("button", { name: "Send message" });
    expect(send).toHaveClass(
      "size-[26px]",
      "rounded-full",
      "bg-white",
      "text-black",
    );
    expect(send.querySelector("svg")).toHaveClass("size-4");
    expect(screen.getByRole("combobox", { name: "Provider" })).toHaveTextContent(
      "Production · gpt-5",
    );
  });

  it("draws the composer focus indicator on the rounded container", () => {
    renderWorkspace();
    const input = screen.getByRole("textbox", { name: "Message" });
    const composer = input.parentElement;

    expect(input).toHaveClass("focus-visible:outline-hidden!");
    expect(composer).toHaveClass(
      "rounded-xl",
      "focus-within:border-accent",
      "focus-within:ring-1",
      "focus-within:ring-accent/50",
    );
  });

  it("switches the original send button to cancel while running", () => {
    renderWorkspace({ tab: runningTab });

    expect(screen.queryByRole("button", { name: "Send message" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel response" })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: /approve|resume/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/tool call|stdout|stderr/i)).not.toBeInTheDocument();
  });

  it("uses the same button for cancellation and restores sending afterward", () => {
    const workspace = renderWorkspace();
    const button = screen.getByRole("button", { name: "Send message" });
    workspace.view.rerender(<AgentWorkspace {...workspace.props} tab={runningTab} />);
    expect(screen.getByRole("button", { name: "Cancel response" })).toBe(button);
    fireEvent.click(button);
    expect(workspace.props.onCancelTurn).toHaveBeenCalledOnce();
    expect(workspace.props.onRequestSend).not.toHaveBeenCalled();
    workspace.view.rerender(<AgentWorkspace {...workspace.props} tab={{
      ...idleTab, messages: [{ id: "cancelled-1", kind: "cancelled" }],
    }} />);
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBe(button);
    fireEvent.click(button);
    expect(workspace.props.onRequestSend).toHaveBeenCalledOnce();
  });

  it("shows a transient thinking status only while the Run is active", () => {
    const workspace = renderWorkspace({ tab: runningTab });
    const status = screen.getByRole("status");

    expect(status).toHaveTextContent("Thinking…");
    expect(status.querySelector("[aria-hidden='true']")).toHaveClass(
      "animate-spin",
      "motion-reduce:animate-none",
    );

    workspace.view.rerender(
      <AgentWorkspace {...workspace.props} tab={idleTab} />,
    );

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps a loading icon alongside provisional text until the Run ends", () => {
    const workspace = renderWorkspace({ tab: runningTab });
    expect(screen.getByRole("status")).toHaveTextContent("Thinking…");

    workspace.view.rerender(
      <AgentWorkspace
        {...workspace.props}
        tab={{
          ...runningTab,
          activeRun: {
            ...runningTab.activeRun!,
            nextSequence: 2,
            streamedText: "hello",
          },
        }}
      />,
    );

    const provisional = screen.getByText("hello").closest("article");
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
    expect(provisional).toHaveAttribute("data-provisional", "true");
    expect(provisional?.querySelector(".animate-spin")).toBeInTheDocument();
    expect(provisional).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText(/Run details/)).not.toBeInTheDocument();
    workspace.view.rerender(<AgentWorkspace {...workspace.props} tab={idleTab} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("scrolls when the same provisional bubble receives another delta", () => {
    const first = {
      ...runningTab,
      activeRun: {
        ...runningTab.activeRun!,
        nextSequence: 2,
        streamedText: "hel",
      },
    };
    const workspace = renderWorkspace({ tab: first });
    const messageList = screen.getByText("hel").closest("article")?.parentElement;
    expect(messageList).not.toBeNull();
    Object.defineProperty(messageList!, "scrollHeight", {
      configurable: true,
      value: 640,
    });
    messageList!.scrollTop = 0;

    workspace.view.rerender(
      <AgentWorkspace
        {...workspace.props}
        tab={{
          ...first,
          activeRun: {
            ...first.activeRun!,
            nextSequence: 3,
            streamedText: "hello",
          },
        }}
      />,
    );

    expect(messageList!.scrollTop).toBe(640);
  });

  it("sizes user, assistant, error, and thinking bubbles to their content", () => {
    renderWorkspace({
      tab: {
        ...runningTab,
        messages: [
          { id: "user-1", kind: "user", text: "Short user message" },
          {
            id: "assistant-1",
            kind: "assistant", tools: [],
            text: "Short answer",
            run: {
              agentRunId: "run-1",
              status: "COMPLETED",
              reactIteration: 1,
              sshSessionId: "ssh-1",
              provider: runningTab.activeRun!.provider,
            },
          },
          {
            id: "error-1",
            kind: "error",
            error: { code: "MODEL_FAILED", message: "Model failed." },
            run: null,
          },
        ],
      },
    });

    const userBubble = screen.getByText("Short user message").closest("article");
    const assistantBubble = screen.getByText("Short answer").closest("article");
    const errorBubble = screen.getByRole("alert");
    const thinkingBubble = screen.getByRole("status");

    for (const bubble of [
      userBubble,
      assistantBubble,
      errorBubble,
      thinkingBubble,
    ]) {
      expect(bubble).toHaveClass("w-fit", "max-w-[88%]");
    }
    expect(userBubble).toHaveClass("ml-auto");
  });

  it("shows the received Agent error code and message without i18n replacement", () => {
    renderWorkspace({
      tab: {
        ...idleTab,
        messages: [
          {
            id: "error-1",
            kind: "error",
            error: {
              code: "BACKEND_AGENT_STREAM_INVALID",
              message: "Backend rejected Agent frame sequence 7.",
            },
            run: null,
          },
        ],
      },
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(
      "error_code: BACKEND_AGENT_STREAM_INVALID",
    );
    expect(alert).toHaveTextContent(
      "error_message: Backend rejected Agent frame sequence 7.",
    );
    expect(alert).not.toHaveTextContent("Agent stream used an invalid protocol.");
  });

  it("uses Enter to send, Shift+Enter for newline, and ignores composing Enter", () => {
    const onRequestSend = vi.fn();
    renderWorkspace({ onRequestSend });
    const input = screen.getByRole("textbox", { name: "Message" });

    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(onRequestSend).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRequestSend).toHaveBeenCalledOnce();
  });

  it("shows only enabled Providers and exposes Settings when none are available", () => {
    const onOpenProviderSettings = vi.fn();
    const view = renderWorkspace({
      configs: [{ ...config, enabled: false }],
      onOpenProviderSettings,
    });
    fireEvent.click(screen.getByRole("combobox", { name: "Provider" }));
    fireEvent.click(screen.getByRole("button", { name: "Provider settings" }));
    expect(onOpenProviderSettings).toHaveBeenCalledOnce();

    view.view.rerender(
      <AgentWorkspace {...view.props} tab={{ ...idleTab, selectedApiConfigId: null }} configs={[config]} />,
    );
    fireEvent.click(screen.getByRole("combobox", { name: "Provider" }));
    expect(screen.getByRole("option", { name: "Production · gpt-5" })).toBeVisible();
  });

  it("renders assistant Run details from the sent snapshot", () => {
    renderWorkspace({
      tab: {
        ...idleTab,
        draft: "",
        messages: [
          {
            id: "assistant-1",
            kind: "assistant", tools: [],
            text: "Service is healthy.",
            run: {
              agentRunId: "run-1",
              status: "COMPLETED",
              reactIteration: 1,
              sshSessionId: "ssh-1",
              provider: {
                apiConfigId: "config-old",
                displayName: "Sent Provider",
                apiType: "CHAT_COMPLETIONS",
                baseUrl: "https://sent.example/v1",
                model: "sent-model",
                updatedAt: "2026-08-31T00:00:00Z",
              },
            },
          },
        ],
      },
    });

    expect(screen.getByText("Service is healthy.")).toBeVisible();
    fireEvent.click(screen.getByText(/Run details/));
    expect(screen.getByText("Sent Provider")).toBeVisible();
    expect(screen.getByText("sent-model")).toBeVisible();
  });

  it("renders completed assistant answers as safe GitHub-flavored Markdown", () => {
    renderWorkspace({
      tab: {
        ...idleTab,
        draft: "",
        messages: [
          {
            id: "assistant-markdown",
            kind: "assistant", tools: [],
            text: [
              "## Result",
              "",
              "- **healthy**",
              "- ~~deprecated~~",
              "",
              "| Service | Status |",
              "| --- | --- |",
              "| API | online |",
              "",
              "[Documentation](https://example.com/docs)",
              "",
              "![remote status](https://example.com/status.png)",
              "",
              '<img src="invalid" onerror="alert(1)">',
            ].join("\n"),
            run: {
              agentRunId: "run-markdown",
              status: "COMPLETED",
              reactIteration: 1,
              sshSessionId: "ssh-1",
              provider: runningTab.activeRun!.provider,
            },
          },
        ],
      },
    });

    expect(
      screen.getByRole("heading", { level: 2, name: "Result" }),
    ).toBeVisible();
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getByText("deprecated").closest("del")).not.toBeNull();
    expect(screen.getByRole("link", { name: "Documentation" })).toHaveAttribute(
      "rel",
      "noreferrer noopener",
    );
    expect(screen.getByText("remote status")).toBeVisible();
    expect(document.querySelector("img")).toBeNull();
  });

  it("renders a provisional assistant answer with Markdown semantics", () => {
    renderWorkspace({
      tab: {
        ...runningTab,
        activeRun: {
          ...runningTab.activeRun!,
          nextSequence: 2,
          streamedText: "### Checking\n\n`service --status`",
        },
      },
    });

    const heading = screen.getByRole("heading", {
      level: 3,
      name: "Checking",
    });
    expect(heading.closest("article")).toHaveAttribute(
      "data-provisional",
      "true",
    );
    expect(screen.getByText("service --status").closest("code")).not.toBeNull();
  });

  it("renders a local Agent stream error without i18n replacement", () => {
    renderWorkspace({
      tab: {
        ...idleTab,
        messages: [
          {
            id: "error-1",
            kind: "error",
            error: {
              code: "BACKEND_AGENT_STREAM_INVALID",
              message: "BACKEND_AGENT_STREAM_INVALID",
            },
            run: null,
          },
        ],
      },
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(
      "error_code: BACKEND_AGENT_STREAM_INVALID",
    );
    expect(alert).toHaveTextContent(
      "error_message: BACKEND_AGENT_STREAM_INVALID",
    );
    expect(alert).not.toHaveTextContent(
      "The Agent stream used an invalid protocol.",
    );
  });

  it("scrolls the conversation to the bottom when a message is appended", () => {
    const firstMessage = {
      id: "user-1",
      kind: "user" as const,
      text: "First message",
    };
    const workspace = renderWorkspace({
      tab: {
        ...idleTab,
        draft: "",
        messages: [firstMessage],
      },
    });
    const messageList = screen.getByText("First message").parentElement;

    expect(messageList).not.toBeNull();
    Object.defineProperty(messageList!, "scrollHeight", {
      configurable: true,
      value: 640,
    });
    messageList!.scrollTop = 0;

    workspace.view.rerender(
      <AgentWorkspace
        {...workspace.props}
        tab={{
          ...idleTab,
          draft: "",
          messages: [
            firstMessage,
            { id: "assistant-1", kind: "user", text: "Second message" },
          ],
        }}
      />,
    );

    expect(messageList!.scrollTop).toBe(640);
  });

  it("does not change the conversation scroll position when only the draft changes", () => {
    const messages = [
      { id: "user-1", kind: "user" as const, text: "Existing message" },
    ];
    const workspace = renderWorkspace({
      tab: { ...idleTab, draft: "initial draft", messages },
    });
    const messageList = screen.getByText("Existing message").parentElement;

    expect(messageList).not.toBeNull();
    Object.defineProperty(messageList!, "scrollHeight", {
      configurable: true,
      value: 640,
    });
    messageList!.scrollTop = 125;

    workspace.view.rerender(
      <AgentWorkspace
        {...workspace.props}
        tab={{ ...idleTab, draft: "updated draft", messages }}
      />,
    );

    expect(messageList!.scrollTop).toBe(125);
  });

  it("scrolls to the bottom when another conversation becomes visible", () => {
    const workspace = renderWorkspace({
      tab: {
        ...idleTab,
        draft: "",
        messages: [
          { id: "user-1", kind: "user", text: "First conversation" },
        ],
      },
    });
    const messageList = screen.getByText("First conversation").parentElement;

    expect(messageList).not.toBeNull();
    Object.defineProperty(messageList!, "scrollHeight", {
      configurable: true,
      value: 720,
    });
    messageList!.scrollTop = 25;

    workspace.view.rerender(
      <AgentWorkspace
        {...workspace.props}
        tab={{
          ...idleTab,
          draft: "",
          messages: [
            { id: "user-2", kind: "user", text: "Second conversation" },
          ],
        }}
      />,
    );

    expect(messageList!.scrollTop).toBe(720);
  });
});


describe("context compaction display isolation", () => {
  afterEach(cleanup);
  it("preserves historical text through started, thinking, and main answer completion", () => {
    const provider = runningTab.activeRun!.provider;
    const history: AgentTabState["messages"] = [
      { id: "old-user", kind: "user", text: "Earlier inspection request" },
      { id: "old-answer", kind: "assistant", tools: [], text: "Earlier verified result", run: {
        agentRunId: "old-run", status: "COMPLETED", reactIteration: 0,
        sshSessionId: "ssh-1", provider,
      } },
    ];
    let state: AgentState = { tabs: { tab: { ...idleTab, messages: history } } };
    state = agentReducer(state, { type: "run/start", tabId: "tab", requestToken: "request-1",
      sshSessionId: "ssh-1", provider, userMessageId: "new-user", userMessage: "Continue inspection" });
    const identity = { schema_version: 1 as const, request_id: "request-id-1",
      conversation_id: "conversation-1", agent_run_id: "run-1" };
    state = agentReducer(state, { type: "run/stream-started", tabId: "tab", requestToken: "request-1",
      event: { ...identity, type: "agent.turn.started", sequence: 0, status: "RUNNING", react_iteration: 0 } });
    const workspace = renderWorkspace({ tab: state.tabs.tab });
    expect(screen.getByRole("status")).toBeVisible();
    expect(screen.getByText("Earlier inspection request")).toBeVisible();
    expect(screen.getByText("Earlier verified result")).toBeVisible();
    expect(state.tabs.tab.messages.slice(0, 2)).toEqual(history);
    // 后端摘要不发出事件，只有主回答进入此 reducer。
    state = agentReducer(state, { type: "run/text-delta", tabId: "tab", requestToken: "request-1",
      event: { ...identity, type: "agent.turn.text_delta", sequence: 1, delta: "New main answer" } });
    state = agentReducer(state, { type: "run/complete", tabId: "tab", requestToken: "request-1", messageId: "new-answer",
      event: { ...identity, type: "agent.turn.completed", sequence: 2, status: "COMPLETED", react_iteration: 0, error_code: null } });
    workspace.view.rerender(<AgentWorkspace {...workspace.props} tab={state.tabs.tab} />);
    expect(state.tabs.tab.messages.slice(0, 2)).toEqual(history);
    expect(state.tabs.tab.messages).toHaveLength(4);
    expect(screen.getByText("Earlier verified result")).toBeVisible();
    expect(screen.getByText("New main answer")).toBeVisible();
    expect(screen.queryByText(/HISTORICAL_CONTEXT_SUMMARY/)).not.toBeInTheDocument();
  });
});


describe("tool execution display", () => {
  afterEach(cleanup);
  it.each(["", "Checking the service"])("shows tool activity and clears it with the next delta: %s", (streamedText) => {
    let state: AgentState = { tabs: { tab: { ...runningTab, activeRun: { ...runningTab.activeRun!, streamedText } } } };
    const identity = { schema_version: 1 as const, request_id: "request-1", conversation_id: "conversation-1", agent_run_id: "run-1" };
    state = agentReducer(state, { type: "run/tool-started", tabId: "tab", requestToken: "request-1",
      event: { ...identity, sequence: 1, type: "agent.turn.tool_started", tool_call_id: "call-1", tool_name: "execute_command", arguments: { command: "pwd" } } });
    const workspace = renderWorkspace({ tab: state.tabs.tab });
    expect(screen.getByText("Executing tool…")).toBeVisible();
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
    if (streamedText) expect(screen.getByText(streamedText)).toBeVisible();
    state = agentReducer(state, { type: "run/text-delta", tabId: "tab", requestToken: "request-1",
      event: { ...identity, sequence: 2, type: "agent.turn.text_delta", delta: " done" } });
    workspace.view.rerender(<AgentWorkspace {...workspace.props} tab={state.tabs.tab} />);
    expect(screen.queryByText("Executing tool…")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toBeVisible();
  });
});


describe("executed tool details", () => {
  afterEach(cleanup);
  it("shows ordered repeated calls in a collapsed list below the completed answer", () => {
    const tools = [
      { tool_call_id: "call-1", tool_name: "execute_command" as const, arguments: { command: "echo '<script>test</script>'" } },
      { tool_call_id: "call-2", tool_name: "execute_command" as const, arguments: { command: "pwd" } },
    ];
    const { view } = renderWorkspace({ tab: { ...idleTab, messages: [{ id: "answer", kind: "assistant", text: "Done", tools,
      run: { agentRunId: "run-1", status: "COMPLETED", reactIteration: 2, sshSessionId: "ssh-1", provider: runningTab.activeRun!.provider } }] } });
    const summary = screen.getByText("Executed tools (2)");
    const details = summary.closest("details")!;
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(summary);
    expect(details).toHaveAttribute("open");
    expect([...details.querySelectorAll("pre")].map((node) => node.textContent)).toEqual(tools.map((tool) => JSON.stringify(tool.arguments, null, 2)));
    expect(view.container.querySelector("script")).toBeNull();
  });
  it("omits the list when no tools ran", () => {
    renderWorkspace({ tab: { ...idleTab, messages: [{ id: "answer", kind: "assistant", text: "Done", tools: [],
      run: { agentRunId: "run-1", status: "COMPLETED", reactIteration: 0, sshSessionId: "ssh-1", provider: runningTab.activeRun!.provider } }] } });
    expect(screen.queryByText(/Executed tools/)).not.toBeInTheDocument();
  });
});
