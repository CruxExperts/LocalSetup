"""One-use, process-qualified local channel for private terminal form values."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import tempfile
import threading
import time

from .command_state import ancestor_chain, process_info
from .dialog_model import Form, Response, validate_response

MAX_FRAME = 524288
_REQUEST = re.compile(r"[0-9a-f]{32}\Z")


class ChannelClosed(Exception):
    """No payload or input values are included in channel exceptions."""


def _wipe(value: bytearray) -> None:
    value[:] = b"\0" * len(value)
    value.clear()


def _peer(connection: socket.socket) -> int:
    pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid() or pid <= 0:
        raise ChannelClosed("Peer identity is unavailable.")
    return pid


def _receive(connection: socket.socket, size: int, current: Callable[[], bool]) -> bytearray:
    result = bytearray(size)
    position = 0
    try:
        while position < size:
            if not current():
                raise ChannelClosed("Request is no longer current.")
            try:
                received = connection.recv_into(memoryview(result)[position:])
            except socket.timeout:
                continue
            if not received:
                raise ChannelClosed("Peer disconnected.")
            position += received
        if not current():
            raise ChannelClosed("Request is no longer current.")
        return result
    except BaseException:
        _wipe(result)
        raise


def receive_frame(connection: socket.socket, current: Callable[[], bool], *, maximum: int = MAX_FRAME) -> bytearray:
    header = _receive(connection, 4, current)
    size = struct.unpack("!I", header)[0]
    if not 1 <= size <= min(maximum, MAX_FRAME):
        raise ChannelClosed("Frame is outside the supported bounds.")
    return _receive(connection, size, current)


def send_frame(connection: socket.socket, value: bytes | bytearray, current: Callable[[], bool]) -> None:
    if not 1 <= len(value) <= MAX_FRAME:
        raise ChannelClosed("Frame is outside the supported bounds.")
    for part in (struct.pack("!I", len(value)), value):
        position = 0
        while position < len(part):
            if not current():
                raise ChannelClosed("Request is no longer current.")
            try:
                sent = connection.send(memoryview(part)[position:])
            except socket.timeout:
                continue
            if sent <= 0:
                raise ChannelClosed("Peer disconnected.")
            position += sent


def _form_mapping(form: Form) -> dict[str, object]:
    """Serialize optional field relations without changing legacy form rows."""
    result = asdict(form)
    for item in result["fields"]:
        if item.get("matches") is None:
            item.pop("matches", None)
    return result


def encode_response(form: Form, response: Response) -> bytearray:
    if validate_response(form, response):
        raise ChannelClosed("Response does not satisfy the form.")
    output = bytearray(b"S")
    try:
        for item in form.fields:
            value = response.values[item.name]
            if isinstance(value, bytearray):
                output.extend(struct.pack("!I", len(value)))
                output.extend(value)
            else:
                output.extend(struct.pack("!I", len(value)))
                for choice in item.choices:
                    if choice.value in value:
                        output.extend(struct.pack("!I", item.choices.index(choice)))
        return output
    except BaseException:
        _wipe(output)
        raise


def decode_response(form: Form, payload: bytearray) -> Response:
    response = Response({})
    position = 1

    def integer() -> int:
        nonlocal position
        if position + 4 > len(payload):
            raise ChannelClosed("Response is truncated.")
        value = struct.unpack_from("!I", payload, position)[0]
        position += 4
        return value

    try:
        if not payload or payload[0] != ord("S"):
            raise ChannelClosed("Response type is invalid.")
        for item in form.fields:
            size = integer()
            if item.kind in {"checkbox", "radio"}:
                if size > len(item.choices):
                    raise ChannelClosed("Selection is outside the supported bounds.")
                chosen = []
                for _ in range(size):
                    index = integer()
                    if index >= len(item.choices):
                        raise ChannelClosed("Selection is invalid.")
                    chosen.append(item.choices[index].value)
                response.values[item.name] = tuple(chosen)
            else:
                if size > item.max_bytes or position + size > len(payload):
                    raise ChannelClosed("Input is outside the supported bounds.")
                response.values[item.name] = payload[position:position + size]
                position += size
        if position != len(payload) or validate_response(form, response):
            raise ChannelClosed("Response does not satisfy the form.")
        return response
    except BaseException:
        response.dispose()
        raise


@dataclass(frozen=True, slots=True)
class HelperIdentity:
    """The fixed renderer argv and its admitted native server ancestor."""

    argv: tuple[str, ...]
    executable: str
    ancestor_pid: int
    ancestor_starttime: int

    def qualifies(self, pid: int) -> bool:
        try:
            info = process_info(pid)
            ancestor = ancestor_chain(pid).get(self.ancestor_pid)
            command = tuple(os.fsdecode(part) for part in Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0"))
            executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        except (OSError, ValueError):
            return False
        return (info is not None and ancestor is not None and pid != self.ancestor_pid
                and ancestor.get("starttime") == self.ancestor_starttime
                and command == self.argv and executable == Path(self.executable).resolve())


class DialogChannel:
    """Own one pending response; callers bind current state to their operation.

    `deliver` must perform its own final operation check and bounded single
    handoff. Only exactly True acknowledges delivery; an exception or any other
    result after invocation is unknown and must never cause replay.
    """

    def __init__(self, form: Form, *, request_id: str, deadline: float,
                 validate_current: Callable[[], bool], deliver: Callable[[Response], bool]):
        if (not isinstance(form, Form) or not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id)
                or type(deadline) not in {int, float} or not math.isfinite(deadline)
                or not time.monotonic() < deadline <= time.monotonic() + 3600
                or not callable(validate_current) or not callable(deliver)):
            raise ValueError("Dialog request binding is invalid.")
        self.form, self.request_id, self.deadline = form, request_id, deadline
        self.validate_current, self.deliver = validate_current, deliver
        self.status = "requested"
        self.peer_identity: dict[str, int] | None = None
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._identity: HelperIdentity | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connection: socket.socket | None = None
        self._peer_fd: int | None = None
        self.path = ""

    def __repr__(self) -> str:
        return f"<DialogChannel status={self.status}>"

    def current(self) -> bool:
        try:
            return (not self._cancel.is_set() and time.monotonic() < self.deadline
                    and (self._peer_fd is None or not select.select([self._peer_fd], [], [], 0)[0])
                    and self.validate_current() is True)
        except Exception:
            return False

    def open(self) -> None:
        if self._temporary is not None or self.status != "requested" or not self.current():
            raise ChannelClosed("Request cannot be opened.")
        self._temporary = tempfile.TemporaryDirectory(prefix="ls-form-")
        self.path = str(Path(self._temporary.name) / "channel.sock")
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(self.path)
            os.chmod(self.path, 0o600)
            self._listener.listen(1)
            self._listener.settimeout(0.1)
            self._thread = threading.Thread(target=self._serve, name="localsetup-dialog", daemon=False)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def admit(self, identity: HelperIdentity) -> None:
        if self._ready.is_set() or not isinstance(identity, HelperIdentity) or not self.current():
            raise ChannelClosed("Renderer admission is unavailable.")
        self._identity = identity
        self._ready.set()

    def _serve(self) -> None:
        try:
            while self.current() and not self._ready.wait(0.1):
                pass
            while self.current() and self._ready.is_set():
                try:
                    connection, _address = self._listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(0.1)
                    pid = _peer(connection)
                    if self._identity is None or not self._identity.qualifies(pid):
                        continue
                    self.peer_identity = process_info(pid)
                    if self.peer_identity is None:
                        continue
                    self._peer_fd = os.pidfd_open(pid)
                    admitted = process_info(pid)
                    if admitted is None or admitted["starttime"] != self.peer_identity["starttime"]:
                        raise ChannelClosed("Renderer generation changed during admission.")
                    self._connection = connection
                    self._exchange(connection)
                    return
            self.status = self._stopped_status()
        except (OSError, ChannelClosed, ValueError, TypeError):
            if self.status in {"requested", "visible"}:
                self.status = self._stopped_status()
        finally:
            self._connection = None

    def _exchange(self, connection: socket.socket) -> None:
        handshake = receive_frame(connection, self.current, maximum=32)
        if handshake != self.request_id.encode("ascii"):
            raise ChannelClosed("Request identity does not match.")
        specification = json.dumps({"version": 1, "deadline": self.deadline, "form": _form_mapping(self.form)},
                                   ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        send_frame(connection, specification, self.current)
        visible = receive_frame(connection, self.current, maximum=16)
        if visible in (b"cancelled", b"expired", b"unavailable", b"failed"):
            self.status = visible.decode("ascii")
            send_frame(connection, b"closed", self.current)
            return
        if visible != b"visible":
            raise ChannelClosed("Renderer did not acknowledge presentation.")
        self.status = "visible"
        payload = receive_frame(connection, self.current, maximum=70000)
        try:
            if payload in (b"cancelled", b"expired", b"unavailable", b"failed"):
                self.status = payload.decode("ascii")
                send_frame(connection, b"closed", self.current)
                return
            response = decode_response(self.form, payload)
            try:
                if not self.current():
                    raise ChannelClosed("Request is no longer current.")
                # Record uncertainty before the consumer can have any effect.
                self.status = "delivery_unknown"
                try:
                    accepted = self.deliver(response) is True
                except Exception:
                    accepted = False
                if accepted:
                    self.status = "delivered"
                send_frame(connection, b"accepted" if accepted else b"unknown", self.current)
            finally:
                response.dispose()
        finally:
            _wipe(payload)

    def _stopped_status(self) -> str:
        return "cancelled" if self._cancel.is_set() else "expired" if time.monotonic() >= self.deadline else "unavailable"

    @property
    def cleanup_pending(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def close(self) -> None:
        self._cancel.set()
        if self._listener is not None:
            self._listener.close()
        if self._connection is not None:
            try:
                self._connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        if self._temporary is not None and (self._thread is None or not self._thread.is_alive()):
            self._temporary.cleanup()
        if self._peer_fd is not None and not self.cleanup_pending:
            os.close(self._peer_fd)
            self._peer_fd = None
        if self.status in {"requested", "visible"}:
            self.status = "cancelled"


def connect(path: str, *, controller_pid: int, controller_starttime: int) -> socket.socket:
    """The helper admits only the exact private listener process generation."""
    target = Path(path)
    if not target.is_absolute():
        raise ChannelClosed("Channel path is not absolute.")
    try:
        parent, entry = target.parent.lstat(), target.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
                or not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077):
            raise ChannelClosed("Channel ownership is invalid.")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(1)
        connection.connect(path)
        info = process_info(_peer(connection))
        if info is None or info["pid"] != controller_pid or info["starttime"] != controller_starttime:
            raise ChannelClosed("Controller generation does not match.")
        connection.settimeout(0.1)
        return connection
    except BaseException:
        if "connection" in locals():
            connection.close()
        raise
