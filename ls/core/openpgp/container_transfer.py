"""Bounded, restartable ciphertext transfer for authenticated containers.

Carrier locations and generation tokens are observations. Every resumed transfer
rechecks protected metadata and current authorities before using cached bytes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from typing import BinaryIO, Callable, Protocol

from ..agent.runtime_lock import runtime_use
from . import trust_state
from .artifact_records import parse_descriptor
from .artifacts import (
    ArtifactDiscoveryPolicy, ArtifactPolicy, VerifiedContainerMetadata,
    inspect_container_metadata,
)
from .container_catalog import CatalogContext
from .container_format import ContainerLimits, MessageDescriptor, copy_message
from .container_preparation import ContainerPreparationLimits, ContainerPreparationError, _Budget
from .container_preparation import PreparedVolume
from .container_runtime import _private_directory, _read, _write
from .container_sources import LocalFileRangeSource, RangeSource
from .contracts import EnvelopePolicy, LocalTrust
from .envelope import seal_envelope_file
from .envelope_io import AnchoredFile, ExclusiveOutput, _open_parent, _parent_chain_matches
from .opening import open_envelope_file
from .secrets import SecretReference, SecretResolver


_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT = re.compile(r"[0-9a-f]{24}\Z")


class TransferError(RuntimeError):
    """Safe failure code with no locator, key material, or private path."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TransferLimits:
    chunk_bytes: int = 8 * 1024 * 1024
    max_cached_bytes: int = 3 * 1024 * 1024 * 1024
    max_chunks: int = 512
    timeout_seconds: float = 3600

    def __post_init__(self) -> None:
        if (type(self.chunk_bytes) is not int or not 64 * 1024 <= self.chunk_bytes <= 8 * 1024 * 1024
                or type(self.max_cached_bytes) is not int
                or not self.chunk_bytes <= self.max_cached_bytes <= 3 * 1024 * 1024 * 1024
                or type(self.max_chunks) is not int or not 1 <= self.max_chunks <= 512
                or type(self.timeout_seconds) not in (int, float)
                or not 0 < self.timeout_seconds <= 3600):
            raise TransferError("limits_invalid")


@dataclass(frozen=True, slots=True)
class TransferResult:
    ciphertext_path: Path
    ciphertext_sha256: str
    logical_commitment_sha256: str
    message_index: int
    cached_bytes: int
    reused_chunks: int


class ConditionalCipherPublisher(Protocol):
    """Injected provider must create exactly once or report an existing key.

    ``create_if_absent`` consumes the stream without retaining plaintext (the
    stream contains verified ciphertext). It returns ``created`` or ``exists``.
    ``observe`` reports a provider-verified length and SHA-256, or ``None``.
    An uncertain create acknowledgement must not be retried automatically.
    Both methods must honor the supplied remaining timeout and cancellation
    callback in their native transport. Stream reads must request bounded chunks.
    """

    def create_if_absent(self, key: str, ciphertext: BinaryIO, *,
                         length: int, sha256: str, timeout_seconds: float,
                         cancel_check: Callable[[], bool]) -> str: ...

    def observe(self, key: str, *, timeout_seconds: float,
                cancel_check: Callable[[], bool]) -> tuple[int, str] | None: ...


class ContentAddressedCipherPublisher(Protocol):
    """Carrier with unconditional put and exact length/SHA-256 readback.

    The owner chooses a hash-qualified location; put must not rename it or
    update a mutable head. Both calls honor the native timeout and cancellation
    contract above. observe verifies actual retained bytes, not request metadata.
    """

    def put(self, key: str, ciphertext: BinaryIO, *, length: int, sha256: str,
            timeout_seconds: float, cancel_check: Callable[[], bool]) -> None: ...

    def observe(self, key: str, *, timeout_seconds: float,
                cancel_check: Callable[[], bool]) -> tuple[int, str] | None: ...


@dataclass(frozen=True, slots=True)
class PublicationResult:
    status: str
    key: str
    sha256: str
    locator_key: str | None = None


def _check(cancel_check: Callable[[], bool] | None, deadline: float) -> None:
    try:
        if cancel_check is not None and cancel_check():
            raise TransferError("cancelled")
    except TransferError:
        raise
    except Exception:
        raise TransferError("cancelled") from None
    if time.monotonic() >= deadline:
        raise TransferError("deadline_exceeded")


class _CheckedSource:
    def __init__(self, source: RangeSource, check: Callable[[], None]) -> None:
        self.source = source
        self.check = check

    @property
    def size(self) -> int:
        self.check()
        result = self.source.size
        self.check()
        return result

    def read_range(self, offset: int, length: int) -> bytes:
        self.check()
        result = self.source.read_range(offset, length)
        self.check()
        return result


def inspect_transfer_metadata(
    source: RangeSource, *, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    expected_key: str | None = None,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    limits: ContainerLimits = ContainerLimits(),
    cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: float = 3600,
    _budget: _Budget | None = None,
) -> VerifiedContainerMetadata:
    """Verify protected metadata without requesting a payload range."""
    if (cancel_check is not None and not callable(cancel_check)
            or type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 3600):
        raise TransferError("limits_invalid")
    budget = _budget or _Budget(ContainerPreparationLimits(
        container_limits=limits, timeout_seconds=timeout_seconds,
        cancel_check=cancel_check,
    ))
    try:
        with tempfile.TemporaryDirectory(prefix=".lssc-transfer-inspect-") as temporary:
            result = inspect_container_metadata(
                source, policy=policy, trust_state_path=trust_state_path,
                gnupg_home=gnupg_home, expected_key=expected_key,
                passphrase_reference=passphrase_reference,
                secret_resolver=secret_resolver, limits=limits,
                _budget=budget, _workspace=Path(temporary),
            )
            _check(budget.limits.cancel_check, budget.deadline)
            return result
    except Exception as exc:
        if isinstance(exc, TransferError):
            raise
        _check(budget.limits.cancel_check, budget.deadline)
        raise


def _local_authority(context: CatalogContext) -> None:
    try:
        current = trust_state.load_trust_state(
            context.trust_state_path, expected_scope=context.expected_scope,
        )
        if (current.store_id != context.trust_store_id
                or current.revision != context.trust_revision
                or current.epoch != context.governance_epoch
                or current.publisher is None
                or current.publisher.fingerprint != context.signer_fingerprint):
            raise TransferError("local_authority_changed")
    except TransferError:
        raise
    except Exception:
        raise TransferError("local_authority_changed") from None


def _generation(source: RangeSource, selected: str | None) -> str:
    try:
        observed = getattr(source, "generation", None)
        if selected is None:
            selected = observed
        elif observed is not None and observed != selected:
            raise TransferError("source_changed")
        if type(selected) is not str or not selected or len(selected.encode("utf-8")) > 256:
            raise TransferError("source_generation_required")
        return hashlib.sha256(selected.encode("utf-8")).hexdigest()
    except TransferError:
        raise
    except Exception:
        raise TransferError("source_changed") from None


def _descriptor(metadata: VerifiedContainerMetadata, index: int) -> MessageDescriptor:
    try:
        if type(index) is not int or index < 0:
            raise ValueError
        messages = metadata.volume_record["messages"]
        if index >= len(messages):
            raise ValueError
        selected = parse_descriptor(messages[index]["descriptor"])
        if selected.message_index != index:
            raise ValueError
        return selected
    except Exception:
        raise TransferError("descriptor_invalid") from None


def _binding(metadata: VerifiedContainerMetadata, descriptor: MessageDescriptor,
             generation: str, context: CatalogContext, limits: TransferLimits) -> dict:
    return {
        "schema": "localsetup.secure-container.transfer-progress", "version": 1,
        "logical_commitment": metadata.logical_commitment_sha256,
        "admission_policy": metadata.admission_policy_sha256,
        "public_header": metadata.header.public_header_sha256,
        "metadata_cipher": hashlib.sha256(metadata.header.encrypted_metadata).hexdigest(),
        "volume_id": metadata.volume_id,
        "descriptor": [descriptor.message_index, descriptor.offset,
                       descriptor.serialized_length, descriptor.ciphertext_length,
                       descriptor.sha256],
        "source_generation": generation,
        "local_store": context.trust_store_id,
        "local_revision": context.trust_revision,
        "local_epoch": context.governance_epoch,
        "local_signer": context.signer_fingerprint,
        "chunk_bytes": limits.chunk_bytes,
    }


def _progress_crypto(payload: bytes, context: CatalogContext, *,
                     budget: _Budget, sealing: bool) -> bytes:
    """Use the existing bounded file engine under the whole transfer budget."""
    budget.use_crypto()
    if len(payload) > (32 * 1024 if sealing else 48 * 1024):
        raise TransferError("progress_limit")
    budget.reserve_temp(len(payload))
    output_size = 0
    try:
        with tempfile.TemporaryDirectory(prefix=".lssc-transfer-record-") as temporary:
            source, output = Path(temporary) / "input", Path(temporary) / "output"
            source.write_bytes(payload)
            source.chmod(0o600)
            signer = context.signer_fingerprint
            arguments = dict(
                limits=replace(budget.file_limits(), max_plaintext_bytes=32 * 1024,
                               max_envelope_bytes=48 * 1024),
                gnupg_home=context.gnupg_home, local_trust=LocalTrust({signer}),
                passphrase_reference=context.passphrase_reference,
                secret_resolver=context.secret_resolver, cancel_check=budget.cancelled,
            )
            if sealing:
                seal_envelope_file(source, output, signer_fingerprint=signer,
                                   recipient_fingerprints=(signer,), disable_compression=True,
                                   **arguments)
            else:
                open_envelope_file(source, output, authorized=True,
                                   policy=EnvelopePolicy(expected_signers={signer},
                                                         expected_recipients={signer}),
                                   **arguments)
            size = output.stat().st_size
            budget.reserve_temp(size)
            output_size = size
            result = output.read_bytes()
            budget.retain(len(result))
            budget.checkpoint()
            return result
    finally:
        budget.release_temp(len(payload) + output_size)


def _seal_progress(value: dict, context: CatalogContext, *, budget: _Budget) -> dict:
    _local_authority(context)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if len(payload) > 32 * 1024:
        raise TransferError("progress_limit")
    ciphertext = _progress_crypto(payload, context, budget=budget, sealing=True)
    return {"sealed": base64.b64encode(ciphertext).decode("ascii")}


def _open_progress(value: dict, context: CatalogContext, *, budget: _Budget | None = None) -> dict:
    budget = budget or _Budget(ContainerPreparationLimits())
    try:
        if set(value) != {"sealed"} or type(value["sealed"]) is not str:
            raise ValueError
        serialized = base64.b64decode(value["sealed"], validate=True)
        payload = _progress_crypto(serialized, context, budget=budget, sealing=False)
        decoded = json.loads(payload)
        if (type(decoded) is not dict or len(payload) > 32 * 1024
                or json.dumps(decoded, sort_keys=True, separators=(",", ":"),
                              allow_nan=False).encode("utf-8") != payload):
            raise ValueError
        return decoded
    except ContainerPreparationError:
        raise
    except Exception:
        _check(budget.limits.cancel_check, budget.deadline)
        raise TransferError("progress_invalid") from None


def _chunk_path(root: Path, attempt: str, index: int) -> Path:
    return root / f"{attempt}-{index:04x}.cipherpart"


def _cache_bytes(root: Path, limits: TransferLimits, check: Callable[[], None]) -> int:
    """Count all retained files, including interrupted stages, without cleanup."""
    directory, _, selected, chain = _open_parent(root / ".probe", private=True)
    total, count = 0, 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                count += 1
                if count > limits.max_chunks + 8:
                    raise TransferError("cache_limit")
                info = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or info.st_mode & 0o077):
                    raise TransferError("cache_invalid")
                total += info.st_size
                if total > limits.max_cached_bytes:
                    raise TransferError("cache_limit")
        if not _parent_chain_matches(selected, directory, chain, private=True):
            raise TransferError("cache_invalid")
        return total
    finally:
        os.close(directory)


def _write_progress(path: Path, progress: dict, context: CatalogContext, budget: _Budget) -> None:
    sealed = _seal_progress(progress, context, budget=budget)
    encoded = json.dumps(sealed, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    old_size = path.stat().st_size if path.exists() else 0
    # Atomic replacement temporarily retains both old and new records.
    budget.reserve_temp(len(encoded))
    _write(path, sealed)
    budget.release_temp(old_size)
    budget.checkpoint()


def _read_private(path: Path, maximum: int, check: Callable[[], None] = lambda: None) -> bytes:
    try:
        with AnchoredFile(path, private=True) as source:
            info = os.fstat(source.file)
            if info.st_size > maximum or info.st_size <= 0:
                raise TransferError("cache_invalid")
            data = bytearray()
            while len(data) < info.st_size:
                check()
                block = os.read(source.file, min(1024 * 1024, info.st_size - len(data)))
                if not block:
                    raise TransferError("cache_invalid")
                data.extend(block)
            if not source.unchanged():
                raise TransferError("cache_invalid")
            check()
            return bytes(data)
    except TransferError:
        raise
    except Exception:
        raise TransferError("cache_invalid") from None


def _hash_private(path: Path, maximum: int,
                  check: Callable[[], None] = lambda: None) -> tuple[int, str]:
    try:
        with AnchoredFile(path, private=True) as source:
            return _hash_anchored(source, maximum, check)
    except TransferError:
        raise
    except Exception:
        raise TransferError("cache_invalid") from None


def _hash_anchored(source: AnchoredFile, maximum: int,
                   check: Callable[[], None] = lambda: None) -> tuple[int, str]:
    size = source.size
    if size <= 0 or size > maximum:
        raise TransferError("cache_invalid")
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        check()
        block = os.read(source.file, min(1024 * 1024, remaining))
        if not block:
            raise TransferError("cache_invalid")
        digest.update(block)
        remaining -= len(block)
    if not source.unchanged():
        raise TransferError("cache_invalid")
    check()
    return size, digest.hexdigest()


def _publish_exact(path: Path, data: bytes, check: Callable[[], None] = lambda: None) -> None:
    """Create only; an existing name is accepted only after exact comparison."""
    try:
        check()
        if path.exists():
            if _read_private(path, len(data), check) != data:
                raise TransferError("ciphertext_collision")
            return
        with ExclusiveOutput(path) as output:
            name, descriptor = output.stage()
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            check()
            try:
                output.publish(name)
            except OSError:
                if _read_private(path, len(data), check) != data:
                    raise TransferError("publish_uncertain") from None
            check()
            output.published_identity = None
    except FileExistsError:
        if _read_private(path, len(data), check) != data:
            raise TransferError("ciphertext_collision") from None
    except TransferError:
        raise
    except OSError:
        if path.exists() and _read_private(path, len(data), check) == data:
            return
        raise TransferError("publish_uncertain") from None


class _CachedSource:
    def __init__(self, source: RangeSource, *, start: int, end: int,
                 root: Path, attempt: str, chunk_bytes: int, chunks: list[list],
                 check: Callable[[], None]) -> None:
        self.source = source
        self.start = start
        self.end = end
        self.root = root
        self.attempt = attempt
        self.chunk_bytes = chunk_bytes
        self.chunks = chunks
        self.check = check
        self._last_index = -1
        self._last_data = b""

    @property
    def size(self) -> int:
        self.check()
        return self.source.size

    def read_range(self, offset: int, length: int) -> bytes:
        self.check()
        if offset < self.start or offset + length > self.end:
            return self.source.read_range(offset, length)
        result = bytearray()
        while length:
            index = (offset - self.start) // self.chunk_bytes
            within = (offset - self.start) % self.chunk_bytes
            entry = self.chunks[index]
            if index != self._last_index:
                data = _read_private(_chunk_path(self.root, self.attempt, index), entry[0], self.check)
                if len(data) != entry[0] or hashlib.sha256(data).hexdigest() != entry[1]:
                    raise TransferError("cache_invalid")
                self._last_index, self._last_data = index, data
            data = self._last_data
            block = data[within:within + length]
            if not block:
                raise TransferError("cache_invalid")
            result.extend(block)
            offset += len(block)
            length -= len(block)
        self.check()
        return bytes(result)


def resume_selected_ciphertext(
    source: RangeSource, message_index: int, *, cache_root: Path,
    local_context: CatalogContext,
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    expected_key: str | None = None,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    source_generation: str | None = None,
    limits: ContainerLimits = ContainerLimits(),
    transfer_limits: TransferLimits = TransferLimits(),
    cancel_check: Callable[[], bool] | None = None,
) -> TransferResult:
    """Resume selected encrypted bytes; no plaintext is staged or returned."""
    if not isinstance(cache_root, Path) or not cache_root.is_absolute() or ".." in cache_root.parts:
        raise TransferError("cache_root_invalid")
    if not isinstance(local_context, CatalogContext):
        raise TransferError("local_context_invalid")
    if (not isinstance(transfer_limits, TransferLimits)
            or not isinstance(limits, ContainerLimits)
            or cancel_check is not None and not callable(cancel_check)):
        raise TransferError("limits_invalid")
    budget = _Budget(ContainerPreparationLimits(
        container_limits=limits, timeout_seconds=transfer_limits.timeout_seconds,
        cancel_check=cancel_check, max_temporary_bytes=transfer_limits.max_cached_bytes,
    ))
    deadline = budget.deadline
    check = lambda: _check(cancel_check, deadline)
    check()
    _local_authority(local_context)
    metadata = inspect_transfer_metadata(
        source, policy=policy, trust_state_path=trust_state_path,
        gnupg_home=gnupg_home, expected_key=expected_key,
        passphrase_reference=passphrase_reference,
        secret_resolver=secret_resolver, limits=limits,
        cancel_check=cancel_check, timeout_seconds=transfer_limits.timeout_seconds,
        _budget=budget,
    )
    descriptor = _descriptor(metadata, message_index)
    if (descriptor.ciphertext_length > limits.max_message_bytes
            or descriptor.serialized_length > transfer_limits.max_cached_bytes
            or (descriptor.serialized_length + transfer_limits.chunk_bytes - 1)
            // transfer_limits.chunk_bytes > transfer_limits.max_chunks):
        raise TransferError("cache_limit")
    observed_generation = _generation(source, source_generation)
    binding = _binding(metadata, descriptor, observed_generation, local_context, transfer_limits)

    def check() -> None:
        _check(cancel_check, deadline)
        if _generation(source, source_generation) != observed_generation:
            raise TransferError("source_changed")

    check()
    _private_directory(cache_root, create=True)
    try:
        with runtime_use(cache_root, exclusive=True, timeout=0):
            check()
            budget.reserve_temp(_cache_bytes(cache_root, transfer_limits, check))
            _local_authority(local_context)
            progress_path = cache_root / "progress.json"
            stored = _read(progress_path)
            if stored is None:
                progress = {"binding": binding, "attempt": secrets.token_hex(12), "chunks": []}
                _write_progress(progress_path, progress, local_context, budget)
            else:
                progress = _open_progress(stored, local_context, budget=budget)
                if (set(progress) != {"binding", "attempt", "chunks"}
                        or progress["binding"] != binding
                        or type(progress["attempt"]) is not str
                        or _ATTEMPT.fullmatch(progress["attempt"]) is None
                        or type(progress["chunks"]) is not list
                        or len(progress["chunks"]) > transfer_limits.max_chunks):
                    raise TransferError("progress_mismatch")
            chunks = progress["chunks"]
            count = (descriptor.serialized_length + transfer_limits.chunk_bytes - 1) // transfer_limits.chunk_bytes
            if len(chunks) > count:
                raise TransferError("progress_invalid")
            start = metadata.header.messages_offset + descriptor.offset
            reused = 0
            for index, entry in enumerate(chunks):
                check()
                expected_length = min(transfer_limits.chunk_bytes,
                                      descriptor.serialized_length - index * transfer_limits.chunk_bytes)
                if (type(entry) is not list or len(entry) != 2
                        or type(entry[0]) is not int or entry[0] != expected_length
                        or type(entry[1]) is not str or _HEX.fullmatch(entry[1]) is None):
                    raise TransferError("progress_invalid")
                data = _read_private(_chunk_path(cache_root, progress["attempt"], index), expected_length, check)
                if len(data) != expected_length or hashlib.sha256(data).hexdigest() != entry[1]:
                    raise TransferError("cache_invalid")
                reused += 1
            checked_source = _CheckedSource(source, check)
            for index in range(len(chunks), count):
                check()
                offset = start + index * transfer_limits.chunk_bytes
                length = min(transfer_limits.chunk_bytes,
                             descriptor.serialized_length - index * transfer_limits.chunk_bytes)
                data = checked_source.read_range(offset, length)
                if len(data) != length:
                    raise TransferError("source_changed")
                chunk_path = _chunk_path(cache_root, progress["attempt"], index)
                if not chunk_path.exists():
                    budget.reserve_temp(len(data))
                _publish_exact(chunk_path, data, check)
                chunks.append([length, hashlib.sha256(data).hexdigest()])
                check()
                _write_progress(progress_path, progress, local_context, budget)
            check()
            _local_authority(local_context)
            def recheck_remote() -> None:
                check()
                current = inspect_transfer_metadata(
                    source, policy=policy, trust_state_path=trust_state_path,
                    gnupg_home=gnupg_home, expected_key=expected_key,
                    passphrase_reference=passphrase_reference,
                    secret_resolver=secret_resolver, limits=limits,
                    cancel_check=cancel_check,
                    _budget=budget,
                )
                if _binding(current, _descriptor(current, message_index),
                            _generation(source, source_generation), local_context,
                            transfer_limits) != binding:
                    raise TransferError("metadata_changed")
                check()

            recheck_remote()
            cached = _CachedSource(
                checked_source, start=start, end=start + descriptor.serialized_length,
                root=cache_root, attempt=progress["attempt"],
                chunk_bytes=transfer_limits.chunk_bytes, chunks=chunks, check=check,
            )
            output = cache_root / "selected.cipher"
            if output.exists():
                if _hash_private(output, descriptor.ciphertext_length, check) != (
                        descriptor.ciphertext_length, descriptor.sha256):
                    raise TransferError("ciphertext_collision")
            else:
                budget.reserve_temp(descriptor.ciphertext_length)
                try:
                    with ExclusiveOutput(output) as target:
                        name, descriptor_fd = target.stage()
                        with os.fdopen(descriptor_fd, "wb") as stream:
                            copied = copy_message(cached, metadata.header, descriptor, stream,
                                                  expected_index=message_index, limits=limits)
                            if copied != descriptor.ciphertext_length:
                                raise TransferError("ciphertext_invalid")
                            stream.flush()
                            os.fsync(stream.fileno())
                        recheck_remote()
                        _local_authority(local_context)
                        try:
                            target.publish(name)
                        except OSError:
                            if _hash_private(output, descriptor.ciphertext_length, check) != (
                                    descriptor.ciphertext_length, descriptor.sha256):
                                raise TransferError("publish_uncertain") from None
                        target.published_identity = None
                except OSError:
                    if _hash_private(output, descriptor.ciphertext_length, check) != (
                            descriptor.ciphertext_length, descriptor.sha256):
                        raise TransferError("publish_uncertain") from None
            recheck_remote()
            _local_authority(local_context)
            check()
            retained = _cache_bytes(cache_root, transfer_limits, check)
            return TransferResult(output, descriptor.sha256,
                                  metadata.logical_commitment_sha256,
                                  message_index, retained, reused)
    except TimeoutError:
        raise TransferError("busy") from None
    except ContainerPreparationError:
        _check(cancel_check, deadline)
        raise TransferError("cache_limit") from None


def _copy_ciphertext(source: AnchoredFile, destination: BinaryIO, *,
                     length: int, sha256: str, check: Callable[[], None]) -> None:
    info = os.fstat(source.file)
    if info.st_size != length:
        raise TransferError("ciphertext_invalid")
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        check()
        block = os.read(source.file, min(1024 * 1024, remaining))
        if not block:
            raise TransferError("ciphertext_invalid")
        digest.update(block)
        destination.write(block)
        remaining -= len(block)
    if not source.unchanged() or digest.hexdigest() != sha256:
        raise TransferError("ciphertext_invalid")
    check()


def _observe_local(path: Path, length: int, sha256: str,
                   check: Callable[[], None]) -> str:
    try:
        seen = _hash_private(path, length, check)
    except TransferError:
        raise TransferError("publish_uncertain") from None
    if seen != (length, sha256):
        raise TransferError("ciphertext_collision")
    return "identical"


class _CheckedReader:
    """Expose only bounded sequential reads of the verified held descriptor."""

    def __init__(self, source: AnchoredFile, check: Callable[[], None]):
        self.source, self.check = source, check
        self.count = 0
        self.digest = hashlib.sha256()

    def read(self, size: int) -> bytes:
        if type(size) is not int or not 0 <= size <= 1024 * 1024:
            raise TransferError("provider_read_unbounded")
        self.check()
        if not self.source.unchanged():
            raise TransferError("ciphertext_invalid")
        data = os.read(self.source.file, size)
        self.count += len(data)
        self.digest.update(data)
        self.check()
        if not self.source.unchanged():
            raise TransferError("ciphertext_invalid")
        return data


def _publish_file_exact(
    source_path: Path, *, destination: Path | ConditionalCipherPublisher | ContentAddressedCipherPublisher,
    key: str, length: int, sha256: str, refresh: Callable[[], None],
    deadline: float | None = None, cancel_check: Callable[[], bool] | None = None,
    publication_mode: str = 'conditional', reconcile_only: bool = False,
) -> PublicationResult:
    """Publish or reconcile exact bytes without updating a mutable head."""
    deadline = time.monotonic() + 3600 if deadline is None else deadline
    check = lambda: _check(cancel_check, deadline)
    if (not isinstance(publication_mode, str)
            or publication_mode not in {'conditional', 'content_addressed'}
            or type(reconcile_only) is not bool):
        raise TransferError('publication_invalid')
    if isinstance(destination, Path) and (not destination.is_absolute() or '..' in destination.parts):
        raise TransferError('publication_invalid')
    check()
    refresh()
    check()
    if publication_mode == 'content_addressed':
        from .container_content_publication import publish_content_addressed
        return publish_content_addressed(source_path, destination=destination, key=key,
            length=length, sha256=sha256, refresh=refresh, check=check, deadline=deadline,
            reconcile_only=reconcile_only)
    if reconcile_only:
        if not isinstance(destination, Path) and not callable(getattr(destination, 'observe', None)):
            raise TransferError('publication_invalid')
        try:
            if isinstance(destination, Path):
                _observe_local(destination, length, sha256, check)
            else:
                observed = destination.observe(key, timeout_seconds=deadline-time.monotonic(),
                    cancel_check=lambda: time.monotonic() >= deadline or bool(cancel_check and cancel_check()))
                if observed is None:
                    raise TransferError('publish_uncertain')
                if observed != (length, sha256):
                    raise TransferError('ciphertext_collision')
        except TransferError:
            raise
        except Exception:
            raise TransferError('publish_uncertain') from None
        refresh()
        check()
        return PublicationResult('identical', key, sha256)
    if isinstance(destination, Path):
        if not destination.is_absolute() or ".." in destination.parts:
            raise TransferError("publication_invalid")
        if destination.exists():
            status = _observe_local(destination, length, sha256, check)
            refresh()
            check()
            return PublicationResult(status, key, sha256)
        try:
            with AnchoredFile(source_path, private=True) as verified, \
                 ExclusiveOutput(destination) as output:
                name, fd = output.stage()
                with os.fdopen(fd, "wb") as stream:
                    _copy_ciphertext(verified, stream, length=length, sha256=sha256, check=check)
                    stream.flush()
                    os.fsync(stream.fileno())
                refresh()
                check()
                try:
                    output.publish(name)
                except OSError:
                    _observe_local(destination, length, sha256, check)
                    refresh()
                    check()
                    output.published_identity = None
                    return PublicationResult("observed_after_uncertain", key, sha256)
                refresh()
                check()
                output.published_identity = None
                return PublicationResult("created", key, sha256)
        except OSError:
            status = _observe_local(destination, length, sha256, check)
            refresh()
            check()
            return PublicationResult(status, key, sha256)
    if not callable(getattr(destination, "create_if_absent", None)) or not callable(getattr(destination, "observe", None)):
        raise TransferError("publication_invalid")
    def cancelled() -> bool:
        try:
            check()
            return False
        except TransferError:
            return True

    dispatched = False
    try:
        with AnchoredFile(source_path, private=True) as verified:
            if _hash_anchored(verified, length, check) != (length, sha256):
                raise TransferError("ciphertext_invalid")
            os.lseek(verified.file, 0, os.SEEK_SET)
            stream = _CheckedReader(verified, check)
            check()
            dispatched = True
            outcome = destination.create_if_absent(
                key, stream, length=length, sha256=sha256,
                timeout_seconds=deadline - time.monotonic(), cancel_check=cancelled,
            )
            check()
            if not verified.unchanged():
                raise TransferError("ciphertext_invalid")
            if outcome == "created" and (stream.count, stream.digest.hexdigest()) != (length, sha256):
                raise TransferError("publish_uncertain")
        refresh()
        check()
        if outcome == "created":
            return PublicationResult("created", key, sha256)
        if outcome != "exists":
            raise TransferError("publish_uncertain")
    except TransferError:
        if not dispatched:
            raise
        outcome = "uncertain"
    except Exception:
        outcome = "uncertain"
    try:
        check()
        observed = destination.observe(
            key, timeout_seconds=deadline - time.monotonic(), cancel_check=cancelled,
        )
        check()
    except Exception:
        raise TransferError("publish_uncertain") from None
    if observed is None:
        raise TransferError("publish_uncertain")
    if observed != (length, sha256):
        raise TransferError("ciphertext_collision")
    refresh()
    check()
    return PublicationResult("observed_after_uncertain" if outcome == "uncertain" else "identical",
                             key, sha256)


def publish_prepared_volume(
    prepared: PreparedVolume, *, destination: Path | ConditionalCipherPublisher | ContentAddressedCipherPublisher,
    local_context: CatalogContext,
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    limits: ContainerLimits = ContainerLimits(),
    cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: float = 3600,
    publication_mode: str = 'conditional', reconcile_only: bool = False,
) -> PublicationResult:
    """Publish exact self-contained `.lssc` bytes proven by preparation.

    The prepared receipt alone is not authority: current protected metadata,
    descriptor inventory, whole-file length, and transport digest are checked
    before publication. Conditional creation remains the default. The explicit
    content-addressed mode requires exact carrier readback. After an uncertain
    dispatch, retain its prepared bytes and use reconcile_only; that mode never
    sends data. Acceptance remains separate.
    """
    if not isinstance(prepared, PreparedVolume) or not isinstance(local_context, CatalogContext):
        raise TransferError("publication_invalid")
    if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600
            or cancel_check is not None and not callable(cancel_check)):
        raise TransferError("limits_invalid")
    budget = _Budget(ContainerPreparationLimits(
        container_limits=limits, timeout_seconds=timeout_seconds, cancel_check=cancel_check,
    ))
    check = lambda: _check(cancel_check, budget.deadline)
    check()
    _local_authority(local_context)

    def verify() -> None:
        check()
        _local_authority(local_context)
        with LocalFileRangeSource(prepared.path) as file_source:
            observed = inspect_transfer_metadata(
                file_source, policy=policy, trust_state_path=trust_state_path,
                gnupg_home=gnupg_home, expected_key=prepared.key,
                passphrase_reference=passphrase_reference,
                secret_resolver=secret_resolver, limits=limits,
                cancel_check=cancel_check,
                _budget=budget,
            )
            if (observed.key != prepared.key or observed.volume_id != prepared.volume_id
                    or len(observed.volume_record["messages"]) != prepared.message_count
                    or file_source.size != prepared.serialized_length):
                raise TransferError("prepared_mismatch")
            digest = hashlib.sha256()
            for offset in range(0, prepared.serialized_length, 1024 * 1024):
                check()
                length = min(1024 * 1024, prepared.serialized_length - offset)
                digest.update(file_source.read_range(offset, length))
            if digest.hexdigest() != prepared.transport_sha256:
                raise TransferError("prepared_mismatch")
        check()

    verify()
    return _publish_file_exact(
        prepared.path, destination=destination, key=prepared.key,
        length=prepared.serialized_length, sha256=prepared.transport_sha256,
        refresh=verify, deadline=budget.deadline, cancel_check=cancel_check,
        publication_mode=publication_mode, reconcile_only=reconcile_only,
    )
