from __future__ import annotations

import os
import random
import struct

import pytest

from homelink.protocol.packets import (
    ConnackPacket,
    ConnectPacket,
    DisconnectPacket,
    MalformedPacketError,
    PingreqPacket,
    PingrespPacket,
    PubackPacket,
    PublishPacket,
    SubackPacket,
    SubscribePacket,
    UnsubackPacket,
    UnsubscribePacket,
    decode_packet,
    decode_remaining_length,
    encode_packet,
    encode_remaining_length,
    parse_fixed_header,
)

PACKETS = [
    ConnectPacket(client_id="vac-001", clean_session=False, keepalive=60),
    ConnectPacket(
        client_id="vac-002",
        clean_session=True,
        keepalive=15,
        will_topic="homelink/vac-002/status",
        will_message=b"offline",
        will_qos=1,
        will_retain=True,
        username="user",
        password=b"secret",
    ),
    ConnackPacket(session_present=True, return_code=0),
    PublishPacket(topic="homelink/vac-001/telemetry", payload=b'{"seq":1}', qos=0),
    PublishPacket(
        topic="homelink/vac-001/telemetry",
        payload=os.urandom(200),
        qos=1,
        retain=True,
        dup=True,
        packet_id=7,
    ),
    PubackPacket(packet_id=7),
    SubscribePacket(packet_id=9, subscriptions=[("#", 0), ("homelink/+/telemetry", 1)]),
    SubackPacket(packet_id=9, return_codes=[0, 1, 0x80]),
    UnsubscribePacket(packet_id=10, topic_filters=["#"]),
    UnsubackPacket(packet_id=10),
    PingreqPacket(),
    PingrespPacket(),
    DisconnectPacket(),
]


@pytest.mark.parametrize("packet", PACKETS, ids=lambda p: type(p).__name__)
def test_packet_round_trip(packet) -> None:
    assert decode_packet(encode_packet(packet)) == packet


def test_remaining_length_round_trip() -> None:
    for value in [0, 1, 127, 128, 16_383, 16_384, 2_097_151, 268_435_455]:
        encoded = encode_remaining_length(value)
        decoded, offset = decode_remaining_length(encoded)
        assert decoded == value
        assert offset == len(encoded)


def test_random_publish_round_trip() -> None:
    rng = random.Random(1234)
    for index in range(100):
        levels = [rng.choice(["a", "b", "thing", "x-y", "0", ""]) for _ in range(rng.randint(1, 5))]
        topic = "/".join(levels) or "root"
        qos = rng.choice([0, 1])
        packet = PublishPacket(
            topic=topic,
            payload=os.urandom(rng.randint(0, 1000)),
            qos=qos,
            retain=rng.choice([False, True]),
            dup=bool(qos) and rng.choice([False, True]),
            packet_id=rng.randint(1, 65535) if qos else None,
        )
        assert decode_packet(encode_packet(packet)) == packet, index


def _connect_bytes(protocol: bytes = b"MQTT", level: int = 4, flags: int = 0x02) -> bytes:
    body = struct.pack("!H", len(protocol)) + protocol + bytes([level, flags])
    body += struct.pack("!H", 30) + b"\x00\x01c"
    return b"\x10" + encode_remaining_length(len(body)) + body


@pytest.mark.parametrize(
    "data",
    [
        _connect_bytes(protocol=b"MQTX"),
        _connect_bytes(level=5),
        _connect_bytes(flags=0x03),  # reserved CONNECT flag
        b"\x30" + encode_remaining_length(300_000),
        b"\x30\x0aabc",
        bytes([0x82, 0x02, 0x00, 0x01]),  # SUBSCRIBE with reserved flags 0
        bytes([0x80, 0x02, 0x00, 0x01]),  # SUBSCRIBE with flags 0
        bytes([0x36, 0x05, 0x00, 0x01, 0x61, 1, 2]),  # PUBLISH QoS 3
        bytes([0x30, 0x04, 0x00, 0x00, 0x7B, 0x7D]),  # zero-length PUBLISH topic
        bytes([0x30, 0x04, 0x00, 0x02, 0xFF, 0xFE]),  # invalid UTF-8 topic
        bytes([0x30, 0x80, 0x80, 0x80, 0x80, 0x00]),  # five-byte remaining length
    ],
    ids=[
        "bad-protocol-name",
        "bad-protocol-level",
        "connect-reserved-flag",
        "oversized-remaining-length",
        "truncated-payload",
        "subscribe-bad-flags-a",
        "subscribe-bad-flags-b",
        "publish-qos-3",
        "empty-topic",
        "invalid-utf8",
        "varint-too-long",
    ],
)
def test_malformed_packets_raise(data: bytes) -> None:
    with pytest.raises(MalformedPacketError):
        decode_packet(data)


def test_oversized_packet_rejected_at_header() -> None:
    header = bytes([0x30]) + encode_remaining_length(300_000)
    with pytest.raises(MalformedPacketError):
        parse_fixed_header(header)


def test_invalid_wildcard_filter_rejected() -> None:
    body = struct.pack("!H", 1) + b"\x00\x05a/#/b" + bytes([0])
    data = bytes([0x82]) + encode_remaining_length(len(body)) + body
    with pytest.raises(MalformedPacketError):
        decode_packet(data)


def test_invalid_utf8_in_subscribe_filter_rejected() -> None:
    body = struct.pack("!H", 1) + b"\x00\x01\xff" + bytes([0])
    data = bytes([0x82]) + encode_remaining_length(len(body)) + body
    with pytest.raises(MalformedPacketError):
        decode_packet(data)
