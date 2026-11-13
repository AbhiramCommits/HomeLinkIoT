from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from homelink.broker.server import Broker, BrokerRunner
from homelink.cloud.shadow import ShadowService
from homelink.cloud.store import SqliteStore
from tests.helpers import free_port


@pytest.fixture
def broker(tmp_path) -> Iterator[tuple[Broker, int]]:
    instance = Broker(port=free_port(), log_path=tmp_path / "broker.log")
    runner = BrokerRunner(instance)
    runner.start()
    instance.runner = runner  # type: ignore[attr-defined]  # test-only restart handle
    try:
        yield instance, instance.port
    finally:
        runner.close()


@pytest.fixture
def shadow(broker, tmp_path) -> Iterator[tuple[ShadowService, SqliteStore, int]]:
    instance, port = broker
    store = SqliteStore(tmp_path / "shadow.db")
    service = ShadowService("127.0.0.1", port, store, client_id=f"shadow-test-{port}")
    service.start()
    time.sleep(0.15)  # allow SUBSCRIBE to be processed before tests publish
    try:
        yield service, store, port
    finally:
        service.stop()
        store.close()
