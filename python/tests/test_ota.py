from __future__ import annotations

import time

import pytest

from homelink.cloud.ota import FirmwareServer
from homelink.device.appliance import Appliance, ApplianceState

THING = "vac-001"


def wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def ota_stack(broker, shadow, tmp_path):
    service, store, port = shadow
    appliance = Appliance(
        thing_id=THING,
        broker_host="127.0.0.1",
        broker_port=port,
        spool_dir=tmp_path / "spool",
        seed=13,
        sample_interval=0.1,
        ota_timeout=1.0,
    )
    appliance.set_credentials("TestNet", "test-psk")
    appliance.start()
    server = FirmwareServer("127.0.0.1", port, client_id=f"ota-test-{port}")
    server.start()
    assert appliance.wait_for_state(ApplianceState.ONLINE, 3)
    try:
        yield appliance, store, server
    finally:
        server.stop()
        appliance.stop()


def test_ota_success_updates_version(ota_stack) -> None:
    appliance, store, server = ota_stack
    result = server.trigger(THING, "1.3.0")
    spec = result["spec"]
    assert result["chunks_published"] == spec["chunks"]

    assert wait_until(lambda: appliance.ota_status == "success", 8)
    assert appliance.fw_version == "1.3.0"
    assert appliance.error_code == "NONE"

    def shadow_updated() -> bool:
        document = store.get_shadow(THING)
        if not document:
            return False
        reported = document["state"].get("reported", {})
        return reported.get("fw_version") == "1.3.0" and reported.get("ota", {}).get("status") == "success"

    assert wait_until(shadow_updated, 5)
    assert wait_until(
        lambda: (store.get_telemetry(THING) or {}).get("fw_version") == "1.3.0", 5
    )
    appliance.begin_run()
    assert appliance.wait_for_state(ApplianceState.RUNNING, 3)


def test_ota_checksum_mismatch_rolls_back(ota_stack) -> None:
    appliance, store, server = ota_stack
    original = appliance.fw_version
    result = server.trigger(THING, "1.4.0", corrupt=True)
    assert result["corrupted"] is True

    assert wait_until(lambda: appliance.ota_status == "rolled_back", 8)
    assert appliance.fw_version == original
    assert appliance.error_code == "OTA_CHECKSUM"
    assert appliance.state == ApplianceState.ONLINE

    def shadow_reports_rollback() -> bool:
        document = store.get_shadow(THING)
        if not document:
            return False
        reported = document["state"].get("reported", {})
        return (
            reported.get("ota", {}).get("status") == "rolled_back"
            and reported.get("error_code") == "OTA_CHECKSUM"
            and reported.get("fw_version") == original
        )

    assert wait_until(shadow_reports_rollback, 5)

    before = appliance.samples_generated
    appliance.begin_run()
    assert appliance.wait_for_state(ApplianceState.RUNNING, 3)
    assert wait_until(lambda: appliance.samples_generated > before + 2, 3)


def test_ota_dropped_chunk_rolls_back(ota_stack) -> None:
    appliance, _, server = ota_stack
    original = appliance.fw_version
    result = server.trigger(THING, "1.5.0", drop_chunk=0)
    assert result["chunks_dropped"] == 1
    assert wait_until(lambda: appliance.ota_status == "rolled_back", 8)
    assert appliance.fw_version == original
    assert appliance.error_code == "OTA_CHECKSUM"
    assert appliance.state == ApplianceState.ONLINE
