"""Protected generation of explicitly configured OpenPGP keys.

Generation only writes into a caller-selected, empty GnuPG home and returns a
public certificate. Secret material remains in the protected key store.
"""

from __future__ import annotations

import os
import selectors
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO, Final, cast

from .contracts import (
    KeyCapability,
    KeyProfile,
    OpenPGPContractError,
    RSA_4096_TWO_YEAR_PROFILE,
    normalize_fingerprint,
)
from .generation_models import (
    GeneratedKey,
    KeyGenerationError,
    KeyGenerationErrorCode,
    KeyIdentity,
)
from .generation_home import (
    GenerationHomeSelection,
    PreparedGenerationHome,
    check_generation_selection,
    inspect_generation_home,
    normalize_generation_home_path,
)
from .keys import (
    KeyInspection,
    KeyInspectionError,
    KeyInspectionErrorCode,
    KeyRecord,
    inspect_key,
)
from .native_input import (
    InputOperation,
    NativeInputContext,
    describe_generation_operation,
)
from .secrets import SecretReference, SecretResolver

__all__ = [
    "GeneratedKey",
    "KeyGenerationError",
    "KeyGenerationErrorCode",
    "KeyIdentity",
    "generate_key",
    "inspect_generation_candidate",
]

for _public_type in (
    GeneratedKey,
    KeyGenerationError,
    KeyGenerationErrorCode,
    KeyIdentity,
):
    _public_type.__module__ = __name__

_MAX_PASSPHRASE_BYTES: Final = 4096
_MAX_GPG_OUTPUT_BYTES: Final = 1_048_576
_GPG_GENERATION_TIMEOUT_SECONDS: Final = 120.0
_GPG_MUTATION_TIMEOUT_SECONDS: Final = 30.0
_GPG_OUTPUT_TIMEOUT_SECONDS: Final = 15.0
_GPG_READ_SIZE: Final = 8192
_MAX_CANDIDATE_HOME_ENTRIES: Final = 512
_MAX_CANDIDATE_HOME_BYTES: Final = 16_777_216
_MAX_CANDIDATE_HOME_DEPTH: Final = 4


def generate_key(
    *,
    identity: KeyIdentity,
    profile: KeyProfile,
    passphrase_reference: SecretReference | None,
    secret_resolver: SecretResolver,
    keyring_home: str | os.PathLike[str],
    gpg_binary: str | os.PathLike[str] = "gpg",
    native_input: NativeInputContext | None = None,
) -> GeneratedKey:
    """Generate a protected key without retaining sensitive failure frames."""
    try:
        return _generate_key(
            identity=identity,
            profile=profile,
            passphrase_reference=passphrase_reference,
            secret_resolver=secret_resolver,
            keyring_home=keyring_home,
            gpg_binary=gpg_binary,
            native_input=native_input,
        )
    except KeyGenerationError as exc:
        code = exc.code
    except Exception:
        code = KeyGenerationErrorCode.KEYRING_UNAVAILABLE
    del (
        identity, profile, passphrase_reference, secret_resolver, keyring_home,
        gpg_binary, native_input,
    )
    raise KeyGenerationError(code)


def inspect_generation_candidate(
    *,
    keyring_home: str | os.PathLike[str],
    fingerprint: str | None = None,
    expected_identity: KeyIdentity | None = None,
    profile: KeyProfile = RSA_4096_TWO_YEAR_PROFILE,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> GeneratedKey | None:
    """Inspect a recorded generation home without creating or changing it.

    A missing or verified-empty home returns ``None``. If no fingerprint is
    supplied, the caller must provide the identity expected for its private
    attempt and the home must contain exactly one matching public key. The
    result contains only a public certificate and fingerprints; it does not
    prove possession of the private components. A recovery caller must prove
    signing and decryption with its selected secret before adopting it.
    """
    try:
        return _inspect_generation_candidate(
            keyring_home=keyring_home,
            fingerprint=fingerprint,
            expected_identity=expected_identity,
            profile=profile,
            gpg_binary=gpg_binary,
        )
    except KeyGenerationError as exc:
        code = exc.code
    except Exception:
        code = KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
    del keyring_home, fingerprint, expected_identity, profile, gpg_binary
    raise KeyGenerationError(code)


def _generate_key(
    *,
    identity: KeyIdentity,
    profile: KeyProfile,
    passphrase_reference: SecretReference | None,
    secret_resolver: SecretResolver,
    keyring_home: str | os.PathLike[str],
    gpg_binary: str | os.PathLike[str] = "gpg",
    native_input: NativeInputContext | None = None,
) -> GeneratedKey:
    """Generate an RSA-4096 signing key and one encryption subkey.

    ``keyring_home`` must be a new or empty, non-symlink directory selected by
    the caller. It becomes the protected private store; this function never
    enrolls the generated key and never exports secret-key material.
    """
    _validate_request(
        identity, profile, passphrase_reference, secret_resolver, native_input,
    )
    executable = _validated_executable(gpg_binary)
    selection = inspect_generation_home(keyring_home) if native_input is not None else None
    operation: InputOperation | None = None
    lease: object | None = None
    claimed = False
    certification_options = _certification_gpg_options(
        identity.certification_notations
    )
    if native_input is not None:
        try:
            operation = describe_generation_operation(
                identity=identity, profile=profile, keyring_home=selection.path,
            )
            check_generation_selection(selection)
            lease = native_input.claim(operation)
            claimed = True
        except Exception:
            raise KeyGenerationError(
                KeyGenerationErrorCode.SECRET_RESOLUTION_FAILED
            ) from None

    home = selection.path if selection is not None else _prepare_keyring_home(keyring_home)
    prepared: PreparedGenerationHome | None = None

    batch_file: Path | None = None
    batch_identity: tuple[int, int, int, int, int] | None = None
    passphrase: bytearray | None = None
    agent_may_be_running = False
    completed = False
    try:
        passphrase = _resolve_generation_passphrase(
            passphrase_reference, secret_resolver,
            operation=operation, native_input=native_input,
        )
        if passphrase is None:
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PASSPHRASE)

        if native_input is not None and operation is not None and selection is not None:
            _generation_checkpoint(native_input, operation)
            check_generation_selection(selection)
            prepared = PreparedGenerationHome.prepare(
                selection,
                before_mutation=lambda: _generation_checkpoint(
                    native_input, operation, selection=selection,
                ),
            )
            home = prepared.path

        before_launch: Callable[[], None] | None = None
        if native_input is not None and operation is not None and prepared is not None:
            before_launch = lambda: _generation_checkpoint(
                native_input, operation, prepared=prepared,
            )

        initial_expiry = profile.expiry_date(datetime.now(timezone.utc).date())
        if before_launch is not None:
            before_launch()
        batch_file = _write_batch_file(
            home, identity, initial_expiry,
            directory_fd=None if prepared is None else prepared.home_fd,
            before_write=before_launch,
        )
        batch_identity = (
            None if prepared is None else prepared.child_identity(batch_file.name)
        )
        if prepared is not None:
            prepared_only_check = before_launch

            def before_launch_with_batch() -> None:
                if prepared_only_check is not None:
                    prepared_only_check()
                if (
                    batch_identity is None
                    or prepared.child_identity(batch_file.name) != batch_identity
                ):
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.KEYRING_UNAVAILABLE
                    )

            before_launch = before_launch_with_batch
        agent_may_be_running = True
        _run_generation_gpg(
            executable,
            home,
            (
                *certification_options,
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--generate-key",
                os.fspath(batch_file),
            ),
            passphrase=passphrase,
            timeout=_GPG_GENERATION_TIMEOUT_SECONDS,
            before_launch=before_launch,
        )
        _generation_harden(home, prepared, native_input, operation)

        initial_certificate = _export_public_certificate(
            executable, home, fingerprint=None, before_launch=before_launch,
        )
        initial_inspection = _inspect_public_certificate(
            initial_certificate, executable, before_launch=before_launch,
        )
        _validate_generated_components(
            initial_inspection, profile, require_expiry=False
        )

        _set_component_expiry(
            executable,
            home,
            initial_inspection.primary_fingerprint,
            initial_inspection.primary,
            profile,
            passphrase,
            certification_options=certification_options,
            before_launch=before_launch,
        )
        for subkey in initial_inspection.subkeys:
            _set_component_expiry(
                executable,
                home,
                initial_inspection.primary_fingerprint,
                subkey,
                profile,
                passphrase,
                certification_options=certification_options,
                before_launch=before_launch,
            )
        _generation_harden(home, prepared, native_input, operation)

        certificate = _export_public_certificate(
            executable, home, fingerprint=initial_inspection.primary_fingerprint,
            before_launch=before_launch,
        )
        inspection = _inspect_public_certificate(
            certificate, executable, before_launch=before_launch,
        )
        _validate_generated_components(inspection, profile, require_expiry=True)
        if batch_file is not None:
            try:
                if prepared is None:
                    batch_file.unlink()
                else:
                    prepared.check_current()
                    prepared.unlink_child_if_same(batch_file.name, batch_identity)
            except OSError:
                raise KeyGenerationError(
                    KeyGenerationErrorCode.KEYRING_UNAVAILABLE
                ) from None
            batch_file = None
        _generation_harden(home, prepared, native_input, operation)
        if native_input is not None and operation is not None and prepared is not None:
            _generation_checkpoint(native_input, operation, prepared=prepared)
        generated = GeneratedKey(
            public_certificate=certificate,
            primary_fingerprint=inspection.primary_fingerprint,
            subkey_fingerprints=tuple(
                subkey.fingerprint for subkey in inspection.subkeys
            ),
        )
        completed = True
        return generated
    except KeyGenerationError:
        raise
    except (OSError, ValueError):
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
    finally:
        try:
            if batch_file is not None:
                try:
                    if prepared is None:
                        batch_file.unlink(missing_ok=True)
                    else:
                        prepared.unlink_child_if_same(batch_file.name, batch_identity)
                except OSError:
                    pass
            if agent_may_be_running and (prepared is None or _prepared_path_is_current(prepared)):
                if prepared is None:
                    _stop_gpg_agent(home)
                else:
                    if native_input is None or operation is None:
                        _stop_gpg_agent(home, before_launch=prepared.check_current)
                    else:
                        _stop_gpg_agent(home, before_launch=prepared.check_current)
        finally:
            try:
                if prepared is None:
                    if native_input is None:
                        _harden_tree(home)
                else:
                    prepared.harden()
            finally:
                try:
                    if completed and native_input is not None and operation is not None:
                        if prepared is None:
                            raise KeyGenerationError(
                                KeyGenerationErrorCode.KEYRING_UNAVAILABLE
                            )
                        _generation_checkpoint(native_input, operation, prepared=prepared)
                finally:
                    if passphrase is not None:
                        passphrase[:] = b"\x00" * len(passphrase)
                    if prepared is not None:
                        prepared.close()
                    if claimed:
                        native_input.finish(lease)


def _inspect_generation_candidate(
    *,
    keyring_home: str | os.PathLike[str],
    fingerprint: str | None,
    expected_identity: KeyIdentity | None,
    profile: KeyProfile,
    gpg_binary: str | os.PathLike[str],
) -> GeneratedKey | None:
    if fingerprint is None and expected_identity is None:
        raise KeyGenerationError(
            KeyGenerationErrorCode.INVALID_CANDIDATE_SELECTION
        )
    if expected_identity is not None and not isinstance(expected_identity, KeyIdentity):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)
    if not isinstance(profile, KeyProfile):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PROFILE)

    selected_fingerprint: str | None = None
    if fingerprint is not None:
        try:
            selected_fingerprint = normalize_fingerprint(fingerprint)
        except OpenPGPContractError:
            raise KeyGenerationError(
                KeyGenerationErrorCode.INVALID_CANDIDATE_SELECTION
            ) from None

    home = _candidate_home_path(keyring_home)
    home_descriptor, home_identity = _open_candidate_home(home)
    if home_descriptor is None:
        return None
    try:
        before = _snapshot_candidate_home(home_descriptor)
        if not before:
            return None
        executable = _validated_executable(gpg_binary)
        try:
            certificate = _export_public_certificate(
                executable,
                home,
                fingerprint=selected_fingerprint,
            )
        except KeyGenerationError as exc:
            if exc.code is KeyGenerationErrorCode.GENERATED_KEY_INVALID:
                if selected_fingerprint is not None:
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.CANDIDATE_NOT_FOUND
                    ) from None
                raise KeyGenerationError(
                    KeyGenerationErrorCode.CANDIDATE_INVALID
                ) from None
            raise
        try:
            inspection = inspect_key(
                certificate=certificate,
                gpg_binary=executable,
            )
        except KeyInspectionError as inspection_error:
            if inspection_error.code is KeyInspectionErrorCode.AMBIGUOUS_KEY:
                raise KeyGenerationError(
                    KeyGenerationErrorCode.CANDIDATE_AMBIGUOUS
                ) from None
            raise KeyGenerationError(
                KeyGenerationErrorCode.CANDIDATE_INVALID
            ) from None
        if (
            selected_fingerprint is not None
            and inspection.primary_fingerprint != selected_fingerprint
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.CANDIDATE_NOT_FOUND)
        try:
            _validate_generated_components(
                inspection, profile, require_expiry=True
            )
        except KeyGenerationError:
            raise KeyGenerationError(
                KeyGenerationErrorCode.CANDIDATE_INVALID
            ) from None
        if expected_identity is not None:
            _validate_candidate_identity(
                executable,
                home,
                certificate,
                inspection.primary_fingerprint,
                expected_identity,
            )
        result = GeneratedKey(
            public_certificate=certificate,
            primary_fingerprint=inspection.primary_fingerprint,
            subkey_fingerprints=tuple(
                subkey.fingerprint for subkey in inspection.subkeys
            ),
        )
        return result
    finally:
        try:
            _verify_candidate_home_path(home, home_descriptor, home_identity)
            after = _snapshot_candidate_home(home_descriptor)
            if "before" in locals() and after != before:
                raise KeyGenerationError(
                    KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                )
        finally:
            try:
                os.close(home_descriptor)
            except OSError:
                pass


def _candidate_home_path(value: str | os.PathLike[str]) -> Path:
    try:
        return normalize_generation_home_path(value)
    except KeyGenerationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise KeyGenerationError(
            KeyGenerationErrorCode.INVALID_KEYRING_HOME
        ) from None


def _open_candidate_home(home: Path) -> tuple[int | None, tuple[int, ...] | None]:
    try:
        metadata = home.lstat()
    except FileNotFoundError:
        return None, None
    except OSError:
        raise KeyGenerationError(
            KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(home, flags)
        opened = os.fstat(descriptor)
        if _candidate_home_identity(opened) != _candidate_home_identity(metadata):
            raise KeyGenerationError(
                KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
            )
        return descriptor, _candidate_home_identity(opened)
    except KeyGenerationError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise KeyGenerationError(
            KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
        ) from None


def _verify_candidate_home_path(
    home: Path, descriptor: int, expected_identity: tuple[int, ...] | None
) -> None:
    if expected_identity is None:
        raise KeyGenerationError(KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE)
    try:
        _candidate_home_path(home)
        named = home.lstat()
        opened = os.fstat(descriptor)
    except (OSError, KeyGenerationError):
        raise KeyGenerationError(
            KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
        ) from None
    if (
        _candidate_home_identity(named) != expected_identity
        or _candidate_home_identity(opened) != expected_identity
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE)


def _snapshot_candidate_home(descriptor: int) -> tuple[tuple[object, ...], ...]:
    count = [0]
    total_size = [0]
    result: list[tuple[object, ...]] = []
    _snapshot_candidate_directory(
        descriptor,
        (),
        depth=0,
        count=count,
        total_size=total_size,
        result=result,
    )
    return tuple(sorted(result, key=repr))


def _snapshot_candidate_directory(
    descriptor: int,
    parent: tuple[str, ...],
    *,
    depth: int,
    count: list[int],
    total_size: list[int],
    result: list[tuple[object, ...]],
) -> None:
    if depth > _MAX_CANDIDATE_HOME_DEPTH:
        raise KeyGenerationError(KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE)
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                count[0] += 1
                if count[0] > _MAX_CANDIDATE_HOME_ENTRIES:
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                    )
                try:
                    metadata = os.stat(
                        entry.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                    ) from None
                relative = (*parent, entry.name)
                identity = _candidate_file_identity(metadata)
                if stat.S_ISREG(metadata.st_mode):
                    if (
                        metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_nlink != 1
                    ):
                        raise KeyGenerationError(
                            KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                        )
                    total_size[0] += metadata.st_size
                    if total_size[0] > _MAX_CANDIDATE_HOME_BYTES:
                        raise KeyGenerationError(
                            KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                        )
                    result.append((*relative, identity))
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                    )
                if (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                    )
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                child_descriptor = -1
                try:
                    child_descriptor = os.open(
                        entry.name, flags, dir_fd=descriptor
                    )
                    child_info = os.fstat(child_descriptor)
                    if _candidate_file_identity(child_info) != identity:
                        raise KeyGenerationError(
                            KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                        )
                    result.append((*relative, identity))
                    _snapshot_candidate_directory(
                        child_descriptor,
                        relative,
                        depth=depth + 1,
                        count=count,
                        total_size=total_size,
                        result=result,
                    )
                except KeyGenerationError:
                    raise
                except OSError:
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
                    ) from None
                finally:
                    if child_descriptor >= 0:
                        try:
                            os.close(child_descriptor)
                        except OSError:
                            pass
    except KeyGenerationError:
        raise
    except OSError:
        raise KeyGenerationError(
            KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
        ) from None


def _validate_candidate_identity(
    executable: str,
    home: Path,
    certificate: bytes,
    fingerprint: str,
    expected: KeyIdentity,
) -> None:
    del certificate
    check_output = _run_gpg(
        executable,
        home,
        (
            "--with-colons",
            "--with-fingerprint",
            "--no-sig-cache",
            "--list-options",
            "show-sig-subpackets",
            "--check-sigs",
            fingerprint,
        ),
        timeout=_GPG_OUTPUT_TIMEOUT_SECONDS,
    )
    expected_user_id = _expected_user_id(expected).encode("utf-8")
    expected_notations = tuple(
        sorted(f"{name}={value}" for name, value in expected.certification_notations)
    )
    if not _listing_has_expected_selfcert(
        check_output,
        fingerprint=fingerprint,
        expected_user_id=expected_user_id,
        expected_notations=expected_notations,
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.CANDIDATE_INVALID)


def _expected_user_id(identity: KeyIdentity) -> str:
    comment = f" ({identity.comment})" if identity.comment is not None else ""
    email = f" <{identity.email}>" if identity.email is not None else ""
    return f"{identity.name}{comment}{email}"


def _listing_has_expected_selfcert(
    output: bytes,
    *,
    fingerprint: str,
    expected_user_id: bytes,
    expected_notations: tuple[str, ...],
) -> bool:
    selected_fingerprint = fingerprint.upper().encode("ascii")
    primary_key = False
    primary_fingerprint_seen = False
    primary_fingerprint_matches = False
    selected_user_id = False
    signature_notations: list[tuple[str, str, bool]] | None = None
    malformed_notation = False

    def finish_signature() -> bool:
        if signature_notations is None or malformed_notation:
            return False
        actual_notations = tuple(
            sorted(f"{name}={value}" for name, value, _hashed in signature_notations)
        )
        return (
            all(hashed for _name, _value, hashed in signature_notations)
            and actual_notations == expected_notations
        )

    for line in output.splitlines():
        fields = line.split(b":")
        if not fields:
            continue
        record = fields[0]
        if record in {b"pub", b"sub", b"sec", b"ssb"}:
            if finish_signature():
                return True
            signature_notations = None
            malformed_notation = False
            primary_key = record in {b"pub", b"sec"}
            primary_fingerprint_seen = False
            primary_fingerprint_matches = False
            selected_user_id = False
            continue
        if record == b"fpr":
            if primary_key and not primary_fingerprint_seen:
                primary_fingerprint_seen = True
                primary_fingerprint_matches = (
                    len(fields) > 9 and fields[9].upper() == selected_fingerprint
                )
            continue
        if record in {b"uid", b"uat"}:
            if finish_signature():
                return True
            signature_notations = None
            malformed_notation = False
            selected_user_id = False
            if record == b"uid" and primary_key and primary_fingerprint_matches:
                decoded_user_id = (
                    _decode_gpg_colon_string(fields[9])
                    if len(fields) > 9
                    else None
                )
                selected_user_id = decoded_user_id == expected_user_id
            continue
        if record == b"sig":
            if finish_signature():
                return True
            signature_notations = None
            malformed_notation = False
            if (
                selected_user_id
                and len(fields) > 12
                and fields[1][:1] == b"!"
                and fields[10][:2].lower() == b"13"
                and fields[12].upper() == selected_fingerprint
            ):
                signature_notations = []
            continue
        if record == b"spk" and signature_notations is not None:
            if len(fields) <= 4 or fields[1] != b"20":
                continue
            try:
                subpacket_flags = int(fields[2], 16)
                decoded_notation = _decode_gpg_notation_subpacket(fields[4])
            except (ValueError, UnicodeDecodeError):
                decoded_notation = None
                subpacket_flags = 0
            if decoded_notation is None:
                malformed_notation = True
            else:
                name, value = decoded_notation
                signature_notations.append(
                    (name, value, bool(subpacket_flags & 0x01))
                )

    return finish_signature()


def _decode_gpg_colon_string(value: bytes) -> bytes | None:
    decoded = bytearray()
    position = 0
    escapes = {
        ord("a"): 0x07,
        ord("b"): 0x08,
        ord("f"): 0x0C,
        ord("n"): 0x0A,
        ord("r"): 0x0D,
        ord("t"): 0x09,
        ord("v"): 0x0B,
        ord("\\"): 0x5C,
        ord('"'): 0x22,
        ord("'"): 0x27,
    }
    while position < len(value):
        character = value[position]
        position += 1
        if character != ord("\\"):
            decoded.append(character)
            continue
        if position >= len(value):
            return None
        escaped = value[position]
        position += 1
        if escaped == ord("x"):
            digits = value[position : position + 2]
            if len(digits) != 2:
                return None
            try:
                decoded.append(int(digits, 16))
            except ValueError:
                return None
            position += 2
        elif escaped in escapes:
            decoded.append(escapes[escaped])
        elif ord("0") <= escaped <= ord("7"):
            octal = bytearray((escaped,))
            while (
                position < len(value)
                and len(octal) < 3
                and ord("0") <= value[position] <= ord("7")
            ):
                octal.append(value[position])
                position += 1
            decoded.append(int(octal, 8))
        else:
            return None
    return bytes(decoded)


def _decode_gpg_percent_field(value: bytes) -> bytes:
    decoded = bytearray()
    position = 0
    while position < len(value):
        character = value[position]
        if character != ord("%"):
            decoded.append(character)
            position += 1
            continue
        digits = value[position + 1 : position + 3]
        if len(digits) != 2:
            raise ValueError("invalid percent escape")
        decoded.append(int(digits, 16))
        position += 3
    return bytes(decoded)


def _decode_gpg_notation_subpacket(value: bytes) -> tuple[str, str] | None:
    data = _decode_gpg_percent_field(value)
    if len(data) < 8:
        return None
    name_size = int.from_bytes(data[4:6], "big")
    value_size = int.from_bytes(data[6:8], "big")
    if 8 + name_size + value_size != len(data):
        return None
    name = data[8 : 8 + name_size].decode("utf-8")
    notation_value = data[8 + name_size :].decode("utf-8")
    return name, notation_value


def _candidate_home_identity(info: os.stat_result) -> tuple[int, ...]:
    # GnuPG may create and remove temporary lock files during read commands.
    # Entry snapshots below detect persistent changes; this identity pins the
    # selected home itself without rejecting harmless directory timestamp churn.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid)


def _candidate_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


def _validate_request(
    identity: KeyIdentity,
    profile: KeyProfile,
    passphrase_reference: SecretReference | None,
    secret_resolver: SecretResolver,
    native_input: NativeInputContext | None,
) -> None:
    if not isinstance(identity, KeyIdentity):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)
    if (
        not isinstance(profile, KeyProfile)
        or profile != RSA_4096_TWO_YEAR_PROFILE
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PROFILE)
    if not isinstance(secret_resolver, SecretResolver):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_SECRET_REFERENCE)
    if native_input is None:
        if not isinstance(passphrase_reference, SecretReference):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_SECRET_REFERENCE)
    elif (
        not isinstance(native_input, NativeInputContext)
        or passphrase_reference is not None
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_SECRET_REFERENCE)


def _resolve_generation_passphrase(
    reference: SecretReference | None,
    secret_resolver: SecretResolver,
    *,
    operation: InputOperation | None,
    native_input: NativeInputContext | None,
) -> bytearray:
    if native_input is None:
        if not isinstance(reference, SecretReference):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_SECRET_REFERENCE)
        try:
            secret = secret_resolver.resolve(reference)
        except Exception:
            raise KeyGenerationError(
                KeyGenerationErrorCode.SECRET_RESOLUTION_FAILED
            ) from None
        payload = _passphrase_payload(secret)
        del secret
        if payload is None:
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PASSPHRASE)
        return payload

    if operation is None or reference is not None:
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_SECRET_REFERENCE)
    try:
        payload = secret_resolver.resolve_buffer(
            None, operation=operation, native_input=native_input,
        )
    except Exception:
        raise KeyGenerationError(
            KeyGenerationErrorCode.SECRET_RESOLUTION_FAILED
        ) from None
    if (
        type(payload) is not bytearray
        or not payload
        or len(payload) > _MAX_PASSPHRASE_BYTES
        or any(marker in payload for marker in (b"\x00", b"\r", b"\n"))
    ):
        if isinstance(payload, bytearray):
            payload[:] = b"\x00" * len(payload)
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PASSPHRASE)
    try:
        payload.append(ord("\n"))
    except BaseException:
        payload[:] = b"\x00" * len(payload)
        raise KeyGenerationError(
            KeyGenerationErrorCode.INVALID_PASSPHRASE
        ) from None
    return payload


def _generation_checkpoint(
    native_input: NativeInputContext,
    operation: InputOperation,
    *,
    selection: GenerationHomeSelection | None = None,
    prepared: PreparedGenerationHome | None = None,
) -> None:
    try:
        native_input.checkpoint(operation)
        if prepared is not None:
            prepared.check_current()
        elif selection is not None:
            check_generation_selection(selection)
    except Exception:
        raise KeyGenerationError(
            KeyGenerationErrorCode.SECRET_RESOLUTION_FAILED
        ) from None


def _generation_harden(
    home: Path,
    prepared: PreparedGenerationHome | None,
    native_input: NativeInputContext | None,
    operation: InputOperation | None,
) -> None:
    if prepared is None:
        _harden_tree(home)
        return
    if native_input is None or operation is None:
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
    _generation_checkpoint(native_input, operation, prepared=prepared)
    prepared.harden()
    _generation_checkpoint(native_input, operation, prepared=prepared)


def _prepared_path_is_current(prepared: PreparedGenerationHome) -> bool:
    try:
        prepared.check_current()
        return True
    except KeyGenerationError:
        return False


def _validated_executable(value: str | os.PathLike[str]) -> str:
    try:
        executable = os.fspath(value)
    except (TypeError, ValueError):
        raise KeyGenerationError(KeyGenerationErrorCode.GPG_UNAVAILABLE) from None
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise KeyGenerationError(KeyGenerationErrorCode.GPG_UNAVAILABLE)
    return executable


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


def _prepare_keyring_home(value: str | os.PathLike[str]) -> Path:
    try:
        home = normalize_generation_home_path(value)
        try:
            metadata = home.lstat()
        except FileNotFoundError:
            # mkdir claims the leaf atomically. Never resolve a missing leaf:
            # another user can create a symlink there under a sticky parent.
            home.mkdir(mode=0o700)
            metadata = home.lstat()
        else:
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME)
            if next(home.iterdir(), None) is not None:
                raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_NOT_EMPTY)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME)
        os.chmod(home, 0o700)
    except KeyGenerationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME) from None
    return home

def _write_batch_file(
    home: Path,
    identity: KeyIdentity,
    initial_expiry: date,
    *,
    directory_fd: int | None = None,
    before_write: Callable[[], None] | None = None,
) -> Path:
    lines = [
        "Key-Type: RSA",
        "Key-Length: 4096",
        "Key-Usage: sign",
        "Subkey-Type: RSA",
        "Subkey-Length: 4096",
        "Subkey-Usage: encrypt",
        f"Expire-Date: {initial_expiry.isoformat()}",
        f"Name-Real: {identity.name}",
    ]
    if identity.comment is not None:
        lines.append(f"Name-Comment: {identity.comment}")
    if identity.email is not None:
        lines.append(f"Name-Email: {identity.email}")
    lines.append("%commit")
    contents = ("\n".join(lines) + "\n").encode("utf-8")
    name = ".localsetup-keygen.batch"
    path = home / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created_identity: tuple[int, int, int, int] | None = None

    def unlink_batch_if_owned() -> None:
        if created_identity is None:
            return
        try:
            info = (
                path.lstat() if directory_fd is None
                else os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            )
            current = (
                info.st_dev, info.st_ino, info.st_uid,
                stat.S_IFMT(info.st_mode),
            )
            if current != created_identity:
                return
            if directory_fd is None:
                path.unlink()
            else:
                os.unlink(name, dir_fd=directory_fd)
        except OSError:
            pass

    try:
        if before_write is not None:
            before_write()
        if directory_fd is None:
            descriptor = os.open(path, flags, 0o600)
        else:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        opened = os.fstat(descriptor)
        created_identity = (
            opened.st_dev, opened.st_ino, opened.st_uid,
            stat.S_IFMT(opened.st_mode),
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            if before_write is not None:
                before_write()
            stream.write(contents)
            os.fchmod(stream.fileno(), 0o600)
    except KeyGenerationError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        unlink_batch_if_owned()
        raise
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        unlink_batch_if_owned()
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
    return path


def _export_public_certificate(
    executable: str,
    home: Path,
    *,
    fingerprint: str | None,
    before_launch: Callable[[], None] | None = None,
) -> bytes:
    arguments = ("--armor", "--export")
    if fingerprint is not None:
        arguments += (fingerprint,)
    certificate = _run_generation_gpg(
        executable, home, arguments, timeout=_GPG_OUTPUT_TIMEOUT_SECONDS,
        before_launch=before_launch,
    )
    if not certificate:
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID)
    return certificate


def _inspect_public_certificate(
    certificate: bytes,
    executable: str,
    *,
    before_launch: Callable[[], None] | None = None,
) -> KeyInspection:
    try:
        if before_launch is not None:
            before_launch()
        result = inspect_key(certificate=certificate, gpg_binary=executable)
        if before_launch is not None:
            before_launch()
        return result
    except KeyInspectionError:
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID) from None


def _validate_generated_components(
    inspection: KeyInspection,
    profile: KeyProfile,
    *,
    require_expiry: bool,
) -> None:
    primary = inspection.primary
    if len(inspection.subkeys) != 1:
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID)
    subkey = inspection.subkeys[0]
    # GnuPG's colon ``pub`` capabilities aggregate usage advertised by subkeys.
    # Lowercase pub/sub flags describe each component; uppercase pub flags are
    # aggregate metadata that must not satisfy the direct primary-key role.
    if (
        primary.fingerprint == subkey.fingerprint
        or not primary.is_primary
        or primary.algorithm != profile.algorithm
        or primary.bits != profile.bits
        or primary.direct_capabilities
        != frozenset({KeyCapability.SIGN, KeyCapability.CERTIFY})
        or not primary.aggregate_capabilities.issubset(
            primary.direct_capabilities | {KeyCapability.ENCRYPT}
        )
        or primary.revoked
        or primary.expired
        or primary.disabled
        or subkey.is_primary
        or subkey.algorithm != profile.algorithm
        or subkey.bits != profile.bits
        or subkey.capabilities != frozenset({KeyCapability.ENCRYPT})
        or subkey.direct_capabilities != frozenset({KeyCapability.ENCRYPT})
        or subkey.revoked
        or subkey.expired
        or subkey.disabled
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID)
    if require_expiry:
        for record in inspection.records:
            try:
                expected = profile.expiry_date(record.created_on)
            except Exception:
                raise KeyGenerationError(
                    KeyGenerationErrorCode.GENERATED_KEY_INVALID
                ) from None
            if record.expires_on != expected:
                raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID)
    elif any(record.expires_on is None for record in inspection.records):
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID)


def _set_component_expiry(
    executable: str,
    home: Path,
    primary_fingerprint: str,
    record: KeyRecord,
    profile: KeyProfile,
    passphrase: bytearray,
    *,
    certification_options: tuple[str, ...] = (),
    before_launch: Callable[[], None] | None = None,
) -> None:
    try:
        expires_on = profile.expiry_date(record.created_on)
    except Exception:
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID) from None
    subkey = () if record.is_primary else (record.fingerprint,)
    _run_generation_gpg(
        executable,
        home,
        (
            *certification_options,
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--quick-set-expire",
            primary_fingerprint,
            expires_on.isoformat(),
            *subkey,
        ),
        passphrase=passphrase,
        timeout=_GPG_MUTATION_TIMEOUT_SECONDS,
        before_launch=before_launch,
    )


def _certification_gpg_options(
    notations: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    if not notations:
        return ()
    # GnuPG expands percent sequences in notation arguments. Double each
    # percent in the value so arbitrary caller metadata remains literal. Keep
    # it as one argv item, never in the batch file; --utf8-strings preserves
    # Unicode when the child locale is C.
    options = ["--utf8-strings"]
    for name, value in notations:
        escaped_value = value.replace("%", "%%")
        options.extend(("--cert-notation", f"{name}={escaped_value}"))
    return tuple(options)


def _run_gpg(
    executable: str,
    home: Path,
    arguments: tuple[str, ...],
    *,
    passphrase: bytearray | None = None,
    timeout: float,
    before_launch: Callable[[], None] | None = None,
) -> bytes:
    command = [
        executable,
        "--no-options",
        "--batch",
        "--no-tty",
        "--no-auto-key-retrieve",
        "--no-auto-check-trustdb",
    ]
    if passphrase is None:
        command.append("--no-autostart")
    command.extend(("--homedir", os.fspath(home), *arguments))
    if passphrase is not None:
        if not passphrase:
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PASSPHRASE)
        stdin = subprocess.PIPE
    else:
        stdin = subprocess.DEVNULL
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.fspath(home),
        "GNUPGHOME": os.fspath(home),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
        "TMPDIR": os.fspath(home),
    }
    if before_launch is not None:
        before_launch()
    try:
        process = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=environment,
        )
    except (OSError, ValueError):
        raise KeyGenerationError(KeyGenerationErrorCode.GPG_UNAVAILABLE) from None
    assert process.stdout is not None and process.stderr is not None
    stdout = cast(BinaryIO, process.stdout)
    stderr = cast(BinaryIO, process.stderr)
    stdin_pipe = cast(BinaryIO | None, process.stdin)
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, label in ((stdout, "stdout"), (stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                _ = selector.register(pipe, selectors.EVENT_READ, label)
            sent = 0
            if passphrase is not None:
                if stdin_pipe is None:
                    raise KeyGenerationError(
                        KeyGenerationErrorCode.GPG_UNAVAILABLE
                    )
                os.set_blocking(stdin_pipe.fileno(), False)
                _ = selector.register(stdin_pipe, selectors.EVENT_WRITE, "stdin")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KeyGenerationError(KeyGenerationErrorCode.GPG_TIMEOUT)
                events = selector.select(remaining)
                if not events:
                    raise KeyGenerationError(KeyGenerationErrorCode.GPG_TIMEOUT)
                for key, _ in events:
                    selector_data = cast(str, key.data)
                    if selector_data == "stdin":
                        if stdin_pipe is None or passphrase is None:
                            raise KeyGenerationError(
                                KeyGenerationErrorCode.GPG_UNAVAILABLE
                            )
                        try:
                            sent += os.write(
                                stdin_pipe.fileno(), memoryview(passphrase)[sent:]
                            )
                        except BrokenPipeError:
                            sent = len(passphrase)
                        if sent >= len(passphrase):
                            _ = selector.unregister(stdin_pipe)
                            stdin_pipe.close()
                        continue
                    pipe = stdout if selector_data == "stdout" else stderr
                    try:
                        chunk = os.read(pipe.fileno(), _GPG_READ_SIZE)
                    except OSError:
                        raise KeyGenerationError(
                            KeyGenerationErrorCode.GPG_UNAVAILABLE
                        ) from None
                    if not chunk:
                        _ = selector.unregister(pipe)
                        pipe.close()
                        continue
                    total += len(chunk)
                    if total > _MAX_GPG_OUTPUT_BYTES:
                        raise KeyGenerationError(
                            KeyGenerationErrorCode.GPG_OUTPUT_LIMIT
                        )
                    if selector_data == "stdout":
                        output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise KeyGenerationError(KeyGenerationErrorCode.GPG_TIMEOUT)
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise KeyGenerationError(KeyGenerationErrorCode.GPG_TIMEOUT) from None
        if return_code != 0:
            raise KeyGenerationError(KeyGenerationErrorCode.GPG_FAILED)
    except KeyGenerationError:
        _terminate(process)
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        _terminate(process)
        raise KeyGenerationError(KeyGenerationErrorCode.GPG_UNAVAILABLE) from None
    finally:
        for pipe in (stdin_pipe, stdout, stderr):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass
    return bytes(output)


def _run_generation_gpg(
    executable: str,
    home: Path,
    arguments: tuple[str, ...],
    *,
    passphrase: bytearray | None = None,
    timeout: float,
    before_launch: Callable[[], None] | None = None,
) -> bytes:
    if before_launch is None:
        return _run_gpg(
            executable, home, arguments, passphrase=passphrase, timeout=timeout,
        )
    result = _run_gpg(
        executable, home, arguments, passphrase=passphrase, timeout=timeout,
        before_launch=before_launch,
    )
    before_launch()
    return result


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


def _stop_gpg_agent(
    home: Path, *, before_launch: Callable[[], None] | None = None,
) -> None:
    """Stop only the GnuPG agent bound to this isolated home."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.fspath(home),
        "GNUPGHOME": os.fspath(home),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
        "TMPDIR": os.fspath(home),
    }
    if before_launch is not None:
        before_launch()
    try:
        result = subprocess.run(
            (
                "gpgconf",
                "--homedir",
                os.fspath(home),
                "--kill",
                "gpg-agent",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=environment,
            timeout=_GPG_OUTPUT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
    if result.returncode != 0:
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)


def _harden_tree(root: Path) -> None:
    try:
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
        os.chmod(root, 0o700)
        with os.scandir(root) as entries:
            for entry in entries:
                _harden_entry(entry)
    except KeyGenerationError:
        raise
    except OSError:
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None


def _harden_entry(entry: os.DirEntry[str]) -> None:
    try:
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
        if stat.S_ISDIR(info.st_mode):
            os.chmod(entry.path, 0o700, follow_symlinks=False)
            with os.scandir(entry.path) as entries:
                for nested in entries:
                    _harden_entry(nested)
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
            os.chmod(entry.path, 0o600, follow_symlinks=False)
        elif not stat.S_ISSOCK(info.st_mode):
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
    except KeyGenerationError:
        raise
    except OSError:
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
