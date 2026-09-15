import { getBackendClient } from "./bootstrap";

export type AttachmentInfo = {
  attachment_id: string; draft_id: string; filename: string;
  media_type: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  byte_size: number; width: number; height: number;
};

const validateAttachment = (raw: unknown): AttachmentInfo => {
  if (!raw || typeof raw !== "object") throw new Error("AGENT_ATTACHMENT_RESPONSE_INVALID");
  const value = raw as Record<string, unknown>;
  const fields = ["attachment_id", "draft_id", "filename", "media_type", "byte_size", "width", "height"];
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  if (Object.keys(value).length !== fields.length || fields.some(key => !(key in value)) ||
    ["attachment_id", "draft_id", "filename"].some(key => typeof value[key] !== "string") ||
    !uuid.test(String(value.attachment_id)) || !uuid.test(String(value.draft_id)) ||
    [...String(value.filename)].length < 1 || [...String(value.filename)].length > 255 ||
    !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(String(value.media_type)) ||
    ["byte_size", "width", "height"].some(key => !Number.isSafeInteger(value[key]) || Number(value[key]) < 1) ||
    Number(value.byte_size) > 10_485_760 || Number(value.width) * Number(value.height) > 40_000_000) {
    throw new Error("AGENT_ATTACHMENT_RESPONSE_INVALID");
  }
  return value as AttachmentInfo;
};

const remove = async (path: string) => {
  const result = await getBackendClient().http.request<{request_id: string; deleted: boolean}>("DELETE", path);
  if (result.deleted !== true) throw new Error("AGENT_ATTACHMENT_DELETE_FAILED");
};

export const attachmentApi = {
  async upload(draftId: string, file: File, signal: AbortSignal): Promise<AttachmentInfo> {
    const result = await getBackendClient().http.uploadImage<{request_id: string; attachment: unknown}>(draftId, file, signal);
    const attachment = validateAttachment(result.attachment);
    if (attachment.draft_id !== draftId) throw new Error("AGENT_ATTACHMENT_RESPONSE_INVALID");
    return attachment;
  },
  remove: (draftId: string, attachmentId: string) => remove(`/v1/agent/attachments/${encodeURIComponent(attachmentId)}?draft_id=${encodeURIComponent(draftId)}`),
  clearDraft: (draftId: string) => remove(`/v1/agent/attachment-drafts/${encodeURIComponent(draftId)}`),
  deleteConversation: (id: string) => remove(`/v1/agent/conversations/${encodeURIComponent(id)}`),
  readImage: (id: string, signal: AbortSignal) => getBackendClient().http.readImage(id, signal),
};
export type AttachmentApi = typeof attachmentApi;
