"""AWS IoT Device Shadow compatible service backed by the local MQTT broker.

Supported topics (same shape as AWS IoT Core, with ``$iot`` as the local root):

    $iot/things/{thing}/shadow/update            -> accepted / rejected / delta
    $iot/things/{thing}/shadow/get               -> accepted / rejected
    homelink/{thing}/telemetry                   -> latest sample + run history

Semantics implemented: monotonic versions, optimistic concurrency (409 on a
stale ``version``), recursive desired/reported merge with ``null`` deletion,
per-leaf metadata timestamps, clientToken echo, and delta publication only when
the desired state differs from reported state.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import time
import uuid
from typing import Any

import paho.mqtt.client as mqtt

from homelink.cloud.store import Store, make_store, now_ms
from homelink.contracts import ShadowDoc, ValidationError, validate_shadow, validate_telemetry

SHADOW_ROOT = "$iot/things"


def update_topic(thing_id: str) -> str:
    return f"{SHADOW_ROOT}/{thing_id}/shadow/update"


def get_topic(thing_id: str) -> str:
    return f"{SHADOW_ROOT}/{thing_id}/shadow/get"


def accepted_topic(thing_id: str) -> str:
    return f"{SHADOW_ROOT}/{thing_id}/shadow/update/accepted"


def rejected_topic(thing_id: str) -> str:
    return f"{SHADOW_ROOT}/{thing_id}/shadow/update/rejected"


def delta_topic(thing_id: str) -> str:
    return f"{SHADOW_ROOT}/{thing_id}/shadow/update/delta"


def get_accepted_topic(thing_id: str) -> str:
    return f"{SHADOW_ROOT}/{thing_id}/shadow/get/accepted"


def get_rejected_topic(thing_id: str) -> str:
    return f"{SHADOW_ROOT}/{thing_id}/shadow/get/rejected"


def telemetry_topic(thing_id: str) -> str:
    return f"homelink/{thing_id}/telemetry"


def merge_patch(base: Any, patch: Any) -> Any:
    """Recursively merge ``patch`` into ``base``; JSON null deletes a key."""
    if not isinstance(base, dict):
        base = {}
    result = copy.deepcopy(base)
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def compute_delta(desired: Any, reported: Any) -> dict[str, Any]:
    """Return the desired keys not present with an equal value in reported."""
    if not isinstance(desired, dict):
        return {}
    reported_dict = reported if isinstance(reported, dict) else {}
    delta: dict[str, Any] = {}
    for key, value in desired.items():
        reported_value = reported_dict.get(key) if isinstance(reported_dict, dict) else None
        if isinstance(value, dict):
            if isinstance(reported_value, dict):
                nested = compute_delta(value, reported_value)
                if nested:
                    delta[key] = nested
            elif key not in reported_dict or reported_value != value:
                delta[key] = copy.deepcopy(value)
        elif key not in reported_dict or reported_value != value:
            delta[key] = copy.deepcopy(value)
    return delta


def _mark_metadata(metadata: dict[str, Any], patch: dict[str, Any], timestamp: int) -> None:
    for key, value in patch.items():
        if value is None:
            metadata.pop(key, None)
        elif isinstance(value, dict):
            node = metadata.get(key)
            if not isinstance(node, dict):
                node = {}
                metadata[key] = node
            _mark_metadata(node, value, timestamp)
        else:
            metadata[key] = {"timestamp": timestamp}


class ShadowService:
    def __init__(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 18831,
        store: Store | None = None,
        client_id: str | None = None,
        keepalive: int = 30,
    ) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.store = store if store is not None else make_store("sqlite:///homelink.db")
        self.client_id = client_id or f"homelink-shadow-{os.getpid()}"
        self.keepalive = keepalive
        self._lock = threading.RLock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._active_runs: dict[str, dict[str, Any]] = {}
        self._last_seq: dict[str, int] = {}
        self.stats_data: dict[str, int] = {
            "updates_accepted": 0,
            "updates_rejected": 0,
            "gets_accepted": 0,
            "gets_rejected": 0,
            "delta_published": 0,
            "telemetry_persisted": 0,
            "telemetry_invalid": 0,
            "duplicates_dropped": 0,
            "runs_recorded": 0,
        }
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=2)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 5.0) -> None:
        self.client.connect(self.broker_host, self.broker_port, keepalive=self.keepalive)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            self.client.loop_stop()
            raise TimeoutError("shadow service did not connect to the broker")
        self._log(
            f"shadow service ready on {self.broker_host}:{self.broker_port} as {self.client_id}"
        )

    def stop(self) -> None:
        self._stop.set()
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self._connected.wait(timeout)

    def _log(self, message: str) -> None:
        print(f"[shadow] {message}", flush=True)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self.stats_data)

    def get_shadow(self, thing_id: str) -> dict[str, Any] | None:
        return self.store.get_shadow(thing_id)

    def apply_update(
        self, thing_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Apply a shadow update directly (used by tests and local tooling).

        Returns ``(accepted_payload, delta_payload)`` or ``(None, rejection)``.
        """
        with self._lock:
            return self._apply_update_locked(thing_id, payload)

    # ---------------------------------------------------------------- paho callbacks
    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        self._connected.set()
        client.subscribe(
            [
                (f"{SHADOW_ROOT}/+/shadow/update", 1),
                (f"{SHADOW_ROOT}/+/shadow/get", 1),
                ("homelink/+/telemetry", 1),
            ]
        )
        self._log(f"connected rc={reason_code}")

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self._connected.clear()
        self._log("disconnected; paho will reconnect")

    def _on_message(self, client: Any, userdata: Any, message: mqtt.MQTTMessage) -> None:
        try:
            self.handle_message(message.topic, message.payload)
        except Exception as exc:  # never let a bad message kill the network loop
            self._log(f"message handler error on {message.topic}: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- message handling
    def handle_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) == 5 and parts[0] == "$iot" and parts[1] == "things" and parts[3] == "shadow":
            thing_id = parts[2]
            if parts[4] == "update":
                self._handle_update(thing_id, payload)
            elif parts[4] == "get":
                self._handle_get(thing_id, payload)
            return
        if len(parts) == 3 and parts[0] == "homelink" and parts[2] == "telemetry":
            self._handle_telemetry(parts[1], payload)

    def _decode(self, payload: bytes) -> Any:
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def _publish_json(self, topic: str, payload: dict[str, Any], qos: int = 1) -> None:
        self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=qos)

    def _reject_update(
        self, thing_id: str, code: int, message: str, client_token: str | None
    ) -> None:
        response: dict[str, Any] = {"code": code, "message": message, "timestamp": now_ms()}
        if client_token is not None:
            response["clientToken"] = client_token
        self._publish_json(rejected_topic(thing_id), response)
        self.stats_data["updates_rejected"] += 1
        self._log(f"update rejected thing={thing_id} code={code} {message}")

    def _handle_update(self, thing_id: str, raw: bytes) -> None:
        try:
            payload = self._decode(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject_update(thing_id, 400, "Invalid JSON", None)
            return
        if not isinstance(payload, dict):
            self._reject_update(thing_id, 400, "Update payload must be an object", None)
            return
        client_token = payload.get("clientToken")
        if client_token is not None and not isinstance(client_token, str):
            self._reject_update(thing_id, 400, "clientToken must be a string", None)
            return
        requested_version = payload.get("version")
        if requested_version is not None:
            try:
                requested_version = int(requested_version)
            except (TypeError, ValueError):
                self._reject_update(thing_id, 400, "version must be an integer", client_token)
                return
        state_patch = payload.get("state")
        if not isinstance(state_patch, dict):
            self._reject_update(thing_id, 400, "state must be an object", client_token)
            return
        for section in ("desired", "reported"):
            if section in state_patch and state_patch[section] is not None and not isinstance(
                state_patch[section], dict
            ):
                self._reject_update(thing_id, 400, f"state.{section} must be an object", client_token)
                return
        if not any(section in state_patch for section in ("desired", "reported")):
            self._reject_update(
                thing_id, 400, "state must contain desired or reported", client_token
            )
            return

        with self._lock:
            accepted, rejection = self._apply_update_locked(
                thing_id,
                {"state": state_patch, "version": requested_version, "clientToken": client_token},
            )
        if rejection is not None:
            self._reject_update(
                thing_id,
                int(rejection["code"]),
                str(rejection["message"]),
                client_token,
            )
            return
        assert accepted is not None
        self._publish_json(accepted_topic(thing_id), accepted)
        self.stats_data["updates_accepted"] += 1
        delta = accepted["state"].get("delta") or {}
        if delta:
            delta_payload = {
                "state": delta,
                "version": accepted["version"],
                "timestamp": accepted["timestamp"],
            }
            if accepted.get("clientToken") is not None:
                delta_payload["clientToken"] = accepted["clientToken"]
            self._publish_json(delta_topic(thing_id), delta_payload)
            self.stats_data["delta_published"] += 1

    def _apply_update_locked(
        self, thing_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        existing = self.store.get_shadow(thing_id)
        current_version = int(existing.get("version", 0)) if existing else 0
        requested_version = payload.get("version")
        if requested_version is not None and int(requested_version) != current_version:
            return None, {"code": 409, "message": "Version conflict"}

        if existing:
            document = ShadowDoc.from_dict(existing)
        else:
            document = ShadowDoc(state={}, version=0, timestamp=now_ms())
        timestamp = now_ms()
        state_patch = payload.get("state", {})
        for section in ("desired", "reported"):
            if section not in state_patch:
                continue
            patch = state_patch[section] or {}
            base = document.state.get(section) or {}
            merged = merge_patch(base, patch)
            document.state[section] = merged
            section_metadata = document.metadata.setdefault(section, {})
            _mark_metadata(section_metadata, patch, timestamp)
        document.version = current_version + 1
        document.timestamp = timestamp
        document.clientToken = payload.get("clientToken")
        document.state["delta"] = compute_delta(
            document.state.get("desired") or {}, document.state.get("reported") or {}
        )
        document_dict = document.to_dict()
        try:
            validate_shadow(document_dict)
        except ValidationError as exc:  # pragma: no cover - defensive
            return None, {"code": 400, "message": str(exc)}
        self.store.put_shadow(thing_id, document_dict)
        accepted = {
            "state": {
                "desired": document.state.get("desired"),
                "reported": document.state.get("reported"),
                "delta": document.state.get("delta"),
            },
            "version": document.version,
            "timestamp": document.timestamp,
            "metadata": document.metadata,
        }
        if document.clientToken is not None:
            accepted["clientToken"] = document.clientToken
        return accepted, None

    def _handle_get(self, thing_id: str, raw: bytes) -> None:
        client_token: str | None = None
        if raw:
            try:
                payload = self._decode(raw)
                if isinstance(payload, dict) and isinstance(payload.get("clientToken"), str):
                    client_token = payload["clientToken"]
            except (UnicodeDecodeError, json.JSONDecodeError):
                client_token = None
        document = self.store.get_shadow(thing_id)
        if document is None:
            response: dict[str, Any] = {
                "code": 404,
                "message": f"No shadow exists with name: {thing_id}",
                "timestamp": now_ms(),
            }
            if client_token is not None:
                response["clientToken"] = client_token
            self._publish_json(get_rejected_topic(thing_id), response)
            self.stats_data["gets_rejected"] += 1
            return
        response = dict(document)
        response["state"] = dict(document.get("state", {}))
        response["state"].setdefault(
            "delta",
            compute_delta(
                document.get("state", {}).get("desired") or {},
                document.get("state", {}).get("reported") or {},
            ),
        )
        if client_token is not None:
            response["clientToken"] = client_token
        self._publish_json(get_accepted_topic(thing_id), response)
        self.stats_data["gets_accepted"] += 1

    # ---------------------------------------------------------------- telemetry
    def _handle_telemetry(self, thing_id: str, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
            validate_telemetry(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            with self._lock:
                self.stats_data["telemetry_invalid"] += 1
            self._log(f"telemetry rejected thing={thing_id}: {exc}")
            return
        if payload["thing_id"] != thing_id:
            with self._lock:
                self.stats_data["telemetry_invalid"] += 1
            self._log(
                f"telemetry rejected: topic thing={thing_id} payload thing={payload['thing_id']}"
            )
            return

        with self._lock:
            last_seq = self._last_seq.get(thing_id)
            if last_seq is not None and payload["seq"] <= last_seq:
                self.stats_data["duplicates_dropped"] += 1
                return
            self._last_seq[thing_id] = payload["seq"]
            self.store.put_telemetry(thing_id, payload)
            self.stats_data["telemetry_persisted"] += 1
            self._track_run(thing_id, payload)

    def _track_run(self, thing_id: str, telemetry: dict[str, Any]) -> None:
        state = telemetry["state"]
        now = int(telemetry["ts_ms"])
        if state == "RUNNING":
            active = self._active_runs.get(thing_id)
            if active is None:
                active = {
                    "run_id": uuid.uuid4().hex,
                    "started_at": max(0, now - int(float(telemetry["runtime_s"]) * 1000)),
                    "battery_start": telemetry["battery_pct"],
                    "samples": 0,
                    "current_sum": 0.0,
                    "suction_sum": 0.0,
                }
                self._active_runs[thing_id] = active
            active["samples"] += 1
            active["current_sum"] += float(telemetry["motor_current_a"])
            active["suction_sum"] += float(telemetry["suction_pa"])
            active["last_ts"] = now
            active["battery_end"] = telemetry["battery_pct"]
            active["firmware"] = telemetry["fw_version"]
            active["error_code"] = telemetry["error_code"]
            return

        active = self._active_runs.pop(thing_id, None)
        if active is None:
            return
        shadow = self.store.get_shadow(thing_id) or {}
        reported = shadow.get("state", {}).get("reported") or {}
        desired = shadow.get("state", {}).get("desired") or {}
        power_level = reported.get("power_level", desired.get("power_level", 3))
        samples = max(1, int(active["samples"]))
        run = {
            "run_id": active["run_id"],
            "started_at": int(active["started_at"]),
            "ended_at": now,
            "duration_s": round(max(0.0, (now - int(active["started_at"])) / 1000.0), 3),
            "power_level": power_level,
            "firmware": active.get("firmware", telemetry["fw_version"]),
            "error_code": active.get("error_code", "NONE"),
            "battery_start": round(float(active.get("battery_start", 100.0)), 3),
            "battery_end": round(float(active.get("battery_end", telemetry["battery_pct"])), 3),
            "motor_current_avg": round(active["current_sum"] / samples, 4),
            "suction_avg": round(active["suction_sum"] / samples, 2),
            "samples": samples,
        }
        self.store.append_run(thing_id, run)
        self.stats_data["runs_recorded"] += 1
        self._log(
            f"run recorded thing={thing_id} run={run['run_id'][:8]} duration={run['duration_s']}s"
        )


def parse_broker(value: str) -> tuple[str, int]:
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host, int(port)
    return value, 18831


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HomeLink device shadow service")
    parser.add_argument("--broker", default="localhost:18831")
    parser.add_argument("--store", default="sqlite:///homelink.db")
    parser.add_argument("--client-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    host, port = parse_broker(args.broker)
    service = ShadowService(host, port, make_store(args.store), client_id=args.client_id)
    service.start()
    print("homelink shadow service running (Ctrl-C to stop)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
