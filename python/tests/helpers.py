"""Synchronous raw MQTT client used by broker/shadow tests."""

from __future__ import annotations

import socket
import struct
import time
from typing import Any

from homelink.protocol.packets import (
    ConnackPacket,
    ConnectPacket,
    DisconnectPacket,
    Packet,
    PingreqPacket,
    PubackPacket,
    PublishPacket,
    SubackPacket,
    SubscribePacket,
    UnsubscribePacket,
    decode_body,
    encode_packet,
    encode_remaining_length,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RawClient:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: str = "raw-client",
        clean_session: bool = True,
        keepalive: int = 30,
        will: tuple[str, bytes, int, bool] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.clean_session = clean_session
        self.keepalive = keepalive
        self.will = will
        self.sock: socket.socket | None = None
        self._next_pid = 0
        self.buffer = b""

    # ---------------------------------------------------------------- transport
    def connect(self, timeout: float = 3.0) -> ConnackPacket:
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        will_topic = will_message = None
        will_qos = 0
        will_retain = False
        if self.will is not None:
            will_topic, will_message, will_qos, will_retain = self.will
        self.send(
            ConnectPacket(
                client_id=self.client_id,
                clean_session=self.clean_session,
                keepalive=self.keepalive,
                will_topic=will_topic,
                will_message=will_message,
                will_qos=will_qos,
                will_retain=will_retain,
            )
        )
        packet = self.recv(timeout)
        assert isinstance(packet, ConnackPacket), f"expected CONNACK, got {packet!r}"
        return packet

    def send(self, packet: Packet) -> None:
        assert self.sock is not None
        self.sock.sendall(encode_packet(packet))

    def send_raw(self, data: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(data)

    def _recv_exact(self, count: int) -> bytes:
        assert self.sock is not None
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    def recv(self, timeout: float = 2.0) -> Packet:
        assert self.sock is not None
        self.sock.settimeout(timeout)
        first = self._recv_exact(1)
        packet_type = first[0] >> 4
        flags = first[0] & 0x0F
        remaining = 0
        multiplier = 1
        while True:
            digit = self._recv_exact(1)[0]
            remaining += (digit & 0x7F) * multiplier
            if not digit & 0x80:
                break
            multiplier *= 128
        body = self._recv_exact(remaining)
        return decode_body(packet_type, flags, body)

    def recv_until(self, predicate: Any, timeout: float = 3.0) -> Packet:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            packet = self.recv(max(0.05, deadline - time.monotonic()))
            if predicate(packet):
                return packet
        raise TimeoutError("expected packet did not arrive")

    def recv_topic(self, topic: str, timeout: float = 3.0) -> PublishPacket:
        return self.recv_until(  # type: ignore[return-value]
            lambda p: isinstance(p, PublishPacket) and p.topic == topic, timeout
        )

    # ---------------------------------------------------------------- MQTT operations
    def next_pid(self) -> int:
        self._next_pid = (self._next_pid % 65535) + 1
        return self._next_pid

    def subscribe(self, filters: list[tuple[str, int]]) -> SubackPacket:
        packet_id = self.next_pid()
        self.send(SubscribePacket(packet_id=packet_id, subscriptions=filters))
        packet = self.recv_until(lambda p: isinstance(p, SubackPacket))
        return packet  # type: ignore[return-value]

    def unsubscribe(self, filters: list[str]) -> None:
        self.send(UnsubscribePacket(packet_id=self.next_pid(), topic_filters=filters))
        self.recv_until(lambda p: p.__class__.__name__ == "UnsubackPacket")

    def publish(
        self,
        topic: str,
        payload: bytes | str,
        qos: int = 0,
        retain: bool = False,
        wait_ack: bool = True,
    ) -> int | None:
        if isinstance(payload, str):
            payload = payload.encode()
        packet_id = self.next_pid() if qos > 0 else None
        self.send(PublishPacket(topic=topic, payload=payload, qos=qos, retain=retain, packet_id=packet_id))
        if qos > 0 and wait_ack:
            ack = self.recv_until(lambda p: isinstance(p, PubackPacket))
            assert isinstance(ack, PubackPacket)
            assert ack.packet_id == packet_id
        return packet_id

    def ack(self, packet: PublishPacket) -> None:
        assert packet.packet_id is not None
        self.send(PubackPacket(packet_id=packet.packet_id))

    def ping(self, timeout: float = 2.0) -> None:
        self.send(PingreqPacket())
        self.recv_until(lambda p: p.__class__.__name__ == "PingrespPacket", timeout)

    def disconnect(self) -> None:
        if self.sock is None:
            return
        try:
            self.send(DisconnectPacket())
        finally:
            self.close()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


def connect_packet_bytes(client_id: str = "x", clean_session: bool = True, keepalive: int = 30) -> bytes:
    return encode_packet(
        ConnectPacket(client_id=client_id, clean_session=clean_session, keepalive=keepalive)
    )


def raw_publish_bytes(
    topic_bytes: bytes, payload: bytes = b"{}", qos: int = 0, packet_id: int = 1
) -> bytes:
    body = struct.pack("!H", len(topic_bytes)) + topic_bytes
    if qos:
        body += struct.pack("!H", packet_id)
    body += payload
    flags = (qos << 1) & 0x06
    return bytes([0x30 | flags]) + encode_remaining_length(len(body)) + body
