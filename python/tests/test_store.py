from __future__ import annotations

import time

import pytest
from moto import mock_aws

from homelink.cloud.store import DynamoDbStore, SqliteStore, make_store


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    if request.param == "sqlite":
        instance = SqliteStore(tmp_path / "parity.db")
        try:
            yield instance
        finally:
            instance.close()
    else:
        with mock_aws():
            instance = DynamoDbStore(table_name="homelink-parity")
            try:
                yield instance
            finally:
                instance.close()


def test_shadow_put_get(store) -> None:
    document = {
        "state": {"desired": {"power_level": 3}, "reported": {"power_level": 2}},
        "version": 2,
        "timestamp": 1_700_000_000_000,
        "clientToken": "t",
    }
    store.put_shadow("vac-001", document)
    assert store.get_shadow("vac-001") == document
    assert store.get_shadow("missing") is None
    document["version"] = 3
    store.put_shadow("vac-001", document)
    assert store.get_shadow("vac-001")["version"] == 3


def test_run_history_order_and_range(store) -> None:
    base = 1_700_000_000_000
    for index in range(5):
        store.append_run(
            "vac-001",
            {
                "run_id": f"run-{index}",
                "started_at": base + index * 1000,
                "duration_s": 10.0 + index,
                "error_code": "NONE",
            },
        )
    store.append_run("vac-002", {"run_id": "other", "started_at": base, "duration_s": 1.0})
    runs = store.query_runs("vac-001", limit=10)
    assert [run["run_id"] for run in runs] == [f"run-{i}" for i in range(4, -1, -1)]
    ranged = store.query_runs("vac-001", from_ms=base + 1000, to_ms=base + 3000, limit=10)
    assert {run["run_id"] for run in ranged} == {"run-1", "run-2", "run-3"}
    assert len(store.query_runs("vac-001", limit=2)) == 2


def test_command_queue_delivers_once(store) -> None:
    first = store.put_command("vac-001", {"command": "start"})
    second = store.put_command("vac-001", {"command": "set_power", "power_level": 4})
    assert first["status"] == "queued"
    pending = store.pop_commands("vac-001")
    assert [record["command_id"] for record in pending] == [first["command_id"], second["command_id"]]
    assert all(record["status"] == "delivered" for record in pending)
    assert store.pop_commands("vac-001") == []
    assert store.put_command("vac-002", {"command": "dock"})["status"] == "queued"


def test_telemetry_latest_and_things(store) -> None:
    sample = {
        "thing_id": "vac-001",
        "seq": 1,
        "ts_ms": int(time.time() * 1000),
        "state": "RUNNING",
        "battery_pct": 77.0,
    }
    store.put_telemetry("vac-001", sample)
    assert store.get_telemetry("vac-001") == sample
    store.put_shadow("vac-001", {"state": {}, "version": 1, "timestamp": 1})
    store.put_shadow("vac-002", {"state": {}, "version": 1, "timestamp": 2})
    assert store.list_things() == ["vac-001", "vac-002"]


def test_make_store_urls(tmp_path) -> None:
    instance = make_store(f"sqlite:///{tmp_path / 'url.db'}")
    assert isinstance(instance, SqliteStore)
    instance.close()
    with pytest.raises(ValueError):
        make_store("postgres://nope")
