# AWS mapping

HomeLinkIoT runs entirely on localhost. Every local component is named and
shaped to map one-to-one onto a production AWS IoT consumer stack. This
document is the contract for that mapping.

## Component map

| Local component | File(s) | AWS production equivalent | What changes |
| --- | --- | --- | --- |
| MQTT 3.1.1 broker | `python/homelink/broker/server.py` | **AWS IoT Core** MQTT broker (TLS 8883) | X.509 mutual TLS, IoT policies, per-thing client IDs, 128 KB payload limit. Broker internals disappear. |
| Device shadow service | `python/homelink/cloud/shadow.py` | **AWS IoT Device Shadow** | No code; topics change from `$iot/...` to `$aws/things/{thing}/shadow/...`. Versions, optimistic concurrency, deltas, null deletion, metadata timestamps, clientToken are already AWS-compatible. |
| Telemetry topic | `homelink/{thing}/telemetry` | Custom IoT topic + **IoT Rules** | Add an IoT topic rule (`SELECT * FROM 'homelink/+/telemetry'`) with a Lambda action writing to DynamoDB. `infra/template.yaml` declares the rule. |
| Single-table store (SQLite) | `python/homelink/cloud/store.py` | **DynamoDB** table `homelink` | Same `pk`/`sk` item model, same GSI (`thing_id` + `ts`). Swap `SqliteStore` for `DynamoDbStore`; the DynamoDB adapter is already implemented and tested under moto. |
| Lambda handlers | `api/src/handlers/*.ts` | **Lambda** (`nodejs20.x`) behind **API Gateway HTTP API** | Same `APIGatewayProxyEventV2 -> APIGatewayProxyResultV2` signature. Deploy `api/infra/template.yaml` with SAM. One Lambda per route, exactly as mounted locally. |
| Express adapter | `api/src/local.ts` | API Gateway routing | Delete for deployment; `infra/template.yaml` declares each route (`GET /devices`, `GET /devices/{id}`, ...). |
| API key check | `api/src/lib/http.ts` | API Gateway authorizer + **Cognito** or IAM SigV4 | Replace `x-api-key` comparison with a JWT authorizer or IAM auth; the regex-level behaviour (401 before routing) is the same. |
| Command path | `api/src/handlers/postCommand.ts` | Shadow update + IoT publish (or **AWS IoT Jobs**) | Same two messages: desired command in the shadow, and a QoS1 command publish to `homelink/{thing}/commands`. |
| OTA publisher | `python/homelink/cloud/ota.py`, `api/src/lib/ota.ts` | **S3** firmware object + **AWS IoT Jobs** + code signing | Firmware moves to S3 versioned objects; OTA becomes an IoT Job with signed artifacts. Chunk/sha256 verification logic stays on the device. |
| TypeScript MQTT client | `api/src/lib/mqtt.ts` | AWS IoT Core data plane | Add TLS + client certificate; same CONNECT/PUBLISH QoS1/SUBSCRIBE packet rules. |
| BLE GATT provisioning | `python/homelink/device/ble.py` | Real BLE peripheral (BlueZ / CoreBluetooth) + **AWS Fleet Provisioning** | TCP transport becomes a real GATT server; the HKDF/Fernet passkey flow becomes BLE pairing plus claim certificates issued by Fleet Provisioning. |
| Appliance simulator | `python/homelink/device/appliance.py` | Physical appliance firmware | The state machine, spool, shadow and OTA logic become embedded firmware; paho is replaced by the vendor MQTT stack. |
| Fleet analytics batch | `python/homelink/analytics/` | **Glue / EMR / Athena** or a scheduled Lambda | The seeded generator and detector move to a scheduled job; outputs land in S3 and are served by the analytics Lambda. |
| Dashboard | `dashboard/` | **S3 + CloudFront** (or Amplify) | Build once, upload `dist/`; point `VITE_API_URL` at the HTTP API. |

## Shadow topic/state machine

Local topics (AWS equivalent replaces `$iot` with `$aws`):

```
$iot/things/{thing}/shadow/update           device/app -> shadow
$iot/things/{thing}/shadow/update/accepted  shadow -> publisher
$iot/things/{thing}/shadow/update/rejected  shadow -> publisher (400/409)
$iot/things/{thing}/shadow/update/delta     shadow -> device
$iot/things/{thing}/shadow/get              device/app -> shadow
$iot/things/{thing}/shadow/get/accepted     shadow -> requester
$iot/things/{thing}/shadow/get/rejected     shadow -> requester
```

State rules implemented in `cloud/shadow.py`:

1. Every accepted update increments `version` by one and stamps `timestamp`.
2. An update carrying a `version` different from the current version is
   rejected with `{code: 409, message: "Version conflict"}`.
3. `state.desired` and `state.reported` merge recursively; a JSON `null` leaf
   deletes that key.
4. `delta = desired - reported` (recursive, values compared) and is published
   only when non-empty.
5. `clientToken` is echoed on accepted/rejected/get responses.
6. Each leaf in `metadata.desired`/`metadata.reported` carries a `timestamp`.
7. A `get` for an unknown thing returns a 404 on `/shadow/get/rejected`.

Device lifecycle: `UNPROVISIONED -> PAIRING -> PROVISIONED -> CONNECTING ->
ONLINE -> RUNNING -> DOCKED|ERROR` plus `OTA`. The appliance reports these in
`state.reported`, subscribes to `/update/delta`, and applies `power_level`,
`schedule`, `command`, and `ota`.

## DynamoDB single-table layout

```
pk                     sk                              item
THING#<id>             SHADOW                          shadow document (JSON attr)
THING#<id>             TELEMETRY#LATEST                latest telemetry (JSON attr)
THING#<id>             RUN#<started_at:013d>#<run_id>   run history
COMMAND#<id>           CMD#<created_at:013d>#<cmd_id>  command queue/audit
```

GSI: `thing_id` (HASH) + `ts` (RANGE), projection ALL, matching
`api/infra/template.yaml` and `python/homelink/cloud/store.py`.

## Deployment steps

```bash
cd api
npm ci && npm run build
sam build --template infra/template.yaml
sam deploy --guided
```

Production substitutions (all deliberate, all documented in the README's
limitations section): DynamoDB adapter instead of SQLite, TLS/X.509 for MQTT,
Cognito/IAM instead of `x-api-key`, S3 + IoT Jobs for OTA, and Fleet
Provisioning instead of the simulated BLE passkey exchange.
