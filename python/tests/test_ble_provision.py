from __future__ import annotations

import base64
import json

import pytest

from homelink.device.ble import (
    BlePeripheral,
    encode_frame,
    fernet_for,
    split_frames,
)
from homelink.device.provision_client import BleClient, provision
from tests.helpers import free_port

PASSKEY = "482913"


@pytest.fixture
def peripheral():
    instance = BlePeripheral(
        device_id="vac-ble-001",
        passkey=PASSKEY,
        model="HomeLink V1",
        fw_version="1.2.0",
        port=free_port(),
        reassembly_timeout=0.4,
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def test_happy_path_provisioning(peripheral: BlePeripheral) -> None:
    result = provision(
        "127.0.0.1", peripheral.port, PASSKEY, "HomeNet-5G", "correct horse battery"
    )
    assert result["ok"] is True
    assert result["status"] == "PROVISIONED"
    assert result["handshake_ms"] > 0
    assert result["credential_chunks"] >= 2
    assert result["device_info"]["mtu"] == 20
    assert peripheral.credentials == ("HomeNet-5G", "correct horse battery")
    assert peripheral.stats["provisioned"] == 1
    assert peripheral.notifications_with_status("PROVISIONED")


def test_wrong_passkey_auth_failure(peripheral: BlePeripheral) -> None:
    result = provision("127.0.0.1", peripheral.port, "000000", "HomeNet", "nope")
    assert result["ok"] is False
    assert result["status"] == "AUTH_FAILED"
    assert peripheral.credentials is None
    assert peripheral.stats["auth_failures"] == 1
    assert peripheral.notifications_with_status("AUTH_FAILED")


def test_chunk_reassembly_counts(peripheral: BlePeripheral) -> None:
    client = BleClient("127.0.0.1", peripheral.port)
    client.connect()
    try:
        client.subscribe_status()
        assert client.write_characteristic("PROV_PASSKEY", PASSKEY.encode()) == 1
        token = fernet_for(PASSKEY).encrypt(
            json.dumps({"ssid": "ChunkedNet", "psk": "a" * 30}).encode()
        )
        frames = split_frames(token)
        assert len(frames) > 1
        assert all(len(frame) <= 20 for frame in frames)
        for frame in frames:
            response = client.write_frame("WIFI_CREDS", frame)
            assert response["ok"] is True
        notification = client.wait_notification({"PROVISIONED"}, timeout=3)
        assert notification["data"]["ssid"] == "ChunkedNet"
        assert peripheral.credentials == ("ChunkedNet", "a" * 30)
    finally:
        client.close()


def test_truncated_write_reports_incomplete(peripheral: BlePeripheral) -> None:
    client = BleClient("127.0.0.1", peripheral.port)
    client.connect()
    try:
        client.subscribe_status()
        client.write_characteristic("PROV_PASSKEY", PASSKEY.encode())
        token = fernet_for(PASSKEY).encrypt(
            json.dumps({"ssid": "Truncated", "psk": "b" * 40}).encode()
        )
        frames = split_frames(token)
        assert len(frames) >= 3
        client.write_frame("WIFI_CREDS", frames[0])
        notification = client.wait_notification({"INCOMPLETE"}, timeout=3)
        assert notification["data"]["status"] == "INCOMPLETE"
        assert peripheral.credentials is None
        assert peripheral.stats["incomplete_writes"] >= 1
    finally:
        client.close()


def test_out_of_order_and_oversized_frames(peripheral: BlePeripheral) -> None:
    client = BleClient("127.0.0.1", peripheral.port)
    client.connect()
    try:
        client.subscribe_status()
        out_of_order = encode_frame(1, 2, b"x" * 10)
        response = client.request(
            {
                "op": "write",
                "char": "WIFI_CREDS",
                "frame": base64.b64encode(out_of_order).decode(),
            }
        )
        assert response["ok"] is False
        assert response["error"] == "OUT_OF_ORDER"

        oversized = bytes([0, 1]) + b"x" * 19  # 21 bytes > ATT MTU 20
        response = client.request(
            {
                "op": "write",
                "char": "WIFI_CREDS",
                "frame": base64.b64encode(oversized).decode(),
            }
        )
        assert response["ok"] is False
        assert response["error"] == "BAD_FRAME"
    finally:
        client.close()
