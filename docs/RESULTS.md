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

Measured by `python scripts/run_demo.py --seconds 60` (3 appliances):

- BLE provisioning handshake: vac-001 `5.16 ms`, vac-002 `2.02 ms`, vac-003 `1.99 ms`.
- Passkey write: `1` ATT chunk per device (6 ASCII digits).
- Encrypted Wi-Fi credential write: `10` ATT chunks per device (20-byte ATT MTU,
  18-byte payload, 2-byte chunk header).
- Provisioning results: `PROVISIONED` for 3/3 appliances.

BLE tests: 5 tests in `python/tests/test_ble_provision.py` (happy path, wrong
passkey, chunk reassembly, truncated write, out-of-order/oversized frames).

## Telemetry & Shadow

60-second demo, 3 appliances at 2 Hz:

- Telemetry messages published per device: `123` (369 total, 369 persisted).
- Messages spooled during the forced 10-second broker outage: `68`.
- Messages replayed on reconnect: `68`; spooled loss: `0`; duplicate seqs
  persisted: `0`.
- Reconnect recovery (broker restart to all devices connected and spool
  drained): `2089.0 ms`.
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

## Test Suite

- Step 2 Python suite: 74 passed (0 failed) in 6.21s.
- Step 3 Python suite: 86 passed (0 failed) in 19.43s (adds 5 BLE, 4 appliance,
  3 OTA tests).
- Step 4 API: 7 routes (`GET /devices`, `GET /devices/{id}`,
  `GET /devices/{id}/runs`, `POST /devices/{id}/commands`,
  `PATCH /devices/{id}/shadow`, `POST /devices/{id}/ota`,
  `GET /fleet/analytics`); 36 vitest/supertest tests passed in 5.92s across
  8 files.
- API latency over a 200-request `GET /devices` loop: p50 `2.10 ms`,
  p95 `4.44 ms`, p99 `6.01 ms` (in-process supertest, Node 20.19.5).
- Live check with the demo stack running: `GET /devices` with
  `x-api-key: dev-key` returned `3` devices, all `online: true`; the same
  request without a key returned `401`.

## CI
