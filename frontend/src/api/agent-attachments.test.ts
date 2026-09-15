import { beforeEach, expect, it, vi } from "vitest";
import { attachmentApi } from "./agent-attachments";

const {uploadImage} = vi.hoisted(() => ({uploadImage: vi.fn()}));
vi.mock("./bootstrap", () => ({getBackendClient: () => ({http: {uploadImage}})}));
const draft = "10000000-0000-4000-8000-000000000001";
const valid = {attachment_id: "20000000-0000-4000-8000-000000000002", draft_id: draft,
  filename: "image.png", media_type: "image/png", byte_size: 5, width: 1, height: 1};
beforeEach(() => vi.resetAllMocks());
it.each([
  {attachment_id: "not-an-id"}, {filename: ""}, {filename: "x".repeat(256)},
  {byte_size: 10_485_761}, {media_type: "image/svg+xml"}, {draft_id: "30000000-0000-4000-8000-000000000003"},
])("rejects malformed attachment metadata %j", async override => {
  uploadImage.mockResolvedValue({attachment: {...valid, ...override}});
  await expect(attachmentApi.upload(draft, new File(["image"], "image.png"), new AbortController().signal)).rejects.toThrow("AGENT_ATTACHMENT_RESPONSE_INVALID");
});
it("returns validated metadata for the original draft", async () => {
  uploadImage.mockResolvedValue({attachment: valid});
  await expect(attachmentApi.upload(draft, new File(["image"], "image.png"), new AbortController().signal)).resolves.toEqual(valid);
});
