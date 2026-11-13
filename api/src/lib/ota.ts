import { createHash } from "node:crypto";
import type { OutgoingMessage } from "./mqtt.js";

export interface FirmwareSpec {
  fw_version: string;
  sha256: string;
  size: number;
  chunks: number;
}

export interface FirmwareBuild {
  spec: FirmwareSpec;
  payloads: Array<Buffer | null>;
  corrupted: boolean;
}

export function firmwareBlob(version: string, size = 8192): Buffer {
  const seed = createHash("sha256").update(`homelink-firmware-${version}`).digest();
  const repeats = Math.ceil(size / seed.length);
  return Buffer.concat(Array.from({ length: repeats }, () => seed)).subarray(0, size);
}

export function buildFirmware(
  version: string,
  options: { size?: number; chunkSize?: number; corrupt?: boolean; dropChunk?: number } = {},
): FirmwareBuild {
  const size = options.size ?? 8192;
  const chunkSize = options.chunkSize ?? 1024;
  const corrupt = options.corrupt ?? false;
  const blob = firmwareBlob(version, size);
  const chunks: Buffer[] = [];
  for (let offset = 0; offset < blob.length; offset += chunkSize) {
    chunks.push(blob.subarray(offset, offset + chunkSize));
  }
  const payloads: Array<Buffer | null> = chunks.map((chunk) => Buffer.from(chunk));
  if (corrupt && payloads.length > 0) {
    const index = Math.floor(payloads.length / 2);
    const target = payloads[index];
    if (target) {
      target[0] = (target[0] ?? 0) ^ 0xff;
    }
  }
  if (options.dropChunk !== undefined && options.dropChunk >= 0 && options.dropChunk < payloads.length) {
    payloads[options.dropChunk] = null;
  }
  return {
    spec: {
      fw_version: version,
      sha256: createHash("sha256").update(blob).digest("hex"),
      size: blob.length,
      chunks: chunks.length,
    },
    payloads,
    corrupted: corrupt,
  };
}

export function otaMessages(
  thingId: string,
  build: FirmwareBuild,
  updateTopic: string,
  chunkTopic: string,
): { messages: OutgoingMessage[]; published: number; dropped: number } {
  const messages: OutgoingMessage[] = [
    {
      topic: updateTopic,
      payload: JSON.stringify({ state: { desired: { ota: build.spec } } }),
    },
  ];
  let published = 0;
  let dropped = 0;
  build.payloads.forEach((chunk, index) => {
    if (chunk === null) {
      dropped += 1;
      return;
    }
    messages.push({
      topic: chunkTopic,
      payload: JSON.stringify({
        fw_version: build.spec.fw_version,
        index,
        total: build.payloads.length,
        data: chunk.toString("base64"),
      }),
    });
    published += 1;
  });
  return { messages, published, dropped };
}
