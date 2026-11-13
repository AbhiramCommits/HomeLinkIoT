# HomeLinkIoT measured results

Every number in this file was printed by a command run during this build. New
measurements are appended as each step is completed; nothing is estimated.

## Protocol & Broker

- MQTT 3.1.1 packet types implemented: CONNECT, CONNACK, PUBLISH (QoS 0 and 1),
  PUBACK, SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT.
- Broker test count: 25 tests in `python/tests/test_broker.py`.
- Protocol codec test count: 29 tests in `python/tests/test_protocol.py`.
- Measured run: `74 passed in 6.21s` for `pytest python/tests -q` (protocol 29,
  broker 25, shadow 8, store 9, contracts 3).
- `ruff check python`: `All checks passed!`

## Provisioning

Measured by `make demo` (60-second run, 3 appliances):

- BLE provisioning handshake: vac-001 `16.99 ms`, vac-002 `58.75 ms`,
  vac-003 `72.31 ms`.
- Passkey write: `1` ATT chunk per device (6 ASCII digits).
- Encrypted Wi-Fi credential write: `10` ATT chunks per device (20-byte ATT MTU,
  18-byte payload, 2-byte chunk header).
- Provisioning results: `PROVISIONED` for 3/3 appliances.

BLE tests: 5 tests in `python/tests/test_ble_provision.py` (happy path, wrong
passkey, chunk reassembly, truncated write, out-of-order/oversized frames).

## Telemetry & Shadow

60-second demo, 3 appliances at 2 Hz:

- Telemetry messages published per device: `123` (369 total, 369 persisted).
- Messages spooled during the forced 10-second broker outage: `66`.
- Messages replayed on reconnect: `66`; spooled loss: `0`; duplicate seqs
  persisted: `0`.
- Reconnect recovery (broker restart to all devices connected and spool
  drained): `1608.2 ms`.
- Shadow updates accepted: `18`; rejected: `0`; deltas published: `2`.
- Completed runs recorded by the shadow service: `3`.

Appliance tests: 4 tests in `python/tests/test_appliance.py`, including
state-machine transitions, delta/command application, ordered spool replay,
and the forced-outage zero-loss/zero-duplicate integration test.

## OTA

60-second demo:

- vac-001 OTA to `1.3.0`: `success`, 8 chunks published, 0 dropped, firmware
  reported as `1.3.0`.
- vac-002 deliberately corrupted OTA to `1.4.0`: `rolled_back`, 8 chunks
  published, 0 dropped; firmware stayed `1.2.0`; error code `OTA_CHECKSUM`.
- OTA tests: 3 tests in `python/tests/test_ota.py` (success, checksum mismatch
  + rollback, dropped chunk + rollback).

## Fleet Analytics

Command: `python -m homelink.analytics.generate_fleet --seed 7 --devices 40
--days 30` then `python -m homelink.analytics.fleet`.

- Dataset: 40 devices, 1191 runs, span 29.98 days; 45 injected faults, 15
  injected premature stops, 51 anomalous runs.
- Runs per device per week: mean `6.952` (min `4.436`, max `8.873`).
- Runtime: mean `904.32 s`, median `896.18 s`, p95 `1373.58 s` (min
  `111.8 s`, max `2024.78 s`).
- Error-code Pareto (top 3): `BRUSH_JAM 18 faults / 40.0%`,
  `FILTER_CLOG 18 / 40.0%`, `WHEEL_STALL 9 / 20.0%`.
- Firmware adoption: 1.0.0 `18.47%`, 1.1.0 `24.43%`, 1.2.0 `43.16%`,
  1.3.0 `13.94%`.
- Anomaly detector (rolling median + MAD, threshold 3.5): precision
  `0.8033`, recall `0.9608`, F1 `0.875`, false alerts
  `10.076 per 1000 runs`; 61 alerts, 49 TP, 12 FP, 2 FN. Per-fault detection:
  BRUSH_JAM `18/18`, FILTER_CLOG `18/18`, WHEEL_STALL `9/9`.
- Outputs: `analytics/out/fleet.json`, 5 PNG charts, and
  `analytics/out/report.md`.

## Test Suite

- Step 2 Python suite: 74 passed (0 failed) in 6.21s.
- Step 3 Python suite: 86 passed (0 failed) in 19.43s (adds 5 BLE, 4 appliance,
  3 OTA tests).
- Step 4 API: 7 routes (`GET /devices`, `GET /devices/{id}`,
  `GET /devices/{id}/runs`, `POST /devices/{id}/commands`,
  `PATCH /devices/{id}/shadow`, `POST /devices/{id}/ota`,
  `GET /fleet/analytics`); 36 vitest/supertest tests passed in 4.51s across
  8 files.
- API latency over a 200-request `GET /devices` loop: p50 `2.10 ms`,
  p95 `4.44 ms`, p99 `6.01 ms` (in-process supertest, Node 20.19.5).
- Live check with the demo stack running: `GET /devices` with
  `x-api-key: dev-key` returned `3` devices, all `online: true`; the same
  request without a key returned `401`.
- Dashboard unit tests: 9 passed in 1.52s across 3 files (LineChart 2,
  ShadowDiff 3, stale logic 4).
- Playwright e2e: 2 tests passed (test time 7.6s) covering fleet list ->
  device detail -> Dock/Start command state changes -> OTA 1.3.0 success, and
  the analytics page (usage histogram, Pareto, alert feed). The e2e harness
  boots the demo stack (`--no-outage`), the API on 18080, and the Vite
  dashboard on 4173.
- Step 6 negative/regression suite: `101 passed` in 30.13s; `test_negative.py`
  has 7 tests, `test_regression.py` 5 tests, `test_analytics.py` 3 tests.
- Python coverage from `pytest --cov=homelink`: `86%` (2726 statements, 379
  missed), above the CI gate of 80%.

### Benchmarks

`python scripts/bench.py` (latest full run, Python 3.11.14, Node 20.19.5,
API built from `api/dist`):

- Broker throughput: 20,000 QoS1 telemetry messages from 10 concurrent
  clients in 4.911s = `4072.8 msgs/s`.
- End-to-end publish -> shadow-persisted latency, 300 samples:
  p50 `0.569 ms`, p95 `1.289 ms`, p99 `2.744 ms`.
- Shadow update throughput: 500 updates applied in 0.231s =
  `2163.4 updates/s` (applied version 500).
- Reconnect recovery after a 10-second broker outage: `1544.6 ms`.
- API read latency, 200 `GET /devices` requests: p50 `0.857 ms`,
  p95 `1.698 ms`.
- Raw output: `bench/out/bench.json`.

## CI

- Workflow: `.github/workflows/ci.yml` with `python` (3.11 and 3.12 matrix),
  `api`, `dashboard`, `e2e`, and `bench` jobs. The Python job runs ruff and
  pytest with `--cov-fail-under=80`; e2e uploads Playwright reports/traces on
  failure; bench uploads `bench/out/bench.json`.
- Local CI-equivalent sequential wall-clock (the exact commands from every
  job, run by `scripts/ci_local.sh`): `71.80 s` real. GitHub Actions was not
  executed on github.com in this offline build (no remote), so no hosted-CI
  wall-clock figure is claimed.
