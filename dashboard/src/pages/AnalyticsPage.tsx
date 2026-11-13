import { fetchAnalytics } from "../api";
import { usePolling } from "../hooks/usePolling";

export function AnalyticsPage() {
  const { data, error, loading } = usePolling(fetchAnalytics, 2000);
  if (error && !data) {
    return (
      <section>
        <div className="banner banner-error">Analytics unavailable: {error}</div>
        <p className="muted">Generate the dataset with <code>make analytics</code>.</p>
      </section>
    );
  }
  if (!data || loading) return <p className="muted">Loading fleet analytics…</p>;

  const histogram = data.usage.runs_per_device_per_week.histogram;
  const maxHistogram = Math.max(1, ...histogram.map((item) => item.count));
  const maxError = Math.max(1, ...data.error_pareto.map((item) => item.count));
  const maxFirmware = Math.max(1, ...data.firmware_adoption.map((item) => item.runs));
  const anomalies = data.anomalies;

  return (
    <section data-testid="analytics-page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Fleet analytics</p>
          <h1>Usage & reliability</h1>
          <p className="muted">
            {data.fleet.devices} devices · {data.fleet.runs} runs · {data.fleet.span_days} days
          </p>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="stat-value">{anomalies.precision.toFixed(2)}</span>
            <span className="stat-label">precision</span>
          </div>
          <div className="stat">
            <span className="stat-value">{anomalies.recall.toFixed(2)}</span>
            <span className="stat-label">recall</span>
          </div>
          <div className="stat">
            <span className="stat-value">{anomalies.f1.toFixed(2)}</span>
            <span className="stat-label">F1</span>
          </div>
        </div>
      </header>

      <div className="metrics-grid">
        <div className="metric">
          <span className="metric-label">Runtime mean / median / p95</span>
          <strong>
            {data.runtime.mean.toFixed(0)}s / {data.runtime.median.toFixed(0)}s /{" "}
            {data.runtime.p95.toFixed(0)}s
          </strong>
        </div>
        <div className="metric">
          <span className="metric-label">Runs / device / week</span>
          <strong>{data.usage.runs_per_device_per_week.mean.toFixed(2)}</strong>
        </div>
        <div className="metric">
          <span className="metric-label">False alerts / 1000 runs</span>
          <strong>{anomalies.false_alerts_per_1000_runs.toFixed(2)}</strong>
        </div>
        <div className="metric">
          <span className="metric-label">Alerts emitted</span>
          <strong>
            {anomalies.alerts} ({anomalies.true_positives} TP / {anomalies.false_positives} FP)
          </strong>
        </div>
      </div>

      <div className="two-col">
        <div className="panel">
          <div className="panel-head">
            <h2>Runs per device per week</h2>
            <span className="muted">histogram</span>
          </div>
          <div className="bars" data-testid="usage-histogram">
            {histogram.map((bucket) => (
              <div
                className="bar-row"
                key={`${bucket.bin_start}-${bucket.bin_end}`}
                title={`${bucket.bin_start}–${bucket.bin_end} runs/week`}
              >
                <span className="bar-label">
                  {bucket.bin_start.toFixed(1)}–{bucket.bin_end.toFixed(1)}
                </span>
                <div className="bar-track">
                  <div
                    className="bar-fill"
                    style={{ width: `${(bucket.count / maxHistogram) * 100}%` }}
                  />
                </div>
                <span className="bar-value">{bucket.count}</span>
              </div>
            ))}
          </div>
          <div className="panel">
            <div className="panel-head">
              <h2>Firmware adoption</h2>
              <span className="muted">% of runs</span>
            </div>
            <div className="bars">
              {data.firmware_adoption.map((item) => (
                <div className="bar-row" key={item.version}>
                  <span className="bar-label mono">{item.version}</span>
                  <div className="bar-track">
                    <div
                      className="bar-fill bar-fill-alt"
                      style={{ width: `${(item.runs / maxFirmware) * 100}%` }}
                    />
                  </div>
                  <span className="bar-value">{item.pct.toFixed(1)}%</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>Error-code Pareto</h2>
            <span className="muted">% of all faults</span>
          </div>
          <div className="bars" data-testid="error-pareto">
            {data.error_pareto.map((item, index) => (
              <div className="bar-row" key={item.code}>
                <span className="bar-label mono">
                  {index + 1}. {item.code}
                </span>
                <div className="bar-track">
                  <div
                    className="bar-fill bar-fill-error"
                    style={{ width: `${(item.count / maxError) * 100}%` }}
                  />
                </div>
                <span className="bar-value">
                  {item.count} · {item.pct.toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2>Anomaly alert feed</h2>
          <span className="muted">
            rolling median + MAD, |z| &gt; {anomalies.threshold}
          </span>
        </div>
        <div className="alert-feed" data-testid="alert-feed">
          {data.alerts.map((alert) => (
            <div
              className={`alert ${alert.actual_anomaly ? "alert-true" : "alert-false"}`}
              key={alert.run_id}
            >
              <div>
                <strong className="mono">{alert.device_id}</strong>
                <span className="muted"> · {new Date(alert.started_at).toLocaleString()}</span>
              </div>
              <div className="alert-reasons">{alert.reasons.join(" · ")}</div>
              <div className="muted mono">
                current z {alert.z_motor_current ?? "—"} · suction z {alert.z_suction ?? "—"} ·{" "}
                {alert.actual_anomaly ? `injected ${alert.fault_label}` : "no injected fault"}
              </div>
            </div>
          ))}
          {data.alerts.length === 0 && <p className="muted">No alerts.</p>}
        </div>
      </div>
    </section>
  );
}
