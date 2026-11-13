import path from "node:path";

export const STALE_AFTER_MS = 10_000;

export function apiKey(): string {
  return process.env.HOMELINK_API_KEY ?? "dev-key";
}

export function databasePath(): string {
  return process.env.HOMELINK_DB ?? path.resolve(process.cwd(), "../homelink.db");
}

export function brokerConfig(): { host: string; port: number } {
  const host = process.env.HOMELINK_MQTT_HOST ?? "127.0.0.1";
  const port = Number(process.env.HOMELINK_MQTT_PORT ?? "18831");
  return { host, port: Number.isFinite(port) ? port : 18831 };
}

export function analyticsPath(): string {
  return (
    process.env.HOMELINK_ANALYTICS_PATH ??
    path.resolve(process.cwd(), "../analytics/out/fleet.json")
  );
}

export function apiPort(): number {
  return Number(process.env.PORT ?? "8080");
}
