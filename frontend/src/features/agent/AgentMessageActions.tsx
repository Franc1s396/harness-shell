import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";

/** 终态回复的轻量操作栏；只复制展示正文，不包含运行详情。 */
export function AgentMessageActions({ text, retryDisabled, onRetry }: {
  text: string; retryDisabled: boolean; onRetry: () => void;
}) {
  const { t } = useTranslation();
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  // 确认状态属于当前回复；切换消息或进入运行态时随操作栏卸载，不带到另一轮。
  const [retryOpen, setRetryOpen] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };
  const buttonClass = "grid size-7 place-items-center rounded-md text-ink-muted transition hover:bg-raised hover:text-ink focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40";
  return <>
    <div className="mt-1 flex items-center justify-start gap-1">
      <button type="button" className={buttonClass} aria-label={t("agent.copy")}
        title={t(copyState === "copied" ? "agent.copied" : "agent.copy")} onClick={() => void copy()}>
        <svg aria-hidden="true" className="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
          {copyState === "copied" ? <path d="m5 12 4 4L19 6" /> : <>
            <rect x="8" y="8" width="12" height="13" rx="2.5" />
            <path d="M15 8V5.5A2.5 2.5 0 0 0 12.5 3h-7A2.5 2.5 0 0 0 3 5.5v7A2.5 2.5 0 0 0 5.5 15H8" />
          </>}
        </svg>
      </button>
      <button type="button" className={buttonClass} aria-label={t("agent.retry")} title={t("agent.retry")}
        disabled={retryDisabled} onClick={() => setRetryOpen(true)}>
        <svg aria-hidden="true" className="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
          <path d="M20 7v5h-5M19.4 12a7.5 7.5 0 1 0-1.7 4.7" />
        </svg>
      </button>
    </div>
    {copyState === "copied" && <span role="status" className="sr-only">{t("agent.copied")}</span>}
    {copyState === "failed" && <p role="alert" className="mt-1 text-xs text-danger">{t("agent.copyFailed")}</p>}
    <Dialog open={retryOpen} title={t("agent.retryTitle")} onClose={() => setRetryOpen(false)}>
      <p className="mt-3 text-sm text-ink-muted">{t("agent.retryBody")}</p>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onClick={() => setRetryOpen(false)}>{t("common.cancel")}</Button>
        <Button disabled={retryDisabled} onClick={() => {
          // 只在确认后触发真正重试；取消或关闭弹窗不会清理旧回复或发送请求。
          if (retryDisabled) return;
          setRetryOpen(false);
          onRetry();
        }}>{t("agent.confirmRetry")}</Button>
      </div>
    </Dialog>
  </>;
}
