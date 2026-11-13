"""Firmware blob generation and OTA chunk publisher for the local stack."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

from homelink.cloud.shadow import update_topic

DEFAULT_FIRMWARE_SIZE = 8192
DEFAULT_CHUNK_SIZE = 1024


def firmware_blob(version: str, size: int = DEFAULT_FIRMWARE_SIZE) -> bytes:
    """Deterministic pseudo-firmware blob for a version string."""
    seed = hashlib.sha256(f"homelink-firmware-{version}".encode()).digest()
    repeats = size // len(seed) + 1
    return (seed * repeats)[:size]


def build_firmware(
    version: str,
    size: int = DEFAULT_FIRMWARE_SIZE,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    corrupt: bool = False,
    drop_chunk: int | None = None,
) -> tuple[dict[str, Any], list[bytes | None]]:
    blob = firmware_blob(version, size)
    digest = hashlib.sha256(blob).hexdigest()
    chunks = [blob[offset : offset + chunk_size] for offset in range(0, len(blob), chunk_size)]
    spec = {
        "fw_version": version,
        "sha256": digest,
        "size": len(blob),
        "chunks": len(chunks),
    }
    payloads: list[bytes | None] = list(chunks)
    if corrupt and payloads:
        index = len(payloads) // 2
        mutated = bytearray(payloads[index] or b"")
        mutated[0] ^= 0xFF
        payloads[index] = bytes(mutated)
    if drop_chunk is not None and 0 <= drop_chunk < len(payloads):
        payloads[drop_chunk] = None
    return spec, payloads


class FirmwareServer:
    """Publishes a desired OTA shadow update and the firmware chunks."""

    def __init__(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 18831,
        client_id: str = "homelink-ota-service",
    ) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self._connected = threading.Event()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self.client.on_connect = self._on_connect

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if getattr(reason_code, "value", reason_code) == 0:
            self._connected.set()

    def start(self, timeout: float = 5.0) -> None:
        self.client.connect(self.broker_host, self.broker_port, keepalive=20)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            self.client.loop_stop()
            raise TimeoutError("OTA service did not connect to the broker")

    def stop(self) -> None:
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def _publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        info = self.client.publish(topic, payload, qos=qos)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            try:
                info.wait_for_publish(timeout=1.0)
            except RuntimeError:
                pass

    def trigger(
        self,
        thing_id: str,
        version: str = "1.3.0",
        size: int = DEFAULT_FIRMWARE_SIZE,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        corrupt: bool = False,
        drop_chunk: int | None = None,
        settle: float = 0.3,
    ) -> dict[str, Any]:
        spec, payloads = build_firmware(
            version, size=size, chunk_size=chunk_size, corrupt=corrupt, drop_chunk=drop_chunk
        )
        desired = {"state": {"desired": {"ota": spec}}}
        self._publish(update_topic(thing_id), json.dumps(desired).encode())
        # Give the shadow service a moment to emit the delta before chunks.
        time.sleep(settle)
        published = 0
        dropped = 0
        for index, chunk in enumerate(payloads):
            if chunk is None:
                dropped += 1
                continue
            message = {
                "fw_version": version,
                "index": index,
                "total": len(payloads),
                "data": base64.b64encode(chunk).decode("ascii"),
            }
            self._publish(f"homelink/{thing_id}/ota/chunk", json.dumps(message).encode())
            published += 1
        return {
            "spec": spec,
            "chunks_published": published,
            "chunks_dropped": dropped,
            "corrupted": corrupt,
        }
