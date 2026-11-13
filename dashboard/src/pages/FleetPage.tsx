import { Link } from "react-router-dom";
import { fetchDevices } from "../api";
import { usePolling } from "../hooks/usePolling";
import { isStale } from "../lib/stale";

function timeAgo(lastSeen: number, now: number): string {
  const seconds = Math.max(0, Math.round((now - lastSeen) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

export function FleetPage() {
  const { data, error, loading } = usePolling(fetchDevices, 2000);
  const now = Date.now();
  const devices = data?.devices ?? [];
  const online = devices.filter((device) => device.online).length;

  return (
    <section>
      <header className="page-head">
        <div>
          <p className="eyebrow">Fleet control</p>
          <h1>Appliances</h1>
          <p className="muted">
            {devices.length} devices · {online} online · polling every 2s
          </p>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="stat-value">{devices.length}</span>
            <span className="stat-label">devices</span>
          </div>
          <div className="stat stat-good">
            <span className="stat-value">{online}</span>
            <span className="stat-label">online</span>
          </div>
        </div>
      </header>

      {error && (
        <div className="banner banner-error" data-testid="fleet-error">
          API error: {error}
        </div>
      )}
      {loading && !data && <p className="muted">Loading fleet…</p>}

      <div className="panel">
        <table className="data-table" data-testid="fleet-table">
          <thead>
            <tr>
              <th>Device</th>
              <th>State</th>
              <th>Firmware</th>
              <th>Battery</th>
              <th>Last seen</th>
              <th>Status</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {devices.map((device) => {
              const stale = isStale(device.last_seen_ms, now);
              return (
                <tr key={device.thing_id} data-testid={`device-row-${device.thing_id}`}>
                  <td>
                    <Link className="device-link" to={`/devices/${device.thing_id}`}>
                      {device.thing_id}
                    </Link>
                    <span className="muted mono"> v{device.shadow_version}</span>
                  </td>
                  <td>
                    <span className={`state state-${device.state.toLowerCase()}`}>{device.state}</span>
                  </td>
                  <td className="mono">{device.fw_version}</td>
                  <td>
                    <div className="battery">
                      <div className="battery-fill" style={{ width: `${device.battery_pct ?? 0}%` }} />
                      <span>{device.battery_pct === null ? "—" : `${device.battery_pct.toFixed(1)}%`}</span>
                    </div>
                  </td>
                  <td>{timeAgo(device.last_seen_ms, now)}</td>
                  <td>
                    <span className={`pill ${device.online && !stale ? "pill-same" : "pill-missing"}`}>
                      {device.online && !stale ? "online" : "offline"}
                    </span>
                  </td>
                  <td className="mono">{device.error_code}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {devices.length === 0 && !loading && (
          <p className="muted">
            No devices yet. Run <code>make demo</code> to start the simulated fleet.
          </p>
        )}
      </div>
    </section>
  );
}
