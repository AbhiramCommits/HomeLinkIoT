#!/usr/bin/env python3
"""HomeLink benchmark: broker throughput, shadow latency, reconnect, API reads."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from homelink.broker.server import Broker, BrokerRunner
from homelink.cloud.shadow import ShadowService
from homelink.cloud.store import SqliteStore
from homelink.contracts import Telemetry

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "bench" / "out"
DB_PATH = OUT_DIR / "bench.db"
BROKER_PORT = 18861
API_PORT = 18181


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round(fraction * len(sorted_values))) - 1))
    return sorted_values[index]


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(percentile(ordered, 0.50), 3),
        "p95": round(percentile(ordered, 0.95), 3),
        "p99": round(percentile(ordered, 0.99), 3),
        "mean": round(statistics.fmean(ordered), 3) if ordered else 0.0,
    }


def make_client(client_id: str, clean_session: bool = True) -> tuple[mqtt.Client, threading.Event]:
    connected = threading.Event()
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=clean_session,
        protocol=mqtt.MQTTv311,
    )
    client.reconnect_delay_set(min_delay=0.5, max_delay=2)

    def on_connect(_c: Any, _u: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        if getattr(reason_code, "value", reason_code) == 0:
            connected.set()

    client.on_connect = on_connect
    client.connect("127.0.0.1", BROKER_PORT, keepalive=30)
    client.loop_start()
    if not connected.wait(5):
        client.loop_stop()
        raise TimeoutError(f"{client_id} did not connect")
    return client, connected


def telemetry_payload(thing_id: str, seq: int) -> bytes:
    payload = Telemetry(
        thing_id=thing_id,
        seq=seq,
        ts_ms=int(time.time() * 1000),
        fw_version="1.2.0",
        state="IDLE",
        motor_current_a=0.4,
        motor_rpm=0.0,
        suction_pa=0.0,
        battery_pct=95.0,
        bin_pct=2.0,
        runtime_s=0.0,
        error_code="NONE",
    ).to_dict()
    return json.dumps(payload, separators=(",", ":")).encode()


def measure_throughput(messages: int = 20_000, clients: int = 10) -> dict[str, Any]:
    per_client = messages // clients
    barrier = threading.Barrier(clients + 1)
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            client, _ = make_client(f"bench-tp-{index}")
            barrier.wait()
            for seq in range(per_client):
                info = client.publish(
                    f"homelink/bench-tp-{index}/telemetry",
                    telemetry_payload(f"bench-tp-{index}", seq),
                    qos=1,
                )
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    errors.append(f"publish rc={info.rc}")
                    break
                info.wait_for_publish(timeout=3.0)
            barrier.wait()
        except Exception as exc:  # pragma: no cover - benchmark failure path
            errors.append(f"{type(exc).__name__}: {exc}")
            try:
                barrier.abort()
            except Exception:
                pass
        finally:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:
                pass

    threads = [threading.Thread(target=worker, args=(index,), daemon=True) for index in range(clients)]
    for thread in threads:
        thread.start()
    barrier.wait()
    started = time.perf_counter()
    barrier.wait()
    elapsed = time.perf_counter() - started
    for thread in threads:
        thread.join(timeout=10)
    sent = per_client * clients
    return {
        "messages": sent,
        "clients": clients,
        "seconds": round(elapsed, 3),
        "messages_per_second": round(sent / elapsed, 1) if elapsed else 0.0,
        "errors": errors[:5],
    }


def measure_shadow_latency(store: SqliteStore, samples: int = 300) -> dict[str, Any]:
    client, _ = make_client("bench-latency")
    thing_id = "bench-lat"
    latencies: list[float] = []
    missed = 0
    try:
        for seq in range(1, samples + 1):
            started = time.perf_counter()
            info = client.publish(
                f"homelink/{thing_id}/telemetry", telemetry_payload(thing_id, seq), qos=1
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                missed += 1
                continue
            try:
                info.wait_for_publish(timeout=3.0)
            except RuntimeError:
                missed += 1
                continue
            deadline = time.perf_counter() + 3.0
            while time.perf_counter() < deadline:
                latest = store.get_telemetry(thing_id)
                if latest and int(latest.get("seq", -1)) >= seq:
                    latencies.append((time.perf_counter() - started) * 1000.0)
                    break
                time.sleep(0.0005)
            else:
                missed += 1
    finally:
        client.disconnect()
        client.loop_stop()
    result = summary(latencies)
    result.update({"samples": len(latencies), "missed": missed})
    return result


def measure_shadow_updates(store: SqliteStore, total: int = 500) -> dict[str, Any]:
    client, _ = make_client("bench-shadow-pub")
    thing_id = "bench-shadow"
    try:
        started = time.perf_counter()
        last_info = None
        for index in range(1, total + 1):
            last_info = client.publish(
                f"$iot/things/{thing_id}/shadow/update",
                json.dumps({"state": {"reported": {"counter": index}}}),
                qos=1,
            )
        if last_info is not None:
            try:
                last_info.wait_for_publish(timeout=5.0)
            except RuntimeError:
                pass
        deadline = time.perf_counter() + 15.0
        version = 0
        while time.perf_counter() < deadline:
            document = store.get_shadow(thing_id)
            version = int(document.get("version", 0)) if document else 0
            if version >= total:
                break
            time.sleep(0.001)
        elapsed = time.perf_counter() - started
    finally:
        client.disconnect()
        client.loop_stop()
    return {
        "updates": total,
        "applied_version": version,
        "seconds": round(elapsed, 3),
        "updates_per_second": round(version / elapsed, 1) if elapsed else 0.0,
    }


def measure_reconnect(runner: BrokerRunner, outage_seconds: float = 10.0) -> float:
    client, connected = make_client("bench-reconnect", clean_session=False)
    connected.clear()
    try:
        runner.stop()
        time.sleep(outage_seconds)
        restarted = time.perf_counter()
        runner.start()
        if not connected.wait(20):
            return -1.0
        return round((time.perf_counter() - restarted) * 1000.0, 1)
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass


def measure_api(db_path: Path) -> dict[str, Any] | None:
    api_dir = ROOT / "api"
    entry = api_dir / "dist" / "local.js"
    node = shutil.which("node")
    if node is None or not entry.exists():
        print("API_READ_LATENCY skipped (node or api/dist/local.js missing)", flush=True)
        return None
    env = {
        **os.environ,
        "PORT": str(API_PORT),
        "HOMELINK_MQTT_HOST": "127.0.0.1",
        "HOMELINK_MQTT_PORT": str(BROKER_PORT),
        "HOMELINK_DB": str(db_path),
        "HOMELINK_API_KEY": "bench-key",
        "HOMELINK_ANALYTICS_PATH": str(ROOT / "analytics" / "out" / "fleet.json"),
    }
    process = subprocess.Popen(
        [node, str(entry)],
        cwd=api_dir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    latencies: list[float] = []
    try:
        url = f"http://127.0.0.1:{API_PORT}/devices"
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                request = urllib.request.Request(url, headers={"x-api-key": "bench-key"})
                with urllib.request.urlopen(request, timeout=1) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        else:
            return None
        for _ in range(200):
            request = urllib.request.Request(url, headers={"x-api-key": "bench-key"})
            started = time.perf_counter()
            with urllib.request.urlopen(request, timeout=2) as response:
                response.read()
            latencies.append((time.perf_counter() - started) * 1000.0)
        result = summary(latencies)
        result["requests"] = len(latencies)
        return result
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HomeLink benchmark suite")
    parser.add_argument("--messages", type=int, default=20_000)
    parser.add_argument("--latency-samples", type=int, default=300)
    parser.add_argument("--shadow-updates", type=int, default=500)
    parser.add_argument("--outage-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(DB_PATH) + suffix)
        if candidate.exists():
            candidate.unlink()

    broker = Broker(port=BROKER_PORT)
    runner = BrokerRunner(broker)
    store = SqliteStore(DB_PATH)
    shadow: ShadowService | None = None
    result: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0],
    }
    try:
        runner.start()
        result["broker_throughput"] = measure_throughput(messages=args.messages, clients=10)
        print(
            f"BROKER_THROUGHPUT msgs_per_s={result['broker_throughput']['messages_per_second']} "
            f"messages={result['broker_throughput']['messages']} "
            f"clients={result['broker_throughput']['clients']} "
            f"seconds={result['broker_throughput']['seconds']}",
            flush=True,
        )

        shadow = ShadowService("127.0.0.1", BROKER_PORT, store, client_id="bench-shadow-service")
        shadow.start()
        time.sleep(0.2)

        result["e2e_latency_ms"] = measure_shadow_latency(store, samples=args.latency_samples)
        print(
            "E2E_LATENCY_MS "
            f"p50={result['e2e_latency_ms']['p50']} "
            f"p95={result['e2e_latency_ms']['p95']} "
            f"p99={result['e2e_latency_ms']['p99']} "
            f"samples={result['e2e_latency_ms']['samples']}",
            flush=True,
        )

        result["shadow_updates"] = measure_shadow_updates(store, total=args.shadow_updates)
        print(
            f"SHADOW_UPDATES_PER_S {result['shadow_updates']['updates_per_second']} "
            f"updates={result['shadow_updates']['updates']} "
            f"applied_version={result['shadow_updates']['applied_version']}",
            flush=True,
        )

        result["reconnect_recovery_ms"] = measure_reconnect(runner, outage_seconds=args.outage_seconds)
        print(
            f"RECONNECT_RECOVERY_MS {result['reconnect_recovery_ms']} "
            f"outage_s={args.outage_seconds}",
            flush=True,
        )

        api = measure_api(Path(DB_PATH).resolve())
        result["api_read_latency_ms"] = api
        if api:
            print(
                f"API_READ_LATENCY_MS p50={api['p50']} p95={api['p95']} "
                f"requests={api['requests']}",
                flush=True,
            )
        result["broker_stats"] = broker.stats()
        result["shadow_stats"] = shadow.stats()
    finally:
        if shadow is not None:
            shadow.stop()
        store.close()
        runner.close()

    (OUT_DIR / "bench.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"BENCH_OUTPUT {OUT_DIR / 'bench.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
