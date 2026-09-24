"""Bounded GnuPG execution and protected-backup pipeline lifecycle."""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, cast

from . import recovery_io
from .contracts import KeyCapability, OpenPGPContractError, normalize_fingerprint
from .keys import KeyInspection
from .recovery_models import (
    RecoveryError,
    RecoveryErrorCode,
    _GPG_READ_SIZE,
    _GPG_TIMEOUT_SECONDS,
    _MAX_BACKUP_BYTES,
    _MAX_DIAGNOSTIC_BYTES,
    _MAX_PLAINTEXT_BYTES,
)

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
        recovery_io._clear_restore_home(target_home)
        raise
    except Exception:
        recovery_io._clear_restore_home(target_home)
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
