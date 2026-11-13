"""Simulated BLE GATT provisioning transport over TCP.

The transport models the two constraints that make BLE provisioning awkward:

* ATT MTU is 20 bytes, so every characteristic write carries at most 18 bytes
  of application data plus the 2-byte ``(chunk_index, total_chunks)`` header.
* Credentials are encrypted with a key derived from the printed 6-digit
  passkey using HKDF-SHA256 and Fernet (AES-128-CBC + HMAC).

Services/characteristics:
    DEVICE_INFO   read            device id, model, firmware, MTU
    PROV_PASSKEY  write           six ASCII digits, chunked
    WIFI_CREDS    write, encrypted Fernet token, chunked
    PROV_STATUS   notify          PROVISIONED / AUTH_FAILED / INCOMPLETE

The GATT transport is JSON-lines over TCP; one request carries exactly one
20-byte ATT frame (base64-encoded), preserving the chunk-header semantics.
"""

from __future__ import annotations

import base64
import json
import math
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ATT_MTU = 20
CHUNK_HEADER = 2
MAX_CHUNK = ATT_MTU - CHUNK_HEADER
SALT = b"homelink-ble-v1"
INFO = b"wifi-creds"

DEVICE_INFO = "DEVICE_INFO"
PROV_PASSKEY = "PROV_PASSKEY"
WIFI_CREDS = "WIFI_CREDS"
PROV_STATUS = "PROV_STATUS"


class BleError(RuntimeError):
    pass


def derive_session_key(passkey: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT, info=INFO).derive(
        passkey.encode("utf-8")
    )


def fernet_for(passkey: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_session_key(passkey)))


def encode_frame(index: int, total: int, payload: bytes) -> bytes:
    if not 0 <= index < total <= 255:
        raise BleError(f"invalid chunk header ({index}/{total})")
    if len(payload) > MAX_CHUNK:
        raise BleError(f"chunk payload {len(payload)} exceeds ATT MTU payload {MAX_CHUNK}")
    return bytes([index, total]) + payload


def split_frames(data: bytes) -> list[bytes]:
    total = max(1, math.ceil(len(data) / MAX_CHUNK))
    if total > 255:
        raise BleError("payload requires more than 255 BLE chunks")
    frames = []
    for index in range(total):
        frames.append(encode_frame(index, total, data[index * MAX_CHUNK : (index + 1) * MAX_CHUNK]))
    return frames


def decode_frame(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < CHUNK_HEADER:
        raise BleError("frame shorter than the 2-byte chunk header")
    if len(frame) > ATT_MTU:
        raise BleError(f"frame of {len(frame)} bytes exceeds ATT MTU {ATT_MTU}")
    index, total = frame[0], frame[1]
    if total == 0 or index >= total:
        raise BleError(f"invalid chunk header ({index}/{total})")
    return index, total, frame[CHUNK_HEADER:]


@dataclass
class _Reassembly:
    total: int = 0
    next_index: int = 0
    data: bytearray = field(default_factory=bytearray)
    updated_at: float = 0.0


class _Connection:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""
        self.lock = threading.Lock()
        self.subscribed = False
        self.reassembly: dict[str, _Reassembly] = {}
        self.authenticated = False

    def send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def readline(self) -> str | None:
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line.decode("utf-8", errors="replace")


class _GattHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        peripheral: BlePeripheral = self.server.peripheral  # type: ignore[attr-defined]
        conn = _Connection(self.request)
        self.request.settimeout(0.2)
        with peripheral.lock:
            peripheral.connections.add(conn)
        try:
            while peripheral.running:
                try:
                    line = conn.readline()
                except TimeoutError:
                    peripheral.check_reassembly_timeouts(conn)
                    continue
                except OSError:
                    break
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    conn.send({"ok": False, "error": "BAD_REQUEST"})
                    continue
                if not isinstance(request, dict):
                    conn.send({"ok": False, "error": "BAD_REQUEST"})
                    continue
                peripheral.handle_request(conn, request)
        finally:
            with peripheral.lock:
                peripheral.connections.discard(conn)
            try:
                self.request.close()
            except OSError:
                pass


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class BlePeripheral:
    def __init__(
        self,
        device_id: str = "vac-001",
        passkey: str = "482913",
        model: str = "HomeLink V1",
        fw_version: str = "1.2.0",
        host: str = "127.0.0.1",
        port: int = 18832,
        on_provisioned: Callable[[str, str], None] | None = None,
        reassembly_timeout: float = 0.6,
    ) -> None:
        if len(passkey) != 6 or not passkey.isdigit():
            raise ValueError("BLE passkey must be six digits")
        self.device_id = device_id
        self.passkey = passkey
        self.model = model
        self.fw_version = fw_version
        self.host = host
        self.port = port
        self.on_provisioned = on_provisioned
        self.reassembly_timeout = reassembly_timeout
        self.lock = threading.RLock()
        self.connections: set[_Connection] = set()
        self.credentials: tuple[str, str] | None = None
        self.notifications: list[dict[str, Any]] = []
        self.provisioned_event = threading.Event()
        self.stats = {
            "frames_received": 0,
            "passkey_writes": 0,
            "credential_writes": 0,
            "auth_failures": 0,
            "incomplete_writes": 0,
            "provisioned": 0,
        }
        self._fernet = fernet_for(passkey)
        self.running = False
        self._server: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._server = _ThreadingServer((self.host, self.port), _GattHandler)
        self._server.peripheral = self  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self.running = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name=f"ble-{self.device_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        with self.lock:
            for conn in list(self.connections):
                try:
                    conn.sock.close()
                except OSError:
                    pass
            self.connections.clear()

    def device_info(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model": self.model,
            "fw_version": self.fw_version,
            "mtu": ATT_MTU,
            "characteristics": [DEVICE_INFO, PROV_PASSKEY, WIFI_CREDS, PROV_STATUS],
        }

    # ---------------------------------------------------------------- request handling
    def handle_request(self, conn: _Connection, request: dict[str, Any]) -> None:
        op = request.get("op")
        characteristic = request.get("char")
        if op == "read" and characteristic == DEVICE_INFO:
            conn.send({"ok": True, "value": self.device_info()})
            return
        if op == "subscribe" and characteristic == PROV_STATUS:
            conn.subscribed = True
            conn.send({"ok": True, "subscribed": PROV_STATUS})
            return
        if op == "write" and characteristic in (PROV_PASSKEY, WIFI_CREDS):
            self._handle_write(conn, characteristic, request.get("frame"))
            return
        conn.send({"ok": False, "error": "UNKNOWN_OPERATION"})

    def _handle_write(self, conn: _Connection, characteristic: str, frame_b64: Any) -> None:
        try:
            if not isinstance(frame_b64, str):
                raise BleError("frame must be a base64 string")
            frame = base64.b64decode(frame_b64, validate=True)
            index, total, payload = decode_frame(frame)
        except (BleError, ValueError) as exc:
            conn.send({"ok": False, "error": "BAD_FRAME", "detail": str(exc)})
            self._notify("INCOMPLETE", characteristic=characteristic)
            return

        with self.lock:
            self.stats["frames_received"] += 1
            state = conn.reassembly.get(characteristic)
            if index == 0:
                state = _Reassembly(total=total, next_index=0, updated_at=time.monotonic())
                conn.reassembly[characteristic] = state
            elif state is None or total != state.total or index != state.next_index:
                conn.reassembly.pop(characteristic, None)
                conn.send({"ok": False, "error": "OUT_OF_ORDER"})
                self._notify("INCOMPLETE", characteristic=characteristic)
                return
            state.data += payload
            state.next_index = index + 1
            state.updated_at = time.monotonic()
            complete = state.next_index == state.total
            data = bytes(state.data) if complete else None
            if complete:
                conn.reassembly.pop(characteristic, None)

        if not complete:
            conn.send({"ok": True, "accepted": True, "chunk": index, "total": total})
            return
        self._complete_write(conn, characteristic, data or b"")

    def _complete_write(self, conn: _Connection, characteristic: str, data: bytes) -> None:
        if characteristic == PROV_PASSKEY:
            try:
                supplied = data.decode("ascii")
            except UnicodeDecodeError:
                supplied = ""
            authenticated = supplied == self.passkey
            conn.authenticated = authenticated
            with self.lock:
                self.stats["passkey_writes"] += 1
            conn.send(
                {
                    "ok": True,
                    "authenticated": authenticated,
                    "chunks": 1,
                    "status": "PASSKEY_ACCEPTED" if authenticated else "PASSKEY_REJECTED",
                }
            )
            return

        with self.lock:
            self.stats["credential_writes"] += 1
        try:
            cleartext = self._fernet.decrypt(data)
            payload = json.loads(cleartext.decode("utf-8"))
            ssid = payload["ssid"]
            psk = payload["psk"]
            if not isinstance(ssid, str) or not isinstance(psk, str):
                raise ValueError("ssid/psk must be strings")
        except (InvalidToken, ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            with self.lock:
                self.stats["auth_failures"] += 1
            conn.send({"ok": True, "status": "AUTH_FAILED"})
            self._notify("AUTH_FAILED")
            return

        with self.lock:
            self.credentials = (ssid, psk)
            self.stats["provisioned"] += 1
        conn.send({"ok": True, "status": "PROVISIONED"})
        self._notify("PROVISIONED", ssid=ssid)
        if self.on_provisioned is not None:
            self.on_provisioned(ssid, psk)
        self.provisioned_event.set()

    def check_reassembly_timeouts(self, conn: _Connection) -> None:
        now = time.monotonic()
        expired = [
            characteristic
            for characteristic, state in conn.reassembly.items()
            if now - state.updated_at > self.reassembly_timeout
        ]
        for characteristic in expired:
            with self.lock:
                conn.reassembly.pop(characteristic, None)
                self.stats["incomplete_writes"] += 1
            self._notify("INCOMPLETE", characteristic=characteristic)

    def _notify(self, status: str, **extra: Any) -> None:
        notification = {"event": PROV_STATUS, "data": {"status": status, **extra}}
        with self.lock:
            self.notifications.append(notification)
            subscribers = [conn for conn in self.connections if conn.subscribed]
        for conn in subscribers:
            conn.send(notification)

    def notifications_with_status(self, status: str) -> list[dict[str, Any]]:
        with self.lock:
            return [n for n in self.notifications if n["data"]["status"] == status]
