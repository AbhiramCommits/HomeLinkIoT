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

export interface DeviceDetail {
  device: DeviceSummary;
  shadow: ShadowDoc;
  telemetry: Telemetry | null;
  online: boolean;
  stale: boolean;
  last_seen_ms: number;
  fw_version: string;
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
  samples?: number;
}

export interface RunsResponse {
  device_id: string;
  count: number;
  runs: RunRecord[];
}

export interface OtaInfo {
  fw_version: string;
  sha256: string;
  size: number;
  chunks: number;
  status?: string;
}

export interface FleetAnalytics {
  generated_at: string;
  fleet: { devices: number; runs: number; span_days: number; anomalous_runs: number };
  usage: {
    span_days: number;
    devices: number;
    runs_per_device_per_week: {
      mean: number;
      min: number;
      max: number;
      histogram: Array<{ bin_start: number; bin_end: number; count: number }>;
    };
    runtime_s: { mean: number; median: number; p95: number; min: number; max: number };
  };
  runtime: { mean: number; median: number; p95: number; min: number; max: number };
  error_pareto: Array<{ code: string; count: number; pct: number }>;
  top_error_codes: Array<{ code: string; count: number; pct: number }>;
  firmware_adoption: Array<{ version: string; runs: number; pct: number }>;
  anomalies: {
    threshold: number;
    precision: number;
    recall: number;
    f1: number;
    false_alerts_per_1000_runs: number;
    true_positives: number;
    false_positives: number;
    false_negatives: number;
    alerts: number;
  };
  alerts: Array<{
    run_id: string;
    device_id: string;
    started_at: string;
    reasons: string[];
    motor_current_a: number;
    suction_pa: number;
    duration_s: number;
    z_motor_current: number | null;
    z_suction: number | null;
    actual_anomaly: boolean;
    fault_label: string;
    premature_stop: boolean;
    user_stopped_early: boolean;
  }>;
}

const API_BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8080";
const API_KEY = (import.meta.env.VITE_API_KEY as string | undefined) ?? "dev-key";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "x-api-key": API_KEY,
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const message =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : `Request failed with ${response.status}`;
    throw new ApiError(response.status, message, body);
  }
  return body as T;
}

export async function fetchDevices(): Promise<{ devices: DeviceSummary[]; count: number }> {
  return request<{ devices: DeviceSummary[]; count: number }>("/devices");
}

export async function fetchDevice(thingId: string): Promise<DeviceDetail> {
  return request<DeviceDetail>(`/devices/${encodeURIComponent(thingId)}`);
}

export async function fetchRuns(thingId: string): Promise<RunsResponse> {
  return request<RunsResponse>(`/devices/${encodeURIComponent(thingId)}/runs?limit=50`);
}

export async function sendCommand(
  thingId: string,
  action: "start" | "stop" | "dock" | "set_power",
  powerLevel?: number,
): Promise<{ command: { command_id: string; action: string }; queued: boolean }> {
  return request(`/devices/${encodeURIComponent(thingId)}/commands`, {
    method: "POST",
    body: JSON.stringify({
      action,
      ...(powerLevel !== undefined ? { power_level: powerLevel } : {}),
    }),
  });
}

export async function triggerOta(
  thingId: string,
  version = "1.3.0",
  corrupt = false,
): Promise<{ ota: OtaInfo; chunks_published: number; corrupted: boolean }> {
  return request(`/devices/${encodeURIComponent(thingId)}/ota`, {
    method: "POST",
    body: JSON.stringify({ version, corrupt }),
  });
}

export async function fetchAnalytics(): Promise<FleetAnalytics> {
  return request<FleetAnalytics>("/fleet/analytics");
}

export { API_BASE };
