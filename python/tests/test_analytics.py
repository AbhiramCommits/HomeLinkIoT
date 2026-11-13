from __future__ import annotations

import json
from pathlib import Path

import pytest

from homelink.analytics.fleet import (
    detect,
    error_pareto,
    firmware_adoption,
    load_rows,
    operating_point_baselines,
    robust_z,
    usage_analytics,
)
from homelink.analytics.fleet import (
    main as analytics_main,
)
from homelink.analytics.generate_fleet import generate, write_csv


def test_generation_is_deterministic_and_labeled() -> None:
    first = generate(seed=7, devices=5, days=30)
    second = generate(seed=7, devices=5, days=30)
    assert first == second
    assert len(first) > 100
    for row in first:
        if row["fault_label"] != "NONE":
            assert row["error_code"] == row["fault_label"]
            assert row["anomalous"] == 1
        assert row["anomalous"] == int(row["fault_label"] != "NONE" or bool(row["premature_stop"]))


def test_csv_round_trip_and_pipeline_outputs(tmp_path: Path) -> None:
    rows = generate(seed=7, devices=8, days=30)
    data_path = tmp_path / "fleet.csv"
    write_csv(rows, data_path)
    loaded = load_rows(data_path)
    assert len(loaded) == len(rows)
    assert loaded[0]["device_id"] == rows[0]["device_id"]

    out_dir = tmp_path / "out"
    analytics_main(["--data", str(data_path), "--out", str(out_dir)])
    summary = json.loads((out_dir / "fleet.json").read_text(encoding="utf-8"))
    assert summary["fleet"]["devices"] == 8
    assert summary["fleet"]["runs"] == len(rows)
    assert 0.0 <= summary["anomalies"]["precision"] <= 1.0
    assert 0.0 <= summary["anomalies"]["recall"] <= 1.0
    assert sum(bucket["count"] for bucket in summary["usage"]["runs_per_device_per_week"]["histogram"]) == 8
    for name in [
        "report.md",
        "runs_per_device_week.png",
        "runtime_distribution.png",
        "error_pareto.png",
        "firmware_adoption.png",
        "anomaly_scatter.png",
    ]:
        assert (out_dir / name).exists(), name


def test_detector_and_aggregations_on_synthetic_rows() -> None:
    rows = []
    base = 1_700_000_000_000
    for index in range(20):
        rows.append(
            {
                "run_id": f"r-{index}",
                "device_id": "vac-synth",
                "started_at": f"2026-01-01T00:00:{index:02d}Z",
                "started_ms": base + index * 1000,
                "duration_s": 900.0,
                "power_level": 3,
                "battery_start": 80.0,
                "battery_end": 75.0,
                "motor_current_a": 1.5 + (0.01 * (index % 3)),
                "suction_pa": 1500.0,
                "suction_start": 1510.0,
                "suction_end": 1490.0,
                "error_code": "NONE",
                "firmware": "1.2.0",
                "fault_label": "NONE",
                "premature_stop": 0,
                "user_stopped_early": 0,
                "anomalous": 0,
            }
        )
    rows[15]["motor_current_a"] = 5.0
    rows[15]["fault_label"] = "BRUSH_JAM"
    rows[15]["error_code"] = "BRUSH_JAM"
    rows[15]["anomalous"] = 1
    alerts, metrics, _ = detect(rows)
    assert any(alert["run_id"] == "r-15" for alert in alerts)
    assert metrics["true_positives"] >= 1
    assert metrics["false_negatives"] == 0
    assert robust_z(10.0, [1.0, 1.0, 1.0, 1.0, 1.0], min_scale=1.0) == pytest.approx(9.0)
    assert set(operating_point_baselines(rows)) == {3}
    usage = usage_analytics(rows)
    assert usage["devices"] == 1
    assert usage["runtime_s"]["median"] == 900.0
    assert error_pareto(rows)[0]["code"] == "BRUSH_JAM"
    assert firmware_adoption(rows)[0]["version"] == "1.2.0"
