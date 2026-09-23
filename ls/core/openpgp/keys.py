"""Isolated OpenPGP public-key inspection and explicit local enrollment.

Inspection never consults remote discovery or mutates the caller's keyring.
Enrollment returns a new immutable :class:`LocalTrust`; callers own durable
persistence of its fingerprint set.
"""

from __future__ import annotations

import os
import selectors
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final, TypedDict

from .contracts import (
    ContractErrorCode,
    KeyCapability,
    KeyProfile,
    LocalTrust,
    OpenPGPContractError,
    normalize_fingerprint,
    require_capabilities,
    validate_key_profile,
)

__all__ = [
    "KeyEnrollment",
    "KeyInspection",
    "KeyInspectionError",
    "KeyInspectionErrorCode",
    "KeyRecord",
    "enroll_key",
    "inspect_key",
]

_MAX_CERTIFICATE_BYTES: Final = 1_048_576
_MAX_KEYRING_BYTES: Final = 16_777_216
_MAX_GPG_OUTPUT_BYTES: Final = 1_048_576
_GPG_TIMEOUT_SECONDS: Final = 10.0
_GPG_READ_SIZE: Final = 8192
_ALGORITHMS: Final = {
    "1": "RSA",
    "2": "RSA",
    "3": "RSA",
    "16": "ELGAMAL",
    "17": "DSA",
    "18": "ECDH",
    "19": "ECDSA",
    "20": "ELGAMAL",
    "22": "EDDSA",
    "25": "X25519",
    "26": "X448",
    "27": "ED25519",
    "28": "ED448",
}
_CAPABILITIES: Final = {
    "e": KeyCapability.ENCRYPT,
    "s": KeyCapability.SIGN,
    "c": KeyCapability.CERTIFY,
    "a": KeyCapability.AUTHENTICATE,
}
_INSPECTION_SEAL: Final = object()

class _ParsedFields(TypedDict):
    algorithm: str
    bits: int
    capabilities: frozenset[KeyCapability]
    direct_capabilities: frozenset[KeyCapability]
    aggregate_capabilities: frozenset[KeyCapability]
    created: int
    expires: int | None
    is_primary: bool
    revoked: bool
    expired: bool
    disabled: bool
    invalid: bool


class KeyInspectionErrorCode(StrEnum):
    """Stable failures that do not expose certificate or process contents."""

    INVALID_SOURCE = "INVALID_SOURCE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_OUTPUT_LIMIT = "SOURCE_OUTPUT_LIMIT"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    INVALID_KEY = "INVALID_KEY"
    AMBIGUOUS_KEY = "AMBIGUOUS_KEY"
    KEY_NOT_FOUND = "KEY_NOT_FOUND"
    KEYRING_UNAVAILABLE = "KEYRING_UNAVAILABLE"
    UNSUPPORTED_KEYRING = "UNSUPPORTED_KEYRING"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    REVOKED_KEY = "REVOKED_KEY"
    EXPIRED_KEY = "EXPIRED_KEY"
    DISABLED_KEY = "DISABLED_KEY"
    INVALID_CAPABILITIES = "INVALID_CAPABILITIES"
    INVALID_ENROLLMENT = "INVALID_ENROLLMENT"


class KeyInspectionError(RuntimeError):
    """Safe key inspection failure with a stable code and no external details."""

    def __init__(self, code: KeyInspectionErrorCode) -> None:
        self.code = KeyInspectionErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """One primary key or subkey as reported by GnuPG's colon format."""

    fingerprint: str
    algorithm: str
    bits: int
    capabilities: frozenset[KeyCapability]
    created_on: date
    expires_on: date | None
    is_primary: bool
    expires_epoch: int | None = None
    revoked: bool = False
    expired: bool = False
    disabled: bool = False
    direct_capabilities: frozenset[KeyCapability] = field(
        default=frozenset(), repr=False, compare=False
    )
    aggregate_capabilities: frozenset[KeyCapability] = field(
        default=frozenset(), repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class KeyInspection:
    """Parsed certificate facts; it conveys no signer trust by itself."""

    primary: KeyRecord
    subkeys: tuple[KeyRecord, ...]
    _verification: object = field(default=None, repr=False, compare=False)

    @property
    def primary_fingerprint(self) -> str:
        return self.primary.fingerprint

    @property
    def records(self) -> tuple[KeyRecord, ...]:
        return (self.primary, *self.subkeys)

    @property
    def capabilities(self) -> frozenset[KeyCapability]:
        return frozenset(
            capability
            for record in self.records
            if not record.revoked and not record.expired and not record.disabled
            for capability in record.direct_capabilities
        )

    @property
    def signing_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.records
            if KeyCapability.SIGN in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def encryption_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.records
            if KeyCapability.ENCRYPT in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def signing_subkey_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.subkeys
            if KeyCapability.SIGN in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def encryption_subkey_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.subkeys
            if KeyCapability.ENCRYPT in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def revoked_fingerprints(self) -> tuple[str, ...]:
        return tuple(record.fingerprint for record in self.records if record.revoked)

    @property
    def expired_fingerprints(self) -> tuple[str, ...]:
        return tuple(record.fingerprint for record in self.records if record.expired)

    @property
    def disabled_fingerprints(self) -> tuple[str, ...]:
        return tuple(record.fingerprint for record in self.records if record.disabled)

    @property
    def revoked(self) -> bool:
        return bool(self.revoked_fingerprints)

    @property
    def expired(self) -> bool:
        return bool(self.expired_fingerprints)

    @property
    def disabled(self) -> bool:
        return bool(self.disabled_fingerprints)


@dataclass(frozen=True, slots=True)
class KeyEnrollment:
    """Explicit enrollment result; persist ``local_trust.fingerprints`` yourself."""

    fingerprint: str
    local_trust: LocalTrust
    required_capabilities: frozenset[KeyCapability]
    profile: KeyProfile


def inspect_key(
    *,
    certificate: bytes | str | None = None,
    keyring_home: str | os.PathLike[str] | None = None,
    fingerprint: str | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> KeyInspection:
    """Inspect a public key without retaining rejected input in failure frames."""
    try:
        return _inspect_key_impl(
            certificate=certificate,
            keyring_home=keyring_home,
            fingerprint=fingerprint,
            gpg_binary=gpg_binary,
        )
    except KeyInspectionError as exc:
        code = exc.code
    except OpenPGPContractError as exc:
        code = exc.code
    except Exception:
        code = KeyInspectionErrorCode.SOURCE_UNAVAILABLE
    del certificate, keyring_home, fingerprint, gpg_binary
    if isinstance(code, ContractErrorCode):
        raise OpenPGPContractError(code)
    raise KeyInspectionError(code)


def _inspect_key_impl(
    *,
    certificate: bytes | str | None = None,
    keyring_home: str | os.PathLike[str] | None = None,
    fingerprint: str | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> KeyInspection:
    """Inspect one armored public certificate or an explicitly selected keyring key.

    Certificate parsing and import use only a disposable, private GnuPG home.
    Local keyring reads snapshot only a bounded ``pubring.kbx``; legacy pubring
    and keyboxd stores are rejected. No source trust database, secret-key store,
    agent, ambient GnuPG home, or network lookup is used.
    """
    from_certificate = certificate is not None
    from_keyring = keyring_home is not None
    if from_certificate == from_keyring:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)

    selected_fingerprint: str | None = None
    if from_keyring:
        if fingerprint is None:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
        selected_fingerprint = normalize_fingerprint(fingerprint)
    elif fingerprint is not None:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)

    try:
        executable = os.fspath(gpg_binary)
    except (TypeError, ValueError):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE) from None
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)

    source_keyring: Path | None = None
    if from_keyring:
        try:
            source_keyring = Path(keyring_home)
        except (TypeError, ValueError):
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE) from None

    try:
        with tempfile.TemporaryDirectory(prefix="localsetup-openpgp-") as temporary_home:
            home = Path(temporary_home)
            os.chmod(home, 0o700)
            if from_certificate:
                public_certificate = _validated_public_certificate(certificate)
                certificate_path = home / "certificate.asc"
                _write_private_file(certificate_path, public_certificate)
                packet_output = _run_gpg(
                    executable,
                    home,
                    ("--list-packets", os.fspath(certificate_path)),
                    include_stderr=True,
                )
                packet_text = packet_output.lower()
                if any(
                    b"secret" in line and b"key packet" in line
                    for line in packet_text.splitlines()
                ):
                    raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
                _run_gpg(
                    executable,
                    home,
                    ("--import", os.fspath(certificate_path)),
                )
                secret_output = _run_gpg(
                    executable,
                    home,
                    (
                        "--with-colons",
                        "--fixed-list-mode",
                        "--list-secret-keys",
                    ),
                )
                if any(
                    line.startswith((b"sec:", b"ssb:"))
                    for line in secret_output.splitlines()
                ):
                    raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
            else:
                if source_keyring is None:
                    raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
                _copy_public_keybox(source_keyring, home)

            output = _run_gpg(
                executable,
                home,
                (
                    "--with-colons",
                    "--fixed-list-mode",
                    "--with-fingerprint",
                    "--with-subkey-fingerprint",
                    "--list-keys",
                ),
            )
    except KeyInspectionError:
        raise
    except (OSError, ValueError):
        raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_UNAVAILABLE) from None

    parsed = _parse_colon_output(output)
    if selected_fingerprint is not None:
        matching = tuple(
            inspection
            for inspection in parsed
            if inspection.primary_fingerprint == selected_fingerprint
        )
        if not matching:
            raise KeyInspectionError(KeyInspectionErrorCode.KEY_NOT_FOUND)
        if len(matching) != 1:
            raise KeyInspectionError(KeyInspectionErrorCode.AMBIGUOUS_KEY)
        return matching[0]
    if not parsed:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    if len(parsed) != 1:
        raise KeyInspectionError(KeyInspectionErrorCode.AMBIGUOUS_KEY)
    return parsed[0]


def enroll_key(
    inspection: KeyInspection,
    *,
    expected_fingerprint: str,
    required_capabilities: Iterable[KeyCapability | str],
    profile: KeyProfile,
    local_trust: LocalTrust,
) -> KeyEnrollment:
    """Explicitly pin an inspected key after identity, usage, and profile checks.

    This does not write files or trust remote discovery. The returned
    ``LocalTrust`` is the new authority; its immutable ``fingerprints`` collection
    is what the consumer must persist in its own explicitly chosen durable store.
    Existing authority instances remain unchanged.
    """
    if (
        not isinstance(inspection, KeyInspection)
        or inspection._verification is not _INSPECTION_SEAL
        or not isinstance(profile, KeyProfile)
        or not isinstance(local_trust, LocalTrust)
    ):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_ENROLLMENT)
    expected = normalize_fingerprint(expected_fingerprint)
    if expected != inspection.primary_fingerprint:
        raise KeyInspectionError(KeyInspectionErrorCode.FINGERPRINT_MISMATCH)
    if inspection.primary.revoked:
        raise KeyInspectionError(KeyInspectionErrorCode.REVOKED_KEY)
    now = int(time.time())
    if _record_expired(inspection.primary, now):
        raise KeyInspectionError(KeyInspectionErrorCode.EXPIRED_KEY)
    if inspection.primary.disabled:
        raise KeyInspectionError(KeyInspectionErrorCode.DISABLED_KEY)

    requested = _requested_capabilities(required_capabilities)
    usable_capabilities: set[KeyCapability] = set()
    selected_records: dict[str, KeyRecord] = {
        inspection.primary.fingerprint: inspection.primary
    }
    for capability in (KeyCapability.SIGN, KeyCapability.ENCRYPT):
        if capability not in requested:
            continue
        candidates = tuple(
            record
            for record in inspection.records
            if capability in record.direct_capabilities
        )
        usable = tuple(
            record
            for record in candidates
            if not record.revoked and not _record_expired(record, now) and not record.disabled
        )
        if not usable:
            if any(record.revoked for record in candidates):
                raise KeyInspectionError(KeyInspectionErrorCode.REVOKED_KEY)
            if any(_record_expired(record, now) for record in candidates):
                raise KeyInspectionError(KeyInspectionErrorCode.EXPIRED_KEY)
            if any(record.disabled for record in candidates):
                raise KeyInspectionError(KeyInspectionErrorCode.DISABLED_KEY)
        else:
            usable_capabilities.add(capability)
            selected_records.update(
                (record.fingerprint, record) for record in usable
            )
    require_capabilities(usable_capabilities, requested)
    for record in selected_records.values():
        if record.expires_on is None:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
        validate_key_profile(
            record.algorithm,
            record.bits,
            record.created_on,
            record.expires_on,
            profile=profile,
        )
    now = int(time.time())
    if any(_record_expired(record, now) for record in selected_records.values()):
        raise KeyInspectionError(KeyInspectionErrorCode.EXPIRED_KEY)
    updated = LocalTrust(frozenset((*local_trust.fingerprints, expected)))
    return KeyEnrollment(
        fingerprint=expected,
        local_trust=updated,
        required_capabilities=requested,
        profile=profile,
    )


def _record_expired(record: KeyRecord, now: int) -> bool:
    return record.expired or (
        record.expires_epoch is not None and record.expires_epoch <= now
    )


def _requested_capabilities(
    values: Iterable[KeyCapability | str],
) -> frozenset[KeyCapability]:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_ENROLLMENT)
    try:
        selected = frozenset(KeyCapability(value) for value in values)
    except (TypeError, ValueError):
        raise KeyInspectionError(
            KeyInspectionErrorCode.INVALID_ENROLLMENT
        ) from None
    if not selected or not selected.issubset(
        {KeyCapability.SIGN, KeyCapability.ENCRYPT}
    ):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_ENROLLMENT)
    return selected


def _validated_public_certificate(value: bytes | str | None) -> bytes:
    if isinstance(value, str):
        if not value or len(value) > _MAX_CERTIFICATE_BYTES:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
        try:
            contents = value.encode("ascii")
        except UnicodeEncodeError:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE) from None
    elif isinstance(value, bytes):
        contents = value
    else:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
    if not contents or len(contents) > _MAX_CERTIFICATE_BYTES:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
    try:
        armor = contents.decode("ascii")
    except UnicodeDecodeError:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE) from None
    public_begin = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
    public_end = "-----END PGP PUBLIC KEY BLOCK-----"
    armor_lines = armor.splitlines()
    if (
        armor_lines.count(public_begin) != 1
        or armor_lines.count(public_end) != 1
        or any(
            line.startswith("-----BEGIN PGP ") and line != public_begin
            for line in armor_lines
        )
    ):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
    return contents


def _copy_public_keybox(source_home: Path, destination_home: Path) -> None:
    if not source_home.is_dir():
        raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_UNAVAILABLE)
    if (source_home / "public-keys.d").exists() or (source_home / "pubring.gpg").exists():
        raise KeyInspectionError(KeyInspectionErrorCode.UNSUPPORTED_KEYRING)
    candidate = source_home / "pubring.kbx"
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        raise KeyInspectionError(KeyInspectionErrorCode.KEYRING_UNAVAILABLE)
    except OSError:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_SOURCE)
        if metadata.st_size > _MAX_KEYRING_BYTES:
            raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_OUTPUT_LIMIT)
        data = bytearray()
        while len(data) <= _MAX_KEYRING_BYTES:
            chunk = os.read(
                descriptor,
                min(_GPG_READ_SIZE, _MAX_KEYRING_BYTES + 1 - len(data)),
            )
            if not chunk:
                break
            data.extend(chunk)
        if not data or len(data) > _MAX_KEYRING_BYTES:
            raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_OUTPUT_LIMIT)
    except OSError:
        raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_UNAVAILABLE) from None
    finally:
        os.close(descriptor)
    _write_private_file(destination_home / "pubring.kbx", bytes(data))


def _write_private_file(path: Path, contents: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _run_gpg(
    executable: str,
    home: Path,
    arguments: tuple[str, ...],
    *,
    include_stderr: bool = False,
) -> bytes:
    command = [
        executable,
        "--no-options",
        "--batch",
        "--no-tty",
        "--no-autostart",
        "--no-auto-key-retrieve",
        "--no-auto-check-trustdb",
        "--homedir",
        os.fspath(home),
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except (OSError, ValueError):
        raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_UNAVAILABLE) from None
    assert process.stdout is not None and process.stderr is not None
    output = bytearray()
    diagnostics = bytearray()
    total = 0
    deadline = time.monotonic() + _GPG_TIMEOUT_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_TIMEOUT)
                events = selector.select(remaining)
                if not events:
                    raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_TIMEOUT)
                for key, _ in events:
                    pipe = process.stdout if key.fileobj is process.stdout else process.stderr
                    chunk = os.read(pipe.fileno(), _GPG_READ_SIZE)
                    if not chunk:
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    total += len(chunk)
                    if total > _MAX_GPG_OUTPUT_BYTES:
                        raise KeyInspectionError(
                            KeyInspectionErrorCode.SOURCE_OUTPUT_LIMIT
                        )
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
                    elif include_stderr:
                        diagnostics.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_TIMEOUT)
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_TIMEOUT) from None
        if return_code != 0:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    except KeyInspectionError:
        _terminate(process)
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        _terminate(process)
        raise KeyInspectionError(KeyInspectionErrorCode.SOURCE_UNAVAILABLE) from None
    finally:
        for pipe in (process.stdout, process.stderr):
            if not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass
    return bytes(output + diagnostics)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _parse_colon_output(output: bytes) -> tuple[KeyInspection, ...]:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None
    records: list[KeyInspection] = []
    components: list[KeyRecord] = []
    pending: _ParsedFields | None = None
    current_fingerprint: str | None = None
    now = int(datetime.now(timezone.utc).timestamp())

    def finish_component() -> None:
        nonlocal pending, current_fingerprint
        if pending is None:
            return
        if current_fingerprint is None:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
        components.append(_record_from_fields(pending, current_fingerprint))
        pending = None
        current_fingerprint = None

    def finish_certificate() -> None:
        finish_component()
        if not components:
            return
        if not components[0].is_primary or any(
            component.is_primary for component in components[1:]
        ):
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
        records.append(
            KeyInspection(
                components[0],
                tuple(components[1:]),
                _verification=_INSPECTION_SEAL,
            )
        )
        components.clear()

    for line in text.splitlines():
        fields = line.split(":")
        kind = fields[0]
        if kind in {"sec", "ssb"}:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
        if kind == "pub":
            finish_certificate()
            pending = _parse_key_fields(fields, is_primary=True, now=now)
        elif kind == "sub":
            finish_component()
            if not components or pending is not None:
                raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
            pending = _parse_key_fields(fields, is_primary=False, now=now)
        elif kind == "fpr":
            if pending is None or current_fingerprint is not None or len(fields) <= 9:
                raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
            try:
                current_fingerprint = normalize_fingerprint(fields[9])
            except OpenPGPContractError:
                raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None
    finish_certificate()
    return tuple(records)


def _parse_key_fields(
    fields: list[str], *, is_primary: bool, now: int
) -> _ParsedFields:
    if len(fields) <= 11:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    try:
        bits = int(fields[2], 10)
        algorithm = _ALGORITHMS[fields[3]]
        created = int(fields[5], 10)
        expires = int(fields[6], 10) if fields[6] else None
    except (KeyError, ValueError):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None
    if bits < 1 or created < 1 or (expires is not None and expires < 1):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    capabilities, direct_capabilities, aggregate_capabilities = _parse_capabilities(
        fields[11]
    )
    validity = fields[1].casefold()
    return {
        "algorithm": algorithm,
        "bits": bits,
        "capabilities": capabilities,
        "direct_capabilities": direct_capabilities,
        "aggregate_capabilities": aggregate_capabilities,
        "created": created,
        "expires": expires,
        "is_primary": is_primary,
        "revoked": validity == "r",
        "expired": validity == "e" or (expires is not None and expires <= now),
        "disabled": validity == "d",
        "invalid": validity == "i",
    }


def _record_from_fields(fields: _ParsedFields, fingerprint: str) -> KeyRecord:
    if fields["invalid"]:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    created = _timestamp_date(fields["created"])
    expires_epoch = fields["expires"]
    expires_on = None if expires_epoch is None else _timestamp_date(expires_epoch)
    return KeyRecord(
        fingerprint=fingerprint,
        algorithm=fields["algorithm"],
        bits=fields["bits"],
        capabilities=fields["capabilities"],
        direct_capabilities=fields["direct_capabilities"],
        aggregate_capabilities=fields["aggregate_capabilities"],
        created_on=created,
        expires_on=expires_on,
        is_primary=bool(fields["is_primary"]),
        expires_epoch=expires_epoch,
        revoked=bool(fields["revoked"]),
        expired=bool(fields["expired"]),
        disabled=bool(fields["disabled"]),
    )


def _timestamp_date(value: object) -> date:
    if type(value) is not int:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    try:
        return datetime.fromtimestamp(value, timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None


def _parse_capabilities(
    value: str,
) -> tuple[
    frozenset[KeyCapability],
    frozenset[KeyCapability],
    frozenset[KeyCapability],
]:
    direct: set[KeyCapability] = set()
    aggregate: set[KeyCapability] = set()
    for character in value:
        capability = _CAPABILITIES.get(character.casefold())
        if (
            capability is None
            or not character.isascii()
            or not (character.islower() or character.isupper())
        ):
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_CAPABILITIES)
        (direct if character.islower() else aggregate).add(capability)
    direct_capabilities = frozenset(direct)
    aggregate_capabilities = frozenset(aggregate)
    return (
        direct_capabilities | aggregate_capabilities,
        direct_capabilities,
        aggregate_capabilities,
    )
