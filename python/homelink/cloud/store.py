"""Single-table persistence adapters: SQLite (local default) and DynamoDB.

The key design intentionally mirrors DynamoDB so the same access patterns work
in both backends:

    pk                     sk                              contents
    THING#<id>             SHADOW                          device shadow document
    THING#<id>             TELEMETRY#LATEST                latest telemetry sample
    THING#<id>             RUN#<started_at:013d>#<run_id>   completed run history
    COMMAND#<id>           CMD#<created_at:013d>#<cmd_id>  queued/delivered command

DynamoDB also carries ``thing_id``/``ts`` attributes for the ``thing_id-ts``
GSI.  SQLite stores the same values in indexed columns.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import suppress
from pathlib import Path
from typing import Any

TS_MAX = 9_999_999_999_999


def now_ms() -> int:
    return int(time.time() * 1000)


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class Store(ABC):
    """Storage contract shared by SQLite and DynamoDB backends."""

    _command_ts_lock = threading.Lock()
    _last_command_ts = 0

    @abstractmethod
    def put_shadow(self, thing_id: str, document: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_shadow(self, thing_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def append_run(self, thing_id: str, run: dict[str, Any]) -> None: ...

    @abstractmethod
    def query_runs(
        self, thing_id: str, from_ms: int | None = None, to_ms: int | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def put_command(self, thing_id: str, command: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def pop_commands(self, thing_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def put_telemetry(self, thing_id: str, telemetry: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_telemetry(self, thing_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_things(self) -> list[str]: ...

    @abstractmethod
    def close(self) -> None: ...

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _run_key(run: dict[str, Any], run_id: str) -> str:
        started_at = int(run.get("started_at", now_ms()))
        return f"RUN#{started_at:013d}#{run_id}"

    @classmethod
    def _command_record(cls, thing_id: str, command: dict[str, Any]) -> dict[str, Any]:
        record = dict(command)
        record["thing_id"] = thing_id
        record.setdefault("command_id", uuid.uuid4().hex)
        if "created_at" not in record:
            with cls._command_ts_lock:
                cls._last_command_ts = max(now_ms(), cls._last_command_ts + 1)
                record["created_at"] = cls._last_command_ts
        record.setdefault("status", "queued")
        return record


class SqliteStore(Store):
    """Single-table SQLite implementation used by local runs and the dashboard API."""

    def __init__(self, path: str | Path = "homelink.db") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS homelink (
                    pk TEXT NOT NULL,
                    sk TEXT NOT NULL,
                    attrs TEXT NOT NULL,
                    thing_id TEXT,
                    ts INTEGER,
                    PRIMARY KEY (pk, sk)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS thing_id_ts ON homelink (thing_id, ts)"
            )
            self._conn.commit()

    def _put(
        self,
        pk: str,
        sk: str,
        attrs: dict[str, Any],
        thing_id: str | None = None,
        ts: int | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO homelink (pk, sk, attrs, thing_id, ts) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pk, sk) DO UPDATE SET
                    attrs = excluded.attrs,
                    thing_id = excluded.thing_id,
                    ts = excluded.ts
                """,
                (pk, sk, _dumps(attrs), thing_id, ts),
            )
            self._conn.commit()

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT attrs FROM homelink WHERE pk = ? AND sk = ?", (pk, sk)
            ).fetchone()
        return json.loads(row["attrs"]) if row else None

    def put_shadow(self, thing_id: str, document: dict[str, Any]) -> None:
        self._put(f"THING#{thing_id}", "SHADOW", document, thing_id, int(document.get("timestamp", 0)))

    def get_shadow(self, thing_id: str) -> dict[str, Any] | None:
        return self._get(f"THING#{thing_id}", "SHADOW")

    def append_run(self, thing_id: str, run: dict[str, Any]) -> None:
        run = dict(run)
        run["thing_id"] = thing_id
        started_at = int(run.setdefault("started_at", now_ms()))
        run_id = str(run.setdefault("run_id", uuid.uuid4().hex))
        self._put(f"THING#{thing_id}", self._run_key(run, run_id), run, thing_id, started_at)

    def query_runs(
        self, thing_id: str, from_ms: int | None = None, to_ms: int | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        lower = f"RUN#{int(from_ms or 0):013d}"
        upper = f"RUN#{int(to_ms or TS_MAX):013d}#\uffff"
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT attrs FROM homelink
                WHERE pk = ? AND sk >= ? AND sk <= ?
                ORDER BY sk DESC LIMIT ?
                """,
                (f"THING#{thing_id}", lower, upper, int(limit)),
            ).fetchall()
        return [json.loads(row["attrs"]) for row in rows]

    def put_command(self, thing_id: str, command: dict[str, Any]) -> dict[str, Any]:
        record = self._command_record(thing_id, command)
        key = f"CMD#{int(record['created_at']):013d}#{record['command_id']}"
        self._put(f"COMMAND#{thing_id}", key, record, thing_id, int(record["created_at"]))
        return record

    def pop_commands(self, thing_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sk, attrs FROM homelink WHERE pk = ? AND sk LIKE 'CMD#%' ORDER BY sk",
                (f"COMMAND#{thing_id}",),
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                record = json.loads(row["attrs"])
                if record.get("status") != "queued":
                    continue
                record["status"] = "delivered"
                record["delivered_at"] = now_ms()
                self._conn.execute(
                    "UPDATE homelink SET attrs = ? WHERE pk = ? AND sk = ?",
                    (_dumps(record), f"COMMAND#{thing_id}", row["sk"]),
                )
                records.append(record)
            self._conn.commit()
        return records

    def put_telemetry(self, thing_id: str, telemetry: dict[str, Any]) -> None:
        self._put(
            f"THING#{thing_id}",
            "TELEMETRY#LATEST",
            telemetry,
            thing_id,
            int(telemetry.get("ts_ms", 0)),
        )

    def get_telemetry(self, thing_id: str) -> dict[str, Any] | None:
        return self._get(f"THING#{thing_id}", "TELEMETRY#LATEST")

    def list_things(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT thing_id FROM homelink WHERE sk = 'SHADOW' ORDER BY thing_id"
            ).fetchall()
        return [row["thing_id"] for row in rows if row["thing_id"]]

    def close(self) -> None:
        with self._lock, suppress(Exception):
            self._conn.close()


class DynamoDbStore(Store):
    """DynamoDB implementation using the same single-table key schema."""

    def __init__(
        self,
        table_name: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        ensure_table: bool = True,
    ) -> None:
        import boto3
        from botocore.exceptions import ClientError

        self.table_name = table_name or "homelink"
        self._resource = boto3.resource(
            "dynamodb",
            region_name=region or "us-east-1",
            endpoint_url=endpoint_url,
        )
        self._table = self._resource.Table(self.table_name)
        if ensure_table:
            try:
                self._table.load()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                self._create_table()
                self._table.wait_until_exists()

    def _create_table(self) -> None:
        self._resource.create_table(
            TableName=self.table_name,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "thing_id", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "thing_id-ts-index",
                    "KeySchema": [
                        {"AttributeName": "thing_id", "KeyType": "HASH"},
                        {"AttributeName": "ts", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    def _put(
        self,
        pk: str,
        sk: str,
        attrs: dict[str, Any],
        thing_id: str | None = None,
        ts: int | None = None,
    ) -> None:
        item: dict[str, Any] = {"pk": pk, "sk": sk, "attrs": _dumps(attrs)}
        if thing_id is not None:
            item["thing_id"] = thing_id
        if ts is not None:
            item["ts"] = int(ts)
        self._table.put_item(Item=item)

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True)
        item = response.get("Item")
        return json.loads(item["attrs"]) if item else None

    def put_shadow(self, thing_id: str, document: dict[str, Any]) -> None:
        self._put(f"THING#{thing_id}", "SHADOW", document, thing_id, int(document.get("timestamp", 0)))

    def get_shadow(self, thing_id: str) -> dict[str, Any] | None:
        return self._get(f"THING#{thing_id}", "SHADOW")

    def append_run(self, thing_id: str, run: dict[str, Any]) -> None:
        run = dict(run)
        run["thing_id"] = thing_id
        started_at = int(run.setdefault("started_at", now_ms()))
        run_id = str(run.setdefault("run_id", uuid.uuid4().hex))
        self._put(f"THING#{thing_id}", self._run_key(run, run_id), run, thing_id, started_at)

    def query_runs(
        self, thing_id: str, from_ms: int | None = None, to_ms: int | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        lower = f"RUN#{int(from_ms or 0):013d}"
        upper = f"RUN#{int(to_ms or TS_MAX):013d}#\uffff"
        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(f"THING#{thing_id}")
            & Key("sk").between(lower, upper),
            ScanIndexForward=False,
            Limit=int(limit),
        )
        return [json.loads(item["attrs"]) for item in response.get("Items", [])]

    def put_command(self, thing_id: str, command: dict[str, Any]) -> dict[str, Any]:
        record = self._command_record(thing_id, command)
        key = f"CMD#{int(record['created_at']):013d}#{record['command_id']}"
        self._put(f"COMMAND#{thing_id}", key, record, thing_id, int(record["created_at"]))
        return record

    def pop_commands(self, thing_id: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(f"COMMAND#{thing_id}") & Key("sk").begins_with("CMD#"),
            ConsistentRead=True,
        )
        records: list[dict[str, Any]] = []
        for item in response.get("Items", []):
            record = json.loads(item["attrs"])
            if record.get("status") != "queued":
                continue
            record["status"] = "delivered"
            record["delivered_at"] = now_ms()
            self._table.update_item(
                Key={"pk": f"COMMAND#{thing_id}", "sk": item["sk"]},
                UpdateExpression="SET attrs = :attrs",
                ExpressionAttributeValues={":attrs": _dumps(record)},
            )
            records.append(record)
        return records

    def put_telemetry(self, thing_id: str, telemetry: dict[str, Any]) -> None:
        self._put(
            f"THING#{thing_id}",
            "TELEMETRY#LATEST",
            telemetry,
            thing_id,
            int(telemetry.get("ts_ms", 0)),
        )

    def get_telemetry(self, thing_id: str) -> dict[str, Any] | None:
        return self._get(f"THING#{thing_id}", "TELEMETRY#LATEST")

    def list_things(self) -> list[str]:
        from boto3.dynamodb.conditions import Attr

        things: set[str] = set()
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("sk").eq("SHADOW"),
            "ProjectionExpression": "thing_id",
        }
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                if item.get("thing_id"):
                    things.add(item["thing_id"])
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return sorted(things)

    def close(self) -> None:
        return None


def make_store(url: str = "sqlite:///homelink.db") -> Store:
    """Build a store from a CLI-friendly URL (``sqlite:///path`` or ``dynamodb://table``)."""
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        return SqliteStore(path or "homelink.db")
    if url.startswith("sqlite://"):
        path = url[len("sqlite://") :]
        return SqliteStore(path or "homelink.db")
    if url.startswith("dynamodb://"):
        return DynamoDbStore(table_name=url[len("dynamodb://") :] or "homelink")
    if url in ("dynamodb", "dynamo"):
        return DynamoDbStore()
    raise ValueError(f"unsupported store URL: {url!r}")
