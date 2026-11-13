import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import Database from "better-sqlite3";
import { databasePath } from "./config.js";

export interface ShadowState {
  desired?: Record<string, unknown> | null;
  reported?: Record<string, unknown> | null;
  delta?: Record<string, unknown> | null;
}

export interface ShadowDoc {
  state: ShadowState;
  version: number;
  timestamp: number;
  clientToken?: string | null;
  metadata?: Record<string, unknown>;
}

export interface Telemetry {
  thing_id: string;
  seq: number;
  ts_ms: number;
  fw_version: string;
  state: string;
  motor_current_a: number;
  motor_rpm: number;
  suction_pa: number;
  battery_pct: number;
  bin_pct: number;
  runtime_s: number;
  error_code: string;
}

export interface RunRecord {
  run_id: string;
  thing_id: string;
  started_at: number;
  ended_at?: number;
  duration_s: number;
  power_level?: number;
  firmware?: string;
  error_code?: string;
  battery_start?: number;
  battery_end?: number;
  motor_current_avg?: number;
  suction_avg?: number;
  samples?: number;
  [key: string]: unknown;
}

export interface CommandRecord {
  command_id: string;
  thing_id: string;
  action: string;
  power_level?: number;
  created_at: number;
  status: string;
  [key: string]: unknown;
}

const TS_MAX = 9_999_999_999_999;

let lastCommandAt = 0;

export class Store {
  readonly file: string;
  private readonly db: Database.Database;

  constructor(file: string = databasePath()) {
    this.file = file;
    fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
    this.db = new Database(file);
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("busy_timeout = 5000");
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS homelink (
        pk TEXT NOT NULL,
        sk TEXT NOT NULL,
        attrs TEXT NOT NULL,
        thing_id TEXT,
        ts INTEGER,
        PRIMARY KEY (pk, sk)
      );
      CREATE INDEX IF NOT EXISTS thing_id_ts ON homelink (thing_id, ts);
    `);
  }

  private getRow(pk: string, sk: string): { attrs: string } | undefined {
    return this.db
      .prepare("SELECT attrs FROM homelink WHERE pk = ? AND sk = ?")
      .get(pk, sk) as { attrs: string } | undefined;
  }

  getShadow(thingId: string): ShadowDoc | null {
    const row = this.getRow(`THING#${thingId}`, "SHADOW");
    return row ? (JSON.parse(row.attrs) as ShadowDoc) : null;
  }

  listShadows(): Array<{ thingId: string; shadow: ShadowDoc }> {
    const rows = this.db
      .prepare("SELECT thing_id, attrs FROM homelink WHERE sk = 'SHADOW' ORDER BY thing_id")
      .all() as Array<{ thing_id: string; attrs: string }>;
    return rows
      .filter((row) => Boolean(row.thing_id))
      .map((row) => ({ thingId: row.thing_id, shadow: JSON.parse(row.attrs) as ShadowDoc }));
  }

  thingExists(thingId: string): boolean {
    return this.getShadow(thingId) !== null;
  }

  getTelemetry(thingId: string): Telemetry | null {
    const row = this.getRow(`THING#${thingId}`, "TELEMETRY#LATEST");
    return row ? (JSON.parse(row.attrs) as Telemetry) : null;
  }

  queryRuns(thingId: string, fromMs?: number, toMs?: number, limit = 200): RunRecord[] {
    const lower = `RUN#${String(fromMs ?? 0).padStart(13, "0")}`;
    const upper = `RUN#${String(toMs ?? TS_MAX).padStart(13, "0")}#\uffff`;
    const rows = this.db
      .prepare(
        `SELECT attrs FROM homelink
         WHERE pk = ? AND sk >= ? AND sk <= ?
         ORDER BY sk DESC LIMIT ?`,
      )
      .all(`THING#${thingId}`, lower, upper, limit) as Array<{ attrs: string }>;
    return rows.map((row) => JSON.parse(row.attrs) as RunRecord);
  }

  putCommand(
    thingId: string,
    command: { action: string; power_level?: number },
  ): CommandRecord {
    lastCommandAt = Math.max(Date.now(), lastCommandAt + 1);
    const record: CommandRecord = {
      command_id: randomUUID().replaceAll("-", ""),
      thing_id: thingId,
      action: command.action,
      ...(command.power_level !== undefined ? { power_level: command.power_level } : {}),
      created_at: lastCommandAt,
      status: "queued",
    };
    const key = `CMD#${String(record.created_at).padStart(13, "0")}#${record.command_id}`;
    this.db
      .prepare("INSERT INTO homelink (pk, sk, attrs, thing_id, ts) VALUES (?, ?, ?, ?, ?)")
      .run(`COMMAND#${thingId}`, key, JSON.stringify(record), thingId, record.created_at);
    return record;
  }

  close(): void {
    this.db.close();
  }
}

let cached: { file: string; store: Store } | null = null;

export function getStore(): Store {
  const file = databasePath();
  if (cached && cached.file === file) {
    return cached.store;
  }
  cached?.store.close();
  cached = { file, store: new Store(file) };
  return cached.store;
}

export function resetStoreCache(): void {
  cached?.store.close();
  cached = null;
}
