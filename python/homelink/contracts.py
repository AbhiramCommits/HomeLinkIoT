"""Shared contracts and a dependency-free JSON Schema validator.

The schemas in ``schemas/`` are the single source of truth shared by the Python
services, the TypeScript API (contract tests), and the documentation.  We only
implement the JSON Schema keyword subset those documents use, which keeps the
runtime dependency surface small (no ``jsonschema`` package).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(
    os.environ.get(
        "HOMELINK_SCHEMA_DIR",
        Path(__file__).resolve().parents[2] / "schemas",
    )
)


class ValidationError(ValueError):
    """Raised when a payload does not satisfy its JSON Schema."""

    def __init__(self, message: str, path: str = "$") -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _check_type(value: Any, expected: str | list[str], path: str) -> None:
    expected_types = [expected] if isinstance(expected, str) else list(expected)
    for type_name in expected_types:
        py_type = _TYPE_MAP.get(type_name)
        if py_type is None:
            raise ValidationError(f"unsupported schema type {type_name!r}", path)
        if type_name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py_type):
            return
    raise ValidationError(
        f"expected {' or '.join(expected_types)}, got {type(value).__name__}", path
    )


def validate_payload(payload: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate ``payload`` against the JSON Schema subset used in this repo."""

    if "type" in schema:
        _check_type(payload, schema["type"], path)

    if "enum" in schema and payload not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        raise ValidationError(f"value {payload!r} is not one of [{allowed}]", path)

    if "oneOf" in schema:
        matches = 0
        last_error: ValidationError | None = None
        for option in schema["oneOf"]:
            try:
                validate_payload(payload, option, path)
                matches += 1
            except ValidationError as exc:  # noqa: PERF203 - tiny schema branch list
                last_error = exc
        if matches != 1:
            detail = last_error.message if last_error else "no matching branch"
            raise ValidationError(f"does not match exactly one schema ({detail})", path)

    if isinstance(payload, str):
        if "minLength" in schema and len(payload) < schema["minLength"]:
            raise ValidationError(f"shorter than minLength {schema['minLength']}", path)
        if "maxLength" in schema and len(payload) > schema["maxLength"]:
            raise ValidationError(f"longer than maxLength {schema['maxLength']}", path)
        if "pattern" in schema and not re.search(schema["pattern"], payload):
            raise ValidationError(f"does not match pattern {schema['pattern']!r}", path)

    if isinstance(payload, int | float) and not isinstance(payload, bool):
        if "minimum" in schema and payload < schema["minimum"]:
            raise ValidationError(f"less than minimum {schema['minimum']}", path)
        if "maximum" in schema and payload > schema["maximum"]:
            raise ValidationError(f"greater than maximum {schema['maximum']}", path)

    if isinstance(payload, list):
        if "minItems" in schema and len(payload) < schema["minItems"]:
            raise ValidationError(f"fewer than minItems {schema['minItems']}", path)
        if "maxItems" in schema and len(payload) > schema["maxItems"]:
            raise ValidationError(f"more than maxItems {schema['maxItems']}", path)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(payload):
                validate_payload(item, item_schema, f"{path}[{index}]")

    if isinstance(payload, dict):
        for required in schema.get("required", []):
            if required not in payload:
                raise ValidationError(f"missing required property {required!r}", path)
        properties = schema.get("properties", {})
        for key, value in payload.items():
            if key in properties:
                validate_payload(value, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValidationError(f"unexpected property {key!r}", path)


def load_schema(filename: str) -> dict[str, Any]:
    path = SCHEMA_DIR / filename
    with path.open("r", encoding="utf-8") as fh:
        schema: dict[str, Any] = json.load(fh)
        return schema


_TELEMETRY_SCHEMA = load_schema("telemetry.schema.json")
_SHADOW_SCHEMA = load_schema("shadow.schema.json")

STATES = frozenset({"IDLE", "RUNNING", "DOCKED", "ERROR", "OTA"})
ERROR_CODES = frozenset(
    {
        "NONE",
        "BRUSH_JAM",
        "FILTER_CLOG",
        "WHEEL_STALL",
        "LOW_BATTERY",
        "OTA_CHECKSUM",
        "SENSOR_FAULT",
    }
)


@dataclass
class Telemetry:
    thing_id: str
    seq: int
    ts_ms: int
    fw_version: str
    state: str
    motor_current_a: float
    motor_rpm: float
    suction_pa: float
    battery_pct: float
    bin_pct: float
    runtime_s: float
    error_code: str = "NONE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "thing_id": self.thing_id,
            "seq": self.seq,
            "ts_ms": self.ts_ms,
            "fw_version": self.fw_version,
            "state": self.state,
            "motor_current_a": round(float(self.motor_current_a), 4),
            "motor_rpm": round(float(self.motor_rpm), 2),
            "suction_pa": round(float(self.suction_pa), 2),
            "battery_pct": round(float(self.battery_pct), 3),
            "bin_pct": round(float(self.bin_pct), 3),
            "runtime_s": round(float(self.runtime_s), 3),
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Telemetry:
        validate_telemetry(payload)
        return cls(
            thing_id=payload["thing_id"],
            seq=payload["seq"],
            ts_ms=payload["ts_ms"],
            fw_version=payload["fw_version"],
            state=payload["state"],
            motor_current_a=float(payload["motor_current_a"]),
            motor_rpm=float(payload["motor_rpm"]),
            suction_pa=float(payload["suction_pa"]),
            battery_pct=float(payload["battery_pct"]),
            bin_pct=float(payload["bin_pct"]),
            runtime_s=float(payload["runtime_s"]),
            error_code=payload["error_code"],
        )


def validate_telemetry(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValidationError("telemetry payload must be an object")
    validate_payload(payload, _TELEMETRY_SCHEMA)


@dataclass
class ShadowDoc:
    state: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    timestamp: int = 0
    clientToken: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "state": self.state,
            "version": self.version,
            "timestamp": self.timestamp,
        }
        if self.clientToken is not None:
            doc["clientToken"] = self.clientToken
        if self.metadata:
            doc["metadata"] = self.metadata
        return doc

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ShadowDoc:
        validate_shadow(payload)
        return cls(
            state=payload["state"],
            version=payload["version"],
            timestamp=payload["timestamp"],
            clientToken=payload.get("clientToken"),
            metadata=payload.get("metadata", {}),
        )


def validate_shadow(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValidationError("shadow document must be an object")
    validate_payload(payload, _SHADOW_SCHEMA)
