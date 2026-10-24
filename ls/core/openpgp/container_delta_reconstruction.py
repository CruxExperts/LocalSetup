"""Trusted base selection and shared validation for bounded delta chains."""
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import artifacts
from .container_delta_codec import DeltaCodec
from .container_delta_records import FileEvidence


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

    def check(self, admission, budget):
        # The enclosing release transaction checks the same store revision and
        # signing authority. Check every dependency's content lifetime as well.
        for item in self.metadata:
            budget.checkpoint()
            artifacts._policy_logical(item.logical, admission, historical=False,
                                      limits=budget.limits.container_limits)


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
        extract_container_file(sources, delta.base_file_id, destination.name,
            authorized=True, policy=replace(policy, boundary=workspace),
            limits=budget.limits, _budget=budget, _delta_context=context,
            _ancestors=(*ancestors, key), _expected_base=(metadata.logical['artifact_id'], delta),
            **arguments)
    return destination
