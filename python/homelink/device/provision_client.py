"""BLE provisioning client: pairing + encrypted Wi-Fi credential write."""

from __future__ import annotations

import json
import socket
import time
from typing import Any

from homelink.device.ble import (
    DEVICE_INFO,
    PROV_PASSKEY,
    PROV_STATUS,
    WIFI_CREDS,
    BleError,
    fernet_for,
    split_frames,
)


class BleClient:
    """Minimal JSON-lines GATT client used by provisioning and tests."""

    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buffer = b""
        self.notifications: list[dict[str, Any]] = []

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send(self, payload: dict[str, Any]) -> None:
        if self.sock is None:
            raise BleError("client is not connected")
        self.sock.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")

    def _read_message(self, timeout: float | None = None) -> dict[str, Any]:
        if self.sock is None:
            raise BleError("client is not connected")
        self.sock.settimeout(timeout if timeout is not None else self.timeout)
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BleError("peripheral closed the connection")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def _read_response(self, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = max(0.01, deadline - time.monotonic())
            message = self._read_message(remaining)
            if message.get("event") == PROV_STATUS:
                self.notifications.append(message)
                continue
            return message

    def request(self, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.send(payload)
        return self._read_response(timeout)

    def read_device_info(self) -> dict[str, Any]:
        response = self.request({"op": "read", "char": DEVICE_INFO})
        return response.get("value", {})

    def subscribe_status(self) -> None:
        response = self.request({"op": "subscribe", "char": PROV_STATUS})
        if not response.get("ok"):
            raise BleError(f"subscribe failed: {response}")

    def write_frame(self, characteristic: str, frame: bytes) -> dict[str, Any]:
        import base64

        response = self.request(
            {"op": "write", "char": characteristic, "frame": base64.b64encode(frame).decode()}
        )
        if not response.get("ok"):
            raise BleError(f"write rejected: {response}")
        return response

    def write_characteristic(self, characteristic: str, data: bytes) -> int:
        frames = split_frames(data)
        for frame in frames:
            self.write_frame(characteristic, frame)
        return len(frames)

    def wait_notification(self, statuses: set[str], timeout: float = 5.0) -> dict[str, Any]:
        for notification in list(self.notifications):
            if notification["data"]["status"] in statuses:
                return notification
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self._read_message(max(0.01, deadline - time.monotonic()))
            except TimeoutError:
                break
            if message.get("event") != PROV_STATUS:
                continue
            self.notifications.append(message)
            if message["data"]["status"] in statuses:
                return message
        raise TimeoutError(f"no provisioning notification in {statuses}")


def provision(
    host: str,
    port: int,
    passkey: str,
    ssid: str,
    psk: str,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Pair with a simulated appliance and provision Wi-Fi over the BLE GATT transport."""
    started = time.perf_counter()
    client = BleClient(host, port, timeout=timeout)
    result: dict[str, Any] = {
        "ok": False,
        "status": "ERROR",
        "handshake_ms": 0.0,
        "passkey_chunks": 0,
        "credential_chunks": 0,
        "frames_written": 0,
        "device_info": {},
        "ssid": ssid,
    }
    try:
        client.connect()
        result["device_info"] = client.read_device_info()
        client.subscribe_status()
        passkey_chunks = client.write_characteristic(PROV_PASSKEY, passkey.encode("ascii"))
        token = fernet_for(passkey).encrypt(json.dumps({"ssid": ssid, "psk": psk}).encode("utf-8"))
        credential_frames = split_frames(token)
        for frame in credential_frames:
            client.write_frame(WIFI_CREDS, frame)
        result["passkey_chunks"] = passkey_chunks
        result["credential_chunks"] = len(credential_frames)
        result["frames_written"] = passkey_chunks + len(credential_frames)
        notification = client.wait_notification({"PROVISIONED", "AUTH_FAILED"}, timeout=timeout)
        result["status"] = notification["data"]["status"]
        result["ok"] = result["status"] == "PROVISIONED"
        if result["ok"]:
            result["ssid"] = notification["data"].get("ssid", ssid)
    except (OSError, TimeoutError, BleError, json.JSONDecodeError) as exc:
        result["status"] = "ERROR"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["handshake_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        client.close()
    return result
