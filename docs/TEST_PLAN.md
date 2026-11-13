# HomeLinkIoT test plan and results matrix

**Build:** 2026-11-13
**Environment:** macOS, Python 3.11.14, Node.js 20.19.5, npm 10.8.2,
better-sqlite3 11.5.0, Playwright 1.48.2.
**Overall:** 148 automated tests: 101 Python, 36 API, 9 dashboard, 2 browser
e2e. Python coverage 86%. All executed suites pass.

Status legend: **PASS** = executed and green in this build; **PASS (local)** =
the hosted CI workflow was not run (offline repo, no remote), but the exact
command was executed locally through `scripts/ci_local.sh`.

## Matrix

| ID | Feature / behavior | Test type | Evidence | Status |
| --- | --- | --- | --- | --- |
| MQTT-01 | Packet codec round-trip (CONNECT/CONNACK/PUBLISH QoS0/1/PUBACK/SUB/UNSUB/PING/DISCONNECT) | Unit + property | `python/tests/test_protocol.py` (29 tests) | PASS |
| MQTT-02 | Varint remaining length boundaries and four-byte limit | Unit | `test_protocol.py` | PASS |
| MQTT-03 | Bad protocol name/level, reserved flags, QoS 3, zero topic, invalid UTF-8, oversized/truncated packets | Negative | `test_protocol.py`, `test_negative.py` | PASS |
| BROKER-01 | `+` / `#` wildcard matching incl. `$` root rules | Unit table | `test_broker.py` (25 tests) | PASS |
| BROKER-02 | Retained publish, retained delivery, empty-payload delete | Integration | `test_broker.py` | PASS |
| BROKER-03 | Last Will & Testament on ungraceful socket close | Integration | `test_broker.py` | PASS |
| BROKER-04 | QoS1 outbound inflight + DUP redelivery after socket drop | Integration | `test_broker.py` | PASS |
| BROKER-05 | Persistent session offline queue for QoS1 commands | Integration | `test_broker.py`, `test_regression.py` | PASS |
| BROKER-06 | Keepalive timeout (1.5×) enforcement | Integration | `test_broker.py` | PASS |
| BROKER-07 | Malformed packet drops only the offender and increments `malformed_dropped` | Negative | `test_negative.py` (7 tests) | PASS |
| STORE-01 | SQLite/DynamoDB parity for shadows, runs, commands, telemetry, list | Integration (moto) | `test_store.py` (9 tests) | PASS |
| SHADOW-01 | Monotonic version, accepted/update topics, metadata timestamps, clientToken | Integration | `test_shadow.py` (8 tests) | PASS |
| SHADOW-02 | Optimistic concurrency 409 on stale version, no state mutation | Integration + regression | `test_shadow.py`, `test_regression.py` | PASS |
| SHADOW-03 | Desired/reported merge, null delete, delta suppression after convergence | Integration | `test_shadow.py` | PASS |
| SHADOW-04 | Get accepted/rejected; 404 for unknown thing | Integration | `test_shadow.py` | PASS |
| TELEM-01 | Schema-valid telemetry persisted; latest sample + completed run recorded | Integration | `test_shadow.py` | PASS |
| TELEM-02 | Malformed JSON / schema violation rejected without corrupting last good sample | Negative | `test_negative.py` | PASS |
| BLE-01 | 6-digit passkey → HKDF → Fernet; encrypted SSID/PSK; 20-byte ATT framing | Integration | `test_ble_provision.py` (5 tests) | PASS |
| BLE-02 | Wrong passkey → `AUTH_FAILED`, no credential storage | Negative | `test_ble_provision.py` | PASS |
| BLE-03 | Multi-chunk reassembly; out-of-order/oversized frames rejected | Integration + negative | `test_ble_provision.py` | PASS |
| BLE-04 | Truncated credential write → `INCOMPLETE` notification | Negative | `test_ble_provision.py` | PASS |
| DEV-01 | State machine `ONLINE → RUNNING → ONLINE → DOCKED` with shadow reported state | Integration | `test_appliance.py` (4 tests) | PASS |
| DEV-02 | Desired power/schedule/command applied via delta and command topic | Integration | `test_appliance.py` | PASS |
| DEV-03 | Out-of-order spool replay in seq order, no loss/duplicates | Integration + regression | `test_appliance.py`, `test_regression.py` | PASS |
| DEV-04 | Forced broker outage → spool → reconnect → replay, zero loss/dupes | Regression, slow | `test_appliance.py`, `test_regression.py` | PASS |
| DEV-05 | Offline command queued by persistent session, applied on reconnect | Regression | `test_regression.py` | PASS |
| OTA-01 | Chunked OTA success, sha256 verified, firmware version updated | Integration | `test_ota.py` (3 tests) | PASS |
| OTA-02 | Corrupted image / dropped chunk → rollback, `OTA_CHECKSUM`, device serviceable | Negative + regression | `test_ota.py`, `test_regression.py` | PASS |
| API-01 | API key required on every route (401 missing/wrong) | Negative | `api/tests/*` (36 tests) | PASS |
| API-02 | zod body/query validation → 400 `{error, details}` | Negative | `api/tests/*` | PASS |
| API-03 | Unknown device → 404; stale shadow version → 409 | Negative | `api/tests/*` | PASS |
| API-04 | Happy paths for 7 routes, command publish + OTA chunk publish captured | Integration (fake broker) | `api/tests/*` | PASS |
| API-05 | API responses validate against `schemas/*.schema.json` (Ajv) | Contract | `api/tests/contract.test.ts` | PASS |
| API-06 | Live demo stack `GET /devices` with key; 401 without | Manual integration | `curl` against demo + API on 18080 | PASS |
| UI-01 | Hand-rolled SVG line chart renders points/paths and empty state | Unit | `dashboard/src/components/LineChart.test.tsx` | PASS |
| UI-02 | Desired-vs-reported diff flattening, statuses, counts | Unit | `dashboard/src/components/ShadowDiff.test.tsx` | PASS |
| UI-03 | Stale threshold at 10 s boundary (fresh/stale/future) | Unit | `dashboard/src/lib/stale.test.ts` | PASS |
| UI-04 | Fleet → device → Dock → Start → OTA success journey | Browser e2e | `dashboard/e2e/dashboard.spec.ts` (2 tests) | PASS |
| UI-05 | Analytics page charts + alert feed render against live API | Browser e2e | `dashboard/e2e/dashboard.spec.ts` | PASS |
| ANALYTICS-01 | Seeded generator determinism, labels, CSV round-trip | Unit | `test_analytics.py` (3 tests) | PASS |
| ANALYTICS-02 | Usage/runtime/Pareto/adoption + PNG/report/fleet.json outputs | Integration | `test_analytics.py` | PASS |
| ANALYTICS-03 | Golden summary within 1e-6 of committed JSON | Regression | `test_regression.py` + `python/tests/golden/analytics_golden.json` | PASS |
| PERF-01 | 20k QoS1 / 10 clients throughput | Performance | `scripts/bench.py` → `bench/out/bench.json` | PASS |
| PERF-02 | Publish → shadow-persisted p50/p95/p99 | Performance | `scripts/bench.py` | PASS |
| PERF-03 | Shadow updates/s, reconnect recovery, API read latency | Performance | `scripts/bench.py` | PASS |
| CI-01 | Python 3.11/3.12 matrix, ruff, coverage gate 80% | CI config | `.github/workflows/ci.yml` | PASS (local) |
| CI-02 | API build + vitest coverage | CI config | `.github/workflows/ci.yml` | PASS (local) |
| CI-03 | Dashboard build + vitest | CI config | `.github/workflows/ci.yml` | PASS (local) |
| CI-04 | Playwright e2e with artifact upload | CI config | `.github/workflows/ci.yml` | PASS (local) |
| CI-05 | Bench artifact upload | CI config | `.github/workflows/ci.yml` | PASS (local) |

## Failure-injection coverage

| Injected failure | Expected behavior | Proof |
| --- | --- | --- |
| Malformed MQTT frame | Offending socket closes, `malformed_dropped++`, other clients keep working | `test_negative.py` (6 parametrized frames + telemetry corruption) |
| Broker killed mid-cleaning | Telemetry appends to `device_spool/{thing}.ndjson`; reconnect replays in seq order; no loss/dupes | `test_regression.py`, demo (66/66, 0/0) |
| Command to offline device | Persistent-session QoS1 queue delivers after reconnect; state flips | `test_regression.py` |
| Stale shadow version | 409 rejection, stored version unchanged | `test_shadow.py`, `test_regression.py` |
| Corrupted OTA chunk | sha256 mismatch → rollback, `OTA_CHECKSUM`, device still runs | `test_ota.py`, `test_regression.py`, demo |
| Dropped OTA chunk | Timeout → rollback, serviceable | `test_ota.py` |
| Wrong BLE passkey | Fernet decryption fails → `AUTH_FAILED`, credentials not stored | `test_ble_provision.py` |
| Truncated BLE write | Reassembly timeout → `INCOMPLETE` | `test_ble_provision.py` |
| API bad input | 401/400/404/409 with `{error, details}` | API vitest suites |
| Analytics drift | Golden metrics fail outside 1e-6 tolerance | `test_regression.py` |

## Execution summary

| Suite | Command | Result |
| --- | --- | --- |
| Python | `pytest python/tests -q --cov=homelink` | 101 passed, 86% coverage |
| API | `cd api && npm test` | 36 passed |
| Dashboard | `cd dashboard && npm test` | 9 passed |
| E2E | `cd dashboard && npx playwright test` | 2 passed |
| Bench | `python scripts/bench.py` | outputs to `bench/out/bench.json` |
| CI-equivalent | `python scripts/bench.py` + all suites via `scripts/ci_local.sh` | 71.80 s wall-clock, pass |

## Notes and residual risks

- GitHub Actions was configured but not executed on github.com (offline build,
  no remote); all job commands were run locally and pass. The 71.80 s figure is
  a sequential local wall-clock, not a hosted-runner measurement.
- The DynamoDB adapter is verified under moto, not against a live AWS table.
- The analytics detector is evaluated on a synthetic seeded dataset; production
  accuracy requires real telemetry and per-model calibration.
- Security, TLS, OTA signing, and cloud deployment gaps are listed in the
  README's limitations section and in `docs/AWS_MAPPING.md`.
