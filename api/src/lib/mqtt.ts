import { randomUUID } from "node:crypto";
import { createConnection, type Socket } from "node:net";

export class MqttError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MqttError";
  }
}

function encodeRemainingLength(length: number): Buffer {
  const bytes: number[] = [];
  let value = length;
  do {
    let digit = value % 128;
    value = Math.floor(value / 128);
    if (value > 0) digit |= 0x80;
    bytes.push(digit);
  } while (value > 0);
  return Buffer.from(bytes);
}

function encodeString(value: string): Buffer {
  const encoded = Buffer.from(value, "utf-8");
  const prefix = Buffer.alloc(2);
  prefix.writeUInt16BE(encoded.length, 0);
  return Buffer.concat([prefix, encoded]);
}

function packet(header: number, body: Buffer): Buffer {
  return Buffer.concat([Buffer.from([header]), encodeRemainingLength(body.length), body]);
}

interface ParsedPacket {
  type: number;
  flags: number;
  body: Buffer;
}

interface Pending {
  type: number;
  resolve: (packet: ParsedPacket) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

export interface MqttMessage {
  topic: string;
  payload: Buffer;
  qos: number;
}

export class TinyMqttClient {
  private socket: Socket | null = null;
  private buffer = Buffer.alloc(0);
  private pending: Pending | null = null;
  private nextPacketId = 1;
  private connected = false;
  readonly received: MqttMessage[] = [];

  constructor(
    private readonly host: string,
    private readonly port: number,
    private readonly clientId = `homelink-api-${randomUUID().slice(0, 8)}`,
  ) {}

  async connect(timeoutMs = 3000): Promise<void> {
    const socket = createConnection({ host: this.host, port: this.port });
    this.socket = socket;
    socket.setNoDelay(true);
    socket.on("data", (chunk) => this.onData(chunk));
    socket.on("error", (error) => this.fail(error));
    socket.on("close", () => this.fail(new MqttError("connection closed")));
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new MqttError("TCP connect timeout")), timeoutMs);
      socket.once("connect", () => {
        clearTimeout(timer);
        resolve();
      });
      socket.once("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
    });

    const body = Buffer.concat([
      encodeString("MQTT"),
      Buffer.from([4, 0x02]),
      Buffer.from([0x00, 0x1e]),
      encodeString(this.clientId),
    ]);
    const wait = this.waitForPacket(2, timeoutMs);
    this.write(packet(0x10, body));
    const connack = await wait;
    const returnCode = connack.body.length >= 2 ? connack.body.readUInt8(1) : 1;
    if (returnCode !== 0) {
      throw new MqttError(`broker rejected CONNECT with code ${returnCode}`);
    }
    this.connected = true;
  }

  private write(data: Buffer): void {
    if (!this.socket) throw new MqttError("not connected");
    this.socket.write(data);
  }

  private allocPacketId(): number {
    this.nextPacketId = (this.nextPacketId % 65535) + 1;
    return this.nextPacketId;
  }

  private waitForPacket(type: number, timeoutMs: number): Promise<ParsedPacket> {
    if (this.pending) {
      throw new MqttError("concurrent MQTT operation on one client");
    }
    return new Promise<ParsedPacket>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending = null;
        reject(new MqttError(`timed out waiting for MQTT packet type ${type}`));
      }, timeoutMs);
      this.pending = { type, resolve, reject, timer };
    });
  }

  async publish(topic: string, payload: string | Buffer, qos: 0 | 1 = 1): Promise<void> {
    if (!this.connected) throw new MqttError("not connected");
    const body = Buffer.concat([
      encodeString(topic),
      ...(qos > 0 ? [this.uint16(this.allocPacketId())] : []),
      typeof payload === "string" ? Buffer.from(payload, "utf-8") : payload,
    ]);
    if (qos === 0) {
      this.write(packet(0x30, body));
      return;
    }
    const wait = this.waitForPacket(4, 3000);
    this.write(packet(0x32, body));
    await wait;
  }

  async subscribe(filters: Array<{ topic: string; qos: 0 | 1 }>): Promise<void> {
    if (!this.connected) throw new MqttError("not connected");
    const packetId = this.allocPacketId();
    const parts: Buffer[] = [this.uint16(packetId)];
    for (const filter of filters) {
      parts.push(encodeString(filter.topic), Buffer.from([filter.qos]));
    }
    const wait = this.waitForPacket(9, 3000);
    this.write(packet(0x82, Buffer.concat(parts)));
    await wait;
  }

  end(): void {
    if (this.socket) {
      try {
        this.write(Buffer.from([0xe0, 0x00]));
      } catch {
        // socket already gone
      }
      this.socket.destroy();
    }
    this.socket = null;
    this.connected = false;
  }

  private uint16(value: number): Buffer {
    const buffer = Buffer.alloc(2);
    buffer.writeUInt16BE(value, 0);
    return buffer;
  }

  private fail(error: Error): void {
    if (this.pending) {
      clearTimeout(this.pending.timer);
      this.pending.reject(error);
      this.pending = null;
    }
  }

  private onData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const parsed = this.tryParse();
      if (!parsed) return;
      this.handlePacket(parsed);
    }
  }

  private tryParse(): ParsedPacket | null {
    if (this.buffer.length < 2) return null;
    let multiplier = 1;
    let remaining = 0;
    let index = 1;
    for (let count = 0; count < 4; count += 1) {
      if (index >= this.buffer.length) return null;
      const digit = this.buffer.readUInt8(index);
      index += 1;
      remaining += (digit & 0x7f) * multiplier;
      if ((digit & 0x80) === 0) {
        const total = index + remaining;
        if (this.buffer.length < total) return null;
        const type = this.buffer.readUInt8(0) >> 4;
        const flags = this.buffer.readUInt8(0) & 0x0f;
        const body = this.buffer.subarray(index, total);
        this.buffer = this.buffer.subarray(total);
        return { type, flags, body };
      }
      multiplier *= 128;
    }
    throw new MqttError("malformed remaining length");
  }

  private handlePacket(parsed: ParsedPacket): void {
    if (parsed.type === 3) {
      const decoded = this.decodePublish(parsed);
      if (decoded.qos === 1 && decoded.packetId !== null) {
        this.write(packet(0x40, this.uint16(decoded.packetId)));
      }
      this.received.push(decoded.message);
      return;
    }
    if (!this.pending || this.pending.type !== parsed.type) {
      return;
    }
    const pending = this.pending;
    this.pending = null;
    clearTimeout(pending.timer);
    if (parsed.type === 2) {
      pending.resolve(parsed);
      return;
    }
    pending.resolve(parsed);
  }

  private decodePublish(parsed: ParsedPacket): {
    message: MqttMessage;
    qos: number;
    packetId: number | null;
  } {
    const qos = (parsed.flags >> 1) & 0x03;
    const topicLength = parsed.body.readUInt16BE(0);
    let offset = 2;
    const topic = parsed.body.subarray(offset, offset + topicLength).toString("utf-8");
    offset += topicLength;
    let packetId: number | null = null;
    if (qos > 0) {
      packetId = parsed.body.readUInt16BE(offset);
      offset += 2;
    }
    return {
      message: { topic, payload: parsed.body.subarray(offset), qos },
      qos,
      packetId,
    };
  }
}

export interface OutgoingMessage {
  topic: string;
  payload: string | Buffer;
}

export async function publishMessages(
  messages: OutgoingMessage[],
  host: string,
  port: number,
  timeoutMs = 3000,
): Promise<void> {
  const client = new TinyMqttClient(host, port);
  try {
    await client.connect(timeoutMs);
    for (const message of messages) {
      await client.publish(message.topic, message.payload, 1);
    }
  } finally {
    client.end();
  }
}
