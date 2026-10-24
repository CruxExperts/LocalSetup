"""Release a complete logical file from independently authenticated segments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile

from . import artifacts, trust_state
from .artifact_records import canonical_bytes, parse_descriptor, validate_opaque_id
from .container_preparation import ContainerPreparationError, ContainerPreparationLimits, _Budget
from .container_sources import RangeSource
from .envelope_io import AnchoredFile, ExclusiveOutput, OperationBudget, hash_file
from .secrets import SecretReference, SecretResolver


class ReconstructionError(RuntimeError):
    """A safe reconstruction status, without private names or carrier paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ContainerFileReceipt:
    status: str
    logical_commitment_sha256: str
    file_id: str
    plaintext_sha256: str
    plaintext_length: int
    segment_count: int


def _operation_budget(budget: _Budget) -> OperationBudget:
    budget.checkpoint()
    result = OperationBudget(budget.limits.file_limits, budget.cancelled)
    result.deadline = min(result.deadline, budget.deadline)
    return result


def _append_segment(opened: AnchoredFile, destination, *, offset: int, length: int,
                    digest, budget: _Budget) -> None:
    position = offset
    while position < offset + length:
        budget.checkpoint()
        data = os.pread(opened.file, min(64 * 1024, offset + length - position), position)
        if not data or destination.write(data) != len(data):
            raise ReconstructionError("segment_changed")
        digest.update(data)
        position += len(data)
    if not opened.unchanged():
        raise ReconstructionError("segment_changed")
    budget.checkpoint()


def extract_container_file(
    sources: Mapping[str, RangeSource], file_id: str, destination_name: str, *,
    authorized: bool, policy: artifacts.ArtifactPolicy | artifacts.ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    limits: ContainerPreparationLimits = ContainerPreparationLimits(),
) -> ContainerFileReceipt:
    """Reconstruct one current file, publishing only after complete verification.

    Map exact carrier keys to range sources in any order. The protected global
    inventory determines segment ordering; all necessary volumes must be present.
    No other file is decrypted. The caller's explicit protected destination is
    create-only, and existing envelope/segment APIs retain their own semantics.
    """
    if authorized is not True:
        raise artifacts.ArtifactError(artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED)
    artifacts._require_policy(policy)
    boundary = artifacts._require_boundary(policy.boundary)
    name = artifacts._validate_destination_name(destination_name)
    if (not isinstance(limits, ContainerPreparationLimits) or not isinstance(sources, Mapping)
            or not sources or len(sources) > limits.container_limits.max_volumes):
        raise ReconstructionError("invalid_input")
    try:
        validate_opaque_id(file_id)
    except Exception:
        raise ReconstructionError("invalid_input") from None
    budget = _Budget(limits)
    try:
        budget.checkpoint()
        selected_sources = {}
        for key, source in sources.items():
            budget.checkpoint()
            if type(key) is not str or len(selected_sources) >= limits.container_limits.max_volumes:
                raise ReconstructionError("invalid_input")
            selected_sources[key] = artifacts._BudgetedRangeSource(source, budget)
        with tempfile.TemporaryDirectory(prefix=".lssc-file-", dir=boundary) as temporary:
            workspace = Path(temporary)
            metadata_by_volume = {}
            first = None
            inventory_bytes = None
            for key in sorted(selected_sources):
                metadata = artifacts.inspect_container_metadata(
                    selected_sources[key], policy=policy, trust_state_path=trust_state_path,
                    gnupg_home=gnupg_home, expected_key=key,
                    passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                    limits=limits.container_limits, _budget=budget, _workspace=workspace,
                )
                current_inventory = canonical_bytes(metadata.inventory)
                if first is None:
                    first, inventory_bytes = metadata, current_inventory
                elif (metadata.logical_commitment_sha256 != first.logical_commitment_sha256
                      or current_inventory != inventory_bytes):
                    raise ReconstructionError("conflicting_volumes")
                if metadata.origin_volume_id in metadata_by_volume:
                    raise ReconstructionError("duplicate_volume")
                metadata_by_volume[metadata.origin_volume_id] = metadata
            assert first is not None
            file = next((item for item in first.logical["files"] if item["file_id"] == file_id), None)
            if file is None:
                raise ReconstructionError("file_missing")
            if file["length"] > limits.file_limits.max_plaintext_bytes:
                raise ReconstructionError("file_limit")
            segments = []
            for volume in first.inventory["volumes"]:
                for index, message in enumerate(volume["messages"]):
                    budget.checkpoint()
                    if message["file_id"] != file_id:
                        continue
                    metadata = metadata_by_volume.get(volume["volume_id"])
                    if metadata is None:
                        raise ReconstructionError("incomplete")
                    segments.append((message["offset"], index, metadata))
            segments.sort(key=lambda item: item[0])
            # inspect_container_metadata already proves gap-free inventory,
            # segment count, offsets and complete logical length across volumes.
            budget.reserve_temp(file["length"])
            assembled = workspace / "assembled.plain"
            receipts = []
            digest = hashlib.sha256()
            position = 0
            accepted_metadata = set()
            with assembled.open("xb") as destination:
                assembled.chmod(0o600)
                for expected_offset, index, metadata in segments:
                    budget.checkpoint()
                    if expected_offset != position:
                        raise ReconstructionError("incomplete")
                    if metadata.volume_id not in accepted_metadata:
                        artifacts._accept_verified_metadata(metadata, policy=policy,
                                                            trust_state_path=trust_state_path)
                        accepted_metadata.add(metadata.volume_id)
                    descriptor = parse_descriptor(metadata.volume_record["messages"][index]["descriptor"])
                    if descriptor.ciphertext_length > limits.file_limits.max_envelope_bytes:
                        raise ReconstructionError("file_limit")
                    budget.reserve_temp(descriptor.ciphertext_length)
                    plain_size = 0
                    try:
                        with tempfile.TemporaryDirectory(prefix="segment-", dir=workspace) as segment_dir:
                            cipher = Path(segment_dir) / "cipher"
                            plain = Path(segment_dir) / "plain"
                            cipher_digest = artifacts._copy_selected_file(
                                selected_sources[metadata.key], metadata, index, cipher,
                                limits.container_limits, budget,
                            )
                            budget.use_crypto()
                            offset, length = artifacts._open_selected_file(
                                cipher, plain, metadata, index, policy=policy,
                                trust_state_path=trust_state_path, gnupg_home=gnupg_home,
                                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                                historical=False, file_limits=budget.file_limits(plaintext_output=True),
                                budget=budget,
                            )
                            with AnchoredFile(plain, private=True) as opened, \
                                 AnchoredFile(cipher, private=True) as encrypted:
                                budget.reserve_temp(opened.size)
                                plain_size = opened.size
                                _append_segment(opened, destination, offset=offset, length=length,
                                                digest=digest, budget=budget)
                                _status, identity = trust_state._record_accepted_container(
                                    trust_state_path,
                                    identity_prefix=artifacts._selected_prefix(metadata, index),
                                    metadata_identity=artifacts._metadata_identity(metadata),
                                    layers=(*metadata.metadata_layers, (trust_state._ContainerCipherFile(
                                        encrypted, cipher_digest, limits.file_limits.max_envelope_bytes,
                                        _operation_budget(budget)), "publisher", policy.origin_fingerprint)),
                                    **_authority_arguments(policy, metadata),
                                    checkpoint=budget.checkpoint,
                                    release_payload=lambda: None, rollback_payload=lambda: None,
                                )
                                receipts.append(identity)
                                position += length
                    finally:
                        budget.release_temp(descriptor.ciphertext_length + plain_size)
                destination.flush()
                os.fsync(destination.fileno())
            if position != file["length"] or digest.hexdigest() != file["sha256"]:
                raise ReconstructionError("whole_file_mismatch")
            # Use metadata from a selected volume; unrelated supplied volumes
            # need not acquire acceptance receipts merely to inspect them.
            selected_metadata = segments[0][2]
            budget.reserve_temp(file["length"])
            with AnchoredFile(assembled, private=True) as opened, \
                 ExclusiveOutput(boundary / name) as output:
                release_budget = _operation_budget(budget)
                prefix = ":".join((first.logical["artifact_id"], first.logical["version_id"],
                                   file_id, first.logical_commitment_sha256, file["sha256"]))
                def release_check() -> None:
                    budget.checkpoint()
                    artifacts._policy_logical(selected_metadata.logical, policy, historical=False,
                                              limits=limits.container_limits)

                def release() -> None:
                    release_check()
                    observed_hash, observed_length = hash_file(
                        opened, offset=0, maximum=limits.file_limits.max_plaintext_bytes,
                        budget=release_budget,
                    )
                    if (observed_hash, observed_length) != (file["sha256"], file["length"]):
                        raise ReconstructionError("whole_file_mismatch")
                    release_check()
                    artifacts._copy_verified_payload(opened, offset=0, length=position,
                                                     output=output, budget=release_budget)
                    release_check()
                status, _identity = trust_state._record_accepted_container(
                    trust_state_path, identity_prefix=prefix,
                    metadata_identity=artifacts._metadata_identity(selected_metadata),
                    layers=selected_metadata.metadata_layers,
                    **_authority_arguments(policy, selected_metadata),
                    required_selected_receipts=tuple(receipts), checkpoint=budget.checkpoint,
                    release_payload=release,
                    rollback_payload=output.rollback_published,
                )
                budget.checkpoint()
                output.published_identity = None
            return ContainerFileReceipt(status, first.logical_commitment_sha256, file_id,
                                        file["sha256"], file["length"], len(segments))
    except Exception as exc:
        # Crypto wrappers may report their own safe failure after the shared
        # deadline or cancellation. Preserve the overall operation's reason.
        try:
            budget.checkpoint()
        except ContainerPreparationError as stopped:
            code = ("deadline_exceeded" if str(stopped) == "preparation deadline exceeded"
                    else "cancelled")
            raise ReconstructionError(code) from None
        if isinstance(exc, (ReconstructionError, artifacts.ArtifactError)):
            raise
        if isinstance(exc, trust_state.TrustStateError):
            raise artifacts.ArtifactError(artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED) from None
        if isinstance(exc, ContainerPreparationError):
            raise ReconstructionError("resource_limit") from None
        raise ReconstructionError("verification_failed") from None


def _authority_arguments(policy, metadata) -> dict:
    return dict(expected_store_id=policy.trust_store_id, expected_revision=policy.trust_revision,
                expected_epoch=policy.governance_epoch, expected_scope=policy.scope,
                created_at=metadata.logical["created_at"], expires_at=metadata.logical["expires_at"],
                **artifacts._discovery_bounds(policy))
