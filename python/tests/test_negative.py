from __future__ import annotations

import json
import socket
import time

import pytest

from homelink.protocol.packets import encode_remaining_length
from tests.helpers import RawClient


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def raw_connect(protocol: bytes = b"MQTT", level: int = 4) -> bytes:
    body = len(protocol).to_bytes(2, "big") + protocol + bytes([level, 0x02])
    body += b"\x00\x1e" + b"\x00\x04test"
    return b"\x10" + encode_remaining_length(len(body)) + body


MALFORMED_CASES = [
    ("bogus-protocol-name", raw_connect(protocol=b"MQTX"), False, False),
    ("truncated-remaining-length", bytes([0x30, 0x80, 0x80, 0x80, 0x80]), False, False),
    (
        "oversized-packet",
        bytes([0x30]) + encode_remaining_length(300_000),
        False,
        False,
    ),
    (
        "invalid-utf8-topic",
        bytes([0x30, 0x05, 0x00, 0x03, 0xFF, 0xFE, 0x00]),
        True,
        False,
    ),
    (
        "qos-3",
        bytes([0x36, 0x05, 0x00, 0x01, 0x61, 0x00, 0x01]),
        True,
        False,
    ),
    (
        "truncated-payload",
        bytes([0x30, 0x0A, 0x00, 0x01, 0x61]),
        True,
        True,
    ),
]


@pytest.mark.negative
@pytest.mark.parametrize(
    ("case", "payload", "needs_connect", "close_after"),
    MALFORMED_CASES,
    ids=[case[0] for case in MALFORMED_CASES],
)
def test_malformed_mqtt_drops_only_offender(broker, case, payload, needs_connect, close_after) -> None:
    broker_instance, port = broker
    good = RawClient("127.0.0.1", port, client_id=f"good-{case}")
    good.connect()
    good.ping()

    bad = RawClient("127.0.0.1", port, client_id=f"bad-{case}")
    if needs_connect:
        bad.connect()
    else:
        bad.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        bad.sock.settimeout(2)
    bad.send_raw(payload)
    if close_after:
        bad.close()

    assert wait_for(lambda: broker_instance.stats()["malformed_dropped"] >= 1)
    good.ping()
    assert broker_instance.stats()["clients_connected"] >= 1
    bad.close()
    good.disconnect()


@pytest.mark.negative
def test_malformed_telemetry_never_corrupts_latest(shadow) -> None:
    service, store, port = shadow
    client = RawClient("127.0.0.1", port, client_id="telemetry-negative")
    client.connect()
    client.subscribe([("homelink/+/telemetry", 1)])

    valid = {
        "thing_id": "vac-neg",
        "seq": 1,
        "ts_ms": int(time.time() * 1000),
        "fw_version": "1.2.0",
        "state": "IDLE",
        "motor_current_a": 0.2,
        "motor_rpm": 0.0,
        "suction_pa": 0.0,
        "battery_pct": 99.0,
        "bin_pct": 1.0,
        "runtime_s": 0.0,
        "error_code": "NONE",
    }
    client.publish("homelink/vac-neg/telemetry", json.dumps(valid), qos=1)
    assert wait_for(lambda: store.get_telemetry("vac-neg") is not None)

    client.publish("homelink/vac-neg/telemetry", b"{not json", qos=1)
    broken = dict(valid)
    broken["state"] = "BROKEN"
    broken["battery_pct"] = 250
    broken["seq"] = 2
    client.publish("homelink/vac-neg/telemetry", json.dumps(broken), qos=1)

    assert wait_for(lambda: service.stats()["telemetry_invalid"] >= 2)
    latest = store.get_telemetry("vac-neg")
    assert latest is not None
    assert latest["seq"] == 1
    assert latest["state"] == "IDLE"
    client.publish("homelink/vac-neg/telemetry", json.dumps({**valid, "seq": 3}), qos=1)
    assert wait_for(lambda: (store.get_telemetry("vac-neg") or {}).get("seq") == 3)
    client.disconnect()
