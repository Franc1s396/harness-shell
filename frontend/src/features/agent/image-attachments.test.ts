import { beforeEach, describe, expect, it, vi } from "vitest";
import { ImageAttachmentQueue } from "./image-attachments";

describe("image upload queue", () => {
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => "blob:preview");
    URL.revokeObjectURL = vi.fn();
  });
  const file = () => new File(["image"], "photo.png", {type: "image/png"});
  const info = {attachment_id: "image-id", draft_id: "draft", filename: "photo.png", media_type: "image/png" as const, byte_size: 5, width: 1, height: 1};
  it("serializes uploads and never publishes a late result after disposal", async () => {
    let finish!: (value: typeof info) => void;
    const pending = new Promise<typeof info>(resolve => {finish = resolve;});
    const api = {upload: vi.fn().mockReturnValueOnce(pending).mockResolvedValue(info), remove: vi.fn(), clearDraft: vi.fn()};
    const publish = vi.fn();
    const queue = new ImageAttachmentQueue("draft", api, publish);
    queue.add([file(), file()]); await Promise.resolve();
    expect(api.upload).toHaveBeenCalledTimes(1);
    const disposed = queue.dispose();
    expect(api.upload.mock.calls[0][2].aborted).toBe(true);
    const calls = publish.mock.calls.length;
    finish(info); await disposed;
    expect(publish).toHaveBeenCalledTimes(calls);
    expect(api.upload).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  });
  it("retains a usable draft when Backend cleanup fails", async () => {
    const api = {upload: vi.fn().mockResolvedValue(info), remove: vi.fn(), clearDraft: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined)};
    const queue = new ImageAttachmentQueue("draft", api, vi.fn());
    queue.add([file()]); await queue.settle();
    await expect(queue.clear()).rejects.toThrow("offline");
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    queue.add([file()]); await queue.settle();
    expect(api.upload).toHaveBeenCalledTimes(2);
    await queue.clear();
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  });
  it("retries a failed deletion without uploading another copy", async () => {
    const api = {upload: vi.fn().mockResolvedValue(info), remove: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined), clearDraft: vi.fn()};
    const queue = new ImageAttachmentQueue("draft", api, vi.fn());
    queue.add([file()]); await queue.settle();
    const id = queue.items[0].clientId;
    await expect(queue.remove(id)).rejects.toThrow("offline");
    await queue.retry(id);
    expect(queue.items).toHaveLength(0);
    expect(api.upload).toHaveBeenCalledTimes(1);
    expect(api.remove).toHaveBeenLastCalledWith("draft", "image-id");
  });
  it("rejects a batch over capacity without starting uploads", () => {
    const api = { upload: vi.fn(), remove: vi.fn(), clearDraft: vi.fn() };
    const queue = new ImageAttachmentQueue("draft", api, vi.fn());
    expect(() => queue.add(Array.from({length: 6}, () => new File(["a"], "photo.png", {type: "image/png"})))).toThrow();
    expect(api.upload).not.toHaveBeenCalled();
  });
});
