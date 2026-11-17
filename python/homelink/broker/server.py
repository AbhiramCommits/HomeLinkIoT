"""A small, strict MQTT 3.1.1 broker used by the HomeLink local stack.

Design notes
------------
* One asyncio task per client; sessions live in ``Broker.sessions`` so a
  ``clean_session=False`` client can resume subscriptions, queued QoS1
  messages, and unacknowledged inflight publishes after reconnecting.
* Malformed input is contained: the offending connection is closed and
  ``malformed_dropped`` is incremented.  Other clients are untouched.
* The broker is intentionally local-only (no TLS, no auth), mirroring the
  subset of AWS IoT Core topic semantics the project depends on.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from homelink.protocol.packets import (
    ConnackPacket,
    ConnectPacket,
    DisconnectPacket,
    MalformedPacketError,
    Packet,
    PingreqPacket,
    PingrespPacket,
    PubackPacket,
    PublishPacket,
    SubackPacket,
    SubscribePacket,
    UnsubackPacket,
    UnsubscribePacket,
    encode_packet,
    read_packet,
)


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT 3.1.1 topic matching, including ``+`` and ``#`` wildcards."""
    if not topic_filter or not topic:
        return False
    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")
    # Wildcards at the root never match topics beginning with '$'.
    if topic_levels[0].startswith("$") and filter_levels[0] in ("+", "#"):
        return False
    for index, part in enumerate(filter_levels):
        if part == "#":
            return index == len(filter_levels) - 1
        if index >= len(topic_levels):
            return False
        if part != "+" and part != topic_levels[index]:
            return False
    return len(filter_levels) == len(topic_levels)


def validate_subscription_filter(topic_filter: str) -> bool:
    if not topic_filter:
        return False
    levels = topic_filter.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            return False
        if "+" in level and level != "+":
            return False
    return True


@dataclass
class PendingPublish:
    topic: str
    payload: bytes
    qos: int
    retain: bool = False


@dataclass(eq=False)
class Session:
    client_id: str
    clean_session: bool = True
    subscriptions: dict[str, int] = field(default_factory=dict)
    inflight: dict[int, PublishPacket] = field(default_factory=dict)
    queue: deque[PendingPublish] = field(default_factory=deque)
    next_packet_id: int = 0
    connection: ClientConnection | None = None

    def alloc_packet_id(self) -> int:
        for _ in range(65535):
            self.next_packet_id = (self.next_packet_id % 65535) + 1
            if self.next_packet_id not in self.inflight:
                return self.next_packet_id
        raise RuntimeError("no free MQTT packet identifiers")


class ClientConnection:
    def __init__(self, broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.broker = broker
        self.reader = reader
        self.writer = writer
        self.session: Session | None = None
        self.client_id = ""
        self.keepalive = 0
        self.last_activity = time.monotonic()
        self.connected = False
        self.graceful = False
        self.closed = False
        self.will_topic: str | None = None
        self.will_message: bytes | None = None
        self.will_qos = 0
        self.will_retain = False
        self.peer = writer.get_extra_info("peername")

    def send(self, packet: Packet) -> None:
        if self.closed:
            return
        try:
            self.writer.write(encode_packet(packet))
        except (ConnectionError, RuntimeError):
            self.closed = True
            return
        self.broker.stats_data["packets_out"] += 1

    def abort(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            self.writer.close()

    async def close_gracefully(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


class Broker:
    def __init__(self, host: str = "127.0.0.1", port: int = 18831, log_path: str | Path | None = None):
        self.host = host
        self.port = port
        self.log_path = Path(log_path) if log_path else None
        self.sessions: dict[str, Session] = {}
        self.clients: dict[str, ClientConnection] = {}
        self.retained: dict[str, tuple[bytes, int]] = {}
        self.stats_data: dict[str, int] = {
            "packets_in": 0,
            "packets_out": 0,
            "malformed_dropped": 0,
            "clients_connected": 0,
            "retained_count": 0,
            "redelivered": 0,
            "connections_total": 0,
        }
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[ClientConnection] = set()
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self._log(f"broker listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        self._stopping = True
        server, self._server = self._server, None
        if server is not None:
            server.close()  # stop accepting new connections
        for conn in list(self._connections):
            conn.graceful = True  # deliberate shutdown: no Last Will
            conn.abort()
        if server is not None:
            # Since Python 3.12.1, wait_closed() also waits for every accepted
            # connection to close, so abort the clients first or this never returns.
            await server.wait_closed()
        for session in self.sessions.values():
            session.connection = None
        self._log("broker stopped")
        self._stopping = False

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    def stats(self) -> dict[str, int]:
        connected = sum(
            1
            for session in self.sessions.values()
            if session.connection is not None and session.connection.connected
        )
        return {
            **self.stats_data,
            "clients_connected": connected,
            "retained_count": len(self.retained),
        }

    def _log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}"
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # ------------------------------------------------------------------ connection loop
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = ClientConnection(self, reader, writer)
        self._connections.add(conn)
        self.stats_data["connections_total"] += 1
        try:
            try:
                first = await read_packet(reader)
            except EOFError as exc:
                raise MalformedPacketError("connection closed before CONNECT") from exc
            if not isinstance(first, ConnectPacket):
                raise MalformedPacketError("first packet must be CONNECT")
            self.stats_data["packets_in"] += 1
            await self._handle_connect(conn, first)
            while not conn.closed:
                timeout: float | None = None
                if conn.keepalive:
                    deadline = conn.last_activity + conn.keepalive * 1.5
                    timeout = max(0.05, deadline - time.monotonic())
                packet = await asyncio.wait_for(read_packet(reader), timeout=timeout)
                conn.last_activity = time.monotonic()
                self.stats_data["packets_in"] += 1
                await self._dispatch(conn, packet)
        except (MalformedPacketError, TimeoutError) as exc:
            self.stats_data["malformed_dropped"] += 1
            self._log(f"dropping client {conn.peer}: {type(exc).__name__}: {exc}")
            conn.abort()
        except (EOFError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # pragma: no cover - last-resort containment
            self._log(f"connection error {conn.peer}: {type(exc).__name__}: {exc}")
            conn.abort()
        finally:
            await self._disconnect(conn, graceful=conn.graceful)
            self._connections.discard(conn)
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle_connect(self, conn: ClientConnection, packet: ConnectPacket) -> None:
        client_id = packet.client_id
        conn.client_id = client_id
        conn.keepalive = packet.keepalive
        conn.will_topic = packet.will_topic
        conn.will_message = packet.will_message
        conn.will_qos = packet.will_qos
        conn.will_retain = packet.will_retain

        old_conn = self.clients.get(client_id)
        if old_conn is not None and old_conn is not conn:
            old_conn.abort()

        existing = self.sessions.get(client_id)
        if packet.clean_session:
            if existing is not None and existing.connection is not None:
                existing.connection.abort()
            session = Session(client_id=client_id, clean_session=True)
            self.sessions[client_id] = session
            session_present = False
        elif existing is not None:
            session = existing
            session.clean_session = False
            session_present = True
            if session.connection is not None and session.connection is not conn:
                session.connection.abort()
        else:
            session = Session(client_id=client_id, clean_session=False)
            self.sessions[client_id] = session
            session_present = False

        session.connection = conn
        conn.session = session
        conn.connected = True
        self.clients[client_id] = conn
        conn.send(ConnackPacket(session_present=session_present, return_code=0))
        self._log(f"CONNECT client={client_id!r} clean={packet.clean_session} present={session_present}")

        # Resume: retransmit unacknowledged QoS1 publishes with DUP=1, then drain
        # messages that arrived while the persistent session was offline.
        for publish in list(session.inflight.values()):
            publish.dup = True
            conn.send(publish)
            self.stats_data["redelivered"] += 1
        while session.queue:
            pending = session.queue.popleft()
            self._send_publish(session, pending.topic, pending.payload, pending.qos, pending.retain)

    async def _dispatch(self, conn: ClientConnection, packet: Packet) -> None:
        session = conn.session
        if session is None:
            raise MalformedPacketError("packet received before CONNECT")
        if isinstance(packet, PublishPacket):
            await self._handle_publish(conn, session, packet)
        elif isinstance(packet, PubackPacket):
            session.inflight.pop(packet.packet_id, None)
        elif isinstance(packet, SubscribePacket):
            await self._handle_subscribe(conn, session, packet)
        elif isinstance(packet, UnsubscribePacket):
            for topic_filter in packet.topic_filters:
                session.subscriptions.pop(topic_filter, None)
            conn.send(UnsubackPacket(packet_id=packet.packet_id))
        elif isinstance(packet, PingreqPacket):
            conn.send(PingrespPacket())
        elif isinstance(packet, DisconnectPacket):
            conn.graceful = True
            await conn.close_gracefully()
        elif isinstance(packet, ConnectPacket):
            raise MalformedPacketError("second CONNECT on the same connection")
        else:
            raise MalformedPacketError(f"client sent unexpected {type(packet).__name__}")

    async def _handle_publish(
        self, conn: ClientConnection, session: Session, packet: PublishPacket
    ) -> None:
        if packet.qos == 2:
            raise MalformedPacketError("QoS 2 is not supported by this broker")
        if packet.retain:
            if packet.payload:
                self.retained[packet.topic] = (packet.payload, packet.qos)
            else:
                self.retained.pop(packet.topic, None)
        self._route(packet.topic, packet.payload, packet.qos, retain=packet.retain)
        if packet.qos == 1 and packet.packet_id is not None:
            conn.send(PubackPacket(packet_id=packet.packet_id))
        self.stats_data["retained_count"] = len(self.retained)

    async def _handle_subscribe(
        self, conn: ClientConnection, session: Session, packet: SubscribePacket
    ) -> None:
        return_codes: list[int] = []
        granted_filters: list[tuple[str, int]] = []
        for topic_filter, qos in packet.subscriptions:
            if not validate_subscription_filter(topic_filter):
                return_codes.append(0x80)
                continue
            granted = min(qos, 1)  # broker grants at most QoS 1
            session.subscriptions[topic_filter] = granted
            return_codes.append(granted)
            granted_filters.append((topic_filter, granted))
        conn.send(SubackPacket(packet_id=packet.packet_id, return_codes=return_codes))
        for topic_filter, qos in granted_filters:
            for topic, (payload, retained_qos) in list(self.retained.items()):
                if topic_matches(topic_filter, topic):
                    self._send_publish(session, topic, payload, min(qos, retained_qos), retain=True)

    # ------------------------------------------------------------------ routing
    def _route(self, topic: str, payload: bytes, qos: int, retain: bool = False) -> None:
        matches: dict[Session, int] = {}
        for session in list(self.sessions.values()):
            best: int | None = None
            for topic_filter, sub_qos in session.subscriptions.items():
                if topic_matches(topic_filter, topic):
                    best = sub_qos if best is None else max(best, sub_qos)
            if best is not None:
                matches[session] = min(qos, best)
        for session, effective_qos in matches.items():
            if session.connection is not None and session.connection.connected and not session.connection.closed:
                self._send_publish(session, topic, payload, effective_qos)
            elif effective_qos > 0:
                session.queue.append(PendingPublish(topic=topic, payload=payload, qos=effective_qos))

    def _send_publish(
        self,
        session: Session,
        topic: str,
        payload: bytes,
        qos: int,
        retain: bool = False,
        dup: bool = False,
    ) -> None:
        conn = session.connection
        if conn is None or conn.closed or not conn.connected:
            if qos > 0:
                session.queue.append(PendingPublish(topic=topic, payload=payload, qos=qos, retain=retain))
            return
        if qos == 0:
            conn.send(PublishPacket(topic=topic, payload=payload, qos=0, retain=retain))
            return
        packet_id = session.alloc_packet_id()
        packet = PublishPacket(
            topic=topic, payload=payload, qos=qos, retain=retain, dup=dup, packet_id=packet_id
        )
        session.inflight[packet_id] = packet
        conn.send(packet)

    def _publish_will(self, conn: ClientConnection) -> None:
        if conn.will_topic is None:
            return
        payload = conn.will_message or b""
        if conn.will_retain:
            if payload:
                self.retained[conn.will_topic] = (payload, conn.will_qos)
            else:
                self.retained.pop(conn.will_topic, None)
        self._route(conn.will_topic, payload, conn.will_qos, retain=conn.will_retain)
        self._log(f"published LWT for {conn.client_id!r} on {conn.will_topic!r}")

    async def _disconnect(self, conn: ClientConnection, graceful: bool) -> None:
        conn.connected = False
        conn.closed = True
        session = conn.session
        if self.clients.get(conn.client_id) is conn:
            self.clients.pop(conn.client_id, None)
        if session is not None and session.connection is conn:
            session.connection = None
        if not graceful and not self._stopping:
            self._publish_will(conn)
        if session is not None and session.clean_session:
            if self.sessions.get(session.client_id) is session and session.connection is None:
                self.sessions.pop(session.client_id, None)
        self._log(f"DISCONNECT client={conn.client_id!r} graceful={graceful}")


class BrokerRunner:
    """Runs a Broker on a dedicated asyncio loop so tests/demos can stop and restart it."""

    def __init__(self, broker: Broker):
        self.broker = broker
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run, name="homelink-broker", daemon=True)
            self._thread.start()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self.broker.start(), self._loop)
        future.result(timeout=timeout)

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self.broker.stop(), self._loop)
        future.result(timeout=timeout)

    def close(self) -> None:
        if self._loop is None:
            return
        with contextlib.suppress(Exception):
            self.stop()
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HomeLink MQTT 3.1.1 broker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18831)
    parser.add_argument("--log", default="broker.log")
    return parser


async def _serve(args: argparse.Namespace) -> None:
    broker = Broker(host=args.host, port=args.port, log_path=args.log)
    await broker.start()
    print(f"homelink broker listening on {args.host}:{args.port}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await broker.stop()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
