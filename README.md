# HomeLinkIoT

HomeLinkIoT is an end-to-end connected-appliance stack — BLE pairing → Wi-Fi
provisioning → MQTT telemetry → AWS-IoT-compatible device shadow → TypeScript
API → React control dashboard → fleet analytics — built to model production IoT
consumer hardware (robot vacuum / smart blender class) with zero cloud account
and zero network dependencies at runtime. Every local component is named and
shaped to map 1:1 onto AWS IoT Core, API Gateway, Lambda, DynamoDB, and IoT
Rules; that mapping is documented in [`docs/AWS_MAPPING.md`](docs/AWS_MAPPING.md)
and [`api/infra/template.yaml`](api/infra/template.yaml).

## Architecture

```
                     BLE GATT (simulated TCP, 20-byte ATT MTU)
  ┌──────────────────────┐   passkey -> HKDF -> Fernet(Wi-Fi creds)   ┌─────────────────────┐
  │ Appliance simulator  │◄───────────────────────────────────────────│ React dashboard     │
  │ vac-001 .. vac-003   │                                            │ Fleet / Device /    │
  │ python/homelink/     │── telemetry 2 Hz, shadow desired/delta ──┐ │ Analytics           │
  │ device/              │── OTA chunks -> sha256 verify ───────────┐│ :5173               │
  └──────────┬───────────┘                                          │└─────────┬───────────┘
             │ MQTT 3.1.1 QoS1 · persistent session · NDJSON spool   │          │ HTTP poll 2 s
             ▼                                                      ▼          ▼
  ┌──────────────────────────────┐                        ┌─────────────────────────┐
  │ MQTT broker :18831           │◄───────────────────────│ Lambda-style API :8080  │
  │ retained · LWT · keepalive   │  shadow update/command │ 7 handlers · zod · MQTT │
  └──────────────┬───────────────┘                        └────────────┬────────────┘
                 │ $iot/things/{id}/shadow/#, homelink/+/telemetry    │ same SQLite file
                 ▼                                                    ▼
       ┌──────────────────────────┐                        ┌─────────────────────────┐
       │ Shadow service           │── put_shadow/append_run│ single-table store      │
       │ version/delta/OTA/token  │                        │ SQLite  ↔  DynamoDB     │
       └──────────────────────────┘                        └─────────────────────────┘
                 ▲
                 │ seeded CSV → fleet.json + PNG report
       ┌─────────┴────────────────┐
       │ Fleet analytics batch    │
       └──────────────────────────┘
```

### Components

| Path | Responsibility |
| --- | --- |
| `python/homelink/protocol/packets.py` | Strict MQTT 3.1.1 control-packet codec (varint remaining length, UTF-8 fields, malformed-packet errors). |
| `python/homelink/broker/server.py` | asyncio broker: topic wildcards, retained messages, LWT, QoS1 inbound PUBACK/outbound inflight, persistent sessions, keepalive, stats. |
| `python/homelink/cloud/shadow.py` | AWS-compatible shadow service on `$iot/things/...`: versions, 409 conflicts, desired/reported merge, null delete, delta suppression, metadata timestamps, clientToken, telemetry/run persistence. |
| `python/homelink/cloud/store.py` | Single-table `Store` with identical SQLite and DynamoDB implementations (`pk`/`sk` + `thing_id`/`ts` GSI). |
| `python/homelink/cloud/ota.py` | Deterministic firmware blobs, sha256, chunk publisher, corruption/drop injection for tests. |
| `python/homelink/device/ble.py` | Simulated BLE GATT peripheral: 20-byte MTU, 2-byte chunk header, HKDF + Fernet credential encryption, PROV_STATUS notifications. |
| `python/homelink/device/provision_client.py` | `provision(host, port, passkey, ssid, psk)` client with measured handshake/chunk stats. |
| `python/homelink/device/appliance.py` | State machine, seeded physics, 2 Hz telemetry, shadow desired sync, offline spool, OTA assembly/rollback. |
| `python/homelink/analytics/` | Seeded fleet generator with fault labels + rolling-median/MAD analytics, plots, report, golden metrics. |
| `api/src/handlers/*.ts` | One Lambda-shaped file per route, each exporting `handler(event: APIGatewayProxyEventV2)`. |
| `api/src/lib/mqtt.ts` | Hand-written TypeScript MQTT client (CONNECT, PUBLISH QoS1, SUBSCRIBE) over raw TCP. |
| `api/src/local.ts` | Express adapter that mounts the same handlers on `:8080`. |
| `api/infra/template.yaml` | AWS SAM HTTP API, Lambda per route, DynamoDB table, IoT Core telemetry rule. |
| `dashboard/` | Vite + React 18 control plane; hand-rolled SVG charts; 2 s polling; stale banner. |
| `scripts/run_demo.py` | Boots broker + shadow + 3 appliances + BLE provisioning + forced outage + OTA. |
| `scripts/bench.py` | Throughput, shadow latency, reconnect, and API read benchmarks. |
| `scripts/ci_local.sh` | Runs every CI job command sequentially and reports local wall-clock. |

### Shadow topics and state machine

```
$iot/things/{thing}/shadow/update           publisher -> shadow
$iot/things/{thing}/shadow/update/accepted  shadow -> publisher
$iot/things/{thing}/shadow/update/rejected  shadow -> publisher (400 / 409)
$iot/things/{thing}/shadow/update/delta     shadow -> device
$iot/things/{thing}/shadow/get              device -> shadow
$iot/things/{thing}/shadow/get/accepted     shadow -> device
$iot/things/{thing}/shadow/get/rejected     shadow -> device (404)
```

On every accepted update the version increments, the timestamp is set, desired
and reported merge recursively (JSON `null` deletes a leaf), and
`delta = desired - reported` is published only when non-empty. A stale `version`
is rejected with `409 "Version conflict"`. Each leaf gets a
`metadata.<section>.<key>.timestamp`. `clientToken` is echoed. A device without
a shadow gets a 404 on `/shadow/get/rejected`.

Device lifecycle:
`UNPROVISIONED → PAIRING → PROVISIONED → CONNECTING → ONLINE → RUNNING → DOCKED | ERROR`,
plus `OTA`. Telemetry uses the contract states `IDLE`, `RUNNING`, `DOCKED`,
`ERROR`, `OTA`; transport states such as `ONLINE` map to `IDLE`.

### How this maps to AWS

| Local | AWS | Change |
| --- | --- | --- |
| MQTT broker `:18831` | AWS IoT Core (TLS 8883) | X.509 mutual TLS, IoT policies, AWS endpoint. |
| `$iot/...` shadow topics | AWS IoT Device Shadow (`$aws/...`) | Topic prefix only; semantics already match. |
| SQLite single table | DynamoDB table `homelink` + `thing_id-ts` GSI | Swap in `DynamoDbStore`; adapter tested under moto. |
| Express + handlers | API Gateway HTTP API + Lambda per route | Deploy `api/infra/template.yaml` with SAM. |
| Telemetry ingest loop | IoT Rules → Lambda → DynamoDB | Rule declared in the SAM template. |
| `x-api-key` check | Cognito/IAM authorizer | Replace header check with JWT/SigV4. |
| BLE passkey over TCP | Real BLE GATT + AWS Fleet Provisioning | Real radio, certificates, claim workflow. |
| OTA chunks | S3 + AWS IoT Jobs + code signing | Signed artifacts, job documents; checksum stays on device. |
| Vite dev server | S3 + CloudFront | Build once, upload `dist/`. |

Full detail: [`docs/AWS_MAPPING.md`](docs/AWS_MAPPING.md).

## How to run

Prerequisites: Python 3.11+ (3.11 and 3.12 are CI-tested), Node.js 20 (`.nvmrc`
pins 20), and a local C toolchain only if `better-sqlite3` has no prebuilt
binary for your platform.

```bash
# 1. install Python package + API/dashboard npm deps
make install

# 2. one-time browser install for Playwright e2e
cd dashboard && npx playwright install chromium && cd ..

# 3. end-to-end 60-second demo (BLE, telemetry, 10 s outage, OTA success + rollback)
make demo

# 4. control plane (separate terminals)
make api          # Express adapter on :8080
make dashboard    # Vite dashboard on :5173

# 5. fleet analytics (seeded 40 devices × 30 days)
make analytics    # writes analytics/out/fleet.json, *.png, report.md

# 6. benchmarks
make bench        # writes bench/out/bench.json

# 7. tests
make test-py test-ts
make test-e2e     # boots demo + API + dashboard automatically
```

Environment knobs: `HOMELINK_API_KEY` (default `dev-key`), `HOMELINK_DB`
(default `../homelink.db` from `api/`), `HOMELINK_MQTT_HOST` /
`HOMELINK_MQTT_PORT`, `PORT` for the API, `VITE_API_URL` for the dashboard,
`PYTHON_BIN` for the Playwright harness.

### Curl examples

```bash
curl -H "x-api-key: dev-key" localhost:8080/devices
curl -H "x-api-key: dev-key" localhost:8080/devices/vac-001
curl -H "x-api-key: dev-key" "localhost:8080/devices/vac-001/runs?limit=10"

curl -X POST -H "x-api-key: dev-key" -H "content-type: application/json" \
  -d '{"action":"start"}' localhost:8080/devices/vac-001/commands

curl -X PATCH -H "x-api-key: dev-key" -H "content-type: application/json" \
  -d '{"state":{"desired":{"power_level":5}},"version":7}' \
  localhost:8080/devices/vac-001/shadow

curl -X POST -H "x-api-key: dev-key" -H "content-type: application/json" \
  -d '{"version":"1.3.0"}' localhost:8080/devices/vac-001/ota

curl -H "x-api-key: dev-key" localhost:8080/fleet/analytics
```

## Results

All figures below were printed by commands run in this build; the full log is
[`docs/RESULTS.md`](docs/RESULTS.md).

### Broker and latency

| Measurement | Result |
| --- | --- |
| Broker throughput (20,000 QoS1 telemetry messages, 10 concurrent clients) | **4,072.8 msgs/s** (4.911 s) |
| Publish → shadow-persisted, 300 samples | **p50 0.569 ms / p95 1.289 ms / p99 2.744 ms** |
| Shadow update throughput | **2,163.4 updates/s** (500 applied) |
| Reconnect recovery after a 10 s broker outage | **1,544.6 ms** (bench); **1,608.2 ms** (demo) |
| API `GET /devices`, 200-request loop | **p50 2.10 ms / p95 4.44 ms** in-process; live HTTP bench **p50 0.857 ms / p95 1.698 ms** |

### BLE provisioning and telemetry

| Measurement | Result |
| --- | --- |
| BLE provisioning handshake (3 appliances) | **16.99 ms / 58.75 ms / 72.31 ms** |
| Encrypted Wi-Fi credential write | **10 ATT chunks** (20-byte MTU, 18-byte payload, 2-byte header) |
| Telemetry published in the 60 s demo | **123 per device / 369 total**, 369 persisted |
| Messages spooled during the 10 s outage | **66** |
| Messages replayed on reconnect | **66** |
| Message loss / duplicate persisted seqs | **0 / 0** |
| Runs recorded by the shadow service | **3** |

### OTA

| Device | Target | Outcome |
| --- | --- | --- |
| vac-001 | 1.3.0 (8 chunks) | **success**, firmware reported 1.3.0 |
| vac-002 | 1.4.0 corrupted (8 chunks) | **rolled_back**, firmware stayed 1.2.0, error `OTA_CHECKSUM` |

### Fleet analytics

Dataset: **40 devices, 1,191 runs, 29.98 days**, with **45 injected faults**
(and 15 injected premature stops) for honest scoring.

- Anomaly detector (rolling median + MAD, threshold 3.5): **precision 0.8033**,
  **recall 0.9608**, **F1 0.875**, **10.076 false alerts per 1,000 runs**
  (49 TP, 12 FP, 2 FN).
- Top-3 error codes: **BRUSH_JAM 40.0%**, **FILTER_CLOG 40.0%**,
  **WHEEL_STALL 20.0%**.
- Runtime mean / median / p95: **904.32 s / 896.18 s / 1,373.58 s**.
- Runs per device per week: mean **6.952**.
- Firmware adoption: 1.0.0 **18.47%**, 1.1.0 **24.43%**, 1.2.0 **43.16%**,
  1.3.0 **13.94%**.

### Test suite and CI

| Suite | Count | Status |
| --- | --- | --- |
| Python (`pytest`) | **101 tests** | pass; **86% coverage** (2,726 statements, 379 missed) |
| API (`vitest` + `supertest`) | **36 tests** | pass |
| Dashboard (`vitest` + Testing Library) | **9 tests** | pass |
| Browser e2e (`Playwright`) | **2 tests** | pass |
| CI-equivalent local run | **71.80 s** wall-clock | pass |

The GitHub Actions workflow defines python (3.11/3.12 matrix), api, dashboard,
e2e, and bench jobs. It was not executed on github.com in this offline build, so
the 71.80 s figure is the local sequential run of the same commands
(`scripts/ci_local.sh`), not a hosted-runner measurement.

## Testing strategy

**Sanity** tests pin the contracts and the happy path: packet round-trips, schema
validation, store parity, BLE provisioning, shadow merges/deltas, appliance
state transitions, and the API/JSON-Schema contract.

**Negative** tests attack boundaries: each malformed MQTT case (bogus protocol
name, truncated varint, oversized packet, invalid UTF-8 topic, QoS 3, truncated
payload) must drop only the offending socket and bump `malformed_dropped`;
malformed telemetry must not overwrite the last good sample; API requests with
no/incorrect `x-api-key` must be 401, schema-invalid bodies 400, unknown devices
404, and stale shadow versions 409.

**Regression** tests prove the failure-injection claims: killing the broker
mid-run forces NDJSON spooling, reconnects, replays in seq order, and yields zero
loss/duplicates; a command published while a device is offline is queued by the
persistent session and applied after reconnect; delta is not re-emitted after
convergence and stale writes stay rejected; corrupted or dropped OTA chunks roll
back to the previous firmware and the device remains serviceable; the seeded
analytics summary matches the committed golden JSON within tolerance.

**Exploratory/performance** work is the Playwright journey (fleet → device →
Dock/Start → OTA → analytics) and `scripts/bench.py`, which measures broker
throughput, end-to-end shadow persistence, shadow update rate, reconnect
recovery, and API read latency alongside the failure behavior above.

See [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) for the full feature × test-type
matrix.

## Limitations and what production would change

- **Broker**: local-only, plaintext, in-memory sessions. Production uses AWS IoT
  Core with TLS 1.2+, X.509 device certificates, IoT policies, and managed
  persistence/scaling. The broker is a test vehicle, not a hardened server.
- **BLE**: TCP simulates GATT; there is no radio, pairing UI, or real ATT
  stack. Production uses a real BlueZ/CoreBluetooth peripheral plus AWS Fleet
  Provisioning (claim certificates) rather than a printed six-digit passkey.
- **Storage**: SQLite is the default; the DynamoDB adapter is tested under moto
  but production would deploy the table in `api/infra/template.yaml`, use
  on-demand capacity, encryption, PITR, and IAM-scoped access.
- **API**: `x-api-key` is a local stand-in. Production uses API Gateway +
  Cognito/IAM authorizers, usage plans, WAF/rate limits, request validation, and
  structured observability. The handlers are Lambda-shaped but the local Express
  adapter is not deployed.
- **Telemetry ingest**: the Python shadow service handles both shadow and
  telemetry. Production separates the shadow service (managed AWS IoT) from an
  IoT Rule → Lambda → DynamoDB ingest path.
- **OTA**: sha256 detects corruption but does not authenticate firmware.
  Production uses signed artifacts in S3, AWS IoT Jobs, staged rollout, and
  device-side public-key verification with A/B partitions.
- **Analytics**: the dataset is synthetic and the detector is tuned to it. A real
  fleet needs seasonality, per-model baselines, drift monitoring, and alert
  routing; thresholds should be learned per hardware revision.
- **Security/ops**: no secrets manager, no TLS between local services, no
  multi-tenancy, and no cloud deployment in this build. CI lives in the repo but
  was not executed on a hosted runner here.

## Repository layout

```
python/homelink/    protocol · broker · cloud (shadow/store/OTA) · device · analytics
python/tests/       protocol, broker, shadow, store, BLE, appliance, OTA, analytics,
                    negative, regression, golden
api/                Lambda handlers · TS MQTT client · Express adapter · SAM template
dashboard/          React pages · hand-rolled SVG charts · unit tests · Playwright e2e
schemas/            telemetry + shadow JSON Schemas (shared source of truth)
scripts/            run_demo.py · bench.py · ci_local.sh
docs/               RESULTS.md · TEST_PLAN.md · AWS_MAPPING.md
.github/workflows/  ci.yml
```
