"""MQTT 3.1.1 control-packet codec.

Only the subset needed by HomeLink is implemented, but encoding and decoding
are strict: malformed packets raise :class:`MalformedPacketError`, which the
broker turns into a single-connection drop (never a broker crash).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

MAX_REMAINING_LENGTH = 256 * 1024  # 256 KiB application limit
PROTOCOL_NAME = b"MQTT"
PROTOCOL_LEVEL = 4

CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14

PACKET_NAMES = {
    CONNECT: "CONNECT",
    CONNACK: "CONNACK",
    PUBLISH: "PUBLISH",
    PUBACK: "PUBACK",
    SUBSCRIBE: "SUBSCRIBE",
    SUBACK: "SUBACK",
    UNSUBSCRIBE: "UNSUBSCRIBE",
    UNSUBACK: "UNSUBACK",
    PINGREQ: "PINGREQ",
    PINGRESP: "PINGRESP",
    DISCONNECT: "DISCONNECT",
}


class MalformedPacketError(ValueError):
    """The peer sent bytes that are not a valid MQTT 3.1.1 control packet."""


@dataclass
class Packet:
    packet_type: int = 0


@dataclass
class ConnectPacket(Packet):
    client_id: str = ""
    clean_session: bool = True
    keepalive: int = 0
    will_topic: str | None = None
    will_message: bytes | None = None
    will_qos: int = 0
    will_retain: bool = False
    username: str | None = None
    password: bytes | None = None
    packet_type: int = field(default=CONNECT, init=False)


@dataclass
class ConnackPacket(Packet):
    session_present: bool = False
    return_code: int = 0
    packet_type: int = field(default=CONNACK, init=False)


@dataclass
class PublishPacket(Packet):
    topic: str = ""
    payload: bytes = b""
    qos: int = 0
    retain: bool = False
    dup: bool = False
    packet_id: int | None = None
    packet_type: int = field(default=PUBLISH, init=False)


@dataclass
class PubackPacket(Packet):
    packet_id: int = 0
    packet_type: int = field(default=PUBACK, init=False)


@dataclass
class SubscribePacket(Packet):
    packet_id: int = 0
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    packet_type: int = field(default=SUBSCRIBE, init=False)


@dataclass
class SubackPacket(Packet):
    packet_id: int = 0
    return_codes: list[int] = field(default_factory=list)
    packet_type: int = field(default=SUBACK, init=False)


@dataclass
class UnsubscribePacket(Packet):
    packet_id: int = 0
    topic_filters: list[str] = field(default_factory=list)
    packet_type: int = field(default=UNSUBSCRIBE, init=False)


@dataclass
class UnsubackPacket(Packet):
    packet_id: int = 0
    packet_type: int = field(default=UNSUBACK, init=False)


@dataclass
class PingreqPacket(Packet):
    packet_type: int = field(default=PINGREQ, init=False)


@dataclass
class PingrespPacket(Packet):
    packet_type: int = field(default=PINGRESP, init=False)


@dataclass
class DisconnectPacket(Packet):
    packet_type: int = field(default=DISCONNECT, init=False)


def encode_remaining_length(length: int) -> bytes:
    if length < 0 or length > 268_435_455:
        raise ValueError(f"remaining length out of MQTT range: {length}")
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        out.append(digit)
        if length == 0:
            return bytes(out)


def decode_remaining_length(data: bytes | bytearray, offset: int = 0) -> tuple[int, int]:
    multiplier = 1
    value = 0
    index = offset
    for _ in range(4):
        if index >= len(data):
            raise MalformedPacketError("truncated remaining length")
        digit = data[index]
        index += 1
        value += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            return value, index
        multiplier *= 128
    raise MalformedPacketError("remaining length uses more than four bytes")


async def read_remaining_length(reader: Any) -> tuple[int, int]:
    """Read a varint from an asyncio stream; returns (value, bytes_consumed)."""
    multiplier = 1
    value = 0
    consumed = 0
    for _ in range(4):
        raw = await reader.readexactly(1)
        consumed += 1
        digit = raw[0]
        value += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            return value, consumed
        multiplier *= 128
    raise MalformedPacketError("remaining length uses more than four bytes")


def encode_utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 65535:
        raise ValueError("MQTT UTF-8 field larger than 65535 bytes")
    return struct.pack("!H", len(encoded)) + encoded


def decode_utf8(data: bytes | bytearray, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise MalformedPacketError("truncated UTF-8 length")
    (length,) = struct.unpack_from("!H", data, offset)
    start = offset + 2
    end = start + length
    if end > len(data):
        raise MalformedPacketError("truncated UTF-8 string")
    try:
        value = bytes(data[start:end]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedPacketError(f"invalid UTF-8 string: {exc}") from exc
    if "\x00" in value:
        raise MalformedPacketError("MQTT UTF-8 string contains U+0000")
    return value, end


def _encode_binary(value: bytes) -> bytes:
    if len(value) > 65535:
        raise ValueError("MQTT binary field larger than 65535 bytes")
    return struct.pack("!H", len(value)) + value


def _decode_binary(data: bytes | bytearray, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise MalformedPacketError("truncated binary length")
    (length,) = struct.unpack_from("!H", data, offset)
    start = offset + 2
    end = start + length
    if end > len(data):
        raise MalformedPacketError("truncated binary field")
    return bytes(data[start:end]), end


def validate_topic_filter(topic_filter: str) -> None:
    if not topic_filter:
        raise MalformedPacketError("empty topic filter")
    if "\x00" in topic_filter:
        raise MalformedPacketError("topic filter contains U+0000")
    levels = topic_filter.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise MalformedPacketError(f"invalid '#' wildcard in {topic_filter!r}")
        if "+" in level and level != "+":
            raise MalformedPacketError(f"invalid '+' wildcard in {topic_filter!r}")


def _encode_fixed(packet_type: int, flags: int, body: bytes) -> bytes:
    return bytes([(packet_type << 4) | flags]) + encode_remaining_length(len(body)) + body


def encode_packet(packet: Packet) -> bytes:
    if isinstance(packet, ConnectPacket):
        flags = 0
        if packet.clean_session:
            flags |= 0x02
        if packet.will_topic is not None:
            flags |= 0x04
            flags |= (packet.will_qos & 0x03) << 3
            if packet.will_retain:
                flags |= 0x20
        if packet.username is not None:
            flags |= 0x80
        if packet.password is not None:
            flags |= 0x40
        body = bytearray(encode_utf8("MQTT"))
        body += bytes([PROTOCOL_LEVEL, flags])
        body += struct.pack("!H", packet.keepalive)
        body += encode_utf8(packet.client_id)
        if packet.will_topic is not None:
            body += encode_utf8(packet.will_topic)
            body += _encode_binary(packet.will_message or b"")
        if packet.username is not None:
            body += encode_utf8(packet.username)
        if packet.password is not None:
            body += _encode_binary(packet.password)
        return _encode_fixed(CONNECT, 0, bytes(body))
    if isinstance(packet, ConnackPacket):
        return _encode_fixed(CONNACK, 0, bytes([1 if packet.session_present else 0, packet.return_code]))
    if isinstance(packet, PublishPacket):
        if packet.qos not in (0, 1, 2):
            raise ValueError("QoS must be 0, 1, or 2")
        flags = (0x08 if packet.dup else 0) | ((packet.qos & 0x03) << 1) | (0x01 if packet.retain else 0)
        body = bytearray(encode_utf8(packet.topic))
        if packet.qos > 0:
            if packet.packet_id is None:
                raise ValueError("QoS 1/2 PUBLISH requires a packet id")
            body += struct.pack("!H", packet.packet_id)
        body += packet.payload
        return _encode_fixed(PUBLISH, flags, bytes(body))
    if isinstance(packet, PubackPacket):
        return _encode_fixed(PUBACK, 0, struct.pack("!H", packet.packet_id))
    if isinstance(packet, SubscribePacket):
        body = bytearray(struct.pack("!H", packet.packet_id))
        for topic_filter, qos in packet.subscriptions:
            validate_topic_filter(topic_filter)
            if qos not in (0, 1, 2):
                raise ValueError("subscription QoS must be 0, 1, or 2")
            body += encode_utf8(topic_filter)
            body += bytes([qos])
        return _encode_fixed(SUBSCRIBE, 0x02, bytes(body))
    if isinstance(packet, SubackPacket):
        body = struct.pack("!H", packet.packet_id) + bytes(packet.return_codes)
        return _encode_fixed(SUBACK, 0, body)
    if isinstance(packet, UnsubscribePacket):
        body = bytearray(struct.pack("!H", packet.packet_id))
        for topic_filter in packet.topic_filters:
            validate_topic_filter(topic_filter)
            body += encode_utf8(topic_filter)
        return _encode_fixed(UNSUBSCRIBE, 0x02, bytes(body))
    if isinstance(packet, UnsubackPacket):
        return _encode_fixed(UNSUBACK, 0, struct.pack("!H", packet.packet_id))
    if isinstance(packet, PingreqPacket):
        return _encode_fixed(PINGREQ, 0, b"")
    if isinstance(packet, PingrespPacket):
        return _encode_fixed(PINGRESP, 0, b"")
    if isinstance(packet, DisconnectPacket):
        return _encode_fixed(DISCONNECT, 0, b"")
    raise ValueError(f"cannot encode packet {packet!r}")


def decode_body(packet_type: int, flags: int, body: bytes) -> Packet:
    if packet_type == CONNECT:
        if flags != 0:
            raise MalformedPacketError("CONNECT fixed-header flags must be zero")
        name, index = decode_utf8(body, 0)
        if name != "MQTT":
            raise MalformedPacketError(f"bad protocol name {name!r}")
        if index >= len(body):
            raise MalformedPacketError("truncated protocol level")
        level = body[index]
        index += 1
        if level != PROTOCOL_LEVEL:
            raise MalformedPacketError(f"unsupported protocol level {level}")
        if index + 3 > len(body):
            raise MalformedPacketError("truncated connect flags/keepalive")
        connect_flags = body[index]
        index += 1
        if connect_flags & 0x01:
            raise MalformedPacketError("CONNECT reserved flag must be zero")
        (keepalive,) = struct.unpack_from("!H", body, index)
        index += 2
        clean_session = bool(connect_flags & 0x02)
        will_flag = bool(connect_flags & 0x04)
        will_qos = (connect_flags >> 3) & 0x03
        will_retain = bool(connect_flags & 0x20)
        password_flag = bool(connect_flags & 0x40)
        username_flag = bool(connect_flags & 0x80)
        if will_qos == 3:
            raise MalformedPacketError("CONNECT will QoS 3 is reserved")
        if not will_flag and (will_qos or will_retain):
            raise MalformedPacketError("CONNECT will QoS/retain set without will flag")
        if password_flag and not username_flag:
            raise MalformedPacketError("CONNECT password flag requires username flag")

        client_id, index = decode_utf8(body, index)
        if not client_id and not clean_session:
            raise MalformedPacketError("empty client id requires clean_session=1")
        will_topic = None
        will_message = None
        if will_flag:
            will_topic, index = decode_utf8(body, index)
            validate_topic_filter(will_topic)
            will_message, index = _decode_binary(body, index)
        username = None
        password = None
        if username_flag:
            username, index = decode_utf8(body, index)
        if password_flag:
            password, index = _decode_binary(body, index)
        if index != len(body):
            raise MalformedPacketError("trailing bytes after CONNECT payload")
        return ConnectPacket(
            client_id=client_id,
            clean_session=clean_session,
            keepalive=keepalive,
            will_topic=will_topic,
            will_message=will_message,
            will_qos=will_qos,
            will_retain=will_retain,
            username=username,
            password=password,
        )
    if packet_type == CONNACK:
        if flags != 0 or len(body) != 2:
            raise MalformedPacketError("malformed CONNACK")
        return ConnackPacket(session_present=bool(body[0] & 0x01), return_code=body[1])
    if packet_type == PUBLISH:
        qos = (flags >> 1) & 0x03
        dup = bool(flags & 0x08)
        retain = bool(flags & 0x01)
        if qos == 3:
            raise MalformedPacketError("PUBLISH QoS 3 is reserved")
        if qos == 0 and dup:
            raise MalformedPacketError("PUBLISH DUP must be zero for QoS 0")
        topic, index = decode_utf8(body, 0)
        if not topic:
            raise MalformedPacketError("PUBLISH topic must not be empty")
        if "+" in topic or "#" in topic:
            raise MalformedPacketError("PUBLISH topic must not contain wildcards")
        packet_id = None
        if qos > 0:
            if index + 2 > len(body):
                raise MalformedPacketError("truncated PUBLISH packet id")
            (packet_id,) = struct.unpack_from("!H", body, index)
            index += 2
            if packet_id == 0:
                raise MalformedPacketError("PUBLISH packet id must be nonzero")
        return PublishPacket(
            topic=topic,
            payload=bytes(body[index:]),
            qos=qos,
            retain=retain,
            dup=dup,
            packet_id=packet_id,
        )
    if packet_type == PUBACK:
        if flags != 0 or len(body) != 2:
            raise MalformedPacketError("malformed PUBACK")
        return PubackPacket(packet_id=struct.unpack("!H", body)[0])
    if packet_type == SUBSCRIBE:
        if flags != 0x02:
            raise MalformedPacketError("SUBSCRIBE fixed-header flags must be 0x02")
        if len(body) < 4:
            raise MalformedPacketError("truncated SUBSCRIBE")
        (packet_id,) = struct.unpack_from("!H", body, 0)
        if packet_id == 0:
            raise MalformedPacketError("SUBSCRIBE packet id must be nonzero")
        index = 2
        subscriptions: list[tuple[str, int]] = []
        while index < len(body):
            topic_filter, index = decode_utf8(body, index)
            validate_topic_filter(topic_filter)
            if index >= len(body):
                raise MalformedPacketError("truncated SUBSCRIBE QoS byte")
            qos = body[index]
            index += 1
            if qos not in (0, 1, 2):
                raise MalformedPacketError(f"invalid subscription QoS {qos}")
            subscriptions.append((topic_filter, qos))
        if not subscriptions:
            raise MalformedPacketError("SUBSCRIBE requires at least one filter")
        return SubscribePacket(packet_id=packet_id, subscriptions=subscriptions)
    if packet_type == SUBACK:
        if flags != 0 or len(body) < 2:
            raise MalformedPacketError("malformed SUBACK")
        return SubackPacket(packet_id=struct.unpack_from("!H", body, 0)[0], return_codes=list(body[2:]))
    if packet_type == UNSUBSCRIBE:
        if flags != 0x02:
            raise MalformedPacketError("UNSUBSCRIBE fixed-header flags must be 0x02")
        if len(body) < 4:
            raise MalformedPacketError("truncated UNSUBSCRIBE")
        (packet_id,) = struct.unpack_from("!H", body, 0)
        index = 2
        filters: list[str] = []
        while index < len(body):
            topic_filter, index = decode_utf8(body, index)
            validate_topic_filter(topic_filter)
            filters.append(topic_filter)
        if not filters:
            raise MalformedPacketError("UNSUBSCRIBE requires at least one filter")
        return UnsubscribePacket(packet_id=packet_id, topic_filters=filters)
    if packet_type == UNSUBACK:
        if flags != 0 or len(body) != 2:
            raise MalformedPacketError("malformed UNSUBACK")
        return UnsubackPacket(packet_id=struct.unpack("!H", body)[0])
    if packet_type == PINGREQ:
        if flags != 0 or body:
            raise MalformedPacketError("malformed PINGREQ")
        return PingreqPacket()
    if packet_type == PINGRESP:
        if flags != 0 or body:
            raise MalformedPacketError("malformed PINGRESP")
        return PingrespPacket()
    if packet_type == DISCONNECT:
        if flags != 0 or body:
            raise MalformedPacketError("malformed DISCONNECT")
        return DisconnectPacket()
    raise MalformedPacketError(f"unsupported packet type {packet_type}")


def parse_fixed_header(data: bytes) -> tuple[int, int, int, int]:
    """Return (packet_type, flags, remaining_length, header_length)."""
    if not data:
        raise MalformedPacketError("empty packet")
    packet_type = data[0] >> 4
    flags = data[0] & 0x0F
    if packet_type not in PACKET_NAMES:
        raise MalformedPacketError(f"reserved packet type {packet_type}")
    remaining, index = decode_remaining_length(data, 1)
    if remaining > MAX_REMAINING_LENGTH:
        raise MalformedPacketError(f"remaining length {remaining} exceeds 256 KiB limit")
    return packet_type, flags, remaining, index


def decode_packet(data: bytes) -> Packet:
    packet_type, flags, remaining, header_len = parse_fixed_header(data)
    body = data[header_len:]
    if len(body) < remaining:
        raise MalformedPacketError("truncated packet payload")
    if len(body) > remaining:
        raise MalformedPacketError("trailing bytes after packet")
    return decode_body(packet_type, flags, body)


async def read_packet(reader: Any) -> Packet:
    """Read one complete MQTT packet from an asyncio stream."""
    try:
        first = await reader.readexactly(1)
    except Exception as exc:  # asyncio.IncompleteReadError / ConnectionError
        raise EOFError("connection closed") from exc
    packet_type = first[0] >> 4
    flags = first[0] & 0x0F
    if packet_type not in PACKET_NAMES:
        raise MalformedPacketError(f"reserved packet type {packet_type}")
    remaining, _ = await read_remaining_length(reader)
    if remaining > MAX_REMAINING_LENGTH:
        raise MalformedPacketError(f"remaining length {remaining} exceeds 256 KiB limit")
    try:
        body = await reader.readexactly(remaining)
    except Exception as exc:
        raise MalformedPacketError("truncated packet payload") from exc
    return decode_body(packet_type, flags, body)
