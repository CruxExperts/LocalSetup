"""Resolve exact delta dependencies from a frozen operator-selected inventory."""
from contextlib import contextmanager
from dataclasses import replace

from . import artifacts
from .container_delta_reconstruction import DeltaBaseSelection, DeltaReconstructionPolicy, _DeltaContext
from .container_sources import LocalFileRangeSource
from .container_reconstruction import ReconstructionError


def delta_read_context(reader, stack, policy, budget, workspace, dependencies):
    """Keep selected base descriptors alive through the ordinary paging lease."""
    from .container_reader import ContainerReadError
    cache, held = {}, {}

    @contextmanager
    def resolve(metadata, delta, shared):
        if shared is not budget:
            raise ReconstructionError('invalid_input')
        if reader.dependency_check is not None:
            try:
                reader.dependency_check(metadata, delta)
            except ContainerReadError as error:
                raise ReconstructionError('conflicting_volumes' if error.code == 'conflict'
                                          else 'source_changed') from None
        sources, volumes, inventory, selected_reader = {}, set(), None, None
        for path in sorted(reader.source_paths):
            budget.checkpoint()
            source = held.get(path)
            retained = source is not None
            try:
                if source is None:
                    source = LocalFileRangeSource(path)
                generation = source.generation
                previous = cache.get(path)
                if previous is None:
                    admitted = reader if reader.dependency_reader is None else reader.dependency_reader(path)
                    admitted.check()
                    candidate = admitted._inspect(source, replace(admitted.policy, boundary=workspace), budget, workspace)
                    cache[path] = candidate, generation, admitted
                else:
                    candidate, expected, admitted = previous
                    admitted.check(candidate)
                    if generation != expected:
                        raise ReconstructionError('source_changed')
                if (candidate.logical['artifact_id'], candidate.logical['version_id']) != (
                        metadata.logical['artifact_id'], delta.base_version):
                    continue
                if candidate.logical_commitment_sha256 != delta.base_commitment:
                    raise ReconstructionError('conflicting_volumes')
                if selected_reader is not None and (
                        artifacts.container_admission_policy_sha256(selected_reader.policy) !=
                        artifacts.container_admission_policy_sha256(admitted.policy)):
                    raise ReconstructionError('conflicting_volumes')
                selected_reader = admitted
                if inventory is not None and inventory != candidate.inventory:
                    raise ReconstructionError('conflicting_volumes')
                inventory = candidate.inventory
                if candidate.origin_volume_id in volumes:
                    continue
                volumes.add(candidate.origin_volume_id)
                if not retained:
                    if len(held) >= reader.max_sources:
                        raise ReconstructionError('resource_limit')
                    stack.callback(source.close)
                    held[path] = source
                    dependencies.append((source, generation))
                    retained = True
                sources[candidate.key] = source
            except (OSError, artifacts.ArtifactError):
                budget.checkpoint()
                # An unreadable carrier is not permission to discover another
                # location. Absence of an authenticated base remains incomplete.
                continue
            finally:
                if source is not None and not retained:
                    source.close()
        if not sources:
            raise ReconstructionError('incomplete')
        yield DeltaBaseSelection(sources, selected_reader) if reader.dependency_reader is not None else sources

    return _DeltaContext(DeltaReconstructionPolicy(reader.delta_codec, resolve, reader.max_delta_depth))
