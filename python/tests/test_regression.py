from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from homelink.analytics.fleet import (
    detect,
    error_pareto,
    firmware_adoption,
    load_rows,
    usage_analytics,
)
from homelink.analytics.generate_fleet import generate, write_csv
from homelink.cloud.ota import FirmwareServer
from homelink.cloud.shadow import (
    accepted_topic,
    delta_topic,
    rejected_topic,
    update_topic,
)
from homelink.device.appliance import Appliance, ApplianceState
from tests.helpers import RawClient

THING = "vac-reg"
GOLDEN = Path(__file__).parent / "golden" / "analytics_golden.json"


def wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def regression_stack(broker, shadow, tmp_path):
    service, store, port = shadow
    appliance = Appliance(
        thing_id=THING,
        broker_host="127.0.0.1",
        broker_port=port,
        spool_dir=tmp_path / "spool",
        seed=21,
        sample_interval=0.1,
        ota_timeout=1.0,
    )
    appliance.set_credentials("RegressionNet", "psk")
    appliance.start()
    assert appliance.wait_for_state(ApplianceState.ONLINE, 3)
    try:
        yield appliance, store, service, port, broker[0]
    finally:
        appliance.stop()


@pytest.mark.regression
@pytest.mark.slow
def test_broker_outage_spools_replays_and_loses_nothing(regression_stack) -> None:
    appliance, _store, service, _port, broker_instance = regression_stack
    time.sleep(0.6)
    runner = broker_instance.runner  # type: ignore[attr-defined]
    runner.stop()
    time.sleep(3.0)
    spooled = appliance.spooled_samples()
    assert len(spooled) > 0
    assert spooled == sorted(spooled, key=lambda sample: sample["seq"])

    runner.start()
    assert wait_until(lambda: appliance.replayed_count >= len(spooled), 10)
    assert wait_until(lambda: appliance.spooled_samples() == [], 10)
    time.sleep(1.0)
    appliance.stop()
    time.sleep(0.8)

    generated = appliance.samples_generated
    persisted = service.stats()["telemetry_persisted"]
    duplicates = service.stats()["duplicates_dropped"]
    assert generated - persisted == 0, (generated, persisted, duplicates)
    latest = _store.get_telemetry(THING)
    assert latest is not None
    assert latest["seq"] == generated, (generated, persisted, duplicates, latest["seq"])


@pytest.mark.regression
def test_command_to_offline_device_is_queued_and_applied(broker, tmp_path) -> None:
    broker_instance, port = broker
    first = Appliance(
        thing_id="vac-offline",
        broker_host="127.0.0.1",
        broker_port=port,
        spool_dir=tmp_path / "spool",
        seed=31,
        sample_interval=0.1,
    )
    first.set_credentials("OfflineNet", "psk")
    first.start()
    assert first.wait_for_state(ApplianceState.ONLINE, 3)
    first.stop()
    assert wait_until(
        lambda: all(
            session.client_id != "vac-offline"
            for session in broker_instance.sessions.values()
            if session.connection is not None
        ),
        4,
    )

    publisher = RawClient("127.0.0.1", port, client_id="offline-command-pub")
    publisher.connect()
    publisher.publish(
        "homelink/vac-offline/commands",
        json.dumps({"command_id": "offline-cmd-1", "action": "start"}),
        qos=1,
    )
    publisher.disconnect()

    second = Appliance(
        thing_id="vac-offline",
        broker_host="127.0.0.1",
        broker_port=port,
        spool_dir=tmp_path / "spool",
        seed=32,
        sample_interval=0.1,
    )
    second.set_credentials("OfflineNet", "psk")
    second.start()
    try:
        assert second.wait_for_state(ApplianceState.RUNNING, 6)
        assert second._reported_command is not None
        reported_id = second._reported_command.get("id") or second._reported_command.get("command_id")
        assert reported_id == "offline-cmd-1"
    finally:
        second.stop()


@pytest.mark.regression
def test_stale_shadow_write_rejected_and_delta_converges(shadow) -> None:
    _service, store, port = shadow
    client = RawClient("127.0.0.1", port, client_id="shadow-regression")
    client.connect()
    client.subscribe([("$iot/things/+/shadow/#", 1)])

    client.publish(
        update_topic("vac-reg-shadow"),
        json.dumps({"state": {"desired": {"power_level": 5}}, "version": 0}),
        qos=1,
    )
    accepted = client.recv_topic(accepted_topic("vac-reg-shadow"))
    client.ack(accepted)
    delta = client.recv_topic(delta_topic("vac-reg-shadow"))
    client.ack(delta)

    client.publish(
        update_topic("vac-reg-shadow"),
        json.dumps({"state": {"reported": {"power_level": 5}}, "version": 1}),
        qos=1,
    )
    converged = json.loads(client.recv_topic(accepted_topic("vac-reg-shadow")).payload)
    assert converged["state"]["delta"] == {}
    with pytest.raises(TimeoutError):
        client.recv(0.4)

    client.publish(
        update_topic("vac-reg-shadow"),
        json.dumps({"state": {"desired": {"power_level": 1}}, "version": 1}),
        qos=1,
    )
    rejected = json.loads(client.recv_topic(rejected_topic("vac-reg-shadow")).payload)
    assert rejected["code"] == 409
    stored = store.get_shadow("vac-reg-shadow")
    assert stored is not None
    assert stored["state"]["desired"]["power_level"] == 5
    client.disconnect()


@pytest.mark.regression
@pytest.mark.slow
def test_ota_corruption_rolls_back_and_device_stays_serviceable(regression_stack) -> None:
    appliance, _store, _service, port, _broker = regression_stack
    server = FirmwareServer("127.0.0.1", port, client_id=f"ota-regression-{port}")
    server.start()
    try:
        original = appliance.fw_version
        server.trigger(THING, "1.9.0", corrupt=True)
        assert wait_until(lambda: appliance.ota_status == "rolled_back", 8)
        assert appliance.fw_version == original
        assert appliance.error_code == "OTA_CHECKSUM"

        before = appliance.samples_generated
        appliance.begin_run()
        assert appliance.wait_for_state(ApplianceState.RUNNING, 3)
        assert wait_until(lambda: appliance.samples_generated > before + 2, 3)
        assert appliance.ota_status == "rolled_back"
    finally:
        server.stop()


@pytest.mark.regression
def test_analytics_golden_summary(tmp_path: Path) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    rows = generate(seed=7, devices=40, days=30)
    data_path = tmp_path / "fleet_runs.csv"
    write_csv(rows, data_path)
    loaded = load_rows(data_path)
    alerts, metrics = detect(loaded)
    usage = usage_analytics(loaded)
    pareto = error_pareto(loaded)
    adoption = firmware_adoption(loaded)

    assert len(rows) == golden["fleet"]["runs"]
    assert usage["devices"] == golden["fleet"]["devices"]
    assert sum(1 for row in rows if row["anomalous"]) == golden["fleet"]["anomalous_runs"]
    for key in ("mean", "median", "p95", "min", "max"):
        assert usage["runtime_s"][key] == pytest.approx(golden["runtime"][key], abs=1e-6)
    for key in ("mean", "min", "max"):
        assert usage["runs_per_device_per_week"][key] == pytest.approx(
            golden["usage"]["runs_per_device_per_week"][key], abs=1e-6
        )
    assert pareto == golden["error_pareto"]
    assert adoption == golden["firmware_adoption"]
    for key in (
        "precision",
        "recall",
        "f1",
        "false_alerts_per_1000_runs",
        "true_positives",
        "false_positives",
        "false_negatives",
        "alerts",
    ):
        assert metrics[key] == pytest.approx(golden["anomalies"][key], abs=1e-6), key
    assert metrics["by_fault"] == golden["anomalies"]["by_fault"]
    assert len(alerts) == metrics["alerts"]
