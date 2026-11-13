from __future__ import annotations

import json
import time

from homelink.cloud.shadow import (
    accepted_topic,
    delta_topic,
    get_accepted_topic,
    get_rejected_topic,
    get_topic,
    rejected_topic,
    telemetry_topic,
    update_topic,
)
from tests.helpers import RawClient

THING = "vac-001"


def shadow_client(port: int, client_id: str = "shadow-observer") -> RawClient:
    client = RawClient("127.0.0.1", port, client_id=client_id)
    client.connect()
    client.subscribe(
        [
            ("$iot/things/+/shadow/#", 1),
            ("homelink/+/telemetry", 1),
        ]
    )
    return client


def expect(client: RawClient, topic: str, timeout: float = 4.0) -> dict:
    message = client.recv_topic(topic, timeout)
    client.ack(message)
    return json.loads(message.payload)


def update(client: RawClient, payload: dict, qos: int = 1) -> None:
    client.publish(update_topic(THING), json.dumps(payload), qos=qos)


def test_update_creates_desired_delta_and_metadata(shadow) -> None:
    service, store, port = shadow
    client = shadow_client(port)
    update(
        client,
        {
            "state": {"desired": {"power_level": 5, "schedule": {"days": [1, 3]}}},
            "version": 0,
            "clientToken": "tok-1",
        },
    )
    accepted = expect(client, accepted_topic(THING))
    assert accepted["version"] == 1
    assert accepted["clientToken"] == "tok-1"
    assert accepted["state"]["desired"]["power_level"] == 5
    assert isinstance(accepted["metadata"]["desired"]["power_level"]["timestamp"], int)

    delta = expect(client, delta_topic(THING))
    assert delta["state"]["power_level"] == 5
    assert delta["state"]["schedule"] == {"days": [1, 3]}
    assert delta["clientToken"] == "tok-1"

    stored = store.get_shadow(THING)
    assert stored is not None
    assert stored["version"] == 1
    client.disconnect()


def test_delta_suppressed_when_reported_matches_desired(shadow) -> None:
    _, _, port = shadow
    client = shadow_client(port)
    update(client, {"state": {"desired": {"power_level": 4}}, "version": 0})
    expect(client, accepted_topic(THING))
    expect(client, delta_topic(THING))

    update(client, {"state": {"reported": {"power_level": 4}}, "version": 1})
    accepted = expect(client, accepted_topic(THING))
    assert accepted["state"]["delta"] == {}
    try:
        client.recv(0.4)
    except TimeoutError:
        pass
    else:  # pragma: no cover - only breaks if a delta was wrongly emitted
        raise AssertionError("delta should not be published after convergence")
    client.disconnect()


def test_version_conflict_rejected(shadow) -> None:
    service, store, port = shadow
    client = shadow_client(port)
    update(client, {"state": {"desired": {"power_level": 2}}, "version": 0})
    expect(client, accepted_topic(THING))
    expect(client, delta_topic(THING))

    update(client, {"state": {"desired": {"power_level": 1}}, "version": 0, "clientToken": "stale"})
    rejected = expect(client, rejected_topic(THING))
    assert rejected["code"] == 409
    assert rejected["message"] == "Version conflict"
    assert rejected["clientToken"] == "stale"
    stored = store.get_shadow(THING)
    assert stored is not None
    assert stored["version"] == 1
    assert stored["state"]["desired"]["power_level"] == 2
    client.disconnect()


def test_null_means_delete(shadow) -> None:
    _, store, port = shadow
    client = shadow_client(port)
    update(client, {"state": {"desired": {"schedule": {"days": [1]}, "power_level": 3}}, "version": 0})
    expect(client, accepted_topic(THING))
    expect(client, delta_topic(THING))

    update(client, {"state": {"desired": {"schedule": None}}, "version": 1})
    accepted = expect(client, accepted_topic(THING))
    assert "schedule" not in accepted["state"]["desired"]
    assert "schedule" not in accepted["metadata"]["desired"]
    stored = store.get_shadow(THING)
    assert stored is not None
    assert "schedule" not in stored["state"]["desired"]
    client.disconnect()


def test_get_accepted_and_rejected(shadow) -> None:
    _, _, port = shadow
    client = shadow_client(port)
    update(client, {"state": {"desired": {"power_level": 3}}, "version": 0})
    expect(client, accepted_topic(THING))
    expect(client, delta_topic(THING))

    client.publish(get_topic(THING), json.dumps({"clientToken": "get-1"}), qos=1)
    response = expect(client, get_accepted_topic(THING))
    assert response["clientToken"] == "get-1"
    assert response["state"]["desired"]["power_level"] == 3
    assert response["version"] == 1

    unknown = "vac-404"
    client.publish(get_topic(unknown), b"{}", qos=1)
    rejected = expect(client, get_rejected_topic(unknown))
    assert rejected["code"] == 404
    client.disconnect()


def test_malformed_update_is_rejected_without_corruption(shadow) -> None:
    service, store, port = shadow
    client = shadow_client(port)
    client.publish(update_topic(THING), b"{not json", qos=1)
    rejected = expect(client, rejected_topic(THING))
    assert rejected["code"] == 400
    assert store.get_shadow(THING) is None

    client.publish(
        update_topic(THING),
        json.dumps({"state": {"desired": {"power_level": 3}}, "version": 99}),
        qos=1,
    )
    rejected = expect(client, rejected_topic(THING))
    assert rejected["code"] == 409
    assert store.get_shadow(THING) is None
    client.disconnect()


def test_telemetry_persisted_and_run_recorded(shadow) -> None:
    service, store, port = shadow
    client = shadow_client(port)

    def sample(seq: int, state: str, runtime_s: float, battery: float, error: str = "NONE") -> dict:
        return {
            "thing_id": THING,
            "seq": seq,
            "ts_ms": int(time.time() * 1000),
            "fw_version": "1.2.0",
            "state": state,
            "motor_current_a": 1.2,
            "motor_rpm": 1800.0,
            "suction_pa": 1700.0,
            "battery_pct": battery,
            "bin_pct": 5.0,
            "runtime_s": runtime_s,
            "error_code": error,
        }

    for payload in [
        sample(1, "RUNNING", 0.5, 91.0),
        sample(2, "RUNNING", 1.0, 90.9),
        sample(3, "DOCKED", 1.33, 90.8),
    ]:
        client.publish(telemetry_topic(THING), json.dumps(payload), qos=1)
        time.sleep(0.1)

    deadline = time.monotonic() + 4
    latest = None
    while time.monotonic() < deadline:
        latest = store.get_telemetry(THING)
        if latest and latest.get("seq") == 3:
            break
        time.sleep(0.05)
    assert latest is not None
    assert latest["seq"] == 3
    runs = store.query_runs(THING)
    assert len(runs) == 1
    assert runs[0]["samples"] == 2
    assert runs[0]["duration_s"] >= 0.0
    assert service.stats()["telemetry_persisted"] >= 3
    assert service.stats()["runs_recorded"] == 1

    client.publish(telemetry_topic(THING), b"{bad json", qos=1)
    time.sleep(0.2)
    assert service.stats()["telemetry_invalid"] >= 1
    assert store.get_telemetry(THING)["seq"] == 3
    client.disconnect()


def test_duplicate_telemetry_seq_is_dropped(shadow) -> None:
    service, store, port = shadow
    client = shadow_client(port)
    payload = {
        "thing_id": THING,
        "seq": 10,
        "ts_ms": int(time.time() * 1000),
        "fw_version": "1.2.0",
        "state": "IDLE",
        "motor_current_a": 0.1,
        "motor_rpm": 0.0,
        "suction_pa": 0.0,
        "battery_pct": 80.0,
        "bin_pct": 2.0,
        "runtime_s": 0.0,
        "error_code": "NONE",
    }
    client.publish(telemetry_topic(THING), json.dumps(payload), qos=1)
    time.sleep(0.15)
    payload["seq"] = 9
    payload["ts_ms"] += 1
    client.publish(telemetry_topic(THING), json.dumps(payload), qos=1)
    time.sleep(0.15)
    assert service.stats()["duplicates_dropped"] >= 1
    assert store.get_telemetry(THING)["seq"] == 10
    client.disconnect()
