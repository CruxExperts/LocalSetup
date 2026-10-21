"""Private client for the operation-scoped sudo askpass broker."""

from __future__ import annotations

import json
import os
import re
import socket
import struct

from .sudo import _proc_identity

_META_ENV = {
    "socket": "LS_TERMINAL_SUDO_SOCKET",
    "nonce": "LS_TERMINAL_SUDO_NONCE",
    "operation_id": "LS_TERMINAL_SUDO_OPERATION_ID",
    "owner_id": "LS_TERMINAL_SUDO_OWNER_ID",
    "target": "LS_TERMINAL_SUDO_TARGET",
    "endpoint_id": "LS_TERMINAL_SUDO_ENDPOINT_ID",
    "live_owner_id": "LS_TERMINAL_SUDO_LIVE_OWNER_ID",
    "payload_digest": "LS_TERMINAL_SUDO_PAYLOAD_DIGEST",
}
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REPLY = 1 + 4 + 4096
_BROKER_TIMEOUT = 35.0


def _read_exact(connection: socket.socket, size: int) -> bytearray:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise OSError("broker disconnected")
        data.extend(chunk)
    return data


def _request() -> tuple[str, bytes]:
    values = {key: os.environ.get(name, "") for key, name in _META_ENV.items()}
    # A child environment cannot be amended after sudo is launched. Derive the
    # actual parent generation natively; the broker independently verifies it.
    parent_pid = os.getppid()
    parent = _proc_identity(parent_pid)
    if parent is None:
        raise ValueError("sudo process identity is unavailable")
    values["sudo_pid"] = parent_pid
    values["sudo_start_time"] = str(parent[1])
    if not values["socket"] or not _HEX32.fullmatch(values["nonce"]):
        raise ValueError("invalid broker metadata")
    if not _HEX64.fullmatch(values["payload_digest"]):
        raise ValueError("invalid payload identity")
    if parent_pid <= 1 or not values["sudo_start_time"].isdecimal():
        raise ValueError("invalid sudo process identity")
    for name in ("operation_id", "owner_id", "target", "endpoint_id", "live_owner_id"):
        value = values[name]
        if not value or len(value) > 256 or "\0" in value:
            raise ValueError("invalid operation binding")
    request = json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(request) > 4096:
        raise ValueError("request too large")
    return values["socket"], request


def _write_secret(value: bytearray) -> None:
    value.extend(b"\n")
    view = memoryview(value)
    while view:
        count = os.write(1, view)
        if count <= 0:
            raise OSError("askpass output failed")
        view = view[count:]


def main() -> int:
    """Send only the current challenge and write a returned key to stdout."""
    secret = bytearray()
    response = bytearray()
    try:
        path, request = _request()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Controller-held credentials may arrive over a qualified remote
            # authentication channel. Keep that bounded exchange inside this
            # same sudo invocation instead of provoking a second attempt.
            connection.settimeout(_BROKER_TIMEOUT)
            connection.connect(path)
            connection.sendall(struct.pack("!I", len(request)) + request)
            status = _read_exact(connection, 1)
            size_bytes = _read_exact(connection, 4)
            size = struct.unpack("!I", size_bytes)[0]
            if size > 4096:
                return 1
            if status == b"O":
                if not 1 <= size <= 4096:
                    return 1
                response = _read_exact(connection, size)
                secret.extend(response)
                if any(ch in secret for ch in (0, 10, 13)):
                    return 1
                _write_secret(secret)
                return 0
            if status != b"N" or size != 0:
                return 1
            return 1
        finally:
            connection.close()
    except (OSError, ValueError, UnicodeError, struct.error):
        # Askpass diagnostics can end up in operation output; fail closed silently.
        return 1
    finally:
        for index in range(len(secret)):
            secret[index] = 0
        for index in range(len(response)):
            response[index] = 0


__all__ = ["main"]
