import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import { readFileChunks } from "./browser-file-gateway";


export async function sha256File(file: File): Promise<string> {
  const digest = sha256.create();
  for await (const chunk of readFileChunks(file)) digest.update(chunk);
  return bytesToHex(digest.digest());
}
