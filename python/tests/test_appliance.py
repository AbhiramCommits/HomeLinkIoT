from __future__ import annotations

import json
import time

import pytest

from homelink.cloud.shadow import update_topic
from homelink.device.appliance import Appliance, ApplianceState
from tests.helpers import RawClient

THING = "vac-001"


def wait_until(predicate, timeout: float = 6.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def stack(broker, shadow, tmp_path):
    service, store, port = shadow
    appliance = Appliance(
        thing_id=THING,
        broker_host="127.0.0.1",
        broker_port=port,
        spool_dir=tmp_path / "spool",
        fw_version="1.2.0",
        seed=7,
        sample_interval=0.1,
        ota_timeout=1.0,
    )
    appliance.set_credentials("TestNet", "test-psk")
    appliance.start()
    try:
        yield appliance, store, service, port
    finally:
        appliance.stop()


def reported_state(store) -> str | None:
    document = store.get_shadow(THING)
    if not document:
        return None
    return document.get("state", {}).get("reported", {}).get("state")


def test_state_machine_transitions(stack) -> None:
    appliance, store, _, _ = stack
    assert appliance.wait_for_state(ApplianceState.ONLINE, 3)
    assert wait_until(lambda: reported_state(store) == "ONLINE", 4)

    appliance.begin_run()
    assert appliance.wait_for_state(ApplianceState.RUNNING, 2)
    assert wait_until(lambda: reported_state(store) == "RUNNING", 4)

    appliance.stop_run()
    assert appliance.wait_for_state(ApplianceState.ONLINE, 2)
    assert wait_until(lambda: reported_state(store) == "ONLINE", 4)

    appliance.dock()
    assert appliance.wait_for_state(ApplianceState.DOCKED, 2)
    assert wait_until(lambda: reported_state(store) == "DOCKED", 4)
    assert wait_until(lambda: appliance.samples_generated >= 5, 3)
    assert appliance.published_count >= 4
    assert appliance.spooled_samples() == []


def test_delta_applies_power_and_commands(stack) -> None:
    appliance, store, service, _ = stack
    assert appliance.wait_for_state(ApplianceState.ONLINE, 3)

    desired = {
        "power_level": 5,
        "schedule": {"days": [2, 4], "hour": 8},
        "command": {"id": "cmd-start-1", "action": "start"},
    }
    service.client.publish(
        update_topic(THING), json.dumps({"state": {"desired": desired}}), qos=1
    )
    assert wait_until(lambda: appliance.state == ApplianceState.RUNNING, 5)
    assert appliance.power_level == 5
    assert appliance.schedule == {"days": [2, 4], "hour": 8}

    def reported_matches() -> bool:
        document = store.get_shadow(THING)
        if not document:
            return False
        reported = document["state"].get("reported", {})
        return (
            reported.get("power_level") == 5
            and reported.get("schedule") == {"days": [2, 4], "hour": 8}
            and reported.get("command", {}).get("id") == "cmd-start-1"
        )

    assert wait_until(reported_matches, 5)
    assert wait_until(lambda: (store.get_shadow(THING) or {}).get("state", {}).get("delta") == {}, 5)

    service.client.publish(
        update_topic(THING),
        json.dumps({"state": {"desired": {"command": {"id": "cmd-stop-1", "action": "stop"}}}}),
        qos=1,
    )
    assert wait_until(lambda: appliance.state == ApplianceState.ONLINE, 5)


def test_spool_replay_is_sequenced_and_lossless(broker, tmp_path) -> None:
    broker_instance, port = broker
    spool_dir = tmp_path / "spool"
    appliance = Appliance(
        thing_id=THING,
        broker_host="127.0.0.1",
        broker_port=port,
        spool_dir=spool_dir,
        seed=11,
        sample_interval=0.1,
    )

    def sample(seq: int) -> dict:
        return {
            "thing_id": THING,
            "seq": seq,
            "ts_ms": int(time.time() * 1000) + seq,
            "fw_version": "1.2.0",
            "state": "RUNNING",
            "motor_current_a": 1.3,
            "motor_rpm": 1800.0,
            "suction_pa": 1600.0,
            "battery_pct": 90.0,
            "bin_pct": 4.0,
            "runtime_s": 1.0,
            "error_code": "NONE",
        }

    for seq in (3, 1, 2):  # deliberately out of order on disk
        appliance._append_spool(sample(seq))
    appliance.seq = 3
    assert len(appliance.spooled_samples()) == 3

    subscriber = RawClient("127.0.0.1", port, client_id="spool-watcher")
    subscriber.connect()
    subscriber.subscribe([(f"homelink/{THING}/telemetry", 1)])
    appliance.set_credentials("TestNet", "test-psk")
    appliance.start()
    try:
        received: list[int] = []
        deadline = time.monotonic() + 8
        while len(received) < 4 and time.monotonic() < deadline:
            message = subscriber.recv_topic(f"homelink/{THING}/telemetry", 2)
            subscriber.ack(message)
            received.append(json.loads(message.payload)["seq"])
        assert received[:3] == [1, 2, 3], received
        assert appliance.replayed_count == 3
        assert appliance.spooled_samples() == []
        assert broker_instance.stats()["packets_in"] >= 5
    finally:
        appliance.stop()
        subscriber.disconnect()


@pytest.mark.slow
def test_offline_spool_reconnect_zero_loss(stack, broker) -> None:
    appliance, store, service, _ = stack
    broker_instance, _ = broker
    assert appliance.wait_for_state(ApplianceState.ONLINE, 3)
    time.sleep(0.8)

    runner = broker_instance.runner  # type: ignore[attr-defined]
    runner.stop()
    time.sleep(2.0)
    assert len(appliance.spooled_samples()) > 0

    runner.start()
    assert wait_until(lambda: appliance.replayed_count > 0, 10)
    assert wait_until(lambda: appliance.spooled_samples() == [], 10)
    time.sleep(1.2)
    appliance.stop()
    time.sleep(0.8)

    generated = appliance.samples_generated
    persisted = service.stats()["telemetry_persisted"]
    duplicates = service.stats()["duplicates_dropped"]
    assert generated - persisted == 0, (generated, persisted, duplicates)
    latest = store.get_telemetry(THING)
    assert latest is not None
    assert latest["seq"] == generated, (generated, persisted, duplicates, latest["seq"])
