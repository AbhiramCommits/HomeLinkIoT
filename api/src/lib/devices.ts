import { STALE_AFTER_MS } from "./config.js";
import { ApiError } from "./http.js";
import type { ShadowDoc, Store, Telemetry } from "./store.js";

export interface DeviceSummary {
  thing_id: string;
  state: string;
  fw_version: string;
  battery_pct: number | null;
  last_seen_ms: number;
  online: boolean;
  error_code: string;
  shadow_version: number;
}

export function deviceSummary(
  thingId: string,
  shadow: ShadowDoc,
  telemetry: Telemetry | null,
  now = Date.now(),
): DeviceSummary {
  const reported = shadow.state.reported ?? {};
  const lastSeen = telemetry?.ts_ms ?? shadow.timestamp;
  return {
    thing_id: thingId,
    state: String(reported.state ?? telemetry?.state ?? "UNKNOWN"),
    fw_version: String(reported.fw_version ?? telemetry?.fw_version ?? "unknown"),
    battery_pct: telemetry?.battery_pct ?? null,
    last_seen_ms: lastSeen,
    online: now - lastSeen <= STALE_AFTER_MS,
    error_code: String(telemetry?.error_code ?? reported.error_code ?? "NONE"),
    shadow_version: shadow.version,
  };
}

export function requireDevice(store: Store, thingId: string): ShadowDoc {
  const shadow = store.getShadow(thingId);
  if (!shadow) {
    throw new ApiError(404, `Unknown device: ${thingId}`, { thing_id: thingId });
  }
  return shadow;
}
