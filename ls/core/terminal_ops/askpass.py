"""Private client for the operation-scoped sudo askpass broker."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import struct
import time

from .sudo import _proc_generation_is_live, _proc_identity

_META_ENV = {
    "socket": "LS_TERMINAL_SUDO_SOCKET",
    "nonce": "LS_TERMINAL_SUDO_NONCE",
    "operation_id": "LS_TERMINAL_SUDO_OPERATION_ID",
    "owner_id": "LS_TERMINAL_SUDO_OWNER_ID",
    "target": "LS_TERMINAL_SUDO_TARGET",
    "endpoint_id": "LS_TERMINAL_SUDO_ENDPOINT_ID",
    "live_owner_id": "LS_TERMINAL_SUDO_LIVE_OWNER_ID",
    "payload_digest": "LS_TERMINAL_SUDO_PAYLOAD_DIGEST",
    "authentication_deadline": "LS_TERMINAL_SUDO_AUTHENTICATION_DEADLINE",
    "controller_pid": "LS_TERMINAL_SUDO_CONTROLLER_PID",
    "controller_starttime": "LS_TERMINAL_SUDO_CONTROLLER_STARTTIME",
}
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REPLY = 1 + 4 + 4096
_BROKER_TIMEOUT = 35.0


def _read_exact(connection: socket.socket, size: int,
                current=None) -> bytearray:
    data = bytearray()
    while len(data) < size:
        if current is not None and not current():
            raise OSError("request is no longer current")
        try:
            chunk = connection.recv(size - len(data))
        except socket.timeout:
            if current is None:
                raise
            if current is not None and current():
                continue
            raise OSError("request is no longer current") from None
        if not chunk:
            raise OSError("broker disconnected")
        data.extend(chunk)
    return data


def _request() -> tuple[str, bytes, object, float | None]:
    values = {key: os.environ.get(name, "") for key, name in _META_ENV.items()}
    # A child environment cannot be amended after sudo is launched. Derive the
    # actual parent generation natively; the broker independently verifies it.
    parent_pid = os.getppid()
    parent = _proc_identity(parent_pid)
    if parent is None:
        raise ValueError("sudo process identity is unavailable")
    values["sudo_pid"] = parent_pid
    values["sudo_start_time"] = str(parent[1])
    deadline: float | None = None
    if values["authentication_deadline"]:
        try:
            deadline = float(values["authentication_deadline"])
        except ValueError as error:
            raise ValueError("invalid authentication deadline") from error
        if not math.isfinite(deadline) or not time.monotonic() < deadline:
            raise ValueError("invalid authentication deadline")
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
    controller_configured = bool(values["controller_pid"] or values["controller_starttime"])
    controller_pid: int | None = None
    controller_starttime: int | None = None
    if controller_configured:
        if not values["controller_pid"].isdecimal() or not values["controller_starttime"].isdecimal():
            raise ValueError("invalid controller process generation")
        controller_pid = int(values["controller_pid"])
        controller_starttime = int(values["controller_starttime"])
        if (controller_pid <= 1 or controller_starttime <= 0
                or not _proc_generation_is_live(controller_pid, controller_starttime)):
            raise ValueError("controller process generation is not current")
    request = json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(request) > 4096:
        raise ValueError("request too large")
    def current() -> bool:
        if os.getppid() != parent_pid or (deadline is not None and time.monotonic() >= deadline):
            return False
        identity = _proc_identity(parent_pid)
        if identity is None or identity[1] != parent[1]:
            return False
        return (controller_pid is None
                or _proc_generation_is_live(controller_pid, controller_starttime or 0))

    return values["socket"], request, current, deadline


def _write_secret(value: bytearray, current=None) -> None:
    value.extend(b"\n")
    view = memoryview(value)
    while view:
        if current is not None and not current():
            raise OSError("request is no longer current")
        count = os.write(1, view)
        if count <= 0:
            raise OSError("askpass output failed")
        view = view[count:]


def main() -> int:
    """Send only the current challenge and write a returned key to stdout."""
    secret = bytearray()
    response = bytearray()
    try:
        path, request, current, deadline = _request()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Controller-held credentials may arrive over a qualified remote
            # authentication channel. Keep that bounded exchange inside this
            # same sudo invocation instead of provoking a second attempt.
            current_polling = deadline is not None or bool(os.environ.get(_META_ENV["controller_pid"]))
            connection.settimeout(0.2 if current_polling else _BROKER_TIMEOUT)
            if not current():
                return 1
            connection.connect(path)
            encoded_request = struct.pack("!I", len(request)) + request
            if current_polling:
                position = 0
                while position < len(encoded_request):
                    if not current():
                        return 1
                    try:
                        sent = connection.send(encoded_request[position:])
                    except socket.timeout:
                        continue
                    if sent <= 0:
                        return 1
                    position += sent
                read_current = current
            else:
                connection.sendall(encoded_request)
                read_current = None
            status = _read_exact(connection, 1, read_current)
            size_bytes = _read_exact(connection, 4, read_current)
            size = struct.unpack("!I", size_bytes)[0]
            if size > 4096:
                return 1
            if status == b"O":
                if not 1 <= size <= 4096:
                    return 1
                response = _read_exact(connection, size, read_current)
                secret.extend(response)
                if any(ch in secret for ch in (0, 10, 13)):
                    return 1
                if not current():
                    return 1
                _write_secret(secret, read_current)
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
