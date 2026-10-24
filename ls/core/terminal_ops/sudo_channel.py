"""Bounded private request and credential frames for managed sudo input."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import socket
from typing import Callable

from .dialog_channel import ChannelClosed, receive_frame, send_frame

MAX_METADATA = 4096
MAX_SECRET = 4096
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = frozenset({"automatic", "manual"})
_STATUSES = frozenset({
    "available", "unavailable", "empty", "invalid", "loading_error",
    "blocked", "cancelled", "expired", "delivery_unknown",
})


def _wipe(value: bytearray) -> None:
    value[:] = b"\0" * len(value)
    value.clear()


def _send_metadata(connection: socket.socket, value: dict[str, object],
                   current: Callable[[], bool]) -> None:
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True,
                             ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ChannelClosed("Metadata is invalid.") from exc
    if not 1 <= len(encoded) <= MAX_METADATA:
        raise ChannelClosed("Metadata is outside the supported bounds.")
    send_frame(connection, encoded, current)


def _read_metadata(connection: socket.socket, current: Callable[[], bool]) -> dict[str, object]:
    payload = receive_frame(connection, current, maximum=MAX_METADATA)
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ChannelClosed("Metadata is invalid.") from exc
    finally:
        _wipe(payload)
    if not isinstance(value, dict):
        raise ChannelClosed("Metadata is invalid.")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate metadata key")
        value[key] = item
    return value


def _validate_operation(operation_id: object) -> str:
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ChannelClosed("Operation identity is invalid.")
    return operation_id


def _validate_request(value: dict[str, object]) -> dict[str, object]:
    if set(value) != {"version", "operation_id", "kind", "ordinal", "payload_digest"}:
        raise ChannelClosed("Request metadata fields are invalid.")
    operation_id = _validate_operation(value.get("operation_id"))
    kind = value.get("kind")
    ordinal = value.get("ordinal")
    digest = value.get("payload_digest")
    if (type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(kind, str) or kind not in _KINDS
            or type(ordinal) is not int or ordinal not in {1, 2}
            or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        raise ChannelClosed("Request metadata is invalid.")
    return {"version": 1, "operation_id": operation_id, "kind": kind,
            "ordinal": ordinal, "payload_digest": digest}


def send_request(connection: socket.socket, operation_id: str, kind: str, ordinal: int,
                 payload_digest: str, current: Callable[[], bool]) -> None:
    """Send one operation-bound credential request without including a secret."""
    request = _validate_request({
        "version": 1, "operation_id": operation_id, "kind": kind,
        "ordinal": ordinal, "payload_digest": payload_digest,
    })
    _send_metadata(connection, request, current)


def receive_request(connection: socket.socket, current: Callable[[], bool]) -> dict[str, object]:
    """Receive and validate one operation-bound credential request."""
    return _validate_request(_read_metadata(connection, current))


def _validate_secret(secret: bytes | bytearray | memoryview) -> None:
    if not 1 <= len(secret) <= MAX_SECRET or any(value in secret for value in (0, 10, 13)):
        raise ChannelClosed("Credential value is outside the supported bounds.")
    try:
        secret.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise ChannelClosed("Credential value is invalid.") from exc


@dataclass(slots=True, repr=False)
class CredentialReply:
    """A safe status and, only when available, an owned mutable secret."""

    status: str
    source: str
    secret: bytearray | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status == "available" and not isinstance(self.secret, bytearray):
            raise ChannelClosed("Credential value must be a mutable byte buffer.")
        _validate_reply_fields(self.status, self.source, self.secret)

    def dispose(self) -> None:
        if isinstance(self.secret, bytearray):
            _wipe(self.secret)
        self.secret = None


def _validate_reply_fields(status: object, source: object,
                           secret: object) -> None:
    if not isinstance(status, str) or status not in _STATUSES:
        raise ChannelClosed("Reply status is invalid.")
    expected_source = {"generic", "manual"} if status == "available" else {"none"}
    if not isinstance(source, str) or source not in expected_source:
        raise ChannelClosed("Reply source is invalid.")
    if status == "available":
        if not isinstance(secret, (bytes, bytearray, memoryview)):
            raise ChannelClosed("Credential value is missing.")
        _validate_secret(secret)
    elif secret is not None:
        raise ChannelClosed("Unexpected credential value.")


def send_reply(connection: socket.socket, status: str, source: str,
               secret: bytes | bytearray | memoryview | None,
               current: Callable[[], bool]) -> None:
    """Send status metadata and a separate bounded secret frame when available."""
    _validate_reply_fields(status, source, secret)
    _send_metadata(connection, {"version": 1, "status": status, "source": source}, current)
    if status == "available":
        assert secret is not None
        send_frame(connection, secret, current)


def receive_reply(connection: socket.socket, current: Callable[[], bool]) -> CredentialReply:
    """Receive a reply, retaining any secret only in a disposable bytearray."""
    value = _read_metadata(connection, current)
    if set(value) != {"version", "status", "source"} or type(value.get("version")) is not int or value["version"] != 1:
        raise ChannelClosed("Reply metadata fields are invalid.")
    status, source = value.get("status"), value.get("source")
    if not isinstance(status, str) or not isinstance(source, str):
        raise ChannelClosed("Reply metadata is invalid.")
    if status not in _STATUSES or source not in ({"generic", "manual"} if status == "available" else {"none"}):
        raise ChannelClosed("Reply metadata is invalid.")
    secret: bytearray | None = None
    if status == "available":
        secret = receive_frame(connection, current, maximum=MAX_SECRET)
        try:
            _validate_secret(secret)
            return CredentialReply(status, source, secret)
        except BaseException:
            _wipe(secret)
            raise
    return CredentialReply(status, source)


__all__ = ["CredentialReply", "MAX_METADATA", "MAX_SECRET", "receive_reply",
           "receive_request", "send_reply", "send_request"]
