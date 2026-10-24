"""Descriptor-bound publication of a newly initialized private authority store."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .envelope import EnvelopeError
from . import envelope_io
from .envelope_io import ExclusiveOutput
from .key_records import KeyInspection, _INSPECTION_SEAL
from .native_input import (
    InputOperation, NativeInputContext, NativeInputError, NativeInputErrorCode,
    _display_path, _operation, _record_binding,
)

if TYPE_CHECKING:
    from .trust_state import TrustSnapshot


@dataclass(frozen=True)
class PreparedAuthority:
    scope: tuple[str, ...]
    observed_at: int
    owner_certificate: bytes
    publisher_certificate: bytes
    recovery_certificates: tuple[bytes, ...]
    owner: KeyInspection
    publisher: KeyInspection
    recovery: tuple[KeyInspection, ...]

    def revalidate(self, now: int) -> None:
        from . import trust_state as trust

        if now < self.observed_at:
            raise trust.TrustStateError("clock rollback")
        trust.transition._validate_owner_usable(self.owner, now=now, effective_at=now, overlap_seconds=0)
        for inspection in (self.publisher, *self.recovery):
            trust.publishing_transition._validate_party(inspection, now=now, valid_until=now)

    def serialize(self, now: int) -> tuple[TrustSnapshot, bytes]:
        from . import trust_state as trust

        self.revalidate(now)
        return trust._initial_database(
            self.scope, now, self.owner.primary_fingerprint, self.publisher.primary_fingerprint,
            self.owner_certificate, self.publisher_certificate,
            dict(zip((item.primary_fingerprint for item in self.recovery), self.recovery_certificates)),
        )


def _gpg_selection(binary: str | os.PathLike[str]) -> dict:
    """Resolve the same executable for inspection before and after confirmation."""
    try:
        found = shutil.which(os.fspath(binary))
        if found is None:
            raise ValueError
        path = Path(found).resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError
        return {"path": str(path), "device": info.st_dev, "inode": info.st_ino,
                "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
                "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
    except (OSError, TypeError, ValueError):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None


def _describe(authority: PreparedAuthority, output: ExclusiveOutput, executable: dict) -> InputOperation:
    certificates = (authority.owner_certificate, authority.publisher_certificate, *authority.recovery_certificates)
    inspections = (authority.owner, authority.publisher, *authority.recovery)
    roles = ("owner", "publisher", *(f"recovery_{index + 1}" for index in range(len(authority.recovery))))
    bindings = []
    for role, certificate, inspection in zip(roles, certificates, inspections):
        if not isinstance(inspection, KeyInspection) or inspection._verification is not _INSPECTION_SEAL:
            raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
        bindings.append({"role": role, "fingerprint": inspection.primary_fingerprint,
                         "sha256": hashlib.sha256(certificate).hexdigest(), "length": len(certificate),
                         "records": [_record_binding(record) for record in inspection.records]})
    details = (("destination", _display_path(str(output.path))),
               ("scope", json.dumps(authority.scope, ensure_ascii=True)),
               *((item["role"], item["fingerprint"]) for item in bindings))
    summary = "Create a persistent authority store for the selected scope and certificates."
    return _operation("initialize-trust-store", {
        "certificates": bindings, "scope": authority.scope, "gpg": executable,
        "destination": str(output.path), "selection": "absent",
        "parent_chain": [{"path": str(item.path), "device": item.device,
                          "inode": item.inode, "uid": item.uid, "mode": item.mode}
                         for item in output.parent_chain],
    }, summary, details, None)


def _native_available() -> None:
    from .trust_state import TrustStateError

    if not envelope_io._fd_publication_available():
        raise TrustStateError("native trust publication unavailable")
    if not callable(getattr(sqlite3.Connection, "serialize", None)):
        raise TrustStateError("SQLite serialization unavailable")


def describe_trust_initialization_operation(
    path: str | os.PathLike[str], *, scope: tuple[str, ...],
    owner_certificate: bytes, publisher_certificate: bytes,
    recovery_certificates: tuple[bytes, ...] = (),
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> InputOperation:
    """Inspect immutable certificate bytes and select an absent private destination."""
    from . import trust_state as trust

    _native_available()
    executable = _gpg_selection(gpg_binary)
    authority = trust._prepare_initial_authority(
        scope=scope, owner_certificate=owner_certificate, publisher_certificate=publisher_certificate,
        recovery_certificates=recovery_certificates, gpg_binary=executable["path"],
    )
    authority.revalidate(trust._clock())
    with ExclusiveOutput(trust._path(path, create=True)) as output:
        if not output.unchanged() or _gpg_selection(gpg_binary) != executable:
            raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH)
        return _describe(authority, output, executable)


def initialize(
    path: str | os.PathLike[str], *, scope: tuple[str, ...],
    owner_certificate: bytes, publisher_certificate: bytes,
    recovery_certificates: tuple[bytes, ...], gpg_binary: str | os.PathLike[str],
    native_input: NativeInputContext | None,
) -> TrustSnapshot:
    from . import trust_state as trust

    arguments = dict(scope=scope, owner_certificate=owner_certificate,
                     publisher_certificate=publisher_certificate, recovery_certificates=recovery_certificates)
    if native_input is None:
        authority = trust._prepare_initial_authority(**arguments, gpg_binary=gpg_binary)
        selected = trust._path(path, create=True)
        snapshot, serialized = authority.serialize(trust._clock())
        publish_initial_database(selected, serialized)
        return snapshot
    if not isinstance(native_input, NativeInputContext):
        raise NativeInputError(NativeInputErrorCode.INVALID_CONTEXT)
    _native_available()
    executable = _gpg_selection(gpg_binary)
    lease = None
    operation = None
    last_time = trust._clock()
    with ExclusiveOutput(trust._path(path, create=True)) as output:
        def checkpoint():
            nonlocal last_time
            native_input.checkpoint(native_input.expected if operation is None else operation)
            if time.monotonic() >= native_input.deadline:
                raise NativeInputError(NativeInputErrorCode.DEADLINE_EXPIRED)
            now = trust._clock()
            if now < last_time:
                raise trust.TrustStateError("clock rollback")
            last_time = now
            if not output.unchanged() or _gpg_selection(gpg_binary) != executable:
                raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH)

        try:
            authority = trust._prepare_initial_authority(
                **arguments, gpg_binary=executable["path"], checkpoint=checkpoint)
            operation = _describe(authority, output, executable)
            lease = native_input.claim(operation)
            checkpoint()
            authority.revalidate(last_time)
            native_input.confirm_trust_initialization(operation)
            # Reinspect exact immutable bytes after the human wait. Current expiry,
            # executable selection and owner authority can all have changed.
            fresh = trust._prepare_initial_authority(
                **arguments, gpg_binary=executable["path"], checkpoint=checkpoint)
            if _describe(fresh, output, executable) != operation:
                raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH)
            checkpoint()
            snapshot, serialized = fresh.serialize(last_time)
            name, descriptor = output.stage()
            try:
                _write_database(descriptor, serialized)
            finally:
                os.close(descriptor)
            checkpoint()
            fresh.revalidate(last_time)
            output.publish(name)
            checkpoint()
            fresh.revalidate(last_time)
            output.published_identity = None
            return snapshot
        finally:
            if lease is not None:
                native_input.finish(lease)


def _write_database(descriptor: int, serialized: bytes) -> None:
    """Handle short writes without reopening a destination or staging pathname."""
    view = memoryview(serialized)
    position = 0
    while position < len(view):
        written = os.write(descriptor, view[position:])
        if written <= 0:
            raise OSError("database write made no progress")
        position += written
    os.fsync(descriptor)


def publish_initial_database(path: Path, serialized: bytes) -> None:
    """Preserve portable create-once semantics and remove only our own inode.

    This route intentionally does not call the Linux-only staged publisher.
    SQLite never opens the selected pathname during initialization.
    """
    from .trust_state import TrustStateError

    try:
        with ExclusiveOutput(path) as output:
            if not output.unchanged():
                raise OSError("store selection changed")
            descriptor = os.open(
                output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=output.directory,
            )
            try:
                info = os.fstat(descriptor)
                output.published_identity = (info.st_dev, info.st_ino)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    raise OSError("unsafe store")
                if not output.unchanged():
                    raise OSError("store selection changed")
                _write_database(descriptor, serialized)
                os.fsync(output.directory)
                after = os.fstat(descriptor)
                if (after.st_uid != info.st_uid or after.st_mode != info.st_mode
                        or after.st_nlink != 1 or after.st_size != len(serialized)
                        or not output.unchanged()):
                    raise OSError("store selection changed")
            finally:
                os.close(descriptor)
            output.published_identity = None
    except (OSError, EnvelopeError) as exc:
        raise TrustStateError("store already exists or cannot be created") from exc
