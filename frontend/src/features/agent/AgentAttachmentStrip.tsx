import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { attachmentApi, type AttachmentInfo } from "../../api/agent-attachments";
import type { DraftAttachment } from "./image-attachments";
import { AgentImagePreview } from "./AgentImagePreview";

export function AgentAttachmentStrip({items, onRemove, onRetry}: {
  items: DraftAttachment[]; onRemove: (id: string) => void; onRetry: (id: string) => void;
}) {
  const {t} = useTranslation();
  const [preview, setPreview] = useState<DraftAttachment | null>(null);
  const visiblePreview = items.find(item => item.clientId === preview?.clientId);
  return <><div className="flex flex-wrap gap-2 px-3 pt-3">
    {items.map(item => <div key={item.clientId} className="relative w-24 rounded-xl border border-line bg-raised p-1">
      <button type="button" onClick={() => setPreview(item)} aria-label={`${t("agent.imagePreview")}: ${item.filename}`}>
        <img src={item.previewUrl} alt={item.filename} className="h-20 w-20 rounded-lg object-cover" />
      </button>
      {(item.status === "uploading" || item.status === "removing") && <div role="status" className="pointer-events-none absolute inset-0 grid place-items-center rounded-xl bg-panel/70">
        <span className="size-5 animate-spin rounded-full border-2 border-line-strong border-t-accent motion-reduce:animate-none" />
        <span className="sr-only">{t("agent.imageUploading")}</span>
      </div>}
      <button type="button" onClick={() => onRemove(item.clientId)} disabled={item.status === "removing"}
        aria-label={`${t("agent.imageRemove")}: ${item.filename}`} className="absolute -right-1 -top-1 grid size-5 place-items-center rounded-full border border-line bg-panel text-ink">×</button>
      {item.status === "failed" && <div role="alert" className="break-words text-xs text-danger">
        <span>{item.error || t("agent.imageFailed")}</span>
        <button type="button" className="mt-1 underline" onClick={() => onRetry(item.clientId)}>{t("agent.imageRetry")}</button>
      </div>}
    </div>)}
  </div><AgentImagePreview src={visiblePreview?.previewUrl ?? null} alt={visiblePreview?.filename ?? ""} onClose={() => setPreview(null)} /></>;
}

/** 已发送图片只经 typed client 读取，生命周期内取消请求并回收 URL。 */
export function AgentSentImage({image}: {image: AttachmentInfo}) {
  const {t} = useTranslation();
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let localUrl: string | null = null;
    setSrc(null); setError(null);
    void attachmentApi.readImage(image.attachment_id, controller.signal).then(blob => {
      if (controller.signal.aborted) return;
      localUrl = URL.createObjectURL(blob); setSrc(localUrl);
    }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Image read failed"); });
    return () => { controller.abort(); if (localUrl) URL.revokeObjectURL(localUrl); };
  }, [image.attachment_id]);
  return <>{error ? <span role="alert" className="text-xs text-danger">{error}</span> :
    <button type="button" onClick={() => setOpen(true)} disabled={!src} aria-label={`${t("agent.imagePreview")}: ${image.filename}`}
      className="rounded-lg border border-line bg-panel p-1">
      {src ? <img src={src} alt={image.filename} className="h-28 max-w-40 rounded-md object-contain" /> : <span role="status">{t("agent.imageLoading")}</span>}
    </button>}
    <AgentImagePreview src={open ? src : null} alt={image.filename} onClose={() => setOpen(false)} />
  </>;
}
