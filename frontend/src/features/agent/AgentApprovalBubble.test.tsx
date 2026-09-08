// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import "../../i18n";
import { AgentApprovalBubble } from "./AgentApprovalBubble";
import type { AgentApprovalMessage } from "./agent-state";

it("shows the exact command as text and only submits explicit button decisions", () => {
  const message: AgentApprovalMessage = {
    id: "approval", kind: "approval", status: "PENDING", submitting: false, error: null,
    request: { schema_version: 1, type: "agent.turn.approval_requested", sequence: 1,
      request_id: "request", conversation_id: "conversation", agent_run_id: "run", approval_id: "approval",
      ssh_session_id: "ssh", tool_call_id: "call", tool_name: "execute_command",
      arguments: { command: "<img src=x onerror=alert(1)>" },
      target: { display_name: "test", host: "localhost", port: 22, username: "ops" },
      reason: "POSSIBLE_MUTATION_OR_UNKNOWN" },
  };
  const onDecision = vi.fn();
  const view = render(<AgentApprovalBubble message={message} onDecision={onDecision} />);
  expect(view.container.querySelector("img")).toBeNull();
  expect(view.container.textContent).toContain(message.request.arguments.command);
  const buttons = screen.getAllByRole("button");
  fireEvent.click(buttons[0]);
  expect(onDecision).toHaveBeenCalledWith("reject");
  view.rerender(<AgentApprovalBubble message={{ ...message, status: "APPROVED" }} onDecision={onDecision} />);
  expect((buttons[1] as HTMLButtonElement).disabled).toBe(true);
});
