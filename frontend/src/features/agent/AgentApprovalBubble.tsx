import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/controls";
import type { AgentApprovalMessage } from "./agent-state";

/** 只展示经过校验的操作；授权和命令执行仍由 Backend 管理。 */
export function AgentApprovalBubble({ message, onDecision, docked = false }: {
  message: AgentApprovalMessage;
  docked?: boolean;
  onDecision?: (decision: "approve" | "reject") => void;
}) {
  const { t } = useTranslation();
  const disabled = message.status !== "PENDING" || message.submitting;
  const target = message.request.target;
  // 固定审核区仅让命令滚动，标题和按钮始终留在输入框上方；历史记录保留独立边框。
  return <article className={`max-w-full space-y-3 p-3 ${docked ? "" : "rounded-xl border border-line bg-raised"}`}>
    <div role="status" aria-live="polite" className="text-xs text-ink-muted">
      {t(`agent.approvalStatus${message.status}`)}
    </div>
    <p>{t("agent.approvalOperation")}</p>
    <p className="break-all text-xs text-ink-muted">{target.display_name} · {target.username}@{target.host}:{target.port}</p>
    <pre className={`${docked ? "max-h-[min(16rem,25vh)]" : "max-h-64"} overflow-auto whitespace-pre-wrap break-words rounded bg-base p-2 text-xs`}>{message.request.arguments.command}</pre>
    {message.error && <p role="alert" className="break-words text-xs text-danger">{message.error.code}: {message.error.message}</p>}
    {onDecision && <div className="flex justify-end gap-2">
      <Button type="button" variant="secondary" disabled={disabled} onClick={() => onDecision("reject")}>{t("agent.approvalReject")}</Button>
      <Button type="button" disabled={disabled} onClick={() => onDecision("approve")}>{t("agent.approvalApprove")}</Button>
    </div>}
  </article>;
}
