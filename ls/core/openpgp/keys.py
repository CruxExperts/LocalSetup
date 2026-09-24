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
from pathlib import Path
from typing import Final

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
from .key_records import (
    KeyEnrollment,
    KeyInspection,
    KeyInspectionError,
    KeyInspectionErrorCode,
    KeyRecord,
    _INSPECTION_SEAL,
    _parse_colon_output,
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

for _public_type in (
    KeyEnrollment,
    KeyInspection,
    KeyInspectionError,
    KeyInspectionErrorCode,
    KeyRecord,
):
    _public_type.__module__ = __name__

_MAX_CERTIFICATE_BYTES: Final = 1_048_576
_MAX_KEYRING_BYTES: Final = 16_777_216
_MAX_GPG_OUTPUT_BYTES: Final = 1_048_576
_GPG_TIMEOUT_SECONDS: Final = 10.0
_GPG_READ_SIZE: Final = 8192


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
