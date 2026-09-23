"""Protected OpenPGP backup and isolated recovery using explicit key pins.

Secret-key packets are relayed between GnuPG processes through bounded pipes;
this module never writes an unencrypted export package to a file.
"""

from __future__ import annotations

import os
import re
import selectors
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final, Iterable, Iterator, cast

from .contracts import (
    KeyCapability,
    KeyProfile,
    LocalTrust,
    OpenPGPContractError,
    normalize_fingerprint,
    require_capabilities,
)
from .keys import (
    KeyInspection,
    KeyInspectionError,
    enroll_key,
    inspect_key,
)
from .secrets import SecretReference, SecretResolver

__all__ = [
    "ProtectedBackup",
    "RecoveredKey",
    "RecoveryError",
    "RecoveryErrorCode",
    "create_protected_backup",
    "restore_protected_backup",
]

_MAX_PASSPHRASE_BYTES: Final = 4096
_MAX_PLAINTEXT_BYTES: Final = 16_777_216
_MAX_BACKUP_BYTES: Final = 16_777_216
_MAX_DIAGNOSTIC_BYTES: Final = 1_048_576
_GPG_TIMEOUT_SECONDS: Final = 30.0
_GPG_READ_SIZE: Final = 8192


class RecoveryErrorCode(StrEnum):
    """Stable failures without key material, identity text, or diagnostics."""

    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_FINGERPRINT = "INVALID_FINGERPRINT"
    INVALID_KEYRING_HOME = "INVALID_KEYRING_HOME"
    KEYRING_NOT_EMPTY = "KEYRING_NOT_EMPTY"
    KEYRING_UNAVAILABLE = "KEYRING_UNAVAILABLE"
    HOMES_NOT_ISOLATED = "HOMES_NOT_ISOLATED"
    OWNER_KEY_INVALID = "OWNER_KEY_INVALID"
    OWNER_POLICY_INVALID = "OWNER_POLICY_INVALID"
    RECOVERY_KEY_INVALID = "RECOVERY_KEY_INVALID"
    SECRET_RESOLUTION_FAILED = "SECRET_RESOLUTION_FAILED"
    INVALID_PASSPHRASE = "INVALID_PASSPHRASE"
    GPG_UNAVAILABLE = "GPG_UNAVAILABLE"
    GPG_FAILED = "GPG_FAILED"
    GPG_TIMEOUT = "GPG_TIMEOUT"
    IO_LIMIT = "IO_LIMIT"
    BACKUP_INVALID = "BACKUP_INVALID"
    BACKUP_EXISTS = "BACKUP_EXISTS"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    RESTORE_INVALID = "RESTORE_INVALID"


class RecoveryError(RuntimeError):
    """A redacted recovery failure with a stable non-secret code."""

    def __init__(self, code: RecoveryErrorCode) -> None:
        self.code = RecoveryErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class ProtectedBackup:
    """Non-secret receipt for a newly created encrypted backup."""

    path: Path
    primary_fingerprint: str
    recovery_recipient_fingerprint: str


@dataclass(frozen=True, slots=True)
class RecoveredKey:
    """Non-secret result for a key verified in the caller-selected home."""

    keyring_home: Path
    primary_fingerprint: str
    recovery_recipient_fingerprint: str


def create_protected_backup(
    *,
    keyring_home: str | os.PathLike[str],
    expected_primary_fingerprint: str,
    recovery_public_keyring_home: str | os.PathLike[str],
    recovery_recipient_fingerprint: str,
    owner_passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    backup_path: str | os.PathLike[str],
    profile: KeyProfile,
    required_capabilities: Iterable[KeyCapability | str],
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> ProtectedBackup:
    """Create ciphertext for a public-only, independently held recovery key.

    Owner and recovery identities are full-fingerprint pinned. The owner policy
    is selected by the caller, not inferred. The owner's passphrase is resolved
    explicitly and sent only through an inherited pipe. The recovery keyring
    must contain no secret keys; its corresponding private key is needed only
    for restore. ``backup_path`` must not exist and is created mode ``0600``.

    The selected homes must not be used concurrently by other same-UID
    processes.

    GnuPG's ``always`` encryption trust model is used only after the caller
    explicitly pins the recipient. No trust record is written or changed.
    """
    try:
        return _create_protected_backup(
            keyring_home=keyring_home,
            expected_primary_fingerprint=expected_primary_fingerprint,
            recovery_public_keyring_home=recovery_public_keyring_home,
            recovery_recipient_fingerprint=recovery_recipient_fingerprint,
            owner_passphrase_reference=owner_passphrase_reference,
            secret_resolver=secret_resolver,
            backup_path=backup_path,
            profile=profile,
            required_capabilities=required_capabilities,
            gpg_binary=gpg_binary,
        )
    except RecoveryError as exc:
        code = exc.code
    except Exception:
        code = RecoveryErrorCode.INVALID_REQUEST
    del (
        keyring_home,
        expected_primary_fingerprint,
        recovery_public_keyring_home,
        recovery_recipient_fingerprint,
        owner_passphrase_reference,
        secret_resolver,
        backup_path,
        profile,
        required_capabilities,
        gpg_binary,
    )
    raise RecoveryError(code) from None


def _create_protected_backup(
    *,
    keyring_home: str | os.PathLike[str],
    expected_primary_fingerprint: str,
    recovery_public_keyring_home: str | os.PathLike[str],
    recovery_recipient_fingerprint: str,
    owner_passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    backup_path: str | os.PathLike[str],
    profile: KeyProfile,
    required_capabilities: Iterable[KeyCapability | str],
    gpg_binary: str | os.PathLike[str],
) -> ProtectedBackup:
    executable = _validated_executable(gpg_binary)
    owner_home = _existing_private_home(keyring_home)
    recovery_public_home = _existing_private_home(recovery_public_keyring_home)
    if _overlaps(owner_home, recovery_public_home):
        raise RecoveryError(RecoveryErrorCode.HOMES_NOT_ISOLATED)

    primary_fingerprint = _fingerprint(expected_primary_fingerprint)
    recipient_fingerprint = _fingerprint(recovery_recipient_fingerprint)
    requested = _capabilities(required_capabilities)
    owner = _inspect_owner(
        owner_home,
        primary_fingerprint,
        profile=profile,
        required_capabilities=requested,
        executable=executable,
    )
    recipient = _inspect_recovery_recipient(
        recovery_public_home, recipient_fingerprint, executable=executable
    )
    _require_no_secret_keys(recovery_public_home, executable)
    _require_secret_components(
        owner_home,
        primary_fingerprint,
        owner,
        requested,
        executable=executable,
        failure_code=RecoveryErrorCode.OWNER_KEY_INVALID,
    )
    if (
        not isinstance(owner_passphrase_reference, SecretReference)
        or not isinstance(secret_resolver, SecretResolver)
    ):
        raise RecoveryError(RecoveryErrorCode.INVALID_REQUEST)
    passphrase = _resolve_passphrase(
        owner_passphrase_reference,
        secret_resolver,
        failure_code=RecoveryErrorCode.SECRET_RESOLUTION_FAILED,
    )

    try:
        output = _create_backup_file(backup_path)
        descriptor: int | None = None
        identity: tuple[int, int] | None = None
        succeeded = False
        try:
            descriptor = os.open(
                output,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
            os.fchmod(descriptor, 0o600)
            _run_backup_pipeline(
                executable,
                owner_home,
                recovery_public_home,
                primary_fingerprint,
                recipient_fingerprint,
                descriptor,
                passphrase,
            )
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            succeeded = True
        except FileExistsError:
            raise RecoveryError(RecoveryErrorCode.BACKUP_EXISTS) from None
        except RecoveryError:
            raise
        except OSError:
            raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not succeeded and identity is not None and output.exists():
                try:
                    current = output.lstat()
                    if (current.st_dev, current.st_ino) == identity:
                        output.unlink()
                except OSError:
                    pass

        try:
            metadata = output.lstat()
        except OSError:
            raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE) from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_BACKUP_BYTES
        ):
            try:
                output.unlink()
            except OSError:
                pass
            raise RecoveryError(RecoveryErrorCode.IO_LIMIT)
        return ProtectedBackup(output, primary_fingerprint, recipient_fingerprint)
    finally:
        passphrase[:] = b"\x00" * len(passphrase)


def restore_protected_backup(
    *,
    backup_path: str | os.PathLike[str],
    recovery_keyring_home: str | os.PathLike[str],
    recovery_recipient_fingerprint: str,
    keyring_home: str | os.PathLike[str],
    expected_primary_fingerprint: str,
    recovery_passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    profile: KeyProfile,
    required_capabilities: Iterable[KeyCapability | str],
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> RecoveredKey:
    """Decrypt/import only into an explicit empty isolated GnuPG home.

    The recovery passphrase is resolved from the caller-selected reference and
    delivered to GnuPG over an inherited pipe, never in argv or diagnostics.
    Successful import is not proof until the full primary fingerprint, selected
    capabilities, profile, and expiry are re-inspected in the destination home.

    The selected homes must not be used concurrently by other same-UID
    processes.
    """
    try:
        return _restore_protected_backup(
            backup_path=backup_path,
            recovery_keyring_home=recovery_keyring_home,
            recovery_recipient_fingerprint=recovery_recipient_fingerprint,
            keyring_home=keyring_home,
            expected_primary_fingerprint=expected_primary_fingerprint,
            recovery_passphrase_reference=recovery_passphrase_reference,
            secret_resolver=secret_resolver,
            profile=profile,
            required_capabilities=required_capabilities,
            gpg_binary=gpg_binary,
        )
    except RecoveryError as exc:
        code = exc.code
    except Exception:
        code = RecoveryErrorCode.RESTORE_INVALID
    del (
        backup_path,
        recovery_keyring_home,
        recovery_recipient_fingerprint,
        keyring_home,
        expected_primary_fingerprint,
        recovery_passphrase_reference,
        secret_resolver,
        profile,
        required_capabilities,
        gpg_binary,
    )
    raise RecoveryError(code) from None


def _restore_protected_backup(
    *,
    backup_path: str | os.PathLike[str],
    recovery_keyring_home: str | os.PathLike[str],
    recovery_recipient_fingerprint: str,
    keyring_home: str | os.PathLike[str],
    expected_primary_fingerprint: str,
    recovery_passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    profile: KeyProfile,
    required_capabilities: Iterable[KeyCapability | str],
    gpg_binary: str | os.PathLike[str],
) -> RecoveredKey:
    executable = _validated_executable(gpg_binary)
    recovery_home = _existing_private_home(recovery_keyring_home)
    target_path = _selected_home_path(keyring_home)
    if _overlaps(recovery_home, target_path):
        raise RecoveryError(RecoveryErrorCode.HOMES_NOT_ISOLATED)

    recipient_fingerprint = _fingerprint(recovery_recipient_fingerprint)
    recipient = _inspect_recovery_recipient(
        recovery_home, recipient_fingerprint, executable=executable
    )
    _require_secret_components(
        recovery_home,
        recipient_fingerprint,
        recipient,
        frozenset({KeyCapability.ENCRYPT}),
        executable=executable,
        failure_code=RecoveryErrorCode.RECOVERY_KEY_INVALID,
        require_only_primary=True,
    )
    primary_fingerprint = _fingerprint(expected_primary_fingerprint)
    requested = _capabilities(required_capabilities)
    _validate_policy_inputs(profile, requested)
    if (
        not isinstance(recovery_passphrase_reference, SecretReference)
        or not isinstance(secret_resolver, SecretResolver)
    ):
        raise RecoveryError(RecoveryErrorCode.INVALID_REQUEST)

    backup = _open_backup(backup_path)
    passphrase: bytearray | None = None
    target_home: Path | None = None
    try:
        _validate_backup_recipient(
            executable, recovery_home, backup, recipient
        )
        passphrase = _resolve_passphrase(
            recovery_passphrase_reference,
            secret_resolver,
            failure_code=RecoveryErrorCode.SECRET_RESOLUTION_FAILED,
        )
        target_home = _new_empty_home(target_path)
        _run_restore_pipeline(
            executable,
            recovery_home,
            target_home,
            backup,
            passphrase,
        )
    finally:
        backup.close()
        if passphrase is not None:
            passphrase[:] = b"\x00" * len(passphrase)

    if target_home is None:
        raise RecoveryError(RecoveryErrorCode.RESTORE_INVALID)
    try:
        inspection = inspect_key(
            keyring_home=target_home,
            fingerprint=primary_fingerprint,
            gpg_binary=executable,
        )
    except Exception:
        _clear_restore_home(target_home)
        raise RecoveryError(RecoveryErrorCode.RESTORE_INVALID) from None
    if inspection.primary_fingerprint != primary_fingerprint:
        _clear_restore_home(target_home)
        raise RecoveryError(RecoveryErrorCode.FINGERPRINT_MISMATCH)
    try:
        _validate_owner_inspection(
            inspection,
            primary_fingerprint,
            profile=profile,
            required_capabilities=requested,
        )
        _require_secret_components(
            target_home,
            primary_fingerprint,
            inspection,
            requested,
            executable=executable,
            failure_code=RecoveryErrorCode.OWNER_KEY_INVALID,
            require_only_primary=True,
        )
        _validate_private_home_contents(target_home)
    except RecoveryError:
        _clear_restore_home(target_home)
        raise
    except Exception:
        _clear_restore_home(target_home)
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID) from None
    return RecoveredKey(target_home, primary_fingerprint, recipient_fingerprint)


def _inspect_owner(
    home: Path,
    fingerprint: str,
    *,
    profile: KeyProfile,
    required_capabilities: Iterable[KeyCapability | str],
    executable: str,
) -> KeyInspection:
    requested = _capabilities(required_capabilities)
    _validate_policy_inputs(profile, requested)
    try:
        inspection = inspect_key(
            keyring_home=home, fingerprint=fingerprint, gpg_binary=executable
        )
        _validate_owner_inspection(
            inspection,
            fingerprint,
            profile=profile,
            required_capabilities=requested,
        )
        return inspection
    except RecoveryError:
        raise
    except Exception:
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID) from None


def _validate_owner_inspection(
    inspection: KeyInspection,
    fingerprint: str,
    *,
    profile: KeyProfile,
    required_capabilities: Iterable[KeyCapability | str],
) -> None:
    try:
        _ = enroll_key(
            inspection,
            expected_fingerprint=fingerprint,
            required_capabilities=required_capabilities,
            profile=profile,
            # Validation only. The returned local trust value is discarded and
            # no caller trust store or GnuPG ownertrust database is modified.
            local_trust=LocalTrust(),
        )
    except KeyInspectionError as exc:
        if exc.code.name == "FINGERPRINT_MISMATCH":
            raise RecoveryError(RecoveryErrorCode.FINGERPRINT_MISMATCH) from None
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID) from None
    except (OpenPGPContractError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID) from None


def _inspect_recovery_recipient(
    home: Path, fingerprint: str, *, executable: str
) -> KeyInspection:
    try:
        inspection = inspect_key(
            keyring_home=home, fingerprint=fingerprint, gpg_binary=executable
        )
    except Exception:
        raise RecoveryError(RecoveryErrorCode.RECOVERY_KEY_INVALID) from None
    now = int(time.time())
    usable = tuple(
        record
        for record in inspection.records
        if KeyCapability.ENCRYPT in record.direct_capabilities
        and not record.revoked
        and not record.disabled
        and not record.expired
        and (record.expires_epoch is None or record.expires_epoch > now)
    )
    if (
        inspection.primary_fingerprint != fingerprint
        or inspection.primary.revoked
        or inspection.primary.disabled
        or inspection.primary.expired
        or (
            inspection.primary.expires_epoch is not None
            and inspection.primary.expires_epoch <= now
        )
        or not usable
    ):
        raise RecoveryError(RecoveryErrorCode.RECOVERY_KEY_INVALID)
    usable_capabilities = {
        capability
        for record in usable
        for capability in record.direct_capabilities
    }
    try:
        require_capabilities(usable_capabilities, {KeyCapability.ENCRYPT})
    except OpenPGPContractError:
        raise RecoveryError(RecoveryErrorCode.RECOVERY_KEY_INVALID) from None
    return inspection


def _require_secret_components(
    home: Path,
    fingerprint: str,
    inspection: KeyInspection,
    capabilities: frozenset[KeyCapability],
    *,
    executable: str,
    failure_code: RecoveryErrorCode,
    require_only_primary: bool = False,
) -> None:
    arguments = (
        "--with-colons",
        "--fixed-list-mode",
        "--with-fingerprint",
        "--with-subkey-fingerprint",
        "--list-secret-keys",
    )
    if not require_only_primary:
        arguments += (fingerprint,)
    try:
        output = _run_secret_listing_gpg(executable, home, arguments)
        primary_fingerprints, available = _secret_key_fingerprints(output)
    except Exception:
        raise RecoveryError(failure_code) from None
    if require_only_primary and primary_fingerprints != frozenset({fingerprint}):
        raise RecoveryError(failure_code)
    needed = {inspection.primary_fingerprint}
    now = int(time.time())
    for record in inspection.records:
        if (
            capabilities.intersection(record.direct_capabilities)
            and not record.revoked
            and not record.disabled
            and not record.expired
            and (record.expires_epoch is None or record.expires_epoch > now)
        ):
            needed.add(record.fingerprint)
    if fingerprint not in primary_fingerprints or not needed.issubset(available):
        raise RecoveryError(failure_code)


def _secret_key_fingerprints(
    output: bytes,
) -> tuple[frozenset[str], frozenset[str]]:
    primaries: set[str] = set()
    components: set[str] = set()
    pending_primary = False
    pending_component = False
    pending_available = False
    for line in output.splitlines():
        fields = line.split(b":")
        if fields[0] in (b"sec", b"ssb"):
            pending_primary = fields[0] == b"sec"
            pending_component = True
            # GnuPG's secret-key availability field: '+' means local private
            # material; '#' (dummy) and '>' (token) cannot be cold-restored.
            pending_available = len(fields) > 14 and fields[14] == b"+"
        elif fields[0] == b"fpr" and pending_component:
            pending_component = False
            if len(fields) <= 9:
                continue
            try:
                fingerprint = normalize_fingerprint(fields[9].decode("ascii"))
            except (UnicodeDecodeError, OpenPGPContractError):
                continue
            if pending_primary:
                primaries.add(fingerprint)
            if pending_available:
                components.add(fingerprint)
    return frozenset(primaries), frozenset(components)


def _require_no_secret_keys(home: Path, executable: str) -> None:
    try:
        output = _run_secret_listing_gpg(
            executable,
            home,
            (
                "--with-colons",
                "--fixed-list-mode",
                "--with-fingerprint",
                "--with-subkey-fingerprint",
                "--list-secret-keys",
            ),
        )
    except Exception:
        raise RecoveryError(RecoveryErrorCode.RECOVERY_KEY_INVALID) from None
    if any(line.startswith((b"sec:", b"ssb:")) for line in output.splitlines()):
        raise RecoveryError(RecoveryErrorCode.RECOVERY_KEY_INVALID)


def _run_secret_listing_gpg(
    executable: str, home: Path, arguments: tuple[str, ...]
) -> bytes:
    """List secret components with an agent only for this selected home."""
    with _managed_home_agents((home,)):
        return _run_bounded_gpg(_gpg_command(executable, home, arguments))

def _run_bounded_gpg(
    command: list[str],
    *,
    stdin: int | BinaryIO = subprocess.DEVNULL,
    timeout_seconds: float = _GPG_TIMEOUT_SECONDS,
    output_limit: int = _MAX_DIAGNOSTIC_BYTES,
    allow_failure: bool = False,
) -> bytes:
    home = Path(command[command.index("--homedir") + 1])
    try:
        process = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            umask=0o077,
            env=_gpg_environment(home),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
    assert process.stdout is not None and process.stderr is not None
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT)
                events = selector.select(remaining)
                if not events:
                    raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT)
                for key, _ in events:
                    pipe = cast(BinaryIO, key.fileobj)
                    try:
                        chunk = os.read(pipe.fileno(), _GPG_READ_SIZE)
                    except OSError:
                        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
                    if not chunk:
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    total += len(chunk)
                    if total > output_limit:
                        raise RecoveryError(RecoveryErrorCode.IO_LIMIT)
                    if pipe is process.stdout:
                        output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT)
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT) from None
        if status != 0:
            if allow_failure:
                return b""
            raise RecoveryError(RecoveryErrorCode.GPG_FAILED)
    except RecoveryError:
        _terminate(process)
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        _terminate(process)
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
    finally:
        for pipe in (process.stdout, process.stderr):
            if not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass
    return bytes(output)


def _home_agent_is_running(home: Path) -> bool:
    output = _run_bounded_gpg(
        [
            "gpg-connect-agent",
            "--no-autostart",
            "--homedir",
            os.fspath(home),
            "GETINFO pid",
            "/bye",
        ],
        timeout_seconds=5.0,
        output_limit=128,
        allow_failure=True,
    )
    return re.fullmatch(rb"D [0-9]{1,20}\r?\nOK\r?\n?", output) is not None


@contextmanager
def _managed_home_agents(homes: tuple[Path, ...]) -> Iterator[None]:
    """Preserve existing agents; concurrent same-UID home use is unsupported."""
    states = tuple(
        (home, _home_agent_is_running(home)) for home in dict.fromkeys(homes)
    )
    try:
        yield
    finally:
        cleanup_failed = False
        for home, already_running in states:
            if already_running:
                continue
            try:
                if _home_agent_is_running(home) and (
                    not _stop_home_agent(home) or _home_agent_is_running(home)
                ):
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise RecoveryError(RecoveryErrorCode.GPG_FAILED)


def _validate_backup_recipient(
    executable: str,
    recovery_home: Path,
    backup: BinaryIO,
    recipient: KeyInspection,
) -> None:
    try:
        with _managed_home_agents((recovery_home,)):
            try:
                backup.seek(0)
                packet_output = _run_bounded_gpg(
                    _gpg_command(
                        executable,
                        recovery_home,
                        ("--list-only", "--list-packets", "-"),
                    ),
                    stdin=backup,
                )
            finally:
                backup.seek(0)
    except Exception:
        raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID) from None
    recipient_ids = {
        record.fingerprint[-16:].upper()
        for record in recipient.records
        if KeyCapability.ENCRYPT in record.direct_capabilities
        and not record.revoked
        and not record.disabled
        and not record.expired
    }
    observed_ids = {
        match.group(1).decode("ascii").upper()
        for match in re.finditer(rb"\bkeyid\s+([0-9A-Fa-f]{16})\b", packet_output)
    }
    if len(observed_ids) != 1 or not observed_ids.issubset(recipient_ids):
        raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)


def _run_backup_pipeline(
    executable: str,
    owner_home: Path,
    recovery_public_home: Path,
    primary_fingerprint: str,
    recipient_fingerprint: str,
    output_descriptor: int,
    passphrase: bytearray,
) -> None:
    with _managed_home_agents((owner_home, recovery_public_home)):
        read_fd, write_fd = os.pipe()
        writer_owned = False
        try:
            exporter = _gpg_command(
                executable,
                owner_home,
                (
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase-fd",
                    str(read_fd),
                    "--export-secret-keys",
                    primary_fingerprint,
                ),
            )
            # The full-fingerprint recipient is pinned; this changes no trust.
            encryptor = _gpg_command(
                executable,
                recovery_public_home,
                (
                    "--trust-model",
                    "always",
                    "--encrypt",
                    "--recipient",
                    recipient_fingerprint,
                    "--output",
                    "-",
                ),
            )
            writer_owned = True
            _run_pipeline(
                exporter,
                encryptor,
                first_input=subprocess.DEVNULL,
                second_output=output_descriptor,
                require_nonempty_input=True,
                passphrase_read_fd=read_fd,
                passphrase_write_fd=write_fd,
                passphrase=passphrase,
            )
        finally:
            try:
                os.close(read_fd)
            except OSError:
                pass
            if not writer_owned:
                try:
                    os.close(write_fd)
                except OSError:
                    pass


def _run_restore_pipeline(
    executable: str,
    recovery_home: Path,
    target_home: Path,
    backup: BinaryIO,
    passphrase: bytearray,
) -> None:
    read_fd, write_fd = os.pipe()
    writer_owned = False
    try:
        with _managed_home_agents((recovery_home, target_home)):
            decryptor = _gpg_command(
                executable,
                recovery_home,
                (
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase-fd",
                    str(read_fd),
                    "--decrypt",
                ),
            )
            importer = _gpg_command(executable, target_home, ("--import",))
            writer_owned = True
            _run_pipeline(
                decryptor,
                importer,
                first_input=backup,
                second_output=subprocess.DEVNULL,
                passphrase_read_fd=read_fd,
                passphrase_write_fd=write_fd,
                passphrase=passphrase,
                require_nonempty_input=True,
            )
    except RecoveryError:
        _clear_restore_home(target_home)
        raise
    except Exception:
        _clear_restore_home(target_home)
        raise RecoveryError(RecoveryErrorCode.GPG_FAILED) from None
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        if not writer_owned:
            try:
                os.close(write_fd)
            except OSError:
                pass


def _run_pipeline(
    first_command: list[str],
    second_command: list[str],
    *,
    first_input: int | BinaryIO,
    second_output: int,
    require_nonempty_input: bool,
    passphrase_read_fd: int | None = None,
    passphrase_write_fd: int | None = None,
    passphrase: bytearray | None = None,
) -> None:
    first_home = Path(first_command[first_command.index("--homedir") + 1])
    second_home = Path(second_command[second_command.index("--homedir") + 1])
    first: subprocess.Popen[bytes] | None = None
    second: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    output_size = 0
    plaintext_size = 0
    diagnostic_size = 0
    pending = bytearray()
    plain_eof = False
    passphrase_sent = 0
    deadline = time.monotonic() + _GPG_TIMEOUT_SECONDS
    try:
        first = subprocess.Popen(
            first_command,
            stdin=first_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            umask=0o077,
            pass_fds=() if passphrase_read_fd is None else (passphrase_read_fd,),
            env=_gpg_environment(first_home),
        )
        second = subprocess.Popen(
            second_command,
            stdin=subprocess.PIPE,
            stdout=(subprocess.PIPE if second_output != subprocess.DEVNULL else subprocess.DEVNULL),
            stderr=subprocess.PIPE,
            close_fds=True,
            umask=0o077,
            env=_gpg_environment(second_home),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        _terminate(first)
        _terminate(second)
        if passphrase_write_fd is not None:
            try:
                os.close(passphrase_write_fd)
            except OSError:
                pass
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None

    assert first.stdout is not None and first.stderr is not None
    assert second.stdin is not None and second.stderr is not None
    source = cast(BinaryIO, first.stdout)
    sink = cast(BinaryIO, second.stdin)
    first_stderr = cast(BinaryIO, first.stderr)
    second_stderr = cast(BinaryIO, second.stderr)
    ciphertext = None if second.stdout is None else cast(BinaryIO, second.stdout)
    try:
        selector = selectors.DefaultSelector()
        for pipe, label in ((source, "plain"), (first_stderr, "first-stderr"), (second_stderr, "second-stderr")):
            os.set_blocking(pipe.fileno(), False)
            _ = selector.register(pipe, selectors.EVENT_READ, label)
        if ciphertext is not None:
            os.set_blocking(ciphertext.fileno(), False)
            _ = selector.register(ciphertext, selectors.EVENT_READ, "cipher")
        os.set_blocking(sink.fileno(), False)
        if passphrase_write_fd is not None and passphrase is not None:
            os.set_blocking(passphrase_write_fd, False)
            _ = selector.register(passphrase_write_fd, selectors.EVENT_WRITE, "passphrase")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT)
            events = selector.select(remaining)
            if not events:
                raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT)
            for key, _ in events:
                label = cast(str, key.data)
                if label == "plain":
                    try:
                        chunk = os.read(source.fileno(), _GPG_READ_SIZE)
                    except OSError:
                        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
                    _ = selector.unregister(source)
                    if not chunk:
                        source.close()
                        plain_eof = True
                        sink.close()
                        continue
                    plaintext_size += len(chunk)
                    if plaintext_size > _MAX_PLAINTEXT_BYTES:
                        raise RecoveryError(RecoveryErrorCode.IO_LIMIT)
                    if not pending:
                        pending.extend(chunk)
                        _ = selector.register(sink, selectors.EVENT_WRITE, "plain-sink")
                    else:
                        raise RecoveryError(RecoveryErrorCode.GPG_FAILED)
                elif label == "plain-sink":
                    try:
                        written = os.write(sink.fileno(), pending)
                    except BrokenPipeError:
                        raise RecoveryError(RecoveryErrorCode.GPG_FAILED) from None
                    except OSError:
                        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
                    if written <= 0:
                        raise RecoveryError(RecoveryErrorCode.GPG_FAILED)
                    del pending[:written]
                    if not pending:
                        _ = selector.unregister(sink)
                        if plain_eof:
                            sink.close()
                        else:
                            _ = selector.register(source, selectors.EVENT_READ, "plain")
                elif label in {"first-stderr", "second-stderr"}:
                    pipe = first_stderr if label == "first-stderr" else second_stderr
                    try:
                        chunk = os.read(pipe.fileno(), _GPG_READ_SIZE)
                    except OSError:
                        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
                    if not chunk:
                        _ = selector.unregister(pipe)
                        pipe.close()
                        continue
                    diagnostic_size += len(chunk)
                    if diagnostic_size > _MAX_DIAGNOSTIC_BYTES:
                        raise RecoveryError(RecoveryErrorCode.IO_LIMIT)
                elif label == "cipher":
                    if ciphertext is None:
                        raise RecoveryError(RecoveryErrorCode.GPG_FAILED)
                    try:
                        chunk = os.read(ciphertext.fileno(), _GPG_READ_SIZE)
                    except OSError:
                        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
                    if not chunk:
                        _ = selector.unregister(ciphertext)
                        ciphertext.close()
                        continue
                    output_size += len(chunk)
                    if output_size > _MAX_BACKUP_BYTES:
                        raise RecoveryError(RecoveryErrorCode.IO_LIMIT)
                    if second_output != subprocess.DEVNULL:
                        _write_descriptor(second_output, chunk)
                elif label == "passphrase":
                    if passphrase is None or passphrase_write_fd is None:
                        raise RecoveryError(RecoveryErrorCode.INVALID_PASSPHRASE)
                    try:
                        passphrase_sent += os.write(
                            passphrase_write_fd,
                            memoryview(passphrase)[passphrase_sent:],
                        )
                    except BrokenPipeError:
                        passphrase_sent = len(passphrase)
                    except OSError:
                        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
                    if passphrase_sent >= len(passphrase):
                        _ = selector.unregister(passphrase_write_fd)
                        os.close(passphrase_write_fd)
                        passphrase_write_fd = None
        first_remaining = deadline - time.monotonic()
        if first_remaining <= 0:
            raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT)
        try:
            first_status = first.wait(timeout=first_remaining)
            second_status = second.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise RecoveryError(RecoveryErrorCode.GPG_TIMEOUT) from None
        if first_status != 0 or second_status != 0:
            raise RecoveryError(RecoveryErrorCode.GPG_FAILED)
        if require_nonempty_input and plaintext_size == 0:
            raise RecoveryError(RecoveryErrorCode.GPG_FAILED)
        if second_output != subprocess.DEVNULL and output_size == 0:
            raise RecoveryError(RecoveryErrorCode.GPG_FAILED)
    except RecoveryError:
        _terminate(first)
        _terminate(second)
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        _terminate(first)
        _terminate(second)
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
    finally:
        if selector is not None:
            selector.close()
        for pipe in (
            source,
            sink,
            first_stderr,
            second_stderr,
            ciphertext,
        ):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass
        if passphrase_write_fd is not None:
            try:
                os.close(passphrase_write_fd)
            except OSError:
                pass
        for process in (first, second):
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None and not pipe.closed:
                        try:
                            pipe.close()
                        except OSError:
                            pass


def _gpg_command(
    executable: str, home: Path, arguments: tuple[str, ...]
) -> list[str]:
    return [
        executable,
        "--no-options",
        "--batch",
        "--no-tty",
        "--no-auto-key-retrieve",
        "--no-auto-check-trustdb",
        "--homedir",
        os.fspath(home),
        *arguments,
    ]


def _gpg_environment(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.fspath(home),
        "GNUPGHOME": os.fspath(home),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
        "TMPDIR": os.fspath(home),
    }


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _write_descriptor(descriptor: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        try:
            written = os.write(descriptor, view)
        except OSError:
            raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE) from None
        if written <= 0:
            raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE)
        view = view[written:]


def _open_backup(value: str | os.PathLike[str]) -> BinaryIO:
    try:
        path = Path(value).absolute()
        if ".." in path.parts:
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        try:
            _require_private_ancestors(path)
        except RecoveryError:
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID) from None
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_BACKUP_BYTES
        ):
            os.close(descriptor)
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        return os.fdopen(descriptor, "rb", closefd=True)
    except RecoveryError:
        raise
    except (OSError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID) from None


def _create_backup_file(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).absolute()
        if ".." in path.parts:
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        _require_private_ancestors(path)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        return path
    except RecoveryError:
        raise
    except (OSError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID) from None


def _existing_private_home(value: str | os.PathLike[str]) -> Path:
    try:
        home = Path(value).absolute()
        if ".." in home.parts:
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        _require_private_ancestors(home)
        metadata = home.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or _is_ambient_home(home)
        ):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        _validate_private_tree(home)
        return home
    except RecoveryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME) from None


def _selected_home_path(value: str | os.PathLike[str]) -> Path:
    try:
        home = Path(value).absolute()
        if not home.name or ".." in home.parts:
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        return home
    except RecoveryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME) from None


def _new_empty_home(value: str | os.PathLike[str]) -> Path:
    try:
        home = Path(value).absolute()
        if ".." in home.parts:
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        _require_private_ancestors(home)
        if _is_ambient_home(home):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        try:
            metadata = home.lstat()
        except FileNotFoundError:
            home.mkdir(mode=0o700)
            metadata = home.lstat()
        else:
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
            if next(home.iterdir(), None) is not None:
                raise RecoveryError(RecoveryErrorCode.KEYRING_NOT_EMPTY)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        os.chmod(home, 0o700)
        return home
    except RecoveryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME) from None


def _validate_private_home_contents(home: Path) -> None:
    metadata = home.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE)
    _validate_private_tree(home)


def _validate_private_tree(root: Path) -> None:
    with os.scandir(root) as entries:
        for entry in entries:
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
                _validate_private_tree(Path(entry.path))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
            elif not stat.S_ISSOCK(metadata.st_mode):
                raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)


def _clear_restore_home(home: Path) -> None:
    """Roll back only files created in the verified-empty selected home."""
    try:
        _remove_contents(home)
        os.chmod(home, 0o700)
    except (OSError, RuntimeError):
        raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE) from None


def _stop_home_agent(home: Path) -> bool:
    environment = _gpg_environment(home)
    try:
        result = subprocess.run(
            ("gpgconf", "--homedir", os.fspath(home), "--kill", "gpg-agent"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=environment,
            timeout=5.0,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _remove_contents(root: Path) -> None:
    with os.scandir(root) as entries:
        for entry in entries:
            path = Path(entry.path)
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                _remove_contents(path)
                path.rmdir()
            else:
                path.unlink()


def _require_private_ancestors(selected: Path) -> None:
    for parent in selected.parents:
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.geteuid()):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        if stat.S_IMODE(metadata.st_mode) & 0o022 and not (
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        ):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)


def _is_ambient_home(home: Path) -> bool:
    configured = os.environ.get("GNUPGHOME")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(Path.home() / ".gnupg")
    for candidate in candidates:
        try:
            ambient = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if _overlaps(home, ambient):
            return True
    return False


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _validated_executable(value: str | os.PathLike[str]) -> str:
    try:
        executable = os.fspath(value)
    except (TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE)
    return executable


def _fingerprint(value: str) -> str:
    try:
        return normalize_fingerprint(value)
    except (OpenPGPContractError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_FINGERPRINT) from None


def _capabilities(
    values: Iterable[KeyCapability | str],
) -> frozenset[KeyCapability]:
    try:
        if isinstance(values, (str, bytes, bytearray, dict)):
            raise ValueError
        selected = frozenset(KeyCapability(value) for value in values)
        if not selected or not selected.issubset(
            {KeyCapability.SIGN, KeyCapability.ENCRYPT}
        ):
            raise ValueError
        return selected
    except (TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID) from None


def _validate_policy_inputs(
    profile: KeyProfile, capabilities: frozenset[KeyCapability]
) -> None:
    if not isinstance(profile, KeyProfile) or not capabilities:
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID)


def _resolve_passphrase(
    reference: SecretReference,
    resolver: SecretResolver,
    *,
    failure_code: RecoveryErrorCode,
) -> bytearray:
    try:
        secret = resolver.resolve(reference)
    except Exception:
        raise RecoveryError(failure_code) from None
    try:
        payload = _passphrase_payload(secret)
    finally:
        del secret
    if payload is None:
        raise RecoveryError(RecoveryErrorCode.INVALID_PASSPHRASE)
    return payload


def _passphrase_payload(secret: object) -> bytearray | None:
    if (
        not isinstance(secret, str)
        or not secret
        or any(character in secret for character in ("\x00", "\r", "\n"))
    ):
        return None
    try:
        encoded = str.encode(secret, "utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_PASSPHRASE_BYTES:
        return None
    return bytearray(encoded + b"\n")
