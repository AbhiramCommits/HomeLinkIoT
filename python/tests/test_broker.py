from __future__ import annotations

import time

import pytest

from homelink.broker.server import topic_matches
from homelink.protocol.packets import PublishPacket
from tests.helpers import RawClient


@pytest.mark.parametrize(
    ("topic_filter", "topic", "expected"),
    [
        ("sport/tennis/player1", "sport/tennis/player1", True),
        ("sport/tennis/+", "sport/tennis/player1", True),
        ("sport/+", "sport/tennis/player1", False),
        ("sport/+", "sport/", True),
        ("sport/+", "sport", False),
        ("+", "/finance", False),
        ("+/+", "/finance", True),
        ("/+", "/finance", True),
        ("sport/#", "sport", True),
        ("sport/#", "sport/tennis/player1", True),
        ("#", "sport/tennis/player1", True),
        ("a/b", "a/b/c", False),
        ("$SYS/#", "$SYS/broker", True),
        ("#", "$SYS/broker", False),
        ("+/monitor/Clients", "$SYS/monitor/Clients", False),
        ("homelink/+/telemetry", "homelink/vac-001/telemetry", True),
        ("homelink/vac-001/#", "homelink/vac-002/telemetry", False),
    ],
)
def test_wildcard_matching(topic_filter: str, topic: str, expected: bool) -> None:
    assert topic_matches(topic_filter, topic) is expected


def test_connect_subscribe_publish_qos1(broker) -> None:
    _, port = broker
    subscriber = RawClient("127.0.0.1", port, client_id="sub-1")
    connack = subscriber.connect()
    assert connack.return_code == 0
    assert connack.session_present is False
    suback = subscriber.subscribe([("homelink/+/telemetry", 1)])
    assert suback.return_codes == [1]

    publisher = RawClient("127.0.0.1", port, client_id="pub-1")
    publisher.connect()
    publisher.publish("homelink/vac-001/telemetry", b'{"seq":1}', qos=1)

    message = subscriber.recv_topic("homelink/vac-001/telemetry")
    assert isinstance(message, PublishPacket)
    assert message.payload == b'{"seq":1}'
    assert message.qos == 1
    subscriber.ack(message)
    subscriber.disconnect()
    publisher.disconnect()
    assert broker[0].stats()["packets_in"] >= 4


def test_retained_messages_and_delete(broker) -> None:
    _, port = broker
    publisher = RawClient("127.0.0.1", port, client_id="ret-pub")
    publisher.connect()
    publisher.publish("homelink/vac-001/state", b"ONLINE", qos=1, retain=True)
    assert publisher.sock is not None

    subscriber = RawClient("127.0.0.1", port, client_id="ret-sub")
    subscriber.connect()
    subscriber.subscribe([("homelink/+/state", 1)])
    message = subscriber.recv_topic("homelink/vac-001/state")
    assert message.payload == b"ONLINE"
    assert message.retain is True
    subscriber.ack(message)

    publisher.publish("homelink/vac-001/state", b"", qos=1, retain=True)
    assert broker[0].stats()["retained_count"] == 0

    late = RawClient("127.0.0.1", port, client_id="ret-late")
    late.connect()
    late.subscribe([("homelink/+/state", 1)])
    with pytest.raises(TimeoutError):
        late.recv(0.3)
    subscriber.disconnect()
    late.disconnect()
    publisher.disconnect()


def test_last_will_on_ungraceful_disconnect(broker) -> None:
    _, port = broker
    watcher = RawClient("127.0.0.1", port, client_id="lwt-watcher")
    watcher.connect()
    watcher.subscribe([("homelink/+/status", 1)])

    doomed = RawClient(
        "127.0.0.1",
        port,
        client_id="lwt-device",
        will=("homelink/vac-001/status", b"offline", 1, False),
    )
    doomed.connect()
    doomed.close()  # abrupt socket close, not DISCONNECT

    message = watcher.recv_topic("homelink/vac-001/status")
    assert message.payload == b"offline"
    watcher.ack(message)
    watcher.disconnect()


def test_qos1_redelivery_after_socket_drop(broker) -> None:
    broker_instance, port = broker
    subscriber = RawClient("127.0.0.1", port, client_id="persist-1", clean_session=False)
    first_connack = subscriber.connect()
    assert first_connack.session_present is False
    subscriber.subscribe([("redelivery/#", 1)])

    publisher = RawClient("127.0.0.1", port, client_id="redelivery-pub")
    publisher.connect()
    publisher.publish("redelivery/data", b"payload-1", qos=1)

    first = subscriber.recv_topic("redelivery/data")
    assert first.dup is False
    assert first.payload == b"payload-1"
    subscriber.close()  # drop without PUBACK

    resumed = RawClient("127.0.0.1", port, client_id="persist-1", clean_session=False)
    connack = resumed.connect()
    assert connack.session_present is True
    redelivered = resumed.recv_topic("redelivery/data")
    assert redelivered.dup is True
    assert redelivered.payload == b"payload-1"
    resumed.ack(redelivered)
    assert broker_instance.stats()["redelivered"] >= 1
    resumed.disconnect()
    publisher.disconnect()


def test_offline_persistent_session_queues_qos1(broker) -> None:
    _, port = broker
    subscriber = RawClient("127.0.0.1", port, client_id="offline-1", clean_session=False)
    subscriber.connect()
    subscriber.subscribe([("homelink/vac-001/commands", 1)])
    subscriber.disconnect()

    publisher = RawClient("127.0.0.1", port, client_id="offline-pub")
    publisher.connect()
    publisher.publish("homelink/vac-001/commands", b'{"command":"start"}', qos=1)
    publisher.disconnect()

    resumed = RawClient("127.0.0.1", port, client_id="offline-1", clean_session=False)
    connack = resumed.connect()
    assert connack.session_present is True
    message = resumed.recv_topic("homelink/vac-001/commands")
    assert message.payload == b'{"command":"start"}'
    resumed.ack(message)
    resumed.disconnect()


def test_malformed_client_is_isolated(broker) -> None:
    broker_instance, port = broker
    good = RawClient("127.0.0.1", port, client_id="good")
    good.connect()
    good.ping()

    bad = RawClient("127.0.0.1", port, client_id="bad")
    bad.connect()
    bad.send_raw(bytes([0x82, 0x02, 0x00, 0x01]))  # SUBSCRIBE with reserved flags

    deadline = time.monotonic() + 3
    while broker_instance.stats()["malformed_dropped"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert broker_instance.stats()["malformed_dropped"] == 1
    good.ping()  # unaffected
    assert broker_instance.stats()["clients_connected"] >= 1
    good.disconnect()
    bad.close()


def test_keepalive_timeout_disconnects_client(broker) -> None:
    broker_instance, port = broker
    client = RawClient("127.0.0.1", port, client_id="sleepy", keepalive=1)
    client.connect()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if broker_instance.stats()["clients_connected"] == 0:
            break
        time.sleep(0.1)
    assert broker_instance.stats()["clients_connected"] == 0
    client.close()


def test_unsubscribe_stops_delivery(broker) -> None:
    _, port = broker
    subscriber = RawClient("127.0.0.1", port, client_id="unsub")
    subscriber.connect()
    subscriber.subscribe([("unsub/#", 1)])
    subscriber.unsubscribe(["unsub/#"])

    publisher = RawClient("127.0.0.1", port, client_id="unsub-pub")
    publisher.connect()
    publisher.publish("unsub/topic", b"no", qos=1)
    with pytest.raises(TimeoutError):
        subscriber.recv(0.3)
    subscriber.disconnect()
    publisher.disconnect()
