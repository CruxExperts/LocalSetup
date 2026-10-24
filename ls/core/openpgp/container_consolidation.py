"""Prepare immutable complete snapshots through existing read and seal owners.

This operation does not grant upload authority, choose branch winners or remove
history. Callers explicitly supply both reading and publishing authority.
"""
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile

from . import artifacts
from .artifact_records import parse_provenance
from .container_delta_reader import delta_read_context
from .container_delta_reconstruction import _DeltaContext
from .container_preparation import ContainerInput, PreparedContainerSet, _Budget
from .container_reader import ContainerReader, ContainerReadError
from .container_reconstruction import extract_container_file
from .container_snapshot_records import CoveredFile, SnapshotClaim, MAX_COVERED_FILES
from .container_sources import LocalFileRangeSource


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    prepared: PreparedContainerSet
    claim: SnapshotClaim


def prepare_snapshot(path: Path, *, reader: ContainerReader, authorized: bool,
                     output_dir, namespace, publication_policy,
                     trust_state_path, gnupg_home, passphrase_reference, secret_resolver,
                     expected_commitment=None, publication_check=None, _budget=None):
    """Reconstruct all files of one exact version, then sign a new full version.

    The reader's configured inventory is frozen once. New arrivals do not expand
    the covered set. The optional publication check binds a caller's separate
    job or worker authority and runs at preparation's existing publish gates.
    """
    if authorized is not True or not isinstance(reader, ContainerReader):
        raise artifacts.ArtifactError(artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED)
    if publication_check is not None and not callable(publication_check):
        raise ValueError('Invalid snapshot publication check')
    budget = _budget if _budget is not None else _Budget(reader.limits)
    if not isinstance(budget, _Budget) or budget.limits != reader.limits:
        raise ValueError('Invalid snapshot budget')
    initial = budget.temporary
    try:
        budget.checkpoint()
        binding = reader.check()
        if publication_check is not None:
            publication_check()
        boundary = artifacts._require_boundary(reader.policy.boundary)
        with ExitStack() as stack:
            workspace = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.lssc-snapshot-', dir=boundary)))
            admission = replace(reader.policy, boundary=workspace)
            source = stack.enter_context(LocalFileRangeSource(path))
            metadata = reader._inspect(source, admission, budget, workspace)
            if expected_commitment is not None and metadata.logical_commitment_sha256 != expected_commitment:
                raise ContainerReadError('changed')
            frozen = replace(reader, source_paths=reader._paths(budget), source_resolver=None,
                             metadata_resolver=None, reference_resolver=None)
            sources = {metadata.key: source}
            frozen._sources(sources, stack, metadata, admission, budget, workspace, path, None)
            dependencies = [(item, item.generation) for item in sources.values()]
            delta_policy = (delta_read_context(frozen, stack, admission, budget, workspace, dependencies).policy
                            if frozen.delta_codec is not None else None)
            covered, verified_metadata, inputs = {}, {}, []

            def verify():
                budget.checkpoint()
                if frozen.check(metadata) != binding:
                    raise ContainerReadError('changed')
                for item in verified_metadata.values():
                    budget.checkpoint()
                    artifacts._policy_logical(item.logical, admission, historical=False,
                                              limits=budget.limits.container_limits)
                if any(item.generation != generation for item, generation in dependencies):
                    raise ContainerReadError('changed')
                if publication_check is not None:
                    publication_check()

            for file in sorted(metadata.logical['files'], key=lambda item: item['file_id']):
                verify()
                context = _DeltaContext(delta_policy)
                name = file['file_id']+'.plain'
                extract_container_file(sources, file['file_id'], name, authorized=True,
                    policy=admission, trust_state_path=frozen.trust_state_path, gnupg_home=frozen.gnupg_home,
                    passphrase_reference=frozen.passphrase_reference, secret_resolver=frozen.secret_resolver,
                    limits=budget.limits, _budget=budget, _delta_context=context)
                for item, selected in context.selected:
                    claim = CoveredFile(item.logical['version_id'], item.logical_commitment_sha256,
                        selected['file_id'], selected['length'], selected['sha256'],
                        selected.get('delta', {}).get('change_id'))
                    previous = covered.setdefault(claim.key, claim)
                    if previous != claim:
                        raise ContainerReadError('conflict')
                    if len(covered) > MAX_COVERED_FILES:
                        raise ContainerReadError('limit_exceeded')
                    verified_metadata[item.logical_commitment_sha256] = item
                inputs.append(ContainerInput(workspace/name, file['file_id'], parse_provenance(file['provenance'])))
            claim = SnapshotClaim(metadata.logical['version_id'], metadata.logical_commitment_sha256,
                                  tuple(covered[key] for key in sorted(covered)))
            verify()
            prepared = artifacts.prepare_container(tuple(inputs), output_dir=output_dir, namespace=namespace,
                policy=publication_policy, trust_state_path=trust_state_path, gnupg_home=gnupg_home,
                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                limits=budget.limits, artifact_id=metadata.logical['artifact_id'],
                parent_versions=(claim.source_version,), snapshot=claim, _budget=budget,
                publication_check=verify)
            return PreparedSnapshot(prepared, claim)
    finally:
        budget.release_temp(budget.temporary - initial)
