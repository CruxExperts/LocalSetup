"""Bounded file I/O shared by signed OpenPGP stream seal and open operations."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import secrets
import selectors
import stat
import struct
import subprocess
import sys
import time
from typing import Callable, Iterable

from . import envelope
from .contracts import (
    EnvelopeHeader, EnvelopePolicy, LocalTrust, OpenPGPContractError,
    validate_envelope_header, validate_envelope_participants,
)
from .secrets import SecretReference, SecretResolver

_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    path: Path
    device: int
    inode: int
    uid: int
    mode: int


@dataclass(frozen=True, slots=True)
class FileEnvelopeLimits:
    """Explicit byte, temporary disk, diagnostic, and whole-operation time limits."""

    max_plaintext_bytes: int
    max_envelope_bytes: int
    max_temporary_bytes: int
    max_diagnostics_bytes: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        values = (
            self.max_plaintext_bytes, self.max_envelope_bytes,
            self.max_temporary_bytes, self.max_diagnostics_bytes,
        )
        if (
            any(type(value) is not int or value <= 0 for value in values)
            or type(self.timeout_seconds) not in (int, float)
            or not 0 < self.timeout_seconds <= 3600
        ):
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)


class OperationBudget:
    """One deadline and cancellation decision for the complete file operation."""

    def __init__(self, limits: FileEnvelopeLimits, cancel_check: Callable[[], bool] | None):
        if cancel_check is not None and not callable(cancel_check):
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
        self.deadline = time.monotonic() + limits.timeout_seconds
        self.cancel_check = cancel_check
        self._additional_checks: list[Callable[[], None]] = []

    def add_checkpoint(self, check: Callable[[], None]) -> None:
        if not callable(check):
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
        self._additional_checks.append(check)

    def checkpoint(self) -> None:
        try:
            if self.cancel_check is not None and self.cancel_check():
                raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.CANCELLED)
        except envelope.EnvelopeError:
            raise
        except Exception:
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.CANCELLED) from None
        if time.monotonic() >= self.deadline:
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.GPG_TIMEOUT)
        for check in tuple(self._additional_checks):
            check()


class AnchoredFile:
    """Open an absolute regular file without following path components."""

    def __init__(self, path: str | os.PathLike[str], *, private: bool = False) -> None:
        self.directory, self.name, self.path, self.parent_chain = _open_parent(
            path, private=private,
        )
        self.private = private
        self.file = -1
        try:
            self.file = os.open(
                self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self.directory,
            )
            info = os.fstat(self.file)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or (private and stat.S_IMODE(info.st_mode) & 0o077)
            ):
                raise OSError("unsafe input")
            self.identity = _identity(info)
        except BaseException:
            self.close()
            raise

    @property
    def size(self) -> int:
        """Byte length captured with the file's anchored identity."""
        return self.identity[4]

    def unchanged(self) -> bool:
        try:
            named = os.stat(self.name, dir_fd=self.directory, follow_symlinks=False)
            return (
                stat.S_ISREG(named.st_mode)
                and _identity(os.fstat(self.file)) == self.identity
                and _identity(named) == self.identity
                and _parent_chain_matches(
                    self.path, self.directory, self.parent_chain, private=self.private,
                )
            )
        except OSError:
            return False

    def close(self) -> None:
        if self.file >= 0:
            os.close(self.file)
            self.file = -1
        if self.directory >= 0:
            os.close(self.directory)
            self.directory = -1

    def __enter__(self) -> AnchoredFile:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ExclusiveOutput:
    """Keep owned 0600 staging files hidden until one exclusive publication."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.directory, self.name, self.path, self.parent_chain = _open_parent(
            path, private=True,
        )
        self.parent_identity = self.parent_chain[-1]
        self.owned: dict[str, tuple[int, int, int, int]] = {}
        self.published_identity: tuple[int, int] | None = None
        try:
            try:
                os.stat(self.name, dir_fd=self.directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise OSError("output exists")
        except BaseException:
            self.close()
            raise

    def stage(self) -> tuple[str, int]:
        if not self.unchanged():
            raise OSError("output selection changed")
        for _ in range(4):
            name = ".ls-openpgp-" + secrets.token_hex(16)
            try:
                descriptor = os.open(
                    name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=self.directory,
                )
            except FileExistsError:
                continue
            expected: tuple[int, int, int, int] | None = None
            try:
                info = os.fstat(descriptor)
                expected = _stage_identity(info)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    raise OSError("unsafe stage")
                self.owned[name] = expected
            except BaseException:
                os.close(descriptor)
                if expected is not None:
                    try:
                        named = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                        if _stage_identity(named) == expected:
                            os.unlink(name, dir_fd=self.directory)
                    except OSError:
                        pass
                raise
            return name, descriptor
        raise OSError("temporary name unavailable")

    def publish(self, name: str) -> Path:
        expected = self.owned.get(name)
        if expected is None or not self.unchanged():
            raise OSError("unowned output")
        stage_fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=self.directory,
        )
        try:
            info = os.fstat(stage_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or _stage_identity(info) != expected
            ):
                raise OSError("unsafe stage")
            if not _fd_publication_available():
                raise envelope.EnvelopeError(
                    envelope.EnvelopeErrorCode.FILE_PUBLICATION_UNAVAILABLE
                )
            # AT_SYMLINK_FOLLOW resolves this kernel-owned entry to the held
            # inode, even if another process replaces the stage pathname.
            try:
                os.link(
                    f"/proc/self/fd/{stage_fd}", self.name,
                    dst_dir_fd=self.directory, follow_symlinks=True,
                )
            except OSError as exc:
                if exc.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    raise envelope.EnvelopeError(
                        envelope.EnvelopeErrorCode.FILE_PUBLICATION_UNAVAILABLE
                    ) from None
                raise
            except NotImplementedError:
                raise envelope.EnvelopeError(
                    envelope.EnvelopeErrorCode.FILE_PUBLICATION_UNAVAILABLE
                ) from None
            # Record ownership immediately after the link. Even a subsequent
            # descriptor-close failure must leave close() able to roll it back.
            self.published_identity = expected[:2]
        finally:
            os.close(stage_fd)
        # Creating the link changes ctime, so ownership cleanup uses inode
        # identity rather than the complete pre-link stage identity.
        try:
            named_stage = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
            named_output = os.stat(self.name, dir_fd=self.directory, follow_symlinks=False)
        except OSError:
            self.rollback_published()
            raise
        if (
            _stage_identity(named_stage) != expected
            or _stage_identity(named_output) != expected
        ):
            self.rollback_published()
            raise OSError("stage identity changed")
        os.fsync(self.directory)
        if not self.unchanged():
            self.rollback_published()
            raise OSError("output selection changed")
        return self.path

    def unchanged(self) -> bool:
        if self.directory < 0 or not _parent_chain_matches(
            self.path, self.directory, self.parent_chain, private=True,
        ):
            return False
        try:
            held = _directory_identity(self.path.parent, os.fstat(self.directory))
            if held != self.parent_identity:
                return False
            named = os.stat(self.name, dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            return self.published_identity is None
        except OSError:
            return False
        return self.published_identity is not None and (
            named.st_dev, named.st_ino,
        ) == self.published_identity

    def rollback_published(self) -> None:
        if self.published_identity is None:
            return
        try:
            info = os.stat(self.name, dir_fd=self.directory, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == self.published_identity:
                os.unlink(self.name, dir_fd=self.directory)
                try:
                    os.fsync(self.directory)
                except OSError:
                    # The plaintext name is already gone. A failed cleanup
                    # directory sync must not prevent descriptor closure.
                    pass
        except FileNotFoundError:
            pass
        self.published_identity = None

    def close(self) -> None:
        if self.directory < 0:
            return
        try:
            for name, expected in self.owned.items():
                try:
                    info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                    if _stage_identity(info) == expected:
                        os.unlink(name, dir_fd=self.directory)
                except OSError:
                    pass
            if self.published_identity is not None:
                # The caller clears this flag only after the result is complete.
                self.rollback_published()
        finally:
            os.close(self.directory)
            self.directory = -1

    def __enter__(self) -> ExclusiveOutput:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode),
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _stage_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))


def _fd_publication_available() -> bool:
    """Require Python linkat support and Linux's held-fd procfs source."""
    return (
        sys.platform.startswith("linux")
        and os.path.isdir("/proc/self/fd")
        and os.link in os.supports_dir_fd
        and os.link in os.supports_follow_symlinks
    )


def _directory_identity(path: Path, info: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(
        path=path, device=info.st_dev, inode=info.st_ino,
        uid=info.st_uid, mode=stat.S_IMODE(info.st_mode),
    )


def _validate_parent_directory(
    info: os.stat_result, *, private: bool, final: bool,
) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or (stat.S_IMODE(info.st_mode) & 0o022 and not (
            info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        ))
        or (private and final and (
            info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ))
    ):
        raise OSError("unsafe directory")


def _parent_chain_matches(
    path: Path, held_fd: int, expected: tuple[_DirectoryIdentity, ...], *, private: bool,
) -> bool:
    current = -1
    try:
        if not path.is_absolute() or len(expected) != len(path.parts) - 1:
            return False
        current = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current_path = Path(path.anchor)
        root_info = os.fstat(current)
        observed = [_directory_identity(current_path, root_info)]
        _validate_parent_directory(
            root_info, private=private and len(path.parts) == 2, final=len(path.parts) == 2,
        )
        for index, part in enumerate(path.parts[1:-1]):
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            try:
                current_path /= part
                info = os.fstat(next_fd)
                _validate_parent_directory(
                    info, private=private, final=index == len(path.parts[1:-1]) - 1,
                )
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if _directory_identity(current_path, named) != _directory_identity(current_path, info):
                    raise OSError("directory changed")
                observed.append(_directory_identity(current_path, info))
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current)
            current = next_fd
        if tuple(observed) != expected:
            return False
        return _directory_identity(path.parent, os.fstat(current)) == expected[-1] == _directory_identity(
            path.parent, os.fstat(held_fd),
        )
    except (OSError, ValueError, IndexError):
        return False
    finally:
        if current >= 0:
            os.close(current)


def _open_parent(
    path: str | os.PathLike[str], *, private: bool,
) -> tuple[int, str, Path, tuple[_DirectoryIdentity, ...]]:
    try:
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or "\x00" in raw_path:
            raise OSError("unsafe path")
        if any(part in {".", ".."} for part in raw_path.split(os.sep)):
            raise OSError("unsafe path")
        selected = Path(raw_path)
        if (
            not selected.is_absolute()
            or selected.name in {"", ".", ".."}
        ):
            raise OSError("unsafe path")
        descriptor = os.open(selected.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        chain: list[_DirectoryIdentity] = []
        try:
            root_info = os.fstat(descriptor)
            _validate_parent_directory(root_info, private=False, final=False)
            chain.append(_directory_identity(Path(selected.anchor), root_info))
            for index, part in enumerate(selected.parts[1:-1]):
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    current_path = Path(selected.anchor).joinpath(*selected.parts[1:index + 2])
                    info = os.fstat(next_fd)
                    final = index == len(selected.parts[1:-1]) - 1
                    _validate_parent_directory(info, private=private, final=final)
                    named = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                    if _directory_identity(current_path, named) != _directory_identity(current_path, info):
                        raise OSError("directory changed")
                    chain.append(_directory_identity(current_path, info))
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(descriptor)
                descriptor = next_fd
            if private and not selected.parts[1:-1]:
                parent_info = os.fstat(descriptor)
                if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
                    raise OSError("unsafe private directory")
            return descriptor, selected.name, selected, tuple(chain)
        except BaseException:
            os.close(descriptor)
            raise
    except (OSError, TypeError, ValueError):
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT) from None


def hash_file(
    source: AnchoredFile, *, offset: int, maximum: int,
    budget: OperationBudget | None = None,
) -> tuple[str, int]:
    import hashlib

    digest = hashlib.sha256()
    size = 0
    os.lseek(source.file, offset, os.SEEK_SET)
    while chunk := os.read(source.file, _CHUNK):
        if budget is not None:
            budget.checkpoint()
        size += len(chunk)
        if size > maximum:
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
        digest.update(chunk)
    if not source.unchanged():
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if budget is not None:
        budget.checkpoint()
    return digest.hexdigest(), size


def _native_file_binding(
    source: AnchoredFile,
    output: ExclusiveOutput,
    *,
    limits: FileEnvelopeLimits,
    budget: OperationBudget,
) -> dict[str, object]:
    """Describe complete held-file bytes and the exact absent output selection."""
    if not isinstance(limits, FileEnvelopeLimits):
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if not source.unchanged() or not output.unchanged():
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    file_operation = "seal" if source.private else "open"
    maximum = limits.max_plaintext_bytes if file_operation == "seal" else limits.max_envelope_bytes
    digest, length = hash_file(source, offset=0, maximum=maximum, budget=budget)
    if length != source.size:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if not source.unchanged() or not output.unchanged():
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    device, inode, uid, mode, size, mtime_ns, ctime_ns = source.identity
    source_parent = source.parent_chain[-1]
    parent = output.parent_identity
    return {
        "file_operation": file_operation,
        "source": {
            "path": str(source.path),
            "identity": {
                "device": device,
                "inode": inode,
                "uid": uid,
                "mode": mode,
                "size": size,
                "mtime_ns": mtime_ns,
                "ctime_ns": ctime_ns,
            },
            "parent": {
                "path": str(source_parent.path),
                "device": source_parent.device,
                "inode": source_parent.inode,
                "uid": source_parent.uid,
                "mode": source_parent.mode,
            },
            "sha256": digest,
            "length": length,
        },
        "output": {
            "path": str(output.path),
            "parent": {
                "path": str(parent.path),
                "device": parent.device,
                "inode": parent.inode,
                "uid": parent.uid,
                "mode": parent.mode,
            },
            "name": output.name,
            "selection": "absent",
        },
        "limits": {
            "max_plaintext_bytes": limits.max_plaintext_bytes,
            "max_envelope_bytes": limits.max_envelope_bytes,
            "max_temporary_bytes": limits.max_temporary_bytes,
            "max_diagnostics_bytes": limits.max_diagnostics_bytes,
            "timeout_seconds": limits.timeout_seconds,
        },
    }


def _inspect_native_file_binding(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    limits: FileEnvelopeLimits,
    private_source: bool,
) -> dict[str, object]:
    """Build the public pre-dialog file binding from owned, short-lived handles."""
    if not isinstance(limits, FileEnvelopeLimits) or type(private_source) is not bool:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if not _fd_publication_available():
        raise envelope.EnvelopeError(
            envelope.EnvelopeErrorCode.FILE_PUBLICATION_UNAVAILABLE
        )
    budget = OperationBudget(limits, None)
    with AnchoredFile(source_path, private=private_source) as source, ExclusiveOutput(
        output_path,
    ) as output:
        return _native_file_binding(source, output, limits=limits, budget=budget)


def copy_file(
    source: AnchoredFile, destination: int, *, offset: int, length: int,
    budget: OperationBudget | None = None,
) -> None:
    os.lseek(source.file, offset, os.SEEK_SET)
    remaining = length
    while remaining:
        if budget is not None:
            budget.checkpoint()
        chunk = os.read(source.file, min(_CHUNK, remaining))
        if not chunk:
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
        _write_all(destination, chunk)
        remaining -= len(chunk)
    if not source.unchanged():
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if budget is not None:
        budget.checkpoint()


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def run_gpg_file(
    executable: str, home: Path, arguments: tuple[str, ...], *,
    input_fd: int, input_offset: int, output_fd: int | None,
    output_limit: int, limits: FileEnvelopeLimits,
    passphrase: bytearray | bytes | None, failure_code: envelope.EnvelopeErrorCode,
    budget: OperationBudget,
    before_launch: Callable[[], None] | None = None,
) -> tuple[bytes, bytes]:
    """Run selected-home GnuPG with bounded pipe output and diagnostics."""
    passphrase_pipe: envelope.PassphrasePipe | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        budget.checkpoint()
        if passphrase is not None:
            if "{passphrase_fd}" not in arguments:
                envelope._wipe_passphrase(passphrase)
                raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
            passphrase_pipe = envelope.PassphrasePipe(passphrase)
        elif "{passphrase_fd}" in arguments:
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
        command, environment = envelope._gpg_command(
            executable, home, arguments,
            passphrase_pipe.read_fd if passphrase_pipe is not None else None,
        )
        os.lseek(input_fd, input_offset, os.SEEK_SET)
        with os.fdopen(os.dup(input_fd), "rb", closefd=True) as input_stream:
            budget.checkpoint()
            if before_launch is not None:
                before_launch()
            process = subprocess.Popen(
                command, stdin=input_stream, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, close_fds=True,
                pass_fds=(passphrase_pipe.read_fd,) if passphrase_pipe is not None else (),
                env=environment,
            )
            if passphrase_pipe is not None:
                passphrase_pipe.close_reader()
            stdout = bytearray()
            diagnostics = bytearray()
            output_size = 0
            try:
                assert process.stdout is not None and process.stderr is not None
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                    if passphrase_pipe is not None:
                        selector.register(
                            passphrase_pipe.write_fd, selectors.EVENT_WRITE, "passphrase"
                        )
                    while selector.get_map():
                        budget.checkpoint()
                        remaining = budget.deadline - time.monotonic()
                        if remaining <= 0:
                            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.GPG_TIMEOUT)
                        for key, _ in selector.select(min(0.2, remaining)):
                            if key.data == "passphrase":
                                if passphrase_pipe is None:
                                    raise envelope.EnvelopeError(failure_code)
                                if passphrase_pipe.write_some():
                                    selector.unregister(key.fileobj)
                                    passphrase_pipe.close_writer()
                                continue
                            chunk = os.read(key.fileobj.fileno(), _CHUNK)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            if key.data == "stdout":
                                output_size += len(chunk)
                                if output_size > output_limit:
                                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.GPG_OUTPUT_LIMIT)
                                if output_fd is None:
                                    stdout.extend(chunk)
                                else:
                                    _write_all(output_fd, chunk)
                            else:
                                diagnostics.extend(chunk)
                                if len(diagnostics) > limits.max_diagnostics_bytes:
                                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.GPG_OUTPUT_LIMIT)
                while True:
                    budget.checkpoint()
                    try:
                        return_code = process.wait(
                            timeout=min(0.2, budget.deadline - time.monotonic())
                        )
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if return_code != 0 or (passphrase_pipe is not None and passphrase_pipe.pending):
                    raise envelope.EnvelopeError(failure_code)
                return bytes(stdout), bytes(diagnostics)
            except BaseException:
                if process is not None:
                    envelope._terminate(process)
                raise
            finally:
                if process is not None:
                    for pipe in (process.stdout, process.stderr):
                        if pipe is not None and not pipe.closed:
                            pipe.close()
    except envelope.EnvelopeError:
        if process is not None:
            envelope._terminate(process)
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        if process is not None:
            envelope._terminate(process)
        raise envelope.EnvelopeError(failure_code) from None
    except BaseException:
        if process is not None:
            envelope._terminate(process)
        raise
    finally:
        if passphrase_pipe is not None:
            passphrase_pipe.close()
        elif passphrase is not None:
            envelope._wipe_passphrase(passphrase)


def seal_file(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    limits: FileEnvelopeLimits,
    gnupg_home: str | os.PathLike[str],
    signer_fingerprint: str,
    recipient_fingerprints: Iterable[str],
    local_trust: LocalTrust,
    passphrase_reference: SecretReference | None,
    secret_resolver: SecretResolver,
    cancel_check: Callable[[], bool] | None,
    disable_compression: bool = False,
    native_input: envelope.NativeInputContext | None = None,
) -> Path:
    if not isinstance(limits, FileEnvelopeLimits):
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if type(disable_compression) is not bool:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if (
        not isinstance(secret_resolver, SecretResolver)
        or (native_input is None and not isinstance(passphrase_reference, SecretReference))
        or (native_input is not None and (
            not isinstance(native_input, envelope.NativeInputContext)
            or passphrase_reference is not None
        ))
    ):
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
    if not _fd_publication_available():
        raise envelope.EnvelopeError(
            envelope.EnvelopeErrorCode.FILE_PUBLICATION_UNAVAILABLE
        )
    budget = OperationBudget(limits, cancel_check)
    signer, recipients, home, executable = envelope._prepare_seal(
        gnupg_home=gnupg_home, signer_fingerprint=signer_fingerprint,
        recipient_fingerprints=recipient_fingerprints, local_trust=local_trust,
    )
    passphrase: bytearray | None = None
    lease: object | None = None
    claimed = False
    try:
        with AnchoredFile(source_path, private=True) as source, ExclusiveOutput(output_path) as output:
            file_binding = None
            operation = None
            if native_input is not None:
                try:
                    file_binding = _native_file_binding(
                        source, output, limits=limits, budget=budget,
                    )
                    from .native_input import _describe_seal_file_operation

                    operation = _describe_seal_file_operation(
                        file_binding, signer_fingerprint=signer,
                        recipient_fingerprints=recipients, local_trust=local_trust,
                        gnupg_home=home, disable_compression=disable_compression,
                    )
                    lease = native_input.claim(operation, deadline=budget.deadline)
                    claimed = True
                except envelope.EnvelopeError:
                    raise
                except envelope.NativeInputError:
                    if operation is not None:
                        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.CANCELLED) from None
                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT) from None
                except Exception:
                    if operation is not None:
                        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.CANCELLED) from None
                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT) from None
                budget.add_checkpoint(
                    lambda: envelope._native_checkpoint(native_input, operation)
                )
                passphrase = envelope._resolve_passphrase_buffer(
                    secret_resolver, None, operation, native_input,
                )
                if _native_file_binding(source, output, limits=limits, budget=budget) != file_binding:
                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
                digest = file_binding["source"]["sha256"]
                length = file_binding["source"]["length"]
            else:
                digest, length = hash_file(
                    source, offset=0, maximum=limits.max_plaintext_bytes, budget=budget,
                )
            header = EnvelopeHeader()
            manifest = envelope.EnvelopeManifest(
                header=header, signer_fingerprint=signer,
                recipient_fingerprints=recipients, payload_sha256=digest,
                payload_length=length,
            )
            manifest_bytes = envelope._manifest_bytes(manifest)
            if len(manifest_bytes) > envelope._MAX_MANIFEST_BYTES:
                raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
            inner_size = len(envelope._INNER_MAGIC) + 4 + len(manifest_bytes) + length
            header_bytes = header.to_json().encode("utf-8")
            outer_prefix = struct.pack(">H", len(header_bytes)) + header_bytes
            if (
                inner_size >= limits.max_temporary_bytes
                or len(outer_prefix) >= limits.max_envelope_bytes
            ):
                raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.GPG_OUTPUT_LIMIT)
            budget.checkpoint()
            if not source.unchanged() or not output.unchanged():
                raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
            inner_name, inner_fd = output.stage()
            try:
                _write_all(inner_fd, envelope._INNER_MAGIC)
                _write_all(inner_fd, struct.pack(">I", len(manifest_bytes)))
                _write_all(inner_fd, manifest_bytes)
                copy_file(source, inner_fd, offset=0, length=length, budget=budget)
                os.fsync(inner_fd)
                cipher_name, cipher_fd = output.stage()
                try:
                    _write_all(cipher_fd, outer_prefix)
                    available = min(
                        limits.max_envelope_bytes - len(outer_prefix),
                        limits.max_temporary_bytes - inner_size - len(outer_prefix),
                    )
                    if available <= 0:
                        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.GPG_OUTPUT_LIMIT)
                    if native_input is None:
                        assert passphrase_reference is not None
                        passphrase = envelope._legacy_passphrase_buffer(
                            passphrase_reference, secret_resolver,
                        )

                    def before_launch() -> None:
                        budget.checkpoint()
                        if not source.unchanged() or not output.unchanged():
                            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)

                    with envelope._managed_gpg_agent(
                        home, before_launch=before_launch,
                    ):
                        run_gpg_file(
                            executable, home, envelope._seal_arguments(
                                signer, recipients, disable_compression=disable_compression,
                            ),
                            input_fd=inner_fd, input_offset=0, output_fd=cipher_fd,
                            output_limit=available, limits=limits, passphrase=passphrase,
                            failure_code=envelope.EnvelopeErrorCode.SIGN_ENCRYPT_FAILED,
                            budget=budget, before_launch=before_launch,
                        )
                        _check_packets_file(
                            cipher_fd, len(outer_prefix), len(recipients),
                            executable, home, limits, budget,
                            before_launch=before_launch,
                        )
                    if not source.unchanged() or not output.unchanged():
                        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
                    budget.checkpoint()
                    os.fsync(cipher_fd)
                    budget.checkpoint()
                    if native_input is not None:
                        assert file_binding is not None

                        def publish_native() -> Path:
                            budget.checkpoint()
                            if not source.unchanged() or not output.unchanged():
                                raise envelope.EnvelopeError(
                                    envelope.EnvelopeErrorCode.INVALID_INPUT
                                )
                            if _native_file_binding(
                                source, output, limits=limits, budget=budget,
                            ) != file_binding:
                                raise envelope.EnvelopeError(
                                    envelope.EnvelopeErrorCode.INVALID_INPUT
                                )
                            return output.publish(cipher_name)

                        result = publish_native()
                    else:
                        result = output.publish(cipher_name)
                    output.published_identity = None
                    return result
                finally:
                    os.close(cipher_fd)
            finally:
                os.close(inner_fd)
    except envelope.EnvelopeError:
        raise
    except (OSError, ValueError):
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT) from None
    finally:
        if passphrase is not None:
            envelope._wipe_passphrase(passphrase)
        if claimed:
            native_input.finish(lease)


def _check_packets_file(
    descriptor: int, offset: int, recipient_count: int,
    executable: str, home: Path, limits: FileEnvelopeLimits,
    budget: OperationBudget,
    before_launch: Callable[[], None] | None = None,
) -> None:
    stdout, stderr = run_gpg_file(
        executable, home, ("--list-only", "--list-packets", "-"),
        input_fd=descriptor, input_offset=offset, output_fd=None,
        output_limit=limits.max_diagnostics_bytes, limits=limits, passphrase=None,
        failure_code=envelope.EnvelopeErrorCode.MALFORMED_CIPHERTEXT,
        budget=budget, before_launch=before_launch,
    )
    envelope._check_hidden_packet_listing(
        stdout + b"\n" + stderr, recipient_count=recipient_count,
    )


def _outer_header(source: AnchoredFile, limits: FileEnvelopeLimits) -> tuple[EnvelopeHeader, int]:
    size = os.fstat(source.file).st_size
    if size > limits.max_envelope_bytes or size < 3:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_ENVELOPE)
    os.lseek(source.file, 0, os.SEEK_SET)
    prefix = os.read(source.file, 2)
    if len(prefix) != 2:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_ENVELOPE)
    header_length = struct.unpack(">H", prefix)[0]
    if header_length == 0 or header_length > envelope._MAX_HEADER_BYTES or size <= 2 + header_length:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_ENVELOPE)
    header_bytes = os.read(source.file, header_length)
    try:
        header_text = header_bytes.decode("utf-8")
        header = EnvelopeHeader.from_json(header_text)
        if header.to_json() != header_text:
            raise ValueError
        validate_envelope_header(header)
    except (UnicodeDecodeError, OpenPGPContractError, ValueError):
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_ENVELOPE) from None
    return header, 2 + header_length


def _signed_inner_file(
    descriptor: int, *, maximum: int, budget: OperationBudget,
) -> tuple[envelope.EnvelopeManifest, int]:
    budget.checkpoint()
    size = os.fstat(descriptor).st_size
    os.lseek(descriptor, 0, os.SEEK_SET)
    fixed = os.read(descriptor, len(envelope._INNER_MAGIC) + 4)
    if (
        not fixed.startswith(envelope._INNER_MAGIC)
        or len(fixed) != len(envelope._INNER_MAGIC) + 4
    ):
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    manifest_length = struct.unpack_from(">I", fixed, len(envelope._INNER_MAGIC))[0]
    if manifest_length == 0 or manifest_length > envelope._MAX_MANIFEST_BYTES:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    offset = len(fixed) + manifest_length
    if offset > size or size - offset > maximum:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    manifest_bytes = os.read(descriptor, manifest_length)
    if len(manifest_bytes) != manifest_length:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    manifest = envelope._parse_manifest(manifest_bytes, max_payload_bytes=maximum)
    if manifest.payload_length != size - offset:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    digest = hashlib.sha256()
    remaining = manifest.payload_length
    while remaining:
        budget.checkpoint()
        chunk = os.read(descriptor, min(_CHUNK, remaining))
        if not chunk:
            raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INNER_MESSAGE)
        digest.update(chunk)
        remaining -= len(chunk)
    if digest.hexdigest() != manifest.payload_sha256:
        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INNER_MESSAGE)
    budget.checkpoint()
    return manifest, offset


def _strip_inner_prefix(
    descriptor: int, *, offset: int, length: int, budget: OperationBudget,
) -> None:
    read_position = offset
    write_position = 0
    remaining = length
    while remaining:
        budget.checkpoint()
        os.lseek(descriptor, read_position, os.SEEK_SET)
        chunk = os.read(descriptor, min(_CHUNK, remaining))
        if not chunk:
            raise OSError("short temporary payload")
        os.lseek(descriptor, write_position, os.SEEK_SET)
        _write_all(descriptor, chunk)
        read_position += len(chunk)
        write_position += len(chunk)
        remaining -= len(chunk)
    os.ftruncate(descriptor, length)
    os.fsync(descriptor)
    budget.checkpoint()


def open_file(
    envelope_path: str | os.PathLike[str], output_path: str | os.PathLike[str],
    *, limits: FileEnvelopeLimits, gnupg_home: str | os.PathLike[str],
    policy: EnvelopePolicy, local_trust: LocalTrust, authorized: bool,
    passphrase_reference: SecretReference | None,
    secret_resolver: SecretResolver | None,
    cancel_check: Callable[[], bool] | None,
    native_input: envelope.NativeInputContext | None = None,
    allow_historical_signature: bool = False,
    expected_ciphertext_sha256: str | None = None,
    receipt_callback: Callable[[AnchoredFile, Callable[[], None], Callable[[], None], OperationBudget], None] | None = None,
    operation_budget: OperationBudget | None = None,
) -> Path:
    from . import opening

    if (
        not isinstance(limits, FileEnvelopeLimits)
        or not isinstance(policy, EnvelopePolicy)
        or not isinstance(local_trust, LocalTrust)
        or type(authorized) is not bool or not authorized
        or (native_input is None and (passphrase_reference is None) != (secret_resolver is None))
        or (passphrase_reference is not None and not isinstance(passphrase_reference, SecretReference))
        or (secret_resolver is not None and not isinstance(secret_resolver, SecretResolver))
        or (native_input is not None and (
            not isinstance(native_input, envelope.NativeInputContext)
            or passphrase_reference is not None
            or not isinstance(secret_resolver, SecretResolver)
            or allow_historical_signature
            or expected_ciphertext_sha256 is not None
            or receipt_callback is not None
            or operation_budget is not None
        ))
    ):
        raise opening.EnvelopeOpenError(opening.EnvelopeOpenErrorCode.INVALID_INPUT)
    if len(policy.expected_signers) != 1:
        raise opening.EnvelopeOpenError(opening.EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH)
    if not policy.expected_signers <= local_trust.fingerprints:
        raise opening.EnvelopeOpenError(opening.EnvelopeOpenErrorCode.UNTRUSTED_SIGNER)
    if len(policy.expected_recipients) > 64:
        raise opening.EnvelopeOpenError(opening.EnvelopeOpenErrorCode.INVALID_INPUT)
    if not _fd_publication_available():
        raise opening.EnvelopeOpenError(
            opening.EnvelopeOpenErrorCode.FILE_PUBLICATION_UNAVAILABLE
        )
    try:
        budget = operation_budget or OperationBudget(limits, cancel_check)
        budget.checkpoint()
    except envelope.EnvelopeError as exc:
        raise opening._map_envelope_error(exc) from None
    passphrase: bytearray | None = None
    lease: object | None = None
    claimed = False
    try:
        with AnchoredFile(envelope_path) as source, ExclusiveOutput(output_path) as output:
            header, cipher_offset = _outer_header(source, limits)
            if expected_ciphertext_sha256 is not None:
                digest, _ = hash_file(
                    source, offset=0, maximum=limits.max_envelope_bytes, budget=budget,
                )
                if digest != expected_ciphertext_sha256:
                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_ENVELOPE)
            home = envelope._validated_gnupg_home(gnupg_home)
            executable = envelope.shutil.which("gpg") or envelope.shutil.which("gpg2")
            if not executable:
                raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.GPG_NOT_FOUND)

            def before_launch() -> None:
                budget.checkpoint()
                if not source.unchanged() or not output.unchanged():
                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)

            file_binding = None
            operation = None
            if native_input is not None:
                try:
                    file_binding = _native_file_binding(
                        source, output, limits=limits, budget=budget,
                    )
                    from .native_input import _describe_open_file_operation

                    operation = _describe_open_file_operation(
                        file_binding, policy=policy, local_trust=local_trust,
                        gnupg_home=home, authorized=authorized,
                    )
                except envelope.EnvelopeError:
                    raise
                except envelope.NativeInputError:
                    raise opening.EnvelopeOpenError(
                        opening.EnvelopeOpenErrorCode.INVALID_INPUT
                    ) from None
                except Exception:
                    raise opening.EnvelopeOpenError(
                        opening.EnvelopeOpenErrorCode.INVALID_INPUT
                    ) from None
                try:
                    lease = native_input.claim(operation, deadline=budget.deadline)
                    claimed = True
                except Exception:
                    raise opening.EnvelopeOpenError(
                        opening.EnvelopeOpenErrorCode.CANCELLED
                    ) from None
                budget.add_checkpoint(
                    lambda: envelope._native_checkpoint(native_input, operation)
                )

            _check_packets_file(
                source.file, cipher_offset, len(policy.expected_recipients),
                executable, home, limits, budget,
                before_launch=before_launch,
            )
            if not source.unchanged() or not output.unchanged():
                raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_ENVELOPE)
            if native_input is not None:
                assert operation is not None and file_binding is not None
                passphrase = envelope._resolve_passphrase_buffer(
                    secret_resolver, None, operation, native_input,
                )
                if _native_file_binding(source, output, limits=limits, budget=budget) != file_binding:
                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
            elif passphrase_reference is not None and secret_resolver is not None:
                try:
                    passphrase = envelope._legacy_passphrase_buffer(
                        passphrase_reference, secret_resolver,
                    )
                except Exception:
                    raise opening.EnvelopeOpenError(
                        opening.EnvelopeOpenErrorCode.PASSPHRASE_UNAVAILABLE
                    ) from None
            arguments: list[str] = ["--status-fd=2"]
            if passphrase is None:
                arguments.extend(("--pinentry-mode", "error"))
            else:
                arguments.extend(("--pinentry-mode", "loopback", "--passphrase-fd", "{passphrase_fd}"))
            arguments.extend(("--output", "-", "--decrypt", "-"))
            stage_name, stage_fd = output.stage()
            try:
                with envelope._managed_gpg_agent(
                    home, before_launch=before_launch,
                ):
                    _stdout, diagnostics = run_gpg_file(
                        executable, home, tuple(arguments), input_fd=source.file,
                        input_offset=cipher_offset, output_fd=stage_fd,
                        output_limit=min(
                            limits.max_temporary_bytes,
                            limits.max_plaintext_bytes + envelope._MAX_MANIFEST_BYTES
                            + len(envelope._INNER_MAGIC) + 4,
                        ),
                        limits=limits, passphrase=passphrase,
                        failure_code=envelope.EnvelopeErrorCode.MALFORMED_CIPHERTEXT,
                        budget=budget,
                        before_launch=before_launch,
                    )
                signer = opening._verified_signer(
                    diagnostics, allow_historical=allow_historical_signature,
                )
                manifest, payload_offset = _signed_inner_file(
                    stage_fd, maximum=limits.max_plaintext_bytes, budget=budget,
                )
                if manifest.header != header:
                    raise opening.EnvelopeOpenError(opening.EnvelopeOpenErrorCode.INVALID_INNER_MESSAGE)
                if manifest.signer_fingerprint != signer:
                    raise opening.EnvelopeOpenError(opening.EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH)
                try:
                    validate_envelope_participants((signer,), manifest.recipient_fingerprints, policy)
                    local_trust.require_signer(signer)
                except OpenPGPContractError as exc:
                    raise opening._map_contract_error(exc) from None
                if not source.unchanged() or not output.unchanged():
                    raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_ENVELOPE)
                _strip_inner_prefix(
                    stage_fd, offset=payload_offset, length=manifest.payload_length,
                    budget=budget,
                )
                budget.checkpoint()

                def publish_verified() -> Path:
                    budget.checkpoint()
                    if not source.unchanged():
                        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
                    result = output.publish(stage_name)
                    budget.checkpoint()
                    if not source.unchanged():
                        raise envelope.EnvelopeError(envelope.EnvelopeErrorCode.INVALID_INPUT)
                    return result

                if receipt_callback is not None:
                    receipt_callback(
                        source, publish_verified,
                        output.rollback_published, budget,
                    )
                    result = output.path
                else:
                    if native_input is not None:
                        assert file_binding is not None

                        def publish_native() -> Path:
                            budget.checkpoint()
                            if not source.unchanged() or not output.unchanged():
                                raise envelope.EnvelopeError(
                                    envelope.EnvelopeErrorCode.INVALID_INPUT
                                )
                            if _native_file_binding(
                                source, output, limits=limits, budget=budget,
                            ) != file_binding:
                                raise envelope.EnvelopeError(
                                    envelope.EnvelopeErrorCode.INVALID_INPUT
                                )
                            return publish_verified()

                        result = publish_native()
                    else:
                        result = publish_verified()
                output.published_identity = None
                return result
            finally:
                os.close(stage_fd)
    except opening.EnvelopeOpenError:
        raise
    except envelope.EnvelopeError as exc:
        raise opening._map_envelope_error(exc, decryption=True) from None
    except (OSError, ValueError):
        raise opening.EnvelopeOpenError(opening.EnvelopeOpenErrorCode.INVALID_INPUT) from None
    finally:
        if passphrase is not None:
            envelope._wipe_passphrase(passphrase)
        if claimed:
            native_input.finish(lease)
