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

from .contracts import KeyCapability, KeyProfile, RSA_4096_TWO_YEAR_PROFILE
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
