"""Compare authenticated final ciphertext and its exact reconstruction closure.

Selection neither signs nor uploads. Destination observations are current byte
evidence, not durable availability or authorization; publication rechecks both.
"""
from contextlib import ExitStack
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import tempfile
import time

from . import artifacts
from .container_delta_reader import delta_read_context
from .container_delta_reconstruction import _DeltaContext
from .container_preparation import PreparedContainerSet, PreparedVolume, _Budget
from .container_reader import ContainerReader, ContainerReadError
from .container_reconstruction import extract_container_file, ReconstructionError
from .container_sources import LocalFileRangeSource
from .container_transfer import TransferError, _hash_private
from .envelope_io import ExclusiveOutput


@dataclass(frozen=True, slots=True, repr=False)
class DeliverySelection:
    prepared: PreparedContainerSet
    dependencies: tuple[PreparedContainerSet, ...]
    representation: str
    reason: str
    full_bytes: int
    delta_bytes: int | None


def _equivalent(logical):
    return {**{key: value for key, value in logical.items() if key not in ('version_id', 'files')},
            'files': sorted(({key: value for key, value in file.items() if key != 'delta'}
                             for file in logical['files']), key=lambda file: file['file_id'])}


def _measured(source, metadata, check):
    size, digest = source.size, hashlib.sha256()
    for offset in range(0, size, 1024*1024):
        check()
        digest.update(source.read_range(offset, min(1024*1024, size-offset)))
    check()
    return PreparedVolume(metadata.key, source.path, size, digest.hexdigest(),
                          metadata.volume_id, len(metadata.volume_record['messages']))


def _inspect_set(prepared, reader, stack, workspace, budget, check, retained):
    if (not isinstance(prepared, PreparedContainerSet) or type(prepared.volumes) is not tuple
            or not 1 <= len(prepared.volumes) <= min(reader.max_sources, budget.limits.container_limits.max_volumes)):
        raise ContainerReadError('incomplete')
    sources, origins = {}, set()
    first = None
    for volume in prepared.volumes:
        check()
        if not isinstance(volume, PreparedVolume):
            raise ContainerReadError('changed')
        source = stack.enter_context(LocalFileRangeSource(volume.path))
        metadata = reader._inspect(source, reader.policy, budget, workspace)
        retained.append((source, source.generation, metadata))
        if (_measured(source, metadata, check) != volume
                or (metadata.logical['artifact_id'], metadata.logical['version_id'], metadata.logical_commitment_sha256)
                   != (prepared.artifact_id, prepared.version_id, prepared.logical_commitment_sha256)
                or first is not None and (metadata.logical != first.logical or metadata.inventory != first.inventory)
                or metadata.origin_volume_id in origins):
            raise ContainerReadError('changed')
        first = metadata
        origins.add(metadata.origin_volume_id)
        sources[metadata.key] = source
    if origins != {row['volume_id'] for row in first.inventory['volumes']}:
        raise ContainerReadError('incomplete')
    return first, sources


def _verify_files(metadata, sources, reader, stack, workspace, budget, check, retained):
    """Reconstruct every file through the existing receipt-bound read owner."""
    dependencies, selected = [], {}
    resolver = (delta_read_context(reader, stack, reader.policy, budget, workspace, dependencies)
                if reader.delta_codec is not None else _DeltaContext(None))
    for file in metadata.logical['files']:
        check()
        # Each release requires only that file's chain receipts. Share resolver
        # discovery and the budget, not receipts accumulated for other files.
        context = _DeltaContext(resolver.policy)
        initial = budget.temporary
        with tempfile.TemporaryDirectory(prefix='.file-', dir=workspace) as directory:
            extract_container_file(sources, file['file_id'], 'content', authorized=True,
                policy=replace(reader.policy, boundary=Path(directory)),
                trust_state_path=reader.trust_state_path, gnupg_home=reader.gnupg_home,
                passphrase_reference=reader.passphrase_reference, secret_resolver=reader.secret_resolver,
                limits=budget.limits, _budget=budget, _delta_context=context)
        budget.release_temp(budget.temporary-initial)
        for item, used_file in context.selected:
            selected.setdefault(item.logical_commitment_sha256, set()).add(used_file['file_id'])
    groups = {}
    for source, generation in dependencies:
        check()
        item = reader._inspect(source, reader.policy, budget, workspace)
        retained.append((source, generation, item))
        used = selected.get(item.logical_commitment_sha256, set())
        if not any(message['file_id'] in used for message in item.volume_record['messages']):
            continue
        volume = _measured(source, item, check)
        group = groups.setdefault(item.logical_commitment_sha256, (item, {}))
        previous = group[1].setdefault(volume.volume_id, volume)
        if previous != volume:
            raise ContainerReadError('conflict')
    return tuple(PreparedContainerSet(item.logical['artifact_id'], item.logical['version_id'], commitment,
                 tuple(volumes[key] for key in sorted(volumes)))
                 for commitment, (item, volumes) in sorted(groups.items()))


def _delivery_cost(volumes, destination_for, mode, budget, check):
    """Charge absent exact bytes; collisions and unavailable observations fail."""
    cost = 0
    for volume in volumes:
        check()
        destination = destination_for(volume)
        key = volume.key + '.sha256-' + volume.transport_sha256 if mode == 'content_addressed' else volume.key
        if isinstance(destination, Path):
            if mode != 'conditional' or not destination.is_absolute() or '..' in destination.parts:
                raise TransferError('publication_invalid')
            # No symlink or changed-file fallback: only an absent leaf costs bytes.
            try:
                destination.lstat()
            except FileNotFoundError:
                observed = None
            else:
                observed = _hash_private(destination, volume.serialized_length, check)
        else:
            if not callable(getattr(destination, 'observe', None)):
                raise TransferError('publication_invalid')
            def cancelled():
                try:
                    check()
                    return False
                except Exception:
                    return True
            try:
                observed = destination.observe(key, timeout_seconds=budget.deadline-time.monotonic(),
                                               cancel_check=cancelled)
            except Exception:
                raise TransferError('publish_uncertain') from None
        check()
        if observed is None:
            cost += volume.serialized_length
        elif observed != (volume.serialized_length, volume.transport_sha256):
            raise TransferError('ciphertext_collision')
    return cost


def _stage_dependencies(groups, directory, budget, check):
    """Retain exact ciphertext from shared carriers in private restartable files."""
    boundary = artifacts._require_boundary(directory)
    staged, result = [], []
    initial = budget.temporary
    try:
        with ExitStack() as stack:
            for group in groups:
                volumes = []
                for volume in group.volumes:
                    check()
                    target = boundary / (volume.transport_sha256+'.lssc')
                    try:
                        target.lstat()
                    except FileNotFoundError:
                        budget.reserve_temp(volume.serialized_length)
                        output = stack.enter_context(ExclusiveOutput(target))
                        name, fd = output.stage()
                        with os.fdopen(fd, 'wb') as stream, LocalFileRangeSource(volume.path) as source:
                            digest = hashlib.sha256()
                            if source.size != volume.serialized_length:
                                raise ContainerReadError('changed')
                            for offset in range(0, source.size, 1024*1024):
                                check()
                                data = source.read_range(offset, min(1024*1024, source.size-offset))
                                stream.write(data)
                                digest.update(data)
                            if digest.hexdigest() != volume.transport_sha256:
                                raise ContainerReadError('changed')
                            stream.flush()
                            os.fsync(stream.fileno())
                        budget.retain(volume.serialized_length)
                        staged.append((output, name))
                    else:
                        if _hash_private(target, volume.serialized_length, check) != (
                                volume.serialized_length, volume.transport_sha256):
                            raise ContainerReadError('changed')
                    volumes.append(replace(volume, path=target))
                result.append(replace(group, volumes=tuple(volumes)))
            for output, name in staged:
                check()
                output.publish(name)
            check()
            for output, _ in staged:
                output.published_identity = None
            return tuple(result)
    finally:
        budget.release_temp(budget.temporary-initial)


def select_prepared_delivery(full, delta, *, reader, destination_for, authorization_check, dependency_output_dir,
                             publication_mode='conditional', _budget=None):
    """Choose the smaller complete approved delivery, with full winning ties.

    Both alternatives must describe identical content, provenance, ancestry and
    policy apart from their version and per-file delta encoding. The reader's
    frozen authorized inventory supplies dependencies. Missing bases select the
    verified full alternative; invalid content, changed trust or failed carrier
    observation never silently falls back. Keep all returned dependencies in the
    publication job, including those observed present: observations can expire.
    Selected dependencies are copied unchanged to the explicitly supplied private
    directory, so shared carrier permissions cannot prevent resumable delivery.
    """
    if (not isinstance(reader, ContainerReader) or reader.policy.controller_fingerprint is None
            or not callable(destination_for)
            or not callable(authorization_check) or publication_mode not in ('conditional', 'content_addressed')):
        raise ValueError('Invalid delivery selection')
    budget = _budget if _budget is not None else _Budget(reader.limits)
    if not isinstance(budget, _Budget) or budget.limits != reader.limits:
        raise ValueError('Invalid delivery budget')
    retained, binding, initial = [], reader.check(), budget.temporary

    def check():
        budget.checkpoint()
        authorization_check()
        if reader.check() != binding:
            raise ContainerReadError('changed')
        for source, generation, metadata in retained:
            if source.generation != generation:
                raise ContainerReadError('changed')
            reader.check(metadata)

    try:
        check()
        reader = replace(reader, source_paths=reader._paths(budget), source_resolver=None,
                         metadata_resolver=None, reference_resolver=None)
        if any(not isinstance(item, PreparedContainerSet) or type(item.volumes) is not tuple
               for item in (full, delta)):
            raise ValueError('Invalid delivery candidates')
        if len(full.volumes) + len(delta.volumes) + len(reader.source_paths) > reader.max_sources:
            raise ContainerReadError('limit_exceeded')
        with ExitStack() as stack:
            workspace = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix='.lssc-select-', dir=artifacts._require_boundary(reader.policy.boundary))))
            full_meta, full_sources = _inspect_set(full, reader, stack, workspace, budget, check, retained)
            delta_meta, delta_sources = _inspect_set(delta, reader, stack, workspace, budget, check, retained)
            if (any('delta' in file for file in full_meta.logical['files'])
                    or not any('delta' in file for file in delta_meta.logical['files'])
                    or 'snapshot' in full_meta.logical or 'snapshot' in delta_meta.logical
                    or _equivalent(full_meta.logical) != _equivalent(delta_meta.logical)):
                raise ContainerReadError('conflict')
            _verify_files(full_meta, full_sources, reader, stack, workspace, budget, check, retained)
            full_cost = _delivery_cost(full.volumes, destination_for, publication_mode, budget, check)
            try:
                dependencies = _verify_files(delta_meta, delta_sources, reader, stack, workspace,
                                             budget, check, retained)
            except ReconstructionError as error:
                check()
                if error.code != 'incomplete':
                    raise
                return DeliverySelection(full, (), 'full', 'dependency_missing', full_cost, None)
            volumes = tuple(volume for group in dependencies for volume in group.volumes) + delta.volumes
            if len(volumes) > reader.max_sources or len({volume.key for volume in volumes}) != len(volumes):
                raise ContainerReadError('limit_exceeded')
            delta_cost = _delivery_cost(volumes, destination_for, publication_mode, budget, check)
            check()
            if delta_cost < full_cost:
                dependencies = _stage_dependencies(dependencies, dependency_output_dir, budget, check)
                return DeliverySelection(delta, dependencies, 'delta', 'smaller_delivery', full_cost, delta_cost)
            return DeliverySelection(full, (), 'full', 'full_not_larger', full_cost, delta_cost)
    finally:
        budget.release_temp(budget.temporary-initial)
