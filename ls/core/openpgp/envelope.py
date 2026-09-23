"""Bounded, explicitly pinned OpenPGP envelope sealing primitives.

This module seals signed inner messages and parses the outer framing. It does
not decrypt envelopes or release plaintext. Packet key IDs are always hidden;
recipient fingerprints in the signed manifest are the signer's authenticated
intended set, not identities recoverable from ciphertext packet headers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import struct
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Any

from .contracts import (
    EnvelopeHeader,
    EnvelopePolicy,
    LocalTrust,
    OpenPGPContractError,
    normalize_fingerprint,
    validate_envelope_participants,
)
from .secrets import SecretReference, SecretResolver

__all__ = [
    "MAX_ENVELOPE_BYTES",
    "MAX_PAYLOAD_BYTES",
    "EnvelopeError",
    "EnvelopeErrorCode",
    "EnvelopeManifest",
    "ParsedEnvelope",
    "ParsedInnerMessage",
    "parse_envelope",
    "parse_inner_message",
    "seal_envelope",
]

MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_ENVELOPE_BYTES = 32 * 1024 * 1024
_MAX_RECIPIENTS = 64
_MAX_HEADER_BYTES = 256
_MAX_MANIFEST_BYTES = 4096
_MAX_PASSPHRASE_BYTES = 4096
_GPG_TIMEOUT_SECONDS = 60.0
_GPG_READ_SIZE = 8192

_INNER_MAGIC = b"localsetup.openpgp-inner\x00\x01"

_PACKET_LINE_RE = re.compile(r"^\s*:pubkey enc packet:\s*(.*)$", re.IGNORECASE)
_PACKET_VERSION_RE = re.compile(r"\bversion\s+(\d+)\b", re.IGNORECASE)
_PACKET_KEY_ID_RE = re.compile(r"\bkeyid\s+([0-9a-f]{16})\b", re.IGNORECASE)
_ENCRYPTED_PACKET_RE = re.compile(
    r"^\s*:(?:encrypted data|aead encrypted) packet:", re.IGNORECASE
)
_SYMMETRIC_PACKET_RE = re.compile(r"^\s*:symkey enc packet:", re.IGNORECASE)
_MALFORMED_PACKET_RE = re.compile(
    r"invalid packet|packet(?:\(\d+\))? (?:too short|truncated)|premature eof",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class EnvelopeErrorCode(StrEnum):
    """Stable, non-secret envelope failures."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_GPG_HOME = "INVALID_GPG_HOME"
    GPG_NOT_FOUND = "GPG_NOT_FOUND"
    SIGN_ENCRYPT_FAILED = "SIGN_ENCRYPT_FAILED"
    GPG_TIMEOUT = "GPG_TIMEOUT"
    GPG_OUTPUT_LIMIT = "GPG_OUTPUT_LIMIT"
    MALFORMED_CIPHERTEXT = "MALFORMED_CIPHERTEXT"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    INVALID_INNER_MESSAGE = "INVALID_INNER_MESSAGE"


class EnvelopeError(RuntimeError):
    """Safe failure containing only a stable code, never GPG or input text."""

    def __init__(self, code: EnvelopeErrorCode) -> None:
        self.code = EnvelopeErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class EnvelopeManifest:
    """Inner manifest fields; authenticated only after successful GPG verification."""

    header: EnvelopeHeader
    signer_fingerprint: str
    recipient_fingerprints: tuple[str, ...]
    payload_sha256: str
    payload_length: int


@dataclass(frozen=True, slots=True)
class ParsedEnvelope:
    """Outer framing with opaque encrypted bytes and a format-only clear header.

    The hidden-key-ID PKESK packets reveal a recipient count, not the recipient
    fingerprints. Do not infer actual recipient identities from ``ciphertext``.
    """

    header: EnvelopeHeader
    ciphertext: bytes


@dataclass(frozen=True, slots=True)
class ParsedInnerMessage:
    """Parsed signed plaintext; parsing alone does not authenticate or release it."""

    manifest: EnvelopeManifest
    payload: bytes


def seal_envelope(
    payload: bytes,
    *,
    gnupg_home: str | os.PathLike[str],
    signer_fingerprint: str,
    recipient_fingerprints: Iterable[str],
    local_trust: LocalTrust,
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
) -> bytes:
    """Sign then encrypt exact payload bytes for only the explicit recipients.

    ``gnupg_home`` must be an existing private GnuPG home selected by the
    caller. The signer must be locally trusted; all signer and recipient
    selectors are normalized full primary-key fingerprints. The local GnuPG
    configuration and ambient home are bypassed, implicit recipient expansion
    is disabled, and the signing passphrase is delivered only over an inherited
    file descriptor.
    """
    if type(payload) is not bytes or len(payload) > MAX_PAYLOAD_BYTES:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)
    if not isinstance(local_trust, LocalTrust):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)
    if not isinstance(passphrase_reference, SecretReference) or not isinstance(
        secret_resolver, SecretResolver
    ):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)

    signer = normalize_fingerprint(signer_fingerprint)
    if isinstance(recipient_fingerprints, (str, bytes, bytearray, dict)):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)
    selected_recipients = tuple(islice(recipient_fingerprints, _MAX_RECIPIENTS + 1))
    if len(selected_recipients) > _MAX_RECIPIENTS:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)
    policy = EnvelopePolicy(
        expected_signers=(signer,), expected_recipients=selected_recipients
    )
    recipients = tuple(sorted(policy.expected_recipients))
    validate_envelope_participants((signer,), recipients, policy)
    local_trust.require_signer(signer)
    home = _validated_gnupg_home(gnupg_home)
    executable = shutil.which("gpg") or shutil.which("gpg2")
    if not executable:
        raise EnvelopeError(EnvelopeErrorCode.GPG_NOT_FOUND)

    passphrase = secret_resolver.resolve(passphrase_reference)
    passphrase_bytes = _encode_passphrase(passphrase)
    header = EnvelopeHeader()
    manifest = EnvelopeManifest(
        header=header,
        signer_fingerprint=signer,
        recipient_fingerprints=recipients,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        payload_length=len(payload),
    )
    signed_inner = _encode_inner_message(manifest, payload)

    arguments: list[str] = [
        "--pinentry-mode",
        "loopback",
        "--passphrase-fd",
        "{passphrase_fd}",
        "--no-encrypt-to",
        "--trust-model",
        "always",
        "--no-emit-version",
        "--no-comments",
        "--set-filename",
        "",
        "--throw-keyids",
        "--no-armor",
        "--local-user",
        signer,
    ]
    for recipient in recipients:
        arguments.extend(("--recipient", recipient))
    arguments.extend(("--output", "-", "--sign", "--encrypt", "-"))
    ciphertext, _diagnostics = _run_gpg(
        executable,
        home,
        tuple(arguments),
        stdin_data=signed_inner,
        passphrase=passphrase_bytes,
        failure_code=EnvelopeErrorCode.SIGN_ENCRYPT_FAILED,
    )
    _validate_hidden_recipient_packets(
        ciphertext,
        recipient_count=len(recipients),
        executable=executable,
        home=home,
    )
    return _encode_envelope(header, ciphertext)


def parse_envelope(serialized: bytes) -> ParsedEnvelope:
    """Parse bounded outer framing without decrypting or inferring identities."""
    if type(serialized) is not bytes or len(serialized) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_ENVELOPE)
    if len(serialized) < 2:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_ENVELOPE)
    header_length = struct.unpack_from(">H", serialized)[0]
    if header_length == 0 or header_length > _MAX_HEADER_BYTES:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_ENVELOPE)
    header_end = 2 + header_length
    if header_end >= len(serialized):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_ENVELOPE)
    try:
        header_json = serialized[2:header_end].decode("utf-8")
        header = EnvelopeHeader.from_json(header_json)
        if header_json != header.to_json():
            raise ValueError
    except (UnicodeDecodeError, OpenPGPContractError, ValueError):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_ENVELOPE) from None
    return ParsedEnvelope(header=header, ciphertext=serialized[header_end:])


def parse_inner_message(verified_plaintext: bytes) -> ParsedInnerMessage:
    """Parse an inner message after L08 has verified its OpenPGP signature.

    This function validates framing, canonical metadata, the signed header and
    payload digest/length, but performs no cryptographic verification. Callers
    must not use its returned payload unless signature and participant checks
    have already succeeded.
    """
    if type(verified_plaintext) is not bytes or len(verified_plaintext) > (
        MAX_PAYLOAD_BYTES + _MAX_MANIFEST_BYTES + len(_INNER_MAGIC) + 4
    ):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    prefix_length = len(_INNER_MAGIC) + 4
    if len(verified_plaintext) < prefix_length or not verified_plaintext.startswith(
        _INNER_MAGIC
    ):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    manifest_length = struct.unpack_from(">I", verified_plaintext, len(_INNER_MAGIC))[0]
    if manifest_length == 0 or manifest_length > _MAX_MANIFEST_BYTES:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    payload_start = prefix_length + manifest_length
    if payload_start > len(verified_plaintext):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    manifest_bytes = verified_plaintext[prefix_length:payload_start]
    manifest = _parse_manifest(manifest_bytes)
    payload = verified_plaintext[payload_start:]
    if manifest.payload_length != len(payload):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    if hashlib.sha256(payload).hexdigest() != manifest.payload_sha256:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    return ParsedInnerMessage(manifest=manifest, payload=payload)


def _validated_gnupg_home(value: str | os.PathLike[str]) -> Path:
    try:
        selected = Path(value)
        if not selected.is_absolute() or selected.is_symlink():
            raise ValueError
        home = selected.resolve(strict=True)
        metadata = home.stat()
    except (OSError, TypeError, ValueError, RuntimeError):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_GPG_HOME) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_uid != os.geteuid()
    ):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_GPG_HOME)
    return home

def _encode_passphrase(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT) from None
    if len(encoded) > _MAX_PASSPHRASE_BYTES:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)
    return encoded


def _encode_inner_message(manifest: EnvelopeManifest, payload: bytes) -> bytes:
    manifest_bytes = json.dumps(
        {
            "header": manifest.header.to_json(),
            "payload_length": manifest.payload_length,
            "payload_sha256": manifest.payload_sha256,
            "recipient_fingerprints": list(manifest.recipient_fingerprints),
            "signer_fingerprint": manifest.signer_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return (
        _INNER_MAGIC
        + struct.pack(">I", len(manifest_bytes))
        + manifest_bytes
        + payload
    )


def _parse_manifest(serialized: bytes) -> EnvelopeManifest:
    try:
        text = serialized.decode("utf-8")
        obj = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        expected_keys = {
            "header",
            "payload_length",
            "payload_sha256",
            "recipient_fingerprints",
            "signer_fingerprint",
        }
        if type(obj) is not dict or set(obj) != expected_keys:
            raise ValueError
        header_json = obj["header"]
        if not isinstance(header_json, str):
            raise ValueError
        header = EnvelopeHeader.from_json(header_json)
        if header_json != header.to_json():
            raise ValueError
        signer_value = obj["signer_fingerprint"]
        signer = normalize_fingerprint(signer_value)
        if signer_value != signer:
            raise ValueError
        recipient_values = obj["recipient_fingerprints"]
        if (
            type(recipient_values) is not list
            or not recipient_values
            or len(recipient_values) > _MAX_RECIPIENTS
        ):
            raise ValueError
        recipients = tuple(normalize_fingerprint(item) for item in recipient_values)
        if (
            recipients != tuple(sorted(set(recipients)))
            or any(
                item != normalized
                for item, normalized in zip(recipient_values, recipients)
            )
        ):
            raise ValueError
        payload_sha256 = obj["payload_sha256"]
        if (
            not isinstance(payload_sha256, str)
            or _SHA256_RE.fullmatch(payload_sha256) is None
        ):
            raise ValueError
        payload_length = obj["payload_length"]
        if (
            type(payload_length) is not int
            or payload_length < 0
            or payload_length > MAX_PAYLOAD_BYTES
        ):
            raise ValueError
        canonical = json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if canonical != serialized:
            raise ValueError
        return EnvelopeManifest(
            header=header,
            signer_fingerprint=signer,
            recipient_fingerprints=recipients,
            payload_sha256=payload_sha256,
            payload_length=payload_length,
        )
    except (
        UnicodeDecodeError,
        OpenPGPContractError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INNER_MESSAGE) from None


def _encode_envelope(header: EnvelopeHeader, ciphertext: bytes) -> bytes:
    header_bytes = header.to_json().encode("utf-8")
    if (
        not ciphertext
        or len(header_bytes) > _MAX_HEADER_BYTES
        or len(ciphertext) + 2 + len(header_bytes) > MAX_ENVELOPE_BYTES
    ):
        raise EnvelopeError(EnvelopeErrorCode.GPG_OUTPUT_LIMIT)
    return struct.pack(">H", len(header_bytes)) + header_bytes + ciphertext


def _validate_hidden_recipient_packets(
    ciphertext: bytes,
    *,
    recipient_count: int,
    executable: str,
    home: Path,
) -> None:
    listing_stdout, listing_stderr = _run_gpg(
        executable,
        home,
        ("--list-only", "--list-packets", "-"),
        stdin_data=ciphertext,
        failure_code=EnvelopeErrorCode.MALFORMED_CIPHERTEXT,
    )
    try:
        listing = (listing_stdout + b"\n" + listing_stderr).decode("utf-8")
    except UnicodeDecodeError:
        raise EnvelopeError(EnvelopeErrorCode.MALFORMED_CIPHERTEXT) from None
    lines = listing.splitlines()
    if any(_MALFORMED_PACKET_RE.search(line) for line in lines):
        raise EnvelopeError(EnvelopeErrorCode.MALFORMED_CIPHERTEXT)
    packet_lines = [
        match.group(1)
        for line in lines
        if (match := _PACKET_LINE_RE.match(line)) is not None
    ]
    if len(packet_lines) != recipient_count:
        raise EnvelopeError(EnvelopeErrorCode.MALFORMED_CIPHERTEXT)
    for packet_line in packet_lines:
        version_match = _PACKET_VERSION_RE.search(packet_line)
        key_id_match = _PACKET_KEY_ID_RE.search(packet_line)
        if (
            version_match is None
            or version_match.group(1) != "3"
            or key_id_match is None
            or key_id_match.group(1) != "0000000000000000"
        ):
            raise EnvelopeError(EnvelopeErrorCode.MALFORMED_CIPHERTEXT)
    encrypted_packet_count = sum(
        _ENCRYPTED_PACKET_RE.match(line) is not None for line in lines
    )
    if (
        any(_SYMMETRIC_PACKET_RE.match(line) for line in lines)
        or encrypted_packet_count != 1
    ):
        raise EnvelopeError(EnvelopeErrorCode.MALFORMED_CIPHERTEXT)


def _run_gpg(
    executable: str,
    home: Path,
    arguments: tuple[str, ...],
    *,
    stdin_data: bytes,
    passphrase: bytes | None = None,
    failure_code: EnvelopeErrorCode,
) -> tuple[bytes, bytes]:
    passphrase_file = tempfile.TemporaryFile(mode="w+b") if passphrase is not None else None
    pass_fds: tuple[int, ...] = ()
    rendered_arguments = tuple(arguments)
    if passphrase_file is not None:
        if "{passphrase_fd}" not in rendered_arguments:
            passphrase_file.close()
            raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)
        assert passphrase is not None
        passphrase_file.write(passphrase + b"\n")
        passphrase_file.flush()
        passphrase_file.seek(0)
        descriptor = passphrase_file.fileno()
        rendered_arguments = tuple(
            str(descriptor) if item == "{passphrase_fd}" else item
            for item in rendered_arguments
        )
        pass_fds = (descriptor,)
    elif "{passphrase_fd}" in rendered_arguments:
        raise EnvelopeError(EnvelopeErrorCode.INVALID_INPUT)

    command = [
        executable,
        "--no-options",
        "--batch",
        "--no-tty",
        "--no-autostart",
        "--no-auto-key-retrieve",
        "--no-auto-key-locate",
        "--no-auto-key-import",
        "--no-auto-check-trustdb",
        "--homedir",
        os.fspath(home),
        *rendered_arguments,
    ]
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GNUPGHOME": os.fspath(home),
        "LC_ALL": "C",
        "LANG": "C",
    }

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=pass_fds,
            env=environment,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        if passphrase_file is not None:
            passphrase_file.close()
        raise EnvelopeError(failure_code) from None

    output = bytearray()
    diagnostics = bytearray()
    input_offset = 0
    deadline = time.monotonic() + _GPG_TIMEOUT_SECONDS
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if stdin_data:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EnvelopeError(EnvelopeErrorCode.GPG_TIMEOUT)
                events = selector.select(remaining)
                if not events:
                    raise EnvelopeError(EnvelopeErrorCode.GPG_TIMEOUT)
                for key, _mask in events:
                    if key.data == "stdin":
                        try:
                            written = os.write(
                                process.stdin.fileno(),
                                stdin_data[input_offset : input_offset + _GPG_READ_SIZE],
                            )
                        except BrokenPipeError:
                            written = 0
                            input_offset = -1
                        if written > 0:
                            input_offset += written
                        if input_offset < 0 or input_offset >= len(stdin_data):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    pipe = process.stdout if key.data == "stdout" else process.stderr
                    chunk = os.read(pipe.fileno(), _GPG_READ_SIZE)
                    if not chunk:
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    if key.data == "stdout":
                        output.extend(chunk)
                    else:
                        diagnostics.extend(chunk)
                    if len(output) + len(diagnostics) > MAX_ENVELOPE_BYTES:
                        raise EnvelopeError(EnvelopeErrorCode.GPG_OUTPUT_LIMIT)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EnvelopeError(EnvelopeErrorCode.GPG_TIMEOUT)
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise EnvelopeError(EnvelopeErrorCode.GPG_TIMEOUT) from None
        if return_code != 0 or input_offset < 0 or input_offset < len(stdin_data):
            raise EnvelopeError(failure_code)
        return bytes(output), bytes(diagnostics)
    except EnvelopeError:
        _terminate(process)
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        _terminate(process)
        raise EnvelopeError(failure_code) from None
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass
        if passphrase_file is not None:
            passphrase_file.close()


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError
