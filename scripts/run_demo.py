#!/usr/bin/env python3
"""End-to-end HomeLink demo: BLE provisioning -> telemetry -> outage -> OTA.

Spins up the broker, shadow service, three simulated appliances and an OTA
service entirely on localhost, then prints the measured summary table.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

from homelink.broker.server import Broker, BrokerRunner
from homelink.cloud.ota import FirmwareServer
from homelink.cloud.shadow import ShadowService
from homelink.cloud.store import SqliteStore
from homelink.device.appliance import Appliance, ApplianceState
from homelink.device.ble import BlePeripheral
from homelink.device.provision_client import provision

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def clean_runtime(db_path: Path, spool_dir: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    if spool_dir.exists():
        shutil.rmtree(spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HomeLink end-to-end demo")
    parser.add_argument("--seconds", type=float, default=60.0, help="cleaning-cycle duration")
    parser.add_argument("--devices", type=int, default=3)
    parser.add_argument("--broker-port", type=int, default=18831)
    parser.add_argument("--ble-base-port", type=int, default=18840)
    parser.add_argument("--db", default="homelink.db")
    parser.add_argument("--spool-dir", default="device_spool")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json-out", default=None)
    parser.add_argument(
        "--no-outage",
        action="store_true",
        help="skip the forced broker outage (used by Playwright e2e for determinism)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    spool_dir = Path(args.spool_dir)
    clean_runtime(db_path, spool_dir)
    if Path("broker.log").exists():
        Path("broker.log").unlink()

    broker = Broker(port=args.broker_port, log_path="broker.log")
    runner = BrokerRunner(broker)
    store: SqliteStore | None = None
    shadow: ShadowService | None = None
    ota: FirmwareServer | None = None
    appliances: list[Appliance] = []
    peripherals: list[BlePeripheral] = []
    provisioning: list[dict[str, Any]] = []
    ota_results: list[dict[str, Any]] = []

    try:
        runner.start()
        store = SqliteStore(db_path)
        shadow = ShadowService("127.0.0.1", args.broker_port, store, client_id="demo-shadow")
        shadow.start()
        ota = FirmwareServer("127.0.0.1", args.broker_port, client_id="demo-ota")
        ota.start()
        time.sleep(0.2)

        # ---------------------------------------------------------- BLE provisioning
        print("Provisioning appliances over simulated BLE GATT ...", flush=True)
        for index in range(args.devices):
            thing = f"vac-{index + 1:03d}"
            passkey = f"{482913 + index:06d}"
            appliance = Appliance(
                thing_id=thing,
                broker_host="127.0.0.1",
                broker_port=args.broker_port,
                spool_dir=spool_dir,
                seed=args.seed + index,
                sample_interval=0.5,
            )
            appliances.append(appliance)

            def link(ssid: str, psk: str, target: Appliance = appliance) -> None:
                target.set_credentials(ssid, psk)

            peripheral = BlePeripheral(
                device_id=thing,
                passkey=passkey,
                fw_version=appliance.fw_version,
                port=args.ble_base_port + index,
                on_provisioned=link,
            )
            peripheral.start()
            peripherals.append(peripheral)
            appliance.begin_pairing()
            result = provision(
                "127.0.0.1",
                peripheral.port,
                passkey,
                "HomeNet-5G",
                f"wifi-secret-{thing}",
                timeout=6.0,
            )
            result["thing_id"] = thing
            provisioning.append(result)
            if not result["ok"]:
                print(f"  {thing}: provisioning FAILED ({result.get('error')})", flush=True)

        if not all(item["ok"] for item in provisioning):
            raise RuntimeError("provisioning failed; aborting demo")

        for appliance in appliances:
            appliance.start()
        if not wait_until(lambda: all(a.state != ApplianceState.CONNECTING for a in appliances), 8):
            raise RuntimeError("appliances did not connect")

        for appliance in appliances:
            appliance.begin_run()
        time.sleep(0.2)

        # ---------------------------------------------------------- timed scenario
        start = time.monotonic()
        broker_down = False
        outage_started_at: float | None = None
        outage_done = args.no_outage or args.seconds <= 15
        restart_at: float | None = None
        reconnect_ms: float | None = None
        ota_success_triggered = False
        ota_corrupt_triggered = False
        fault_injected = False
        event_log: list[dict[str, Any]] = []

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= args.seconds:
                break

            if elapsed >= 10.0 and not broker_down and not outage_done and outage_started_at is None:
                runner.stop()
                broker_down = True
                outage_started_at = time.monotonic()
                event_log.append({"t_s": round(elapsed, 2), "event": "broker killed for 10s"})
                print(f"  t={elapsed:5.1f}s broker killed (forced 10s outage)", flush=True)

            if broker_down and outage_started_at is not None and time.monotonic() - outage_started_at >= 10.0:
                runner.start()
                broker_down = False
                outage_done = True
                restart_at = time.monotonic()
                event_log.append({"t_s": round(elapsed, 2), "event": "broker restarted"})
                print(f"  t={elapsed:5.1f}s broker restarted", flush=True)

            if restart_at is not None and reconnect_ms is None:
                if all(a.is_connected and not a.spooled_samples() for a in appliances):
                    reconnect_ms = round((time.monotonic() - restart_at) * 1000.0, 1)
                    event_log.append(
                        {"t_s": round(elapsed, 2), "event": f"all devices reconnected in {reconnect_ms} ms"}
                    )

            if elapsed >= 30.0 and not ota_success_triggered and len(appliances) >= 1:
                result = ota.trigger(appliances[0].thing_id, "1.3.0")
                result["thing_id"] = appliances[0].thing_id
                result["expected"] = "success"
                ota_results.append(result)
                ota_success_triggered = True
                event_log.append({"t_s": round(elapsed, 2), "event": "OTA 1.3.0 -> vac-001"})
                print(f"  t={elapsed:5.1f}s OTA 1.3.0 -> {appliances[0].thing_id}", flush=True)

            if elapsed >= 33.0 and not ota_corrupt_triggered and len(appliances) >= 2:
                result = ota.trigger(appliances[1].thing_id, "1.4.0", corrupt=True)
                result["thing_id"] = appliances[1].thing_id
                result["expected"] = "rolled_back"
                ota_results.append(result)
                ota_corrupt_triggered = True
                event_log.append({"t_s": round(elapsed, 2), "event": "corrupt OTA 1.4.0 -> vac-002"})
                print(
                    f"  t={elapsed:5.1f}s corrupt OTA 1.4.0 -> {appliances[1].thing_id}",
                    flush=True,
                )

            if elapsed >= 36.0 and not fault_injected and len(appliances) >= 3:
                appliances[2].inject_fault("BRUSH_JAM", duration_s=6.0)
                fault_injected = True
                event_log.append({"t_s": round(elapsed, 2), "event": "BRUSH_JAM injected -> vac-003"})
                print(f"  t={elapsed:5.1f}s BRUSH_JAM injected -> {appliances[2].thing_id}", flush=True)

            time.sleep(0.1)

        # Let any in-flight OTA finish, then dock and drain.
        wait_until(
            lambda: all(
                (a.ota_status in ("success", "rolled_back")) or a.ota_status is None for a in appliances
            ),
            8,
        )
        for appliance in appliances:
            appliance.dock()
        time.sleep(1.0)
        expected_total = sum(a.samples_generated for a in appliances)
        wait_until(
            lambda: (shadow.stats()["telemetry_persisted"] >= expected_total)
            and all(not a.spooled_samples() for a in appliances),
            10,
        )
        time.sleep(1.0)

        # ---------------------------------------------------------- summary
        broker_stats = broker.stats()
        shadow_stats = shadow.stats()
        generated = sum(a.samples_generated for a in appliances)
        published = sum(a.published_count for a in appliances)
        spooled = sum(a.spooled_count for a in appliances)
        replayed = sum(a.replayed_count for a in appliances)
        persisted = shadow_stats["telemetry_persisted"]
        duplicates = shadow_stats["duplicates_dropped"]
        lost = max(0, generated - persisted)

        print("\n=== Provisioning (BLE GATT) ===")
        print(f"{'device':<9}{'status':<14}{'handshake_ms':>13}{'passkey_chunks':>16}{'cred_chunks':>13}")
        for item in provisioning:
            print(
                f"{item['thing_id']:<9}{item['status']:<14}"
                f"{item['handshake_ms']:>13.2f}{item['passkey_chunks']:>16}{item['credential_chunks']:>13}"
            )

        print("\n=== Telemetry & shadow ===")
        print(
            f"{'device':<9}{'generated':>10}{'published':>10}{'spooled':>9}{'replayed':>9}"
            f"{'last_seq':>9}{'fw':>7}{'state':>8}{'error':>13}"
        )
        for appliance in appliances:
            print(
                f"{appliance.thing_id:<9}{appliance.samples_generated:>10}{appliance.published_count:>10}"
                f"{appliance.spooled_count:>9}{appliance.replayed_count:>9}{appliance.seq:>9}"
                f"{appliance.fw_version:>7}{appliance.state.value:>8}{appliance.error_code:>13}"
            )

        print("\n=== OTA ===")
        print(f"{'device':<9}{'target':<8}{'expected':<12}{'status':<12}{'chunks':>7}{'dropped':>8}{'corrupt':>8}")
        for result in ota_results:
            appliance = next(a for a in appliances if a.thing_id == result["thing_id"])
            print(
                f"{result['thing_id']:<9}{result['spec']['fw_version']:<8}{result['expected']:<12}"
                f"{str(appliance.ota_status):<12}{result['chunks_published']:>7}"
                f"{result['chunks_dropped']:>8}{str(result['corrupted']):>8}"
            )

        print("\n=== Totals ===")
        print(
            f"generated={generated} published={published} persisted={persisted} "
            f"spooled={spooled} replayed={replayed} lost={lost} duplicates={duplicates}"
        )
        print(f"reconnect_recovery_ms={reconnect_ms}")
        print(f"broker={broker_stats}")
        print(f"shadow={shadow_stats}")
        print(f"shadow_versions={[ (store.get_shadow(a.thing_id) or {}).get('version') for a in appliances ]}")
        print("\nEvents:")
        for event in event_log:
            print(f"  t={event['t_s']:5.1f}s {event['event']}")

        ota_ok = True
        if len(ota_results) >= 2:
            first = next(a for a in appliances if a.thing_id == ota_results[0]["thing_id"])
            second = next(a for a in appliances if a.thing_id == ota_results[1]["thing_id"])
            ota_ok = first.ota_status == "success" and second.ota_status == "rolled_back"
        elif len(ota_results) == 1:
            ota_ok = appliances[0].ota_status == "success"

        summary = {
            "seconds": args.seconds,
            "outage": not args.no_outage,
            "devices": len(appliances),
            "provisioning": [
                {
                    "thing_id": item["thing_id"],
                    "status": item["status"],
                    "handshake_ms": item["handshake_ms"],
                    "passkey_chunks": item["passkey_chunks"],
                    "credential_chunks": item["credential_chunks"],
                }
                for item in provisioning
            ],
            "telemetry": {
                "generated": generated,
                "published": published,
                "persisted": persisted,
                "spooled": spooled,
                "replayed": replayed,
                "lost": lost,
                "duplicates": duplicates,
            },
            "reconnect_recovery_ms": reconnect_ms,
            "ota": [
                {
                    "thing_id": result["thing_id"],
                    "fw_version": result["spec"]["fw_version"],
                    "expected": result["expected"],
                    "status": next(a.ota_status for a in appliances if a.thing_id == result["thing_id"]),
                    "chunks_published": result["chunks_published"],
                    "chunks_dropped": result["chunks_dropped"],
                    "corrupted": result["corrupted"],
                }
                for result in ota_results
            ],
            "broker": broker_stats,
            "shadow": shadow_stats,
            "events": event_log,
        }
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")

        return 0 if (lost == 0 and duplicates == 0 and ota_ok) else 1

    finally:
        for appliance in appliances:
            try:
                appliance.stop()
            except Exception:
                pass
        for peripheral in peripherals:
            try:
                peripheral.stop()
            except Exception:
                pass
        if ota is not None:
            ota.stop()
        if shadow is not None:
            shadow.stop()
        if store is not None:
            store.close()
        runner.close()


if __name__ == "__main__":
    sys.exit(main())
