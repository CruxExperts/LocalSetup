"""Bounded construction of signed, encrypted secure-container volumes.

The caller supplies a separately verified local publishing authority. This
module does not enroll keys, trust carrier paths, or release plaintext.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field, replace
import hashlib
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Callable, Sequence

from .artifact_records import (
    ArtifactProvenance, ArtifactRecordError, canonical_bytes, descriptor_object,
    encode_message_prefix, logical_commitment, provenance_object, schema_envelope,
    validate_inventory, validate_opaque_id, parse_descriptor, canonical_container_key,
)
from .container_delta_records import FileDelta
from .container_snapshot_records import SnapshotClaim
from .container_format import (
    PRIVATE_CONTAINER_VERSION, ContainerLimitError, ContainerLimits,
    ContainerWriteResult, write_container,
)
from .container_packing import PackingItem, PackingLimits, PackedPart, plan_volumes
from .envelope import MAX_PAYLOAD_BYTES, seal_envelope_file
from .envelope_io import AnchoredFile, ExclusiveOutput, FileEnvelopeLimits
from .contracts import LocalTrust
from .secrets import SecretReference, SecretResolver

_ZERO_HASH = "0" * 64


class ContainerPreparationError(ValueError):
    """Input, limits, or actual-size convergence prevented safe preparation."""


@dataclass(frozen=True, slots=True)
class ContainerInput:
    path: str | os.PathLike[str]
    file_id: str
    provenance: ArtifactProvenance = field(default_factory=ArtifactProvenance)
    delta: FileDelta | None = None

    def __post_init__(self) -> None:
        validate_opaque_id(self.file_id)
        if (not isinstance(self.path, (str, os.PathLike)) or not isinstance(self.provenance, ArtifactProvenance)
                or self.delta is not None and not isinstance(self.delta, FileDelta)):
            raise ContainerPreparationError("invalid container input")


@dataclass(frozen=True, slots=True)
class ContainerNamespace:
    tenant_id: str
    project_id: str
    publisher_opaque_id: str

    def __post_init__(self) -> None:
        try:
            canonical_container_key(
                self.tenant_id, self.project_id, self.publisher_opaque_id,
                "1" * 32, "2" * 32, "3" * 32,
            )
        except ArtifactRecordError:
            raise ContainerPreparationError("invalid approved public namespace")

    def key(self, artifact_id: str, version_id: str, volume_id: str) -> str:
        return canonical_container_key(
            self.tenant_id, self.project_id, self.publisher_opaque_id,
            artifact_id, version_id, volume_id,
        )


@dataclass(frozen=True, slots=True)
class ContainerPreparationLimits:
    file_limits: FileEnvelopeLimits = field(default_factory=lambda: FileEnvelopeLimits(
        max_plaintext_bytes=2 * 1024 * 1024 * 1024,
        max_envelope_bytes=2 * 1024 * 1024 * 1024,
        max_temporary_bytes=5 * 1024 * 1024 * 1024,
        max_diagnostics_bytes=64 * 1024,
        timeout_seconds=60,
    ))
    container_limits: ContainerLimits = field(default_factory=ContainerLimits)
    packing_limits: PackingLimits = field(default_factory=PackingLimits)
    max_plan_passes: int = 8
    max_generated_bytes: int = 4 * 1024 * 1024 * 1024
    max_temporary_bytes: int = 4 * 1024 * 1024 * 1024
    max_crypto_operations: int = 100_000
    timeout_seconds: float = 3600
    cancel_check: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.file_limits, FileEnvelopeLimits)
                or not isinstance(self.container_limits, ContainerLimits)
                or not isinstance(self.packing_limits, PackingLimits)
                or any(type(getattr(self, name)) is not int or getattr(self, name) <= 0
                       for name in ("max_plan_passes", "max_generated_bytes",
                                    "max_temporary_bytes", "max_crypto_operations"))
                or type(self.timeout_seconds) not in (int, float)
                or not 0 < self.timeout_seconds <= 86400
                or (self.cancel_check is not None and not callable(self.cancel_check))):
            raise ContainerPreparationError("invalid preparation limits")


@dataclass(frozen=True, slots=True)
class PreparedVolume:
    key: str
    path: Path
    serialized_length: int
    transport_sha256: str
    volume_id: str
    message_count: int


@dataclass(frozen=True, slots=True)
class PreparedContainerSet:
    artifact_id: str
    version_id: str
    logical_commitment_sha256: str
    volumes: tuple[PreparedVolume, ...]


class _Budget:
    def __init__(self, limits: ContainerPreparationLimits) -> None:
        self.limits = limits
        self.deadline = time.monotonic() + limits.timeout_seconds
        self.generated = 0
        self.temporary = 0
        self.temporary_peak = 0
        self.crypto = 0

    def checkpoint(self) -> None:
        if self.limits.cancel_check is not None:
            try:
                cancelled = self.limits.cancel_check()
            except Exception:
                raise ContainerPreparationError("preparation cancelled") from None
            if cancelled:
                raise ContainerPreparationError("preparation cancelled")
        if time.monotonic() >= self.deadline:
            raise ContainerPreparationError("preparation deadline exceeded")

    def use_crypto(self) -> None:
        self.checkpoint()
        self.crypto += 1
        if self.crypto > self.limits.max_crypto_operations:
            raise ContainerPreparationError("cryptographic operation budget exceeded")

    def retain(self, count: int) -> None:
        self.checkpoint()
        self.generated += count
        if self.generated > self.limits.max_generated_bytes:
            raise ContainerPreparationError("generated byte budget exceeded")

    def reserve_temp(self, count: int) -> None:
        self.checkpoint()
        if count < 0:
            raise ContainerPreparationError("invalid temporary byte reservation")
        if self.temporary + count > self.limits.max_temporary_bytes:
            raise ContainerPreparationError("temporary byte budget exceeded")
        self.temporary += count
        self.temporary_peak = max(self.temporary_peak, self.temporary)

    def release_temp(self, count: int) -> None:
        if count < 0 or count > self.temporary:
            raise ContainerPreparationError("invalid temporary byte release")
        self.temporary -= count

    def file_limits(self, *, plaintext_output: bool = False) -> FileEnvelopeLimits:
        self.checkpoint()
        remaining_bytes = self.limits.max_temporary_bytes - self.temporary
        remaining_generated = self.limits.max_generated_bytes - self.generated
        remaining_seconds = self.deadline - time.monotonic()
        if remaining_bytes <= 0 or remaining_generated <= 0 or remaining_seconds <= 0:
            raise ContainerPreparationError("preparation resource budget exceeded")
        values = {
            "max_temporary_bytes": min(self.limits.file_limits.max_temporary_bytes,
                                       remaining_bytes),
            "timeout_seconds": min(self.limits.file_limits.timeout_seconds,
                                   remaining_seconds),
        }
        if plaintext_output:
            values["max_plaintext_bytes"] = min(
                self.limits.file_limits.max_plaintext_bytes, remaining_generated,
            )
        else:
            values["max_envelope_bytes"] = min(
                self.limits.file_limits.max_envelope_bytes, remaining_generated,
            )
        return replace(self.limits.file_limits, **values)

    def cancelled(self) -> bool:
        try:
            self.checkpoint()
        except ContainerPreparationError:
            return True
        return False


def _scan_file(value: ContainerInput, budget: _Budget) -> tuple[int, str, tuple[int, ...]]:
    sha = hashlib.sha256()
    with AnchoredFile(value.path) as source:
        size = source.size
        if size > budget.limits.packing_limits.max_total_plaintext_bytes:
            raise ContainerPreparationError("input exceeds total plaintext limit")
        position = 0
        while position < size:
            budget.checkpoint()
            chunk = os.pread(source.file, min(64 * 1024, size - position), position)
            if not chunk:
                raise ContainerPreparationError("input changed while reading")
            sha.update(chunk)
            position += len(chunk)
        if not source.unchanged():
            raise ContainerPreparationError("input changed while reading")
        return size, sha.hexdigest(), source.identity


def _walk_part(value: ContainerInput, part: PackedPart, identity: tuple[int, ...],
               budget: _Budget, sink: Callable[[bytes], None]) -> None:
    budget.checkpoint()
    with AnchoredFile(value.path) as source:
        if source.identity != identity:
            raise ContainerPreparationError("input changed during preparation")
        position = part.offset
        end = position + part.length
        while position < end:
            budget.checkpoint()
            chunk = os.pread(source.file, min(64 * 1024, end - position), position)
            if not chunk:
                raise ContainerPreparationError("input changed during preparation")
            sink(chunk)
            position += len(chunk)
        if not source.unchanged():
            raise ContainerPreparationError("input changed during preparation")


def _hash_part(value: ContainerInput, part: PackedPart, identity: tuple[int, ...],
               budget: _Budget) -> str:
    sha = hashlib.sha256()
    _walk_part(value, part, identity, budget, sha.update)
    return sha.hexdigest()


class _DiscardWriter:
    def __init__(self) -> None:
        self.count = 0

    def write(self, data: bytes) -> int:
        self.count += len(data)
        return len(data)


class _CheckedWriter:
    def __init__(self, destination, budget: _Budget, maximum: int | None = None) -> None:
        self.destination = destination
        self.budget = budget
        self.maximum = maximum
        self.count = 0

    def write(self, data: bytes) -> int:
        self.budget.checkpoint()
        if self.maximum is not None and self.count + len(data) > self.maximum:
            raise ContainerPreparationError("serialized volume exceeds reserved bytes")
        written = self.destination.write(data)
        self.count += written
        return written


def _write_stream(destination, metadata: bytes, messages: Sequence[Path],
                  limits: ContainerLimits,
                  budget: _Budget, maximum: int | None = None) -> ContainerWriteResult:
    def streams():
        for path in messages:
            with path.open("rb") as handle:
                yield handle

    with closing(streams()) as selected:
        # The audience is authenticated inside the encrypted metadata/envelopes.
        # Normal preparation and approval never publish its fingerprints.
        return write_container(_CheckedWriter(destination, budget, maximum), recipient_hints=(),
                               version=PRIVATE_CONTAINER_VERSION,
                               metadata_ciphertext=metadata, messages=selected,
                               limits=limits)


def _pad_metadata(value: dict, target: int) -> bytes:
    value["padding"] = ""
    minimum = canonical_bytes(value)
    if len(minimum) > target:
        raise ContainerPreparationError("protected metadata exceeds planning length")
    value["padding"] = "0" * (target - len(minimum))
    result = canonical_bytes(value)
    if len(result) != target:
        raise ContainerPreparationError("protected metadata length did not converge")
    return result


def _seal(payload: bytes, *, home: str | os.PathLike[str], signer: str,
          audience: tuple[str, ...], passphrase_reference: SecretReference,
          secret_resolver: SecretResolver, budget: _Budget, workspace: Path) -> bytes:
    budget.use_crypto()
    if len(payload) > min(MAX_PAYLOAD_BYTES, budget.limits.file_limits.max_plaintext_bytes):
        raise ContainerPreparationError("signed payload exceeds per-message limit")
    plain_path = workspace / (secrets.token_hex(16) + ".metadata-plain")
    cipher_path = workspace / (secrets.token_hex(16) + ".metadata-cipher")
    budget.reserve_temp(len(payload))
    try:
        with plain_path.open("xb") as handle:
            os.chmod(plain_path, 0o600)
            handle.write(payload)
        seal_envelope_file(
            plain_path, cipher_path, limits=budget.file_limits(),
            gnupg_home=home, signer_fingerprint=signer,
            recipient_fingerprints=audience, local_trust=LocalTrust({signer}),
            passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
            cancel_check=budget.cancelled, disable_compression=True,
        )
        size = cipher_path.stat().st_size
        budget.reserve_temp(size)
        try:
            if size > budget.limits.container_limits.max_metadata_bytes:
                raise ContainerPreparationError("encrypted metadata exceeds container limit")
            result = cipher_path.read_bytes()
            budget.checkpoint()
            budget.retain(size)
            return result
        finally:
            cipher_path.unlink(missing_ok=True)
            budget.release_temp(size)
    finally:
        plain_path.unlink(missing_ok=True)
        budget.release_temp(len(payload))


def prepare_volumes(
    files: Sequence[ContainerInput], *, namespace: ContainerNamespace,
    logical_policy: dict, output_dir: str | os.PathLike[str],
    gnupg_home: str | os.PathLike[str], passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    limits: ContainerPreparationLimits = ContainerPreparationLimits(),
    final_check: Callable[[], None] | None = None,
    artifact_id: str | None = None, parent_versions: tuple[str, ...] = (),
    snapshot: SnapshotClaim | None = None, _budget: _Budget | None = None,
    _version_id: str | None = None,
) -> PreparedContainerSet:
    """Prepare an immutable set, publishing only after final exact-size checks."""
    destination = Path(output_dir)
    if not destination.is_absolute() or ".." in destination.parts:
        raise ContainerPreparationError("output directory must be absolute and anchored")
    budget = _budget if _budget is not None else _Budget(limits)
    if not isinstance(budget, _Budget) or budget.limits != limits:
        raise ContainerPreparationError("invalid shared preparation budget")
    before = budget.temporary
    try:
        with tempfile.TemporaryDirectory(prefix=".lssc-prep-", dir=destination) as temporary:
            return _prepare_volumes_in_workspace(
                files, namespace=namespace, logical_policy=logical_policy,
                output_dir=output_dir, workspace=Path(temporary),
                gnupg_home=gnupg_home, passphrase_reference=passphrase_reference,
                secret_resolver=secret_resolver, limits=limits,
                final_check=final_check, artifact_id=artifact_id,
                parent_versions=parent_versions, snapshot=snapshot, shared_budget=budget,
                _version_id=_version_id,
            )
    finally:
        budget.release_temp(budget.temporary - before)


def _prepare_volumes_in_workspace(
    files: Sequence[ContainerInput], *, namespace: ContainerNamespace,
    logical_policy: dict, output_dir: str | os.PathLike[str], workspace: Path,
    gnupg_home: str | os.PathLike[str], passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    limits: ContainerPreparationLimits,
    final_check: Callable[[], None] | None,
    artifact_id: str | None,
    parent_versions: tuple[str, ...],
    snapshot: SnapshotClaim | None = None, shared_budget: _Budget | None = None,
    _version_id: str | None = None,
) -> PreparedContainerSet:
    if (not files or len(files) > min(limits.packing_limits.max_entries, limits.container_limits.max_files)
            or not isinstance(namespace, ContainerNamespace)
            or not isinstance(passphrase_reference, SecretReference)
            or not isinstance(secret_resolver, SecretResolver)):
        raise ContainerPreparationError("invalid preparation request")
    try:
        if artifact_id is not None:
            validate_opaque_id(artifact_id)
        if _version_id is not None:
            validate_opaque_id(_version_id)
        if (type(parent_versions) is not tuple or len(parent_versions) > 64
                or any(validate_opaque_id(parent) != parent for parent in parent_versions)
                or tuple(sorted(set(parent_versions))) != parent_versions):
            raise ContainerPreparationError("invalid parent versions")
    except ArtifactRecordError:
        raise ContainerPreparationError("invalid container identity") from None
    if snapshot is not None and not isinstance(snapshot, SnapshotClaim):
        raise ContainerPreparationError("invalid snapshot claim")
    budget = shared_budget if shared_budget is not None else _Budget(limits)
    if not isinstance(budget, _Budget) or budget.limits != limits:
        raise ContainerPreparationError("invalid shared preparation budget")
    ordered = tuple(files)
    if any(not isinstance(value, ContainerInput) for value in ordered):
        raise ContainerPreparationError("invalid container input")
    by_id = {value.file_id: value for value in ordered}
    if len(by_id) != len(ordered):
        raise ContainerPreparationError("duplicate input file identity")
    total = 0
    scanned: dict[str, tuple[int, str, tuple[int, ...]]] = {}
    for value in ordered:
        scanned[value.file_id] = _scan_file(value, budget)
        if value.delta is not None:
            if ((scanned[value.file_id][0], scanned[value.file_id][1]) !=
                    (value.delta.patch.length, value.delta.patch.sha256)
                    or value.delta.base_version not in parent_versions):
                raise ContainerPreparationError("delta input does not match its protected claim")
        total += scanned[value.file_id][0]
        if total > limits.packing_limits.max_total_plaintext_bytes:
            raise ContainerPreparationError("aggregate plaintext limit exceeded")
    artifact_id = secrets.token_hex(16) if artifact_id is None else artifact_id
    version_id = secrets.token_hex(16) if _version_id is None else _version_id
    logical = dict(logical_policy)
    logical.update({
        "artifact_id": artifact_id, "version_id": version_id,
        "parent_versions": list(parent_versions),
        "files": [{"file_id": value.file_id, "length": value.delta.result.length if value.delta else scanned[value.file_id][0],
                   "sha256": value.delta.result.sha256 if value.delta else scanned[value.file_id][1],
                   "provenance": provenance_object(value.provenance),
                   **({"delta": value.delta.record()} if value.delta else {})} for value in ordered],
    })
    if snapshot is not None:
        logical['snapshot'] = snapshot.record()
    commitment = logical_commitment(logical, limits=limits.container_limits)
    signer = logical["origin"]["fingerprint"]
    audience = tuple(logical["audience"])
    cipher_cache: dict[PackedPart, tuple[Path, str]] = {}
    identities = {value.file_id: scanned[value.file_id][2] for value in ordered}
    ceiling = min(limits.packing_limits.target_bytes * (1 + limits.packing_limits.tolerance),
                  limits.packing_limits.hard_ceiling_bytes or float("inf"))
    metadata_target = max(2048, 1024 + len(ordered) * 512)
    volume_ids: list[str] = []

    def cipher(part: PackedPart) -> Path:
        budget.checkpoint()
        existing = cipher_cache.get(part)
        if existing is not None:
            return existing[0]
        plaintext_sha = _hash_part(by_id[part.identity], part, identities[part.identity], budget)
        prefix = encode_message_prefix(
            commitment, file_id=part.identity, offset=part.offset,
            segment_index=part.segment_index, segment_count=part.segment_count,
            payload_length=part.length, plaintext_sha256=plaintext_sha,
        )
        if part.length + len(prefix) > limits.file_limits.max_plaintext_bytes:
            raise ContainerPreparationError("segment requires splitting")
        token = secrets.token_hex(16)
        plain_path = workspace / (token + ".plain")
        cipher_path = workspace / (token + ".cipher")
        plain_reservation = part.length + len(prefix)
        budget.reserve_temp(plain_reservation)
        try:
            with plain_path.open("xb") as handle:
                os.chmod(plain_path, 0o600)
                handle.write(prefix)
                _walk_part(by_id[part.identity], part, identities[part.identity], budget, handle.write)
                handle.flush()
                os.fsync(handle.fileno())
            budget.use_crypto()
            seal_envelope_file(
                plain_path, cipher_path, limits=budget.file_limits(),
                gnupg_home=gnupg_home, signer_fingerprint=signer,
                recipient_fingerprints=audience, local_trust=LocalTrust({signer}),
                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                cancel_check=budget.cancelled,
                disable_compression=True,
            )
            budget.checkpoint()
            encrypted_length = cipher_path.stat().st_size
            budget.reserve_temp(encrypted_length)
            budget.retain(encrypted_length)
            cipher_cache[part] = (cipher_path, plaintext_sha)
            return cipher_path
        finally:
            plain_path.unlink(missing_ok=True)
            budget.release_temp(plain_reservation)

    for _pass in range(limits.max_plan_passes):
        budget.checkpoint()
        # Real signed ciphertext fixes metadata framing length for candidate
        # measurements. Final protected records are padded to the same length.
        trial_metadata = _seal(b"0" * metadata_target, home=gnupg_home, signer=signer,
                               audience=audience, passphrase_reference=passphrase_reference,
                               secret_resolver=secret_resolver, budget=budget,
                               workspace=workspace)

        def measure(parts: tuple[PackedPart, ...]) -> int:
            budget.checkpoint()
            try:
                messages = tuple(cipher(part) for part in parts)
            except ContainerPreparationError as exc:
                if str(exc) == "segment requires splitting":
                    return int(ceiling) + 1
                raise
            sink = _DiscardWriter()
            try:
                _write_stream(sink, trial_metadata, messages,
                              limits.container_limits, budget)
            except ContainerLimitError:
                return int(ceiling) + 1
            return sink.count

        items = [PackingItem(value.file_id, scanned[value.file_id][0]) for value in ordered]
        packing_limits = replace(
            limits.packing_limits,
            max_entries=min(limits.packing_limits.max_entries, limits.container_limits.max_files),
            max_parts=min(limits.packing_limits.max_parts, limits.container_limits.max_total_segments),
            max_volumes=min(limits.packing_limits.max_volumes, limits.container_limits.max_volumes),
        )
        plan = plan_volumes(items, measure_serialized_size=measure, limits=packing_limits)
        while len(volume_ids) < len(plan.volumes):
            volume_ids.append(secrets.token_hex(16))
        inventory = {"volumes": []}
        for volume in plan.volumes:
            budget.checkpoint()
            messages = tuple(cipher(part) for part in volume.parts)
            framed = _write_stream(_DiscardWriter(), trial_metadata, messages,
                                   limits.container_limits, budget)
            inventory["volumes"].append({
                "volume_id": volume_ids[volume.index],
                "key": namespace.key(artifact_id, version_id, volume_ids[volume.index]),
                "messages": [{
                    "file_id": part.identity, "offset": part.offset, "length": part.length,
                    "segment_index": part.segment_index, "segment_count": part.segment_count,
                    "plaintext_sha256": cipher_cache[part][1],
                    "descriptor": descriptor_object(framed.message_descriptors[index]),
                } for index, part in enumerate(volume.parts)],
            })
        validate_inventory(inventory, logical, limits=limits.container_limits)
        base = {"schema": schema_envelope(deltas=any(value.delta for value in ordered), snapshot='snapshot' in logical,
                                          worker_snapshot=snapshot is not None and snapshot.worker is not None), "type": "origin", "logical": logical,
                "logical_sha256": commitment, "inventory": inventory,
                "this_volume": volume_ids[0], "header_sha256": _ZERO_HASH, "padding": ""}
        minimum = len(canonical_bytes(base))
        if minimum > metadata_target:
            metadata_target = minimum
            continue
        if metadata_target > limits.container_limits.max_metadata_bytes:
            raise ContainerPreparationError("protected metadata exceeds configured limit")

        final: list[tuple[bytes, ContainerWriteResult]] = []
        retry = False
        for volume in plan.volumes:
            budget.checkpoint()
            metadata = dict(base)
            metadata["this_volume"] = volume_ids[volume.index]
            candidate_cipher = _seal(_pad_metadata(metadata, metadata_target),
                                     home=gnupg_home, signer=signer, audience=audience,
                                     passphrase_reference=passphrase_reference,
                                     secret_resolver=secret_resolver, budget=budget,
                                     workspace=workspace)
            messages = tuple(cipher(part) for part in volume.parts)
            candidate_frame = _write_stream(_DiscardWriter(), candidate_cipher, messages,
                                            limits.container_limits, budget)
            metadata["header_sha256"] = candidate_frame.public_header_sha256
            final_cipher = _seal(_pad_metadata(metadata, metadata_target),
                                 home=gnupg_home, signer=signer, audience=audience,
                                 passphrase_reference=passphrase_reference,
                                 secret_resolver=secret_resolver, budget=budget,
                                 workspace=workspace)
            if len(final_cipher) != len(candidate_cipher) or len(final_cipher) != len(trial_metadata):
                retry = True
                break
            sink = _DiscardWriter()
            result = _write_stream(sink, final_cipher, messages,
                                   limits.container_limits, budget)
            if (result.public_header_sha256 != metadata["header_sha256"]
                    or result.message_descriptors != tuple(
                        parse_descriptor(item["descriptor"])
                        for item in inventory["volumes"][volume.index]["messages"]
                    )):
                raise ContainerPreparationError("final framing differs from protected inventory")
            if sink.count > ceiling:
                retry = True
                break
            final.append((final_cipher, result))
        if retry:
            metadata_target += max(256, metadata_target // 8)
            continue
        destination = Path(output_dir)
        staged: list[tuple[ExclusiveOutput, int]] = []
        prepared: list[PreparedVolume] = []
        try:
            for volume, (metadata_cipher, expected) in zip(plan.volumes, final, strict=True):
                key = inventory["volumes"][volume.index]["key"]
                output = ExclusiveOutput(destination / (volume_ids[volume.index] + ".lssc"))
                staged.append((output, 0))
                budget.reserve_temp(expected.serialized_length)
                staged[-1] = (output, expected.serialized_length)
                budget.retain(expected.serialized_length)
                name, descriptor = output.stage()
                with os.fdopen(descriptor, "w+b", closefd=True) as handle:
                    messages = tuple(cipher(part) for part in volume.parts)
                    result = _write_stream(handle, metadata_cipher, messages,
                                           limits.container_limits,
                                           budget, expected.serialized_length)
                    handle.flush()
                    os.fsync(handle.fileno())
                    if result != expected or handle.tell() > ceiling:
                        raise ContainerPreparationError("published framing differs from measured volume")
                    handle.seek(0)
                    digest = hashlib.sha256()
                    while chunk := handle.read(64 * 1024):
                        budget.checkpoint()
                        digest.update(chunk)
                prepared.append(PreparedVolume(
                    key=key, path=output.path, serialized_length=result.serialized_length,
                    transport_sha256=digest.hexdigest(),
                    volume_id=volume_ids[volume.index], message_count=len(volume.parts),
                ))
            budget.checkpoint()
            if final_check is not None:
                final_check()
            budget.checkpoint()
            for output, _reservation in staged:
                output.publish(next(iter(output.owned)))
                budget.checkpoint()
                if final_check is not None:
                    final_check()
                budget.checkpoint()
            for output, _reservation in staged:
                output.published_identity = None
            return PreparedContainerSet(artifact_id, version_id, commitment, tuple(prepared))
        finally:
            cleanup_error: Exception | None = None
            for output, reservation in staged:
                try:
                    output.close()
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                else:
                    budget.release_temp(reservation)
            if cleanup_error is not None:
                raise cleanup_error
    raise ContainerPreparationError("container packing did not converge within plan budget")
