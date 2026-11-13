"""Fleet analytics: usage, error Pareto, firmware adoption, MAD anomaly detection."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = ROOT / "data" / "fleet_runs.csv"
DEFAULT_OUT = ROOT / "analytics" / "out"
THRESHOLD = 3.5
WINDOW = 20
MIN_HISTORY = 12
NUMERIC = {
    "started_ms": int,
    "duration_s": float,
    "power_level": int,
    "battery_start": float,
    "battery_end": float,
    "motor_current_a": float,
    "suction_pa": float,
    "suction_start": float,
    "suction_end": float,
    "premature_stop": int,
    "user_stopped_early": int,
    "anomalous": int,
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        for key, caster in NUMERIC.items():
            row[key] = caster(row[key])
    return rows


def operating_point_baselines(rows: list[dict[str, Any]]) -> dict[int, tuple[float, float]]:
    """Fleet median current/suction per power level (robust to ~6% injected outliers)."""
    by_power: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_power[int(row["power_level"])].append(row)
    baselines: dict[int, tuple[float, float]] = {}
    for power, power_rows in by_power.items():
        baselines[power] = (
            float(np.median([row["motor_current_a"] for row in power_rows])),
            float(np.median([row["suction_pa"] for row in power_rows])),
        )
    return baselines


def robust_z(value: float, history: list[float], min_scale: float = 0.0) -> float:
    values = np.asarray(history, dtype=float)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    scale = 1.4826 * mad
    # A MAD floor prevents a degenerate scale when a short window happens to
    # be tightly clustered; 3% is the modeled sensor noise floor.
    scale = max(scale, min_scale, 1e-9)
    return (value - median) / scale


def detect(
    rows: list[dict[str, Any]], threshold: float = THRESHOLD
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_device[row["device_id"]].append(row)
    baselines = operating_point_baselines(rows)
    alerts: list[dict[str, Any]] = []
    predicted_ids: set[str] = set()
    fault_totals: Counter[str] = Counter()
    fault_detected: Counter[str] = Counter()
    tp = fp = fn = tn = 0

    for device_id in sorted(by_device):
        runs = sorted(by_device[device_id], key=lambda row: row["started_ms"])
        history: deque[dict[str, Any]] = deque(maxlen=WINDOW)
        for row in runs:
            reasons: list[str] = []
            z_current: float | None = None
            z_suction: float | None = None
            baseline_current, baseline_suction = baselines[int(row["power_level"])]
            # Compare each sample as a fraction of its operating point so the
            # MAD scale is consistent across power levels.
            residual_current = (float(row["motor_current_a"]) - baseline_current) / baseline_current
            residual_suction = (float(row["suction_pa"]) - baseline_suction) / baseline_suction
            if len(history) >= MIN_HISTORY:
                z_current = robust_z(
                    residual_current,
                    [
                        (float(item["motor_current_a"]) - baselines[int(item["power_level"])][0])
                        / baselines[int(item["power_level"])][0]
                        for item in history
                    ],
                    min_scale=0.03,
                )
                z_suction = robust_z(
                    residual_suction,
                    [
                        (float(item["suction_pa"]) - baselines[int(item["power_level"])][1])
                        / baselines[int(item["power_level"])][1]
                        for item in history
                    ],
                    min_scale=0.03,
                )
                if abs(z_current) > threshold:
                    reasons.append("MOTOR_CURRENT_OUTLIER")
                if abs(z_suction) > threshold:
                    reasons.append("SUCTION_OUTLIER")
                durations = [float(item["duration_s"]) for item in history]
                if float(row["duration_s"]) < 0.30 * float(np.median(durations)):
                    reasons.append("PREMATURE_STOP")

            predicted = bool(reasons)
            actual = row["fault_label"] != "NONE" or bool(row["premature_stop"])
            if row["fault_label"] != "NONE":
                fault_totals[row["fault_label"]] += 1
                if predicted:
                    fault_detected[row["fault_label"]] += 1
            if predicted and actual:
                tp += 1
            elif predicted:
                fp += 1
            elif actual:
                fn += 1
            else:
                tn += 1
            if predicted:
                predicted_ids.add(row["run_id"])
                alerts.append(
                    {
                        "run_id": row["run_id"],
                        "device_id": device_id,
                        "started_at": row["started_at"],
                        "reasons": reasons,
                        "motor_current_a": row["motor_current_a"],
                        "suction_pa": row["suction_pa"],
                        "duration_s": row["duration_s"],
                        "z_motor_current": round(z_current, 3) if z_current is not None else None,
                        "z_suction": round(z_suction, 3) if z_suction is not None else None,
                        "actual_anomaly": actual,
                        "fault_label": row["fault_label"],
                        "premature_stop": bool(row["premature_stop"]),
                        "user_stopped_early": bool(row["user_stopped_early"]),
                    }
                )
            history.append(row)

    evaluated = tp + fp + fn + tn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    false_per_1000 = fp / evaluated * 1000.0 if evaluated else 0.0
    metrics = {
        "threshold": threshold,
        "window": WINDOW,
        "min_history": MIN_HISTORY,
        "evaluated_runs": evaluated,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "alerts": len(alerts),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_alerts_per_1000_runs": round(false_per_1000, 3),
        "by_fault": {
            fault: {
                "injected": fault_totals[fault],
                "detected": fault_detected[fault],
                "rate": round(
                    fault_detected[fault] / fault_totals[fault]
                    if fault_totals[fault]
                    else 0.0,
                    4,
                ),
            }
            for fault in sorted(fault_totals)
        },
    }
    alerts.sort(key=lambda alert: alert["started_at"], reverse=True)
    return alerts, metrics, predicted_ids and {"predicted_ids": sorted(predicted_ids)} or {}


def usage_analytics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_device[row["device_id"]].append(row)
    started = min(row["started_ms"] for row in rows)
    ended = max(row["started_ms"] for row in rows)
    span_days = max(1.0, (ended - started) / 86_400_000)
    weeks = span_days / 7.0
    rates = sorted(len(device_runs) / weeks for device_runs in by_device.values())
    hist, edges = np.histogram(rates, bins=8)
    durations = np.asarray([row["duration_s"] for row in rows], dtype=float)
    return {
        "span_days": round(span_days, 2),
        "devices": len(by_device),
        "runs_per_device_per_week": {
            "mean": round(float(np.mean(rates)), 3),
            "min": round(float(np.min(rates)), 3),
            "max": round(float(np.max(rates)), 3),
            "histogram": [
                {
                    "bin_start": round(float(edges[index]), 3),
                    "bin_end": round(float(edges[index + 1]), 3),
                    "count": int(hist[index]),
                }
                for index in range(len(hist))
            ],
        },
        "runtime_s": {
            "mean": round(float(np.mean(durations)), 2),
            "median": round(float(np.median(durations)), 2),
            "p95": round(float(np.percentile(durations, 95)), 2),
            "min": round(float(np.min(durations)), 2),
            "max": round(float(np.max(durations)), 2),
        },
    }


def error_pareto(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(row["error_code"] for row in rows if row["error_code"] != "NONE")
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        {
            "code": code,
            "count": count,
            "pct": round(count / total * 100.0, 2) if total else 0.0,
        }
        for code, count in ordered
    ]


def firmware_adoption(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(row["firmware"] for row in rows)
    total = len(rows)
    return [
        {
            "version": version,
            "runs": count,
            "pct": round(count / total * 100.0, 2) if total else 0.0,
        }
        for version, count in sorted(counts.items())
    ]


def _plot_usage(usage: dict[str, Any], out_dir: Path) -> None:
    histogram = usage["runs_per_device_per_week"]["histogram"]
    centers = [(item["bin_start"] + item["bin_end"]) / 2 for item in histogram]
    counts = [item["count"] for item in histogram]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(centers, counts, width=(centers[1] - centers[0]) * 0.9 if len(centers) > 1 else 0.8)
    ax.set_title("Runs per device per week")
    ax.set_xlabel("runs / week")
    ax.set_ylabel("devices")
    fig.tight_layout()
    fig.savefig(out_dir / "runs_per_device_week.png", dpi=130)
    plt.close(fig)


def _plot_runtime(rows: list[dict[str, Any]], runtime: dict[str, Any], out_dir: Path) -> None:
    durations = [row["duration_s"] for row in rows]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(durations, bins=30, color="#2b7a78", edgecolor="white")
    for label, value, color in [
        ("mean", runtime["mean"], "#e07a5f"),
        ("median", runtime["median"], "#3d405b"),
        ("p95", runtime["p95"], "#81b29a"),
    ]:
        ax.axvline(value, linestyle="--", color=color, label=f"{label} {value:.0f}s")
    ax.set_title("Run duration distribution")
    ax.set_xlabel("duration (s)")
    ax.set_ylabel("runs")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "runtime_distribution.png", dpi=130)
    plt.close(fig)


def _plot_pareto(pareto: list[dict[str, Any]], out_dir: Path) -> None:
    if not pareto:
        return
    labels = [item["code"] for item in pareto]
    counts = [item["count"] for item in pareto]
    cumulative = np.cumsum(counts) / sum(counts) * 100.0
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(labels, counts, color="#b23a48")
    ax.set_ylabel("fault count")
    ax.set_title("Error-code Pareto")
    ax2 = ax.twinx()
    ax2.plot(labels, cumulative, color="#1d3557", marker="o")
    ax2.set_ylabel("cumulative %")
    ax2.set_ylim(0, 105)
    fig.tight_layout()
    fig.savefig(out_dir / "error_pareto.png", dpi=130)
    plt.close(fig)


def _plot_firmware(adoption: list[dict[str, Any]], out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar([item["version"] for item in adoption], [item["runs"] for item in adoption], color="#3d5a80")
    ax.set_title("Firmware adoption (runs)")
    ax.set_xlabel("firmware version")
    ax.set_ylabel("runs")
    fig.tight_layout()
    fig.savefig(out_dir / "firmware_adoption.png", dpi=130)
    plt.close(fig)


def _plot_anomalies(rows: list[dict[str, Any]], alerts: list[dict[str, Any]], out_dir: Path) -> None:
    alert_ids = {alert["run_id"] for alert in alerts}
    normal = [row for row in rows if row["run_id"] not in alert_ids]
    flagged = [row for row in rows if row["run_id"] in alert_ids]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.scatter(
        [row["motor_current_a"] for row in normal],
        [row["suction_pa"] for row in normal],
        s=12,
        alpha=0.5,
        label="normal",
        color="#457b9d",
    )
    ax.scatter(
        [row["motor_current_a"] for row in flagged],
        [row["suction_pa"] for row in flagged],
        s=26,
        alpha=0.9,
        label="alert",
        color="#e63946",
    )
    ax.set_title("MAD anomaly alerts by current/suction")
    ax.set_xlabel("motor current (A)")
    ax.set_ylabel("suction (Pa)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "anomaly_scatter.png", dpi=130)
    plt.close(fig)


def write_report(
    path: Path,
    summary: dict[str, Any],
    pareto: list[dict[str, Any]],
    adoption: list[dict[str, Any]],
) -> None:
    anomalies = summary["anomalies"]
    usage = summary["usage"]
    runtime = usage["runtime_s"]
    lines = [
        "# HomeLink Fleet Analytics Report",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Fleet",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| devices | {summary['fleet']['devices']} |",
        f"| runs | {summary['fleet']['runs']} |",
        f"| span (days) | {usage['span_days']} |",
        f"| runs/device/week (mean) | {usage['runs_per_device_per_week']['mean']} |",
        f"| runtime mean / median / p95 (s) | {runtime['mean']} / {runtime['median']} / {runtime['p95']} |",
        "",
        "## Top error codes",
        "",
        "| code | count | % of faults |",
        "| --- | --- | --- |",
    ]
    for item in pareto[:3]:
        lines.append(f"| {item['code']} | {item['count']} | {item['pct']}% |")
    lines += [
        "",
        "## Firmware adoption",
        "",
        "| version | runs | % |",
        "| --- | --- | --- |",
    ]
    for item in adoption:
        lines.append(f"| {item['version']} | {item['runs']} | {item['pct']}% |")
    lines += [
        "",
        "## Anomaly detection (rolling median + MAD, threshold 3.5)",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| evaluated runs | {anomalies['evaluated_runs']} |",
        f"| precision | {anomalies['precision']} |",
        f"| recall | {anomalies['recall']} |",
        f"| F1 | {anomalies['f1']} |",
        f"| false alerts / 1000 runs | {anomalies['false_alerts_per_1000_runs']} |",
        f"| true positives | {anomalies['true_positives']} |",
        f"| false positives | {anomalies['false_positives']} |",
        f"| false negatives | {anomalies['false_negatives']} |",
        "",
        "| injected fault | injected | detected | rate |",
        "| --- | --- | --- | --- |",
    ]
    for fault, values in anomalies["by_fault"].items():
        lines.append(
            f"| {fault} | {values['injected']} | {values['detected']} | {values['rate']} |"
        )
    lines += [
        "",
        "Charts: `runs_per_device_week.png`, `runtime_distribution.png`,",
        "`error_pareto.png`, `firmware_adoption.png`, `anomaly_scatter.png`.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Compute HomeLink fleet analytics")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    args = parser.parse_args(argv)
    rows = load_rows(Path(args.data))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    alerts, metrics, _ = detect(rows, threshold=args.threshold)
    usage = usage_analytics(rows)
    pareto = error_pareto(rows)
    adoption = firmware_adoption(rows)
    generated_at = datetime.now(UTC).isoformat()
    summary: dict[str, Any] = {
        "generated_at": generated_at,
        "fleet": {
            "devices": usage["devices"],
            "runs": len(rows),
            "span_days": usage["span_days"],
            "anomalous_runs": sum(1 for row in rows if row["anomalous"]),
        },
        "usage": usage,
        "runtime": usage["runtime_s"],
        "error_pareto": pareto,
        "top_error_codes": pareto[:3],
        "firmware_adoption": adoption,
        "anomalies": metrics,
        "alerts": alerts[:100],
    }
    (out_dir / "fleet.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _plot_usage(usage, out_dir)
    _plot_runtime(rows, usage["runtime_s"], out_dir)
    _plot_pareto(pareto, out_dir)
    _plot_firmware(adoption, out_dir)
    _plot_anomalies(rows, alerts, out_dir)
    write_report(out_dir / "report.md", summary, pareto, adoption)

    print(f"FLEET devices={usage['devices']} runs={len(rows)} span_days={usage['span_days']}")
    print(
        "RUNTIME_S "
        f"mean={usage['runtime_s']['mean']} median={usage['runtime_s']['median']} "
        f"p95={usage['runtime_s']['p95']}"
    )
    print(
        "USAGE runs_per_device_per_week="
        f"{usage['runs_per_device_per_week']['mean']} "
        f"min={usage['runs_per_device_per_week']['min']} "
        f"max={usage['runs_per_device_per_week']['max']}"
    )
    print(
        "TOP_ERRORS "
        + " ".join(f"{item['code']}={item['pct']}%" for item in pareto[:3])
    )
    print(
        "ANOMALY "
        f"precision={metrics['precision']} recall={metrics['recall']} "
        f"f1={metrics['f1']} false_alerts_per_1000_runs={metrics['false_alerts_per_1000_runs']} "
        f"tp={metrics['true_positives']} fp={metrics['false_positives']} fn={metrics['false_negatives']}"
    )
    print(f"OUTPUT {out_dir / 'fleet.json'}")


if __name__ == "__main__":
    main()
