import net from "node:net";
import type { AddressInfo } from "node:net";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import Database from "better-sqlite3";

export interface CapturedMessage {
  topic: string;
  payload: Buffer;
}

export interface FakeBroker {
  port: number;
  messages: CapturedMessage[];
  close: () => Promise<void>;
}

function encodeRemaining(length: number): Buffer {
  const out: number[] = [];
  let value = length;
  do {
    let digit = value % 128;
    value = Math.floor(value / 128);
    if (value > 0) digit |= 0x80;
    out.push(digit);
  } while (value > 0);
  return Buffer.from(out);
}

function encodePacket(header: number, body: Buffer): Buffer {
  return Buffer.concat([Buffer.from([header]), encodeRemaining(body.length), body]);
}

function uint16(value: number): Buffer {
  const buffer = Buffer.alloc(2);
  buffer.writeUInt16BE(value, 0);
  return buffer;
}

export async function startFakeBroker(): Promise<FakeBroker> {
  const messages: CapturedMessage[] = [];
  const server = net.createServer((socket) => {
    let buffer = Buffer.alloc(0);
    socket.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      for (;;) {
        if (buffer.length < 2) return;
        let multiplier = 1;
        let remaining = 0;
        let index = 1;
        let complete = false;
        for (let count = 0; count < 4; count += 1) {
          if (index >= buffer.length) return;
          const digit = buffer.readUInt8(index);
          index += 1;
          remaining += (digit & 0x7f) * multiplier;
          if ((digit & 0x80) === 0) {
            complete = true;
            break;
          }
          multiplier *= 128;
        }
        if (!complete) return;
        const total = index + remaining;
        if (buffer.length < total) return;
        const type = buffer.readUInt8(0) >> 4;
        const flags = buffer.readUInt8(0) & 0x0f;
        const body = buffer.subarray(index, total);
        buffer = buffer.subarray(total);

        if (type === 1) {
          socket.write(Buffer.from([0x20, 0x02, 0x00, 0x00]));
        } else if (type === 3) {
          const qos = (flags >> 1) & 0x03;
          const topicLength = body.readUInt16BE(0);
          let offset = 2 + topicLength;
          const topic = body.subarray(2, offset).toString("utf-8");
          let packetId: number | null = null;
          if (qos > 0) {
            packetId = body.readUInt16BE(offset);
            offset += 2;
          }
          messages.push({ topic, payload: Buffer.from(body.subarray(offset)) });
          if (qos === 1 && packetId !== null) {
            socket.write(encodePacket(0x40, uint16(packetId)));
          }
        } else if (type === 8) {
          const packetId = body.readUInt16BE(0);
          socket.write(encodePacket(0x90, Buffer.concat([uint16(packetId), Buffer.from([0x01])])));
        } else if (type === 12) {
          socket.write(Buffer.from([0xd0, 0x00]));
        } else if (type === 14) {
          socket.end();
        }
      }
    });
    socket.on("error", () => undefined);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  return {
    port,
    messages,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
      }),
  };
}

const SCHEMA = `
  CREATE TABLE IF NOT EXISTS homelink (
    pk TEXT NOT NULL,
    sk TEXT NOT NULL,
    attrs TEXT NOT NULL,
    thing_id TEXT,
    ts INTEGER,
    PRIMARY KEY (pk, sk)
  );
  CREATE INDEX IF NOT EXISTS thing_id_ts ON homelink (thing_id, ts);
`;

function runKey(startedAt: number, runId: string): string {
  return `RUN#${String(startedAt).padStart(13, "0")}#${runId}`;
}

export interface SeedPaths {
  dbFile: string;
  analyticsFile: string;
  now: number;
}

export function seedDatabase(): SeedPaths {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "homelink-api-test-"));
  const dbFile = path.join(dir, "homelink.db");
  const analyticsFile = path.join(dir, "fleet.json");
  const now = Date.now();
  const db = new Database(dbFile);
  db.exec(SCHEMA);
  const insert = db.prepare(
    "INSERT INTO homelink (pk, sk, attrs, thing_id, ts) VALUES (?, ?, ?, ?, ?)",
  );
  const telemetry = (thing: string, ts: number, overrides: Record<string, unknown> = {}) => ({
    thing_id: thing,
    seq: 42,
    ts_ms: ts,
    fw_version: "1.2.0",
    state: "RUNNING",
    motor_current_a: 1.42,
    motor_rpm: 2120.0,
    suction_pa: 1760.0,
    battery_pct: 87.5,
    bin_pct: 12.0,
    runtime_s: 32.5,
    error_code: "NONE",
    ...overrides,
  });
  const shadow = (
    version: number,
    ts: number,
    desired: Record<string, unknown>,
    reported: Record<string, unknown>,
    delta: Record<string, unknown>,
  ) => ({
    state: { desired, reported, delta },
    version,
    timestamp: ts,
    metadata: {
      desired: Object.fromEntries(Object.keys(desired).map((key) => [key, { timestamp: ts }])),
      reported: Object.fromEntries(Object.keys(reported).map((key) => [key, { timestamp: ts }])),
    },
  });

  insert.run(
    "THING#vac-001",
    "SHADOW",
    JSON.stringify(
      shadow(
        3,
        now - 2000,
        { power_level: 4 },
        { power_level: 2, state: "ONLINE", fw_version: "1.2.0", error_code: "NONE" },
        { power_level: 4 },
      ),
    ),
    "vac-001",
    now - 2000,
  );
  insert.run(
    "THING#vac-001",
    "TELEMETRY#LATEST",
    JSON.stringify(telemetry("vac-001", now - 1000)),
    "vac-001",
    now - 1000,
  );
  for (const [index, startedAt] of [now - 3_600_000, now - 1_800_000].entries()) {
    insert.run(
      "THING#vac-001",
      runKey(startedAt, `run-${index}`),
      JSON.stringify({
        run_id: `run-${index}`,
        thing_id: "vac-001",
        started_at: startedAt,
        ended_at: startedAt + 600_000,
        duration_s: 600 + index * 10,
        power_level: 3,
        firmware: "1.2.0",
        error_code: "NONE",
        samples: 1200,
      }),
      "vac-001",
      startedAt,
    );
  }
  insert.run(
    "THING#vac-002",
    "SHADOW",
    JSON.stringify(
      shadow(
        1,
        now - 60_000,
        {},
        { state: "IDLE", fw_version: "1.1.0", error_code: "NONE" },
        {},
      ),
    ),
    "vac-002",
    now - 60_000,
  );
  insert.run(
    "THING#vac-002",
    "TELEMETRY#LATEST",
    JSON.stringify(telemetry("vac-002", now - 60_000, { battery_pct: 64.0, state: "IDLE" })),
    "vac-002",
    now - 60_000,
  );
  db.close();

  fs.writeFileSync(
    analyticsFile,
    JSON.stringify({
      generated_at: now,
      fleet: { devices: 40, runs: 1200, days: 30 },
      usage: { runs_per_device_per_week: 5.4, runtime: { mean: 812.0, median: 790.0, p95: 1400.0 } },
      error_pareto: [
        { code: "BRUSH_JAM", count: 90, pct: 45.0 },
        { code: "FILTER_CLOG", count: 60, pct: 30.0 },
        { code: "WHEEL_STALL", count: 50, pct: 25.0 },
      ],
      anomalies: { precision: 0.92, recall: 0.88, f1: 0.9, false_alerts_per_1000_runs: 12.0 },
    }),
  );
  return { dbFile, analyticsFile, now };
}
