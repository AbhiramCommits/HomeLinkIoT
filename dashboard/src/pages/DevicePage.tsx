import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  fetchDevice,
  fetchRuns,
  sendCommand,
  triggerOta,
  type RunRecord,
  type Telemetry,
} from "../api";
import { usePolling } from "../hooks/usePolling";
import { isStale } from "../lib/stale";
import { LineChart } from "../components/LineChart";
import { ShadowDiff } from "../components/ShadowDiff";

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function formatTime(value: number): string {
  return new Date(value).toLocaleString();
}

export function DevicePage() {
  const { id = "" } = useParams<{ id: string }>();
  const detail = usePolling(() => fetchDevice(id), 2000);
  const runsState = usePolling(() => fetchRuns(id), 5000);
  const [history, setHistory] = useState<Telemetry[]>([]);
  const [power, setPower] = useState(3);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [otaBusy, setOtaBusy] = useState(false);
  const [otaMessage, setOtaMessage] = useState<string | null>(null);

  useEffect(() => {
    setHistory([]);
    setMessage(null);
    setOtaMessage(null);
  }, [id]);

  useEffect(() => {
    const sample = detail.data?.telemetry;
    if (!sample) return;
    setHistory((previous) =>
      previous.some((item) => item.seq === sample.seq)
        ? previous
        : [...previous, sample].slice(-120),
    );
  }, [detail.data]);

  useEffect(() => {
    const reported = record(detail.data?.shadow.state.reported);
    const reportedPower = reported?.power_level;
    if (typeof reportedPower === "number") setPower(reportedPower);
  }, [detail.data]);

  if (detail.error && !detail.data) {
    return (
      <section>
        <div className="banner banner-error">Could not load {id}: {detail.error}</div>
        <Link to="/devices">← Back to fleet</Link>
      </section>
    );
  }

  if (!detail.data) return <p className="muted">Loading device…</p>;

  const { shadow, telemetry, online } = detail.data;
  const reported = record(shadow.state.reported) ?? {};
  const desired = record(shadow.state.desired) ?? {};
  const currentState = String(reported.state ?? detail.data.device.state);
  const firmware = String(reported.fw_version ?? detail.data.fw_version);
  const errorCode = String(reported.error_code ?? "NONE");
  const battery = typeof telemetry?.battery_pct === "number" ? telemetry.battery_pct : null;
  const stale = isStale(detail.data.last_seen_ms) || !online;
  const ota = record(reported.ota);
  const otaStatus = typeof ota?.status === "string" ? ota.status : null;
  const otaInProgress = otaBusy || currentState === "OTA" || otaStatus === "downloading";
  const runs: RunRecord[] = runsState.data?.runs ?? [];

  const command = async (action: "start" | "stop" | "dock" | "set_power", level?: number) => {
    setBusy(true);
    setMessage(null);
    try {
      const response = await sendCommand(id, action, level);
      setMessage(
        `${response.command.action} ${response.queued ? "queued for offline device" : "sent"} (${response.command.command_id.slice(0, 8)})`,
      );
      await detail.refresh();
    } catch (caught) {
      setMessage(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const installOta = async (corrupt: boolean) => {
    setOtaBusy(true);
    setOtaMessage(corrupt ? "Publishing corrupted image…" : "Publishing firmware 1.3.0…");
    try {
      const response = await triggerOta(id, corrupt ? "1.4.0" : "1.3.0", corrupt);
      setOtaMessage(
        `${response.chunks_published} chunks published${response.corrupted ? " (corrupted image)" : ""}`,
      );
      await detail.refresh();
    } catch (caught) {
      setOtaMessage(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setOtaBusy(false);
    }
  };

  return (
    <section>
      <header className="page-head">
        <div>
          <p className="eyebrow">
            <Link to="/devices">Fleet</Link> / {id}
          </p>
          <h1 data-testid="device-title">{id}</h1>
          <p className="muted">
            Firmware <span className="mono" data-testid="fw-version">{firmware}</span> · state{" "}
            <strong data-testid="reported-state">{currentState}</strong> · error{" "}
            <span className="mono">{errorCode}</span>
          </p>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="stat-value">{battery === null ? "—" : `${battery.toFixed(0)}%`}</span>
            <span className="stat-label">battery</span>
          </div>
          <div className={`stat ${stale ? "stat-bad" : "stat-good"}`}>
            <span className="stat-value">{stale ? "offline" : "online"}</span>
            <span className="stat-label">link</span>
          </div>
        </div>
      </header>

      {stale && (
        <div className="banner banner-warning" data-testid="stale-banner">
          Stale / offline — showing last reported state (last seen{" "}
          {formatTime(detail.data.last_seen_ms)}).
        </div>
      )}

      <div className="two-col">
        <div className="panel">
          <div className="panel-head">
            <h2>Commands</h2>
            <span className="muted">desired updates over MQTT</span>
          </div>
          <div className="button-row">
            <button type="button" disabled={busy} onClick={() => void command("start")}>
              Start
            </button>
            <button type="button" disabled={busy} onClick={() => void command("stop")}>
              Stop
            </button>
            <button type="button" disabled={busy} onClick={() => void command("dock")}>
              Dock
            </button>
          </div>
          <div className="power-row">
            <label htmlFor="power">Power level: {power}</label>
            <input
              id="power"
              data-testid="power-slider"
              type="range"
              min={1}
              max={5}
              value={power}
              onChange={(event) => setPower(Number(event.target.value))}
            />
            <button type="button" disabled={busy} onClick={() => void command("set_power", power)}>
              Apply power
            </button>
          </div>
          {message && <p className="notice">{message}</p>}
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>Firmware / OTA</h2>
            <span className="muted">chunked + sha256 verified</span>
          </div>
          <div className="button-row">
            <button type="button" disabled={otaBusy} onClick={() => void installOta(false)}>
              Install v1.3.0
            </button>
            <button
              type="button"
              className="danger"
              disabled={otaBusy}
              onClick={() => void installOta(true)}
            >
              Corrupt OTA (rollback test)
            </button>
          </div>
          {otaInProgress && (
            <div className="progress" aria-label="OTA progress">
              <div className="progress-fill" />
            </div>
          )}
          <p className="notice" data-testid="ota-status">
            {otaStatus
              ? `OTA status: ${otaStatus}`
              : otaInProgress
                ? "OTA in progress…"
                : "No OTA in the current shadow."}
          </p>
          {otaMessage && <p className="muted">{otaMessage}</p>}
          {otaStatus === "rolled_back" && (
            <p className="banner banner-error" data-testid="ota-rollback">
              Checksum failed — device rolled back to {firmware}.
            </p>
          )}
        </div>
      </div>

      <div className="two-col">
        <LineChart
          title="Motor current"
          unit="A"
          series={[
            {
              label: "motor current",
              color: "#3fb59a",
              points: history.map((item) => ({ x: item.ts_ms, y: item.motor_current_a })),
            },
          ]}
        />
        <LineChart
          title="Suction"
          unit="Pa"
          series={[
            {
              label: "suction",
              color: "#e0a458",
              points: history.map((item) => ({ x: item.ts_ms, y: item.suction_pa })),
            },
          ]}
        />
      </div>

      <div className="two-col">
        <ShadowDiff desired={record(shadow.state.desired)} reported={reported} />
        <div className="panel">
          <div className="panel-head">
            <h2>Run history</h2>
            <span className="muted">{runs.length} recent runs</span>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Duration</th>
                <th>Power</th>
                <th>Firmware</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <td>{formatTime(run.started_at)}</td>
                  <td>{Math.round(run.duration_s)}s</td>
                  <td>{run.power_level ?? "—"}</td>
                  <td className="mono">{run.firmware ?? "—"}</td>
                  <td className="mono">{run.error_code ?? "NONE"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {runs.length === 0 && <p className="muted">No completed runs yet.</p>}
        </div>
      </div>
    </section>
  );
}
