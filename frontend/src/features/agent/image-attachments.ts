import type { AttachmentInfo, AttachmentApi } from "../../api/agent-attachments";

export type DraftAttachment = {
  clientId: string; filename: string; previewUrl: string;
  status: "uploading" | "ready" | "failed" | "removing";
  attachment: AttachmentInfo | null; error: string | null;
};

/** 每草稿独占 File、object URL、上传 controller 和串行尾 Promise。 */
export class ImageAttachmentQueue {
  readonly items: DraftAttachment[] = [];
  private files = new Map<string, File>();
  private controllers = new Map<string, AbortController>();
  private tail: Promise<void> = Promise.resolve();
  private active = true;
  constructor(readonly draftId: string, private api: Pick<AttachmentApi, "upload" | "remove" | "clearDraft">,
    private publish: (items: DraftAttachment[]) => void) {}

  private emit() { if (this.active) this.publish(this.items.map(item => ({...item}))); }
  add(files: readonly File[]): void {
    if (!this.active || this.items.length + files.length > 5) throw new Error("AGENT_IMAGE_COUNT_EXCEEDED");
    if (files.some(file => file.size < 1 || file.size > 10_485_760)) throw new Error("AGENT_IMAGE_TOO_LARGE");
    for (const file of files) {
      const item: DraftAttachment = {clientId: crypto.randomUUID(), filename: file.name,
        previewUrl: URL.createObjectURL(file), status: "uploading", attachment: null, error: null};
      this.items.push(item);
      this.files.set(item.clientId, file);
      this.enqueue(item);
    }
    this.emit();
  }
  private enqueue(item: DraftAttachment) {
    this.tail = this.tail.then(async () => {
      if (!this.active || item.status !== "uploading") return;
      const controller = new AbortController();
      this.controllers.set(item.clientId, controller);
      try {
        const result = await this.api.upload(this.draftId, this.files.get(item.clientId)!, controller.signal);
        item.attachment = result;
        if (item.status === "uploading") item.status = "ready";
      } catch (error) {
        if (item.status === "uploading") {
          item.status = "failed";
          item.error = error instanceof Error ? error.message : "AGENT_IMAGE_UPLOAD_FAILED";
        }
      } finally {
        this.controllers.delete(item.clientId);
        this.emit();
      }
    });
  }
  async retry(clientId: string): Promise<void> {
    const item = this.items.find(value => value.clientId === clientId);
    if (!item || item.status !== "failed" || !this.active) return;
    // 删除失败仍拥有服务端 ID；重试删除不能重新上传并遗失旧附件。
    if (item.attachment) { await this.remove(clientId); return; }
    item.status = "uploading"; item.error = null; this.enqueue(item); this.emit();
  }
  async remove(clientId: string): Promise<void> {
    const item = this.items.find(value => value.clientId === clientId);
    if (!item || item.status === "removing") return;
    item.status = "removing"; this.emit();
    this.controllers.get(clientId)?.abort();
    await this.tail;
    try {
      if (item.attachment) await this.api.remove(this.draftId, item.attachment.attachment_id);
      URL.revokeObjectURL(item.previewUrl);
      this.files.delete(clientId);
      this.items.splice(this.items.indexOf(item), 1);
    } catch (error) {
      item.status = "failed";
      item.error = error instanceof Error ? error.message : "AGENT_IMAGE_DELETE_FAILED";
      throw error;
    } finally { this.emit(); }
  }
  settle(): Promise<void> { return this.tail; }
  async clear(): Promise<void> {
    // 先停住上传，再清理 Backend。失败时保留 File 和预览供用户重试。
    this.active = false;
    for (const controller of this.controllers.values()) controller.abort();
    await this.tail;
    try { await this.api.clearDraft(this.draftId); }
    catch (error) {
      this.active = true;
      for (const item of this.items) {
        if (item.status === "uploading") {
          item.status = "failed"; item.error = "AGENT_IMAGE_UPLOAD_CANCELLED";
        }
      }
      this.emit();
      throw error;
    }
    await this.dispose();
  }
  async dispose(): Promise<void> {
    this.active = false;
    for (const controller of this.controllers.values()) controller.abort();
    await this.tail;
    for (const item of this.items) URL.revokeObjectURL(item.previewUrl);
    this.files.clear();
  }
}
