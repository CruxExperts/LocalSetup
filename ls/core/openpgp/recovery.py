"""Protected OpenPGP backup and isolated recovery using explicit key pins.

Secret-key packets are relayed between GnuPG processes through bounded pipes;
this module never writes an unencrypted export package to a file.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Iterable

from . import recovery_io as _recovery_io
from . import recovery_process as _recovery_process
from .contracts import (
    KeyCapability,
    KeyProfile,
    LocalTrust,
    OpenPGPContractError,
    require_capabilities,
)
from .keys import (
    KeyInspection,
    KeyInspectionError,
    enroll_key,
    inspect_key,
)
from .recovery_models import (
    ProtectedBackup,
    RecoveredKey,
    RecoveryError,
    RecoveryErrorCode,
    _MAX_BACKUP_BYTES,
    _capabilities,
    _fingerprint,
    _resolve_passphrase,
    _validate_policy_inputs,
    _validated_executable,
)
from .secrets import SecretReference, SecretResolver

# Preserve agent helpers consumed from this module by the transition layer.
_gpg_environment = _recovery_process._gpg_environment
_home_agent_is_running = _recovery_process._home_agent_is_running
_managed_home_agents = _recovery_process._managed_home_agents

__all__ = [
    "ProtectedBackup",
    "RecoveredKey",
    "RecoveryError",
    "RecoveryErrorCode",
    "create_protected_backup",
    "restore_protected_backup",
]

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
    owner_home = _recovery_io._existing_private_home(keyring_home)
    recovery_public_home = _recovery_io._existing_private_home(
        recovery_public_keyring_home
    )
    if _recovery_io._overlaps(owner_home, recovery_public_home):
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
    _recovery_process._require_no_secret_keys(recovery_public_home, executable)
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
        output = _recovery_io._create_backup_file(backup_path)
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
            _recovery_process._run_backup_pipeline(
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
    recovery_home = _recovery_io._existing_private_home(recovery_keyring_home)
    target_path = _recovery_io._selected_home_path(keyring_home)
    if _recovery_io._overlaps(recovery_home, target_path):
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

    backup = _recovery_io._open_backup(backup_path)
    passphrase: bytearray | None = None
    target_home: Path | None = None
    try:
        _recovery_process._validate_backup_recipient(
            executable, recovery_home, backup, recipient
        )
        passphrase = _resolve_passphrase(
            recovery_passphrase_reference,
            secret_resolver,
            failure_code=RecoveryErrorCode.SECRET_RESOLUTION_FAILED,
        )
        target_home = _recovery_io._new_empty_home(target_path)
        _recovery_process._run_restore_pipeline(
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
        _recovery_io._clear_restore_home(target_home)
        raise RecoveryError(RecoveryErrorCode.RESTORE_INVALID) from None
    if inspection.primary_fingerprint != primary_fingerprint:
        _recovery_io._clear_restore_home(target_home)
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
        _recovery_io._validate_private_home_contents(target_home)
    except RecoveryError:
        _recovery_io._clear_restore_home(target_home)
        raise
    except Exception:
        _recovery_io._clear_restore_home(target_home)
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
        output = _recovery_process._run_secret_listing_gpg(
            executable, home, arguments
        )
        primary_fingerprints, available = (
            _recovery_process._secret_key_fingerprints(output)
        )
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
