"""Trusted base selection and shared validation for bounded delta chains."""
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
from typing import Callable

from . import artifacts
from .container_delta_codec import DeltaCodec
from .container_delta_records import FileEvidence
from .container_dependency_receipts import DependencyReceipt


@dataclass(frozen=True, slots=True)
class DeltaBaseSelection:
    """Operator-selected base carriers and their independently admitted reader."""

    sources: object
    reader: object

    def __post_init__(self):
        from collections.abc import Mapping
        from .container_reader import ContainerReader
        if not isinstance(self.sources, Mapping) or not isinstance(self.reader, ContainerReader):
            raise ValueError('Invalid delta base selection')


@dataclass(frozen=True, slots=True)
class DeltaReconstructionPolicy:
    """Operator-selected codec and resolver, never supplied by container claims.

    The resolver returns a context manager holding the candidate base carriers.
    Their signatures and exact logical identity are verified by reconstruction.
    """

    codec: DeltaCodec
    resolve_base: Callable = field(repr=False, compare=False)
    max_chain_depth: int = 16

    def __post_init__(self):
        if (not isinstance(self.codec, DeltaCodec) or not callable(self.resolve_base)
                or type(self.max_chain_depth) is not int or not 1 <= self.max_chain_depth <= 64):
            raise ValueError("Invalid delta reconstruction policy")


@dataclass
class _DeltaContext:
    policy: DeltaReconstructionPolicy | None
    metadata: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    admissions: list = field(default_factory=list)
    receipt_groups: list = field(default_factory=list)

    def check(self, admission, budget):
        # Current stores/receipts are checked by the enclosing release guard.
        # Do not open new writer transactions while that guard holds the stores.
        for item, policy, _path in self.admissions:
            budget.checkpoint()
            artifacts._policy_logical(item.logical, policy, historical=False,
                                      limits=budget.limits.container_limits)

    def remember_receipts(self, path, metadata, policy, receipts):
        self.receipts.extend(receipts)
        self.receipt_groups.append(DependencyReceipt(Path(path), artifacts._metadata_identity(metadata),
            tuple(receipts), tuple((hashlib.sha256(data).hexdigest(), role, signer)
                                  for data, role, signer in metadata.metadata_layers),
            policy.trust_store_id, policy.trust_revision, policy.governance_epoch, policy.scope,
            metadata.logical['created_at'], metadata.logical['expires_at'],
            **artifacts._discovery_bounds(policy)))

    def local_receipts(self, path):
        return tuple(identity for item in self.receipt_groups if item.path == Path(path)
                     for identity in item.selected_receipts)

    def foreign_receipts(self, path):
        return tuple(item for item in self.receipt_groups if item.path != Path(path))


def verify_base(metadata, file, expected):
    from .container_reconstruction import ReconstructionError
    artifact, delta = expected
    if ((metadata.logical['artifact_id'], metadata.logical['version_id'],
         metadata.logical_commitment_sha256, file['file_id'],
         FileEvidence(file['length'], file['sha256'])) !=
            (artifact, delta.base_version, delta.base_commitment, delta.base_file_id, delta.base)):
        raise ReconstructionError('base_mismatch')


def reconstruct_base(metadata, delta, workspace, *, context, ancestors, policy,
                     budget, arguments):
    from .container_reconstruction import ReconstructionError, extract_container_file
    key = (metadata.logical['artifact_id'], metadata.logical['version_id'],
           delta.change_id)
    if key in ancestors:
        raise ReconstructionError('delta_cycle')
    if len(ancestors) >= context.policy.max_chain_depth:
        raise ReconstructionError('delta_depth')
    destination = Path(workspace) / 'base.plain'
    with context.policy.resolve_base(metadata, delta, budget) as sources:
        if isinstance(sources, DeltaBaseSelection):
            reader = sources.reader
            # Admitting two scopes for indexing does not permit combining their
            # plaintext. Only the signer/trust context may differ in a chain.
            if (reader.policy.scope, reader.policy.audience, reader.policy.purpose) != (
                    policy.scope, policy.audience, policy.purpose):
                raise ReconstructionError('base_mismatch')
            reader.check()
            policy = reader.policy
            arguments = dict(trust_state_path=reader.trust_state_path, gnupg_home=reader.gnupg_home,
                passphrase_reference=reader.passphrase_reference, secret_resolver=reader.secret_resolver)
            sources = sources.sources
        extract_container_file(sources, delta.base_file_id, destination.name,
            authorized=True, policy=replace(policy, boundary=workspace),
            limits=budget.limits, _budget=budget, _delta_context=context,
            _ancestors=(*ancestors, key), _expected_base=(metadata.logical['artifact_id'], delta),
            **arguments)
    return destination
