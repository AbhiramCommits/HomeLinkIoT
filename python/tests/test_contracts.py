from homelink.contracts import ShadowDoc, Telemetry, ValidationError, validate_telemetry


def test_telemetry_round_trip() -> None:
    telemetry = Telemetry(
        thing_id="vac-001",
        seq=7,
        ts_ms=1_700_000_000_000,
        fw_version="1.2.0",
        state="RUNNING",
        motor_current_a=1.42,
        motor_rpm=2100.0,
        suction_pa=1800.0,
        battery_pct=88.5,
        bin_pct=12.0,
        runtime_s=32.5,
        error_code="NONE",
    )
    parsed = Telemetry.from_dict(telemetry.to_dict())
    assert parsed == telemetry
    assert "NONE" in ("NONE", "BRUSH_JAM")


def test_invalid_state_is_rejected() -> None:
    payload = {
        "thing_id": "vac-001",
        "seq": 1,
        "ts_ms": 1,
        "fw_version": "1.2.0",
        "state": "EXPLODING",
        "motor_current_a": 1.0,
        "motor_rpm": 1000,
        "suction_pa": 1000,
        "battery_pct": 50,
        "bin_pct": 1,
        "runtime_s": 1,
        "error_code": "NONE",
    }
    try:
        validate_telemetry(payload)
    except ValidationError as exc:
        assert "state" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("schema violation was accepted")


def test_shadow_round_trip() -> None:
    doc = ShadowDoc(
        state={"desired": {"power_level": 3}, "reported": {"power_level": 3}},
        version=4,
        timestamp=1_700_000_000_000,
        clientToken="token-1",
    )
    assert ShadowDoc.from_dict(doc.to_dict()) == doc
