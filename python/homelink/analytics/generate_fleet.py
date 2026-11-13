"""Seeded consumer-fleet run generator with injected ground-truth faults."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "data" / "fleet_runs.csv"
FAULT_CHOICES = ["BRUSH_JAM", "FILTER_CLOG", "WHEEL_STALL"]
FAULT_PROBABILITIES = [0.40, 0.35, 0.25]


def _firmware_for(progress: float, rng: np.random.Generator) -> str:
    if progress < 0.20:
        return str(rng.choice(["1.0.0", "1.1.0"], p=[0.7, 0.3]))
    if progress < 0.50:
        return str(rng.choice(["1.0.0", "1.1.0", "1.2.0"], p=[0.1, 0.5, 0.4]))
    if progress < 0.85:
        return str(rng.choice(["1.1.0", "1.2.0", "1.3.0"], p=[0.15, 0.70, 0.15]))
    return str(rng.choice(["1.2.0", "1.3.0"], p=[0.4, 0.6]))


def generate(seed: int, devices: int, days: int) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    base = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for device_index in range(devices):
        device_id = f"vac-{device_index + 1:03d}"
        run_count = max(10, int(rng.poisson(days * 1.0)))
        offsets = np.sort(rng.uniform(0, days * 86_400, run_count))
        for run_index, offset in enumerate(offsets):
            started = base + timedelta(seconds=float(offset))
            power_level = int(rng.choice([1, 2, 3, 4, 5], p=[0.10, 0.20, 0.35, 0.25, 0.10]))
            duration = float(np.clip(rng.lognormal(np.log(900.0), 0.25), 300.0, 2400.0))
            baseline_current = 0.55 + 0.32 * power_level
            baseline_suction = 780.0 + 250.0 * power_level
            current = baseline_current * (1.0 + float(rng.normal(0, 0.020)))
            suction = baseline_suction * (1.0 + float(rng.normal(0, 0.015)))
            suction_start = suction * (1.0 + float(rng.normal(0, 0.02)))
            suction_end = suction * (1.0 + float(rng.normal(0, 0.04)))
            battery_start = float(np.clip(rng.normal(78, 15), 25, 100))
            fault_label = "NONE"
            premature_stop = False
            user_stopped_early = False

            if run_index >= 14 and rng.random() < 0.075:
                fault_label = str(rng.choice(FAULT_CHOICES, p=FAULT_PROBABILITIES))
                if fault_label == "BRUSH_JAM":
                    current *= float(rng.uniform(2.4, 3.2))
                    duration *= float(rng.uniform(0.55, 0.9))
                elif fault_label == "FILTER_CLOG":
                    suction *= float(rng.uniform(0.35, 0.55))
                    suction_end *= 0.5
                else:  # WHEEL_STALL
                    current *= float(rng.uniform(1.8, 2.3))
                    suction *= float(rng.uniform(0.6, 0.8))
                    duration *= float(rng.uniform(0.10, 0.25))
                    premature_stop = True
            elif run_index >= 14 and rng.random() < 0.015:
                duration *= float(rng.uniform(0.12, 0.25))
                premature_stop = True
            elif rng.random() < 0.02:
                duration *= float(rng.uniform(0.12, 0.25))
                user_stopped_early = True

            battery_end = max(5.0, battery_start - duration / 3600.0 * (4.0 + power_level))
            error_code = fault_label if fault_label != "NONE" else "NONE"
            progress = run_index / max(1, run_count - 1)
            rows.append(
                {
                    "run_id": f"{device_id}-{run_index:04d}",
                    "device_id": device_id,
                    "started_at": started.isoformat().replace("+00:00", "Z"),
                    "started_ms": int(started.timestamp() * 1000),
                    "duration_s": round(duration, 2),
                    "power_level": power_level,
                    "battery_start": round(battery_start, 2),
                    "battery_end": round(battery_end, 2),
                    "motor_current_a": round(current, 4),
                    "suction_pa": round(suction, 2),
                    "suction_start": round(suction_start, 2),
                    "suction_end": round(suction_end, 2),
                    "error_code": error_code,
                    "firmware": _firmware_for(progress, rng),
                    "fault_label": fault_label,
                    "premature_stop": int(premature_stop),
                    "user_stopped_early": int(user_stopped_early),
                    "anomalous": int(fault_label != "NONE" or premature_stop),
                }
            )
    rows.sort(key=lambda row: (row["device_id"], row["started_ms"]))
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate a seeded HomeLink fleet dataset")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--devices", type=int, default=40)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    rows = generate(args.seed, args.devices, args.days)
    path = Path(args.out)
    write_csv(rows, path)
    faults = sum(1 for row in rows if row["fault_label"] != "NONE")
    premature = sum(int(row["premature_stop"]) for row in rows)
    anomalous = sum(int(row["anomalous"]) for row in rows)
    print(
        json.dumps(
            {
                "dataset": str(path),
                "seed": args.seed,
                "devices": args.devices,
                "days": args.days,
                "runs": len(rows),
                "injected_faults": faults,
                "premature_stops": premature,
                "anomalous_runs": anomalous,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
