"""Bounded, advisory comparison with a configured GitHub public certificate."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import http.client
import json
import re
import ssl
import time
from typing import Final, Mapping

from .contracts import OpenPGPContractError, normalize_fingerprint
from .keys import KeyInspectionError, inspect_key

__all__ = [
    "CertificateDiscoveryResult",
    "CertificateDiscoveryStatus",
    "discover_github_certificate",
]

_GITHUB_API_HOST: Final = "api.github.com"
_MAX_RESPONSE_BYTES: Final = 1_048_576
_MAX_CERTIFICATES: Final = 100
_TIMEOUT_SECONDS: Final = 5.0
_READ_CHUNK_BYTES: Final = 8192
_USERNAME_RE: Final = re.compile(r"[A-Za-z0-9-]{1,39}\Z", re.ASCII)


class CertificateDiscoveryStatus(StrEnum):
    """Outcome of an advisory comparison against the configured source."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CertificateDiscoveryResult:
    """Immutable remote comparison; it never changes local trust pins."""

    status: CertificateDiscoveryStatus
    expected_fingerprint: str | None
    discovered_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _HTTPReply:
    status: int
    headers: Mapping[str, str]
    body: bytes


def discover_github_certificate(
    *,
    username: str,
    expected_fingerprint: str,
    api_entry_id: int | None = None,
) -> CertificateDiscoveryResult:
    """Compare a full local pin with certificates from one GitHub user.

    Discovery is advisory only. It uses GitHub's anonymous HTTPS user-key
    endpoint and parses each ``raw_key`` certificate locally. A source failure,
    malformed or truncated response, or paginated result is unavailable; an
    absent fingerprint is called a mismatch only after a complete response.
    """
    try:
        expected = normalize_fingerprint(expected_fingerprint)
        if (
            not isinstance(username, str)
            or _USERNAME_RE.fullmatch(username) is None
            or (api_entry_id is not None and (
                isinstance(api_entry_id, bool)
                or not isinstance(api_entry_id, int)
                or api_entry_id <= 0
            ))
        ):
            return _unavailable(expected)
    except (OpenPGPContractError, TypeError, ValueError):
        return _unavailable(None)

    deadline = time.monotonic() + _TIMEOUT_SECONDS
    path = f"/users/{username}/gpg_keys?per_page={_MAX_CERTIFICATES}"
    try:
        reply = _http_get(path)
        if reply.status != 200 or _has_next_page(reply.headers):
            return _unavailable(expected)
        if _header(reply.headers, "content-encoding").lower() not in ("", "identity"):
            return _unavailable(expected)
        records = json.loads(reply.body)
        if not isinstance(records, list) or len(records) >= _MAX_CERTIFICATES:
            return _unavailable(expected)
        fingerprints: list[str] = []
        seen_ids: set[int] = set()
        for record in records:
            if time.monotonic() >= deadline:
                return _unavailable(expected)
            if not isinstance(record, dict):
                return _unavailable(expected)
            entry_id = record.get("id")
            raw_key = record.get("raw_key")
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id <= 0
                or entry_id in seen_ids
                or not isinstance(raw_key, str)
                or not raw_key
            ):
                return _unavailable(expected)
            seen_ids.add(entry_id)
            # GitHub's public_key field is a serialization detail, not a GPG
            # certificate. Only raw_key is suitable for independent parsing.
            inspection = inspect_key(certificate=raw_key)
            if time.monotonic() >= deadline:
                return _unavailable(expected)
            fingerprints.append(
                normalize_fingerprint(inspection.primary_fingerprint)
            )
        selected = tuple(
            fingerprint
            for record, fingerprint in zip(records, fingerprints, strict=True)
            if api_entry_id is None or record["id"] == api_entry_id
        )
    except (
        OSError,
        http.client.HTTPException,
        json.JSONDecodeError,
        KeyInspectionError,
        OpenPGPContractError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return _unavailable(expected)

    unique = tuple(dict.fromkeys(fingerprints))
    return CertificateDiscoveryResult(
        CertificateDiscoveryStatus.MATCH
        if expected in selected
        else CertificateDiscoveryStatus.MISMATCH,
        expected,
        unique,
    )


def _unavailable(expected: str | None) -> CertificateDiscoveryResult:
    return CertificateDiscoveryResult(
        CertificateDiscoveryStatus.UNAVAILABLE, expected
    )


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    return next(
        (str(value) for key, value in headers.items() if str(key).lower() == wanted),
        "",
    )


def _has_next_page(headers: Mapping[str, str]) -> bool:
    link = _header(headers, "link")
    if not link:
        return False
    for item in link.split(","):
        match = re.fullmatch(r"\s*<[^<>]+>\s*((?:;\s*[^;,]+)*)\s*", item)
        if match is None:
            raise ValueError("invalid pagination metadata")
        parameters = match.group(1).split(";")
        for parameter in parameters:
            if not parameter.strip():
                continue
            name, separator, value = parameter.strip().partition("=")
            if not separator:
                raise ValueError("invalid pagination metadata")
            if name.strip().lower() == "rel":
                relation = value.strip().strip('"').split()
                if "next" in relation:
                    return True
    return False


def _http_get(path: str) -> _HTTPReply:
    """Make one anonymous request with socket timeouts and a body deadline.

    System DNS resolution and an in-flight inspector cannot be interrupted by
    this cooperative budget; callers needing a strict wall-clock cap must
    supervise the operation. The shared inspector has its own process timeout.
    """
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    connection = http.client.HTTPSConnection(
        _GITHUB_API_HOST,
        port=443,
        timeout=_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/vnd.github+json",
                "Accept-Encoding": "identity",
                "User-Agent": "LocalSetup OpenPGP certificate discovery",
            },
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("response deadline exceeded")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > _MAX_RESPONSE_BYTES):
            raise ValueError("response exceeds limit")
        body = bytearray()
        while len(body) <= _MAX_RESPONSE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response deadline exceeded")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(
                min(_READ_CHUNK_BYTES, _MAX_RESPONSE_BYTES + 1 - len(body))
            )
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds limit")
        if length is not None and len(body) != int(length):
            raise ValueError("incomplete response")
        headers: dict[str, str] = {}
        for name, value in response.getheaders():
            key = str(name).lower()
            headers[key] = f"{headers[key]}, {value}" if key in headers else str(value)
        return _HTTPReply(response.status, headers, bytes(body))
    finally:
        connection.close()
