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

## Telemetry & Shadow

## OTA

## Fleet Analytics

## Test Suite

- Step 2 Python suite: 74 passed (0 failed) in 6.21s.

## CI
