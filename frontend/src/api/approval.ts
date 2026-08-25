import { invoke } from "@tauri-apps/api/core";

export type ApprovalContext = {
  pending: boolean;
};

export type CommandError = {
  code: string;
  message: string;
};

export const getApprovalContext = () =>
  invoke<ApprovalContext>("get_approval_context");

export const submitApprovalDecision = () =>
  invoke<void>("submit_approval_decision");
