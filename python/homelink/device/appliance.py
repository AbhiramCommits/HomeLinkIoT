"""Simulated connected appliance: BLE-provisioned, MQTT telemetry, shadow, OTA."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import paho.mqtt.client as mqtt

from homelink.cloud.shadow import (
    accepted_topic,
    delta_topic,
    get_topic,
    update_topic,
)
from homelink.contracts import Telemetry

FAULTS = {"NONE", "BRUSH_JAM", "FILTER_CLOG", "WHEEL_STALL", "LOW_BATTERY", "SENSOR_FAULT"}


class ApplianceState(str, Enum):
    UNPROVISIONED = "UNPROVISIONED"
    PAIRING = "PAIRING"
    PROVISIONED = "PROVISIONED"
    CONNECTING = "CONNECTING"
    ONLINE = "ONLINE"
    RUNNING = "RUNNING"
    DOCKED = "DOCKED"
    ERROR = "ERROR"
    OTA = "OTA"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def command_topic(thing_id: str) -> str:
    return f"homelink/{thing_id}/commands"


def ota_chunk_topic(thing_id: str) -> str:
    return f"homelink/{thing_id}/ota/chunk"


class Appliance:
    def __init__(
        self,
        thing_id: str = "vac-001",
        broker_host: str = "127.0.0.1",
        broker_port: int = 18831,
        spool_dir: str | Path = "device_spool",
        fw_version: str = "1.2.0",
        fault: str = "none",
        seed: int = 42,
        power_level: int = 3,
        ota_timeout: float = 3.0,
        sample_interval: float = 0.5,
        log: bool = False,
    ) -> None:
        self.thing_id = thing_id
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.spool_dir = Path(spool_dir)
        self.spool_path = self.spool_dir / f"{thing_id}.ndjson"
        self.fw_version = fw_version
        self.power_level = int(_clamp(power_level, 1, 5))
        self.sample_interval = sample_interval
        self.ota_timeout = ota_timeout
        self.log_enabled = log
        self.seed = seed
        self._rng = np.random.default_rng(seed)
        self._lock = threading.RLock()
        self._spool_lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._need_replay = threading.Event()
        self._thread: threading.Thread | None = None

        self._state = ApplianceState.UNPROVISIONED
        self.ssid: str | None = None
        self.psk: str | None = None
        self.schedule: dict[str, Any] = {"days": [1, 3, 5], "hour": 9}
        self.motor_current_a = 0.0
        self.motor_rpm = 0.0
        self.suction_pa = 0.0
        self.battery_pct = 100.0
        self.bin_pct = 5.0
        self.runtime_s = 0.0
        self.error_code = "NONE"
        self._error_hold_until = 0.0

        self.fault = "NONE" if fault.lower() in ("none", "") else fault.upper()
        if self.fault not in FAULTS:
            raise ValueError(f"unsupported fault {fault!r}")
        self._fault_until: float | None = None

        self.seq = 0
        self.samples_generated = 0
        self.published_count = 0
        self.spooled_count = 0
        self.replayed_count = 0
        self._reported_command: dict[str, Any] | None = None
        self._applied_commands: set[str] = set()

        self._ota_spec: dict[str, Any] | None = None
        self._ota_chunks: dict[int, bytes] = {}
        self._early_ota_chunks: dict[str, dict[int, bytes]] = {}
        self._ota_started_at = 0.0
        self._ota_status: str | None = None
        self._reported_ota: dict[str, Any] | None = None
        self._ota_results: dict[str, str] = {}
        self._previous_fw = fw_version

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=thing_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        self.client.reconnect_delay_set(min_delay=0.5, max_delay=2)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---------------------------------------------------------------- properties
    @property
    def state(self) -> ApplianceState:
        with self._lock:
            return self._state

    @property
    def ota_status(self) -> str | None:
        with self._lock:
            return self._ota_status

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _log(self, message: str) -> None:
        if self.log_enabled:
            print(f"[device {self.thing_id}] {message}", flush=True)

    # ---------------------------------------------------------------- provisioning
    def begin_pairing(self) -> None:
        with self._lock:
            if self._state == ApplianceState.UNPROVISIONED:
                self._state = ApplianceState.PAIRING

    def set_credentials(self, ssid: str, psk: str) -> None:
        with self._lock:
            self.ssid, self.psk = ssid, psk
            if self._state in (ApplianceState.UNPROVISIONED, ApplianceState.PAIRING):
                self._state = ApplianceState.PROVISIONED
        self._log(f"provisioned for SSID {ssid!r}")

    # ---------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._state = ApplianceState.CONNECTING
        self.client.connect(self.broker_host, self.broker_port, keepalive=20)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            self.client.loop_stop()
            raise TimeoutError(f"{self.thing_id} did not connect to the broker")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._telemetry_loop, name=f"telemetry-{self.thing_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def wait_for_state(self, state: ApplianceState, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state == state:
                return True
            time.sleep(0.03)
        return self.state == state

    # ---------------------------------------------------------------- MQTT callbacks
    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if getattr(reason_code, "value", reason_code) != 0:
            self._log(f"connect failed rc={reason_code}")
            return
        client.subscribe(
            [
                (delta_topic(self.thing_id), 1),
                (accepted_topic(self.thing_id), 1),
                (command_topic(self.thing_id), 1),
                (ota_chunk_topic(self.thing_id), 1),
            ]
        )
        client.publish(
            get_topic(self.thing_id),
            json.dumps({"clientToken": f"get-{uuid.uuid4().hex[:8]}"}),
            qos=1,
        )
        with self._lock:
            if self._state in (
                ApplianceState.UNPROVISIONED,
                ApplianceState.PAIRING,
                ApplianceState.PROVISIONED,
                ApplianceState.CONNECTING,
            ):
                self._state = ApplianceState.ONLINE
        # Publish the replay flag before advertising connectivity so the
        # telemetry thread can never send a fresh sample ahead of the spool.
        self._need_replay.set()
        self._connected.set()
        self.report_state()
        self._log("connected to broker")

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self._connected.clear()
        self._log("disconnected from broker; spooling telemetry")

    def _on_message(self, client: Any, userdata: Any, message: mqtt.MQTTMessage) -> None:
        topic = message.topic
        try:
            if topic == delta_topic(self.thing_id):
                payload = json.loads(message.payload)
                self._apply_desired(payload.get("state") or {})
            elif topic == accepted_topic(self.thing_id):
                payload = json.loads(message.payload)
                self._apply_desired(((payload.get("state") or {}).get("desired")) or {})
            elif topic == command_topic(self.thing_id):
                self._apply_command(json.loads(message.payload))
            elif topic == ota_chunk_topic(self.thing_id):
                self._handle_ota_chunk(json.loads(message.payload))
        except Exception as exc:  # callbacks must never kill paho's network loop
            self._log(f"message error on {topic}: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- desired / commands
    def _apply_desired(self, desired: dict[str, Any]) -> None:
        if not isinstance(desired, dict):
            return
        changed = False
        with self._lock:
            if "power_level" in desired:
                try:
                    value = int(desired["power_level"])
                except (TypeError, ValueError):
                    value = self.power_level
                new_power = int(_clamp(value, 1, 5))
                if new_power != self.power_level:
                    self.power_level = new_power
                    changed = True
            if isinstance(desired.get("schedule"), dict) and desired["schedule"] != self.schedule:
                self.schedule = desired["schedule"]
                changed = True
            if isinstance(desired.get("command"), dict | str):
                changed = self._apply_command_locked(desired["command"]) or changed
            if isinstance(desired.get("ota"), dict):
                changed = self._maybe_start_ota_locked(desired["ota"]) or changed
        if changed:
            self.report_state()

    def _apply_command(self, command: Any) -> None:
        with self._lock:
            changed = self._apply_command_locked(command)
        if changed:
            self.report_state()

    def _apply_command_locked(self, command: Any) -> bool:
        if isinstance(command, str):
            command = {"action": command}
        if not isinstance(command, dict):
            return False
        command_id = command.get("id") or command.get("command_id")
        if command_id:
            if command_id in self._applied_commands:
                return False
            self._applied_commands.add(command_id)
        elif self._reported_command == command:
            return False
        action = command.get("action") or command.get("command")
        if action == "set_power" or "power_level" in command:
            try:
                self.power_level = int(_clamp(int(command.get("power_level", self.power_level)), 1, 5))
            except (TypeError, ValueError):
                pass
        if action == "start":
            self._begin_run_locked()
        elif action in ("stop", "pause"):
            self._stop_run_locked()
        elif action == "dock":
            self._dock_locked()
        self._reported_command = dict(command)
        self._log(f"applied command {action!r} ({command_id})")
        return True

    # ---------------------------------------------------------------- state machine
    def begin_run(self) -> None:
        with self._lock:
            self._begin_run_locked()
        self.report_state()

    def _begin_run_locked(self) -> None:
        if self._state == ApplianceState.OTA:
            return
        if self._state != ApplianceState.RUNNING:
            self.runtime_s = 0.0
            self.error_code = "NONE"
            self._error_hold_until = 0.0
        self._state = ApplianceState.RUNNING

    def stop_run(self) -> None:
        with self._lock:
            self._stop_run_locked()
        self.report_state()

    def _stop_run_locked(self) -> None:
        if self._state == ApplianceState.RUNNING:
            self._state = ApplianceState.ONLINE

    def dock(self) -> None:
        with self._lock:
            self._dock_locked()
        self.report_state()

    def _dock_locked(self) -> None:
        if self._state != ApplianceState.OTA:
            self._state = ApplianceState.DOCKED
            self.runtime_s = 0.0

    def inject_fault(self, fault: str, duration_s: float | None = None) -> None:
        name = "NONE" if fault.lower() in ("none", "") else fault.upper()
        if name not in FAULTS:
            raise ValueError(f"unsupported fault {fault!r}")
        with self._lock:
            self.fault = name
            self._fault_until = time.monotonic() + duration_s if duration_s else None
        self._log(f"injected fault {name}")

    # ---------------------------------------------------------------- telemetry
    def _telemetry_loop(self) -> None:
        while not self._stop.is_set():
            if self._connected.is_set():
                if self._need_replay.is_set():
                    self._need_replay.clear()
                    self.replay_spool()
                sample = self._build_sample()
                if not self._publish_sample(sample):
                    self._append_spool(sample)
            else:
                sample = self._build_sample()
                self._append_spool(sample)
            self._check_ota_timeout()
            self._stop.wait(self.sample_interval)

    def _telemetry_state_locked(self) -> str:
        # The telemetry contract only allows device-activity states; transport
        # states such as CONNECTING/ONLINE are represented as IDLE.
        if self._state in (ApplianceState.RUNNING, ApplianceState.DOCKED, ApplianceState.OTA):
            return self._state.value
        if self._state == ApplianceState.ERROR:
            return "ERROR"
        return "IDLE"

    def _active_fault_locked(self) -> str:
        if self.fault == "NONE":
            return "NONE"
        if self._fault_until is not None and time.monotonic() > self._fault_until:
            self.fault = "NONE"
            self._fault_until = None
            return "NONE"
        return self.fault

    def _build_sample(self) -> dict[str, Any]:
        with self._lock:
            self.seq += 1
            self.samples_generated += 1
            state = self._telemetry_state_locked()
            running = self._state == ApplianceState.RUNNING
            power = self.power_level
            if running:
                self.runtime_s += self.sample_interval
                self.battery_pct = max(
                    0.0, self.battery_pct - (0.0011 + 0.0008 * power) * self.sample_interval
                )
                self.bin_pct = min(100.0, self.bin_pct + 0.0035 * power * self.sample_interval)
            elif self._state == ApplianceState.DOCKED and self.battery_pct < 100.0:
                self.battery_pct = min(100.0, self.battery_pct + 0.02 * self.sample_interval)

            current = 0.55 + 0.33 * power + float(self._rng.normal(0, 0.025))
            rpm = 900.0 + 480.0 * power + float(self._rng.normal(0, 12.0))
            suction = 780.0 + 240.0 * power + float(self._rng.normal(0, 9.0))
            error = "NONE"
            fault = self._active_fault_locked()
            if running:
                if fault == "BRUSH_JAM":
                    current *= 2.9
                    rpm *= 0.5
                    error = "BRUSH_JAM"
                elif fault == "FILTER_CLOG":
                    factor = max(0.35, 1.0 - self.runtime_s / 120.0)
                    suction *= factor
                    if factor < 0.75:
                        error = "FILTER_CLOG"
                elif fault == "WHEEL_STALL":
                    rpm = 0.0
                    current *= 1.75
                    error = "WHEEL_STALL"
                elif fault == "LOW_BATTERY" or self.battery_pct < 5.0:
                    error = "LOW_BATTERY"
                elif fault == "SENSOR_FAULT":
                    error = "SENSOR_FAULT"
            if self.error_code == "OTA_CHECKSUM" and time.monotonic() < self._error_hold_until:
                error = "OTA_CHECKSUM"
            if error != "NONE":
                self.error_code = error
            elif time.monotonic() >= self._error_hold_until:
                self.error_code = "NONE"

            self.motor_current_a = _clamp(current, 0.0, 20.0)
            self.motor_rpm = _clamp(rpm, 0.0, 20000.0)
            self.suction_pa = _clamp(suction, 0.0, 5000.0)
            sample = Telemetry(
                thing_id=self.thing_id,
                seq=self.seq,
                ts_ms=int(time.time() * 1000),
                fw_version=self.fw_version,
                state=state,
                motor_current_a=self.motor_current_a,
                motor_rpm=self.motor_rpm,
                suction_pa=self.suction_pa,
                battery_pct=self.battery_pct,
                bin_pct=self.bin_pct,
                runtime_s=self.runtime_s,
                error_code=self.error_code,
            ).to_dict()
            return sample

    def _forget_outgoing(self, mid: int) -> None:
        """Remove an unacknowledged message from paho's retry queue.

        The sample is written to the durable spool instead, so retransmission
        would create a duplicate seq at the broker. Shadow also dedupes by seq,
        but removing the queued copy keeps the wire semantics exact.
        """
        try:
            with self.client._out_message_mutex:  # type: ignore[attr-defined]
                removed = self.client._out_messages.pop(mid, None)  # type: ignore[attr-defined]
            if removed is not None:
                self.client._inflight_messages = max(  # type: ignore[attr-defined]
                    0, self.client._inflight_messages - 1
                )
        except (AttributeError, KeyError):
            pass

    def _publish_sample(self, sample: dict[str, Any]) -> bool:
        if not self._connected.is_set():
            return False
        info = self.client.publish(
            f"homelink/{self.thing_id}/telemetry",
            json.dumps(sample, separators=(",", ":")),
            qos=1,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self._forget_outgoing(info.mid)
            return False
        try:
            info.wait_for_publish(timeout=1.0)
        except RuntimeError:
            self._forget_outgoing(info.mid)
            return False
        if not info.is_published():
            self._forget_outgoing(info.mid)
            return False
        with self._lock:
            self.published_count += 1
        return True

    # ---------------------------------------------------------------- offline spool
    def _append_spool(self, sample: dict[str, Any]) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(sample, separators=(",", ":")) + "\n"
        with self._spool_lock:
            with self.spool_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
        with self._lock:
            self.spooled_count += 1

    def spooled_samples(self) -> list[dict[str, Any]]:
        with self._spool_lock:
            if not self.spool_path.exists():
                return []
            records = []
            for line in self.spool_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
            return records

    def replay_spool(self) -> dict[str, int]:
        """Replay spooled telemetry in seq order, truncating only acked records."""
        with self._spool_lock:
            if not self.spool_path.exists():
                return {"replayed": 0, "remaining": 0}
            records = []
            for line in self.spool_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
            records.sort(key=lambda record: record["seq"])
            remaining: list[dict[str, Any]] = []
            replayed = 0
            for index, record in enumerate(records):
                if self._publish_sample(record):
                    replayed += 1
                else:
                    remaining.extend(records[index:])
                    break
            if remaining:
                temporary = self.spool_path.with_suffix(".tmp")
                temporary.write_text(
                    "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in remaining),
                    encoding="utf-8",
                )
                temporary.replace(self.spool_path)
            else:
                self.spool_path.unlink(missing_ok=True)
        with self._lock:
            self.replayed_count += replayed
        if replayed:
            self._log(f"replayed {replayed} spooled telemetry samples")
        return {"replayed": replayed, "remaining": len(remaining)}

    # ---------------------------------------------------------------- shadow reported
    def report_state(self) -> None:
        with self._lock:
            reported: dict[str, Any] = {
                "state": self._state.value,
                "fw_version": self.fw_version,
                "power_level": self.power_level,
                "schedule": self.schedule,
                "error_code": self.error_code,
            }
            if self._reported_command is not None:
                reported["command"] = self._reported_command
            if self._reported_ota is not None:
                reported["ota"] = self._reported_ota
        payload = json.dumps({"state": {"reported": reported}}, separators=(",", ":"))
        if self._connected.is_set():
            self.client.publish(update_topic(self.thing_id), payload, qos=1)

    # ---------------------------------------------------------------- OTA
    def _maybe_start_ota_locked(self, spec: dict[str, Any]) -> bool:
        version = spec.get("fw_version")
        sha = spec.get("sha256")
        if not isinstance(version, str) or not isinstance(sha, str):
            return False
        key = f"{version}:{sha}"
        if key in self._ota_results:
            updated = {**spec, "status": self._ota_results[key]}
            if updated == self._reported_ota:
                return False
            self._reported_ota = updated
            return True
        if version == self.fw_version:
            self._ota_results[key] = "success"
            updated = {**spec, "status": "success"}
            if updated == self._reported_ota:
                return False
            self._reported_ota = updated
            return True
        if self._ota_spec is not None and self._ota_spec.get("sha256") == sha:
            return False
        self._previous_fw = self.fw_version
        self._ota_spec = dict(spec)
        self._ota_chunks = dict(self._early_ota_chunks.pop(version, {}))
        self._ota_started_at = time.monotonic()
        self._ota_status = "downloading"
        self._state = ApplianceState.OTA
        self._reported_ota = {**spec, "status": "downloading"}
        self._log(f"OTA started -> {version} ({spec.get('chunks')} chunks)")
        expected = int(spec.get("chunks", 0))
        if expected and len(self._ota_chunks) >= expected:
            self._verify_ota_locked()
        return True

    def _handle_ota_chunk(self, payload: dict[str, Any]) -> None:
        finished = False
        with self._lock:
            try:
                index = int(payload["index"])
                total = int(payload["total"])
                data = base64.b64decode(payload["data"])
                version = str(payload["fw_version"])
            except (KeyError, ValueError, TypeError):
                return
            spec = self._ota_spec
            if spec is None:
                # Chunks can race ahead of the shadow delta; buffer them.
                if len(self._early_ota_chunks) > 8:
                    self._early_ota_chunks.clear()
                self._early_ota_chunks.setdefault(version, {})[index] = data
                return
            if version != spec.get("fw_version"):
                return
            if total != int(spec.get("chunks", total)) or not 0 <= index < total:
                return
            self._ota_chunks[index] = data
            if len(self._ota_chunks) < total:
                return
            self._verify_ota_locked()
            finished = True
        if finished:
            self.report_state()

    def _verify_ota_locked(self) -> None:
        spec = self._ota_spec or {}
        total = int(spec.get("chunks", len(self._ota_chunks)))
        blob = b"".join(self._ota_chunks[i] for i in range(total))
        digest = hashlib.sha256(blob).hexdigest()
        if digest == spec.get("sha256") and len(blob) == int(spec.get("size", len(blob))):
            self._finish_ota_locked(success=True)
        else:
            self._finish_ota_locked(success=False)

    def _check_ota_timeout(self) -> None:
        with self._lock:
            if self._ota_spec is None:
                return
            if time.monotonic() - self._ota_started_at <= self.ota_timeout:
                return
            self._finish_ota_locked(success=False)
        self.report_state()

    def _finish_ota_locked(self, success: bool) -> None:
        spec = self._ota_spec or {}
        key = f"{spec.get('fw_version')}:{spec.get('sha256')}"
        if success:
            self.fw_version = str(spec.get("fw_version", self.fw_version))
            self.error_code = "NONE"
            self._ota_status = "success"
            self._ota_results[key] = "success"
            self._reported_ota = {**spec, "status": "success"}
            self._log(f"OTA success -> {self.fw_version}")
        else:
            self.fw_version = self._previous_fw
            self.error_code = "OTA_CHECKSUM"
            self._error_hold_until = time.monotonic() + 5.0
            self._ota_status = "rolled_back"
            self._ota_results[key] = "rolled_back"
            self._reported_ota = {**spec, "status": "rolled_back"}
            self._log(f"OTA failed; rolled back to {self.fw_version}")
        self._ota_spec = None
        self._ota_chunks = {}
        if self._state == ApplianceState.OTA:
            self._state = ApplianceState.ONLINE
        self.runtime_s = 0.0


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HomeLink simulated appliance")
    parser.add_argument("--thing", default="vac-001")
    parser.add_argument("--broker", default="localhost:18831")
    parser.add_argument("--fault", default="none")
    parser.add_argument("--spool-dir", default="device_spool")
    parser.add_argument("--fw-version", default="1.2.0")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    host, _, raw_port = args.broker.partition(":")
    port = int(raw_port or 18831)
    appliance = Appliance(
        thing_id=args.thing,
        broker_host=host,
        broker_port=port,
        spool_dir=args.spool_dir,
        fw_version=args.fw_version,
        fault=args.fault,
        seed=args.seed,
        log=True,
    )
    appliance.set_credentials("local-dev", "local-dev")
    appliance.start()
    appliance.begin_run()
    print(f"homelink appliance {args.thing} running (Ctrl-C to stop)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        appliance.dock()
        time.sleep(0.6)
        appliance.stop()


if __name__ == "__main__":
    main()
