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
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final, cast

from .contracts import KeyCapability, KeyProfile, RSA_4096_TWO_YEAR_PROFILE
from .keys import KeyInspection, KeyInspectionError, KeyRecord, inspect_key
from .secrets import SecretReference, SecretResolver

__all__ = [
    "GeneratedKey",
    "KeyGenerationError",
    "KeyGenerationErrorCode",
    "KeyIdentity",
    "generate_key",
]

_MAX_IDENTITY_FIELD: Final = 254
_MAX_PASSPHRASE_BYTES: Final = 4096
_MAX_GPG_OUTPUT_BYTES: Final = 1_048_576
_GPG_GENERATION_TIMEOUT_SECONDS: Final = 120.0
_GPG_MUTATION_TIMEOUT_SECONDS: Final = 30.0
_GPG_OUTPUT_TIMEOUT_SECONDS: Final = 15.0
_GPG_READ_SIZE: Final = 8192


class KeyGenerationErrorCode(StrEnum):
    """Stable, non-secret failures raised by protected key generation."""

    INVALID_IDENTITY = "INVALID_IDENTITY"
    INVALID_PROFILE = "INVALID_PROFILE"
    INVALID_SECRET_REFERENCE = "INVALID_SECRET_REFERENCE"
    SECRET_RESOLUTION_FAILED = "SECRET_RESOLUTION_FAILED"
    INVALID_PASSPHRASE = "INVALID_PASSPHRASE"
    INVALID_KEYRING_HOME = "INVALID_KEYRING_HOME"
    KEYRING_NOT_EMPTY = "KEYRING_NOT_EMPTY"
    KEYRING_UNAVAILABLE = "KEYRING_UNAVAILABLE"
    GPG_UNAVAILABLE = "GPG_UNAVAILABLE"
    GPG_FAILED = "GPG_FAILED"
    GPG_TIMEOUT = "GPG_TIMEOUT"
    GPG_OUTPUT_LIMIT = "GPG_OUTPUT_LIMIT"
    GENERATED_KEY_INVALID = "GENERATED_KEY_INVALID"


class KeyGenerationError(RuntimeError):
    """Safe generation failure with no identity, secret, or process details."""

    def __init__(self, code: KeyGenerationErrorCode) -> None:
        self.code = KeyGenerationErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class KeyIdentity:
    """Explicit user-ID fields for a generated key."""

    name: str
    email: str | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        if not _valid_identity_field(self.name, required=True):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)
        if self.email is not None and (
            not _valid_identity_field(self.email, required=True)
            or "@" not in self.email
            or any(character.isspace() for character in self.email)
            or "<" in self.email
            or ">" in self.email
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)
        if self.comment is not None and not _valid_identity_field(
            self.comment, required=True
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """Nonsecret generation result; the private key stays in ``keyring_home``."""

    public_certificate: bytes
    primary_fingerprint: str
    subkey_fingerprints: tuple[str, ...]


def _valid_identity_field(value: object, *, required: bool) -> bool:
    if not isinstance(value, str) or len(value) > _MAX_IDENTITY_FIELD:
        return False
    if required and not value.strip():
        return False
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in value
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def generate_key(
    *,
    identity: KeyIdentity,
    profile: KeyProfile,
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    keyring_home: str | os.PathLike[str],
    gpg_binary: str | os.PathLike[str] = "gpg",
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
        )
    except KeyGenerationError as exc:
        code = exc.code
    except Exception:
        code = KeyGenerationErrorCode.KEYRING_UNAVAILABLE
    del identity, profile, passphrase_reference, secret_resolver, keyring_home, gpg_binary
    raise KeyGenerationError(code)


def _generate_key(
    *,
    identity: KeyIdentity,
    profile: KeyProfile,
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    keyring_home: str | os.PathLike[str],
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> GeneratedKey:
    """Generate an RSA-4096 signing key and one encryption subkey.

    ``keyring_home`` must be a new or empty, non-symlink directory selected by
    the caller. It becomes the protected private store; this function never
    enrolls the generated key and never exports secret-key material.
    """
    _validate_request(identity, profile, passphrase_reference, secret_resolver)
    executable = _validated_executable(gpg_binary)
    home = _prepare_keyring_home(keyring_home)

    batch_file: Path | None = None
    passphrase: bytearray | None = None
    agent_may_be_running = False
    try:
        try:
            secret = secret_resolver.resolve(passphrase_reference)
        except Exception:
            raise KeyGenerationError(
                KeyGenerationErrorCode.SECRET_RESOLUTION_FAILED
            ) from None
        passphrase = _passphrase_payload(secret)
        del secret
        if passphrase is None:
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PASSPHRASE)

        initial_expiry = profile.expiry_date(datetime.now(timezone.utc).date())
        batch_file = _write_batch_file(home, identity, initial_expiry)
        agent_may_be_running = True
        _run_gpg(
            executable,
            home,
            (
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--generate-key",
                os.fspath(batch_file),
            ),
            passphrase=passphrase,
            timeout=_GPG_GENERATION_TIMEOUT_SECONDS,
        )
        _harden_tree(home)

        initial_certificate = _export_public_certificate(
            executable, home, fingerprint=None
        )
        initial_inspection = _inspect_public_certificate(
            initial_certificate, executable
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
        )
        for subkey in initial_inspection.subkeys:
            _set_component_expiry(
                executable,
                home,
                initial_inspection.primary_fingerprint,
                subkey,
                profile,
                passphrase,
            )
        _harden_tree(home)

        certificate = _export_public_certificate(
            executable, home, fingerprint=initial_inspection.primary_fingerprint
        )
        inspection = _inspect_public_certificate(certificate, executable)
        _validate_generated_components(inspection, profile, require_expiry=True)
        if batch_file is not None:
            try:
                batch_file.unlink()
            except OSError:
                raise KeyGenerationError(
                    KeyGenerationErrorCode.KEYRING_UNAVAILABLE
                ) from None
            batch_file = None
        _harden_tree(home)
        return GeneratedKey(
            public_certificate=certificate,
            primary_fingerprint=inspection.primary_fingerprint,
            subkey_fingerprints=tuple(
                subkey.fingerprint for subkey in inspection.subkeys
            ),
        )
    except KeyGenerationError:
        raise
    except (OSError, ValueError):
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
    finally:
        try:
            if batch_file is not None:
                try:
                    batch_file.unlink(missing_ok=True)
                except OSError:
                    pass
            if agent_may_be_running:
                _stop_gpg_agent(home)
        finally:
            try:
                _harden_tree(home)
            finally:
                if passphrase is not None:
                    passphrase[:] = b"\x00" * len(passphrase)


def _validate_request(
    identity: KeyIdentity,
    profile: KeyProfile,
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
) -> None:
    if not isinstance(identity, KeyIdentity):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)
    if (
        not isinstance(profile, KeyProfile)
        or profile != RSA_4096_TWO_YEAR_PROFILE
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_PROFILE)
    if not isinstance(passphrase_reference, SecretReference) or not isinstance(
        secret_resolver, SecretResolver
    ):
        raise KeyGenerationError(KeyGenerationErrorCode.INVALID_SECRET_REFERENCE)


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
        requested = Path(value).expanduser().absolute()
        if ".." in requested.parts:
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME)
        _require_private_ancestors(requested)
        home = requested
        if _is_ambient_home(home):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME)
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

def _require_private_ancestors(selected: Path) -> None:
    """Reject parent paths another Unix identity can replace during generation.

    A root-owned sticky directory (such as /tmp) protects entries owned by the
    caller; otherwise every ancestor must be non-writable by other identities.
    Same-UID processes share the caller's private-key authority by design.
    """
    for parent in selected.parents:
        metadata = parent.lstat()
        mode = metadata.st_mode
        if not stat.S_ISDIR(mode) or metadata.st_uid not in (0, os.geteuid()):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME)
        if stat.S_IMODE(mode) & 0o022 and not (
            metadata.st_uid == 0 and mode & stat.S_ISVTX
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_KEYRING_HOME)


def _is_ambient_home(home: Path) -> bool:
    configured = os.environ.get("GNUPGHOME")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(Path.home() / ".gnupg")
    for candidate in candidates:
        try:
            ambient = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if home == ambient or home.is_relative_to(ambient):
            return True
    return False


def _write_batch_file(
    home: Path, identity: KeyIdentity, initial_expiry: date
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
    path = home / ".localsetup-keygen.batch"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(contents)
        os.chmod(path, 0o600)
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
    return path


def _export_public_certificate(
    executable: str, home: Path, *, fingerprint: str | None
) -> bytes:
    arguments = ("--armor", "--export")
    if fingerprint is not None:
        arguments += (fingerprint,)
    certificate = _run_gpg(
        executable, home, arguments, timeout=_GPG_OUTPUT_TIMEOUT_SECONDS
    )
    if not certificate:
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID)
    return certificate


def _inspect_public_certificate(certificate: bytes, executable: str) -> KeyInspection:
    try:
        return inspect_key(certificate=certificate, gpg_binary=executable)
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
) -> None:
    try:
        expires_on = profile.expiry_date(record.created_on)
    except Exception:
        raise KeyGenerationError(KeyGenerationErrorCode.GENERATED_KEY_INVALID) from None
    subkey = () if record.is_primary else (record.fingerprint,)
    _run_gpg(
        executable,
        home,
        (
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
    )


def _run_gpg(
    executable: str,
    home: Path,
    arguments: tuple[str, ...],
    *,
    passphrase: bytearray | None = None,
    timeout: float,
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


def _stop_gpg_agent(home: Path) -> None:
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
