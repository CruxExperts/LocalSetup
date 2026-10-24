"""Configured discovery, encrypted reconciliation and ordinary reference reads."""
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import tempfile
import time

from ..agent.runtime_lock import runtime_use
from . import artifacts
from .artifact_records import canonical_bytes
from .container_catalog import CatalogStore, CatalogError
from .container_index import parse_reference, stage_index
from .container_preparation import _Budget
from .container_reader import ContainerReadError
from .container_reconcile import (
    CatalogObservation, ContainerCandidate, RejectedObservation, ScanCoverage,
    apply_reconcile_transition, build_transition, inspect_candidate, metadata_cache_for,
    ReconcileError,
    _observation_record,
)
from .container_scan import scan_directories, ScanLimitError
from .container_sources import LocalFileRangeSource
from .container_storage import ensure_private_directory


@dataclass(frozen=True, slots=True)
class ScanResult:
    snapshot: object
    observations: tuple
    paths: dict


def _stable(catalog):
    """Repeated unchanged observations must not grow the encrypted journal."""
    import json
    value = json.loads(canonical_bytes(catalog))
    history = value.pop('reconciliation_history', [])
    for row in value['containers']:
        for observation in row['observations']:
            observation.pop('observed_at', None)
            observation.pop('cache_reused', None)
    for row in value['sources']:
        row.pop('evaluation_time', None)
    if history:
        decision = history[-1]
        value['decision'] = {key: decision[key] for key in (
            'policy_sha256', 'source_config_sha256', 'trust_store_id', 'trust_revision',
            'governance_epoch', 'coverage')}
        value['decision']['rejected'] = [{key: item for key, item in row.items() if key != 'observed_at'}
                                         for row in decision['rejected']]
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class CatalogRuntime:
    def __init__(self, root, config, context, reader, *, configuration=None):
        self.root, self.config, self.context, self.reader = root, config, context, reader
        self.configuration = configuration
        self.store = CatalogStore(root/'catalog', config.runtime_id)
        self.writer_factory = None

    def after_read(self, reader, path, metadata, budget, check):
        check()
        if self.writer_factory is None:
            return
        from .container_consolidation_config import read_binding
        authority = read_binding(self.config.consolidation)
        if (authority['mode'] != 'client' or 'snapshot' in metadata.logical
                or metadata.logical['artifact_id'] not in authority['artifacts']):
            return
        self.writer_factory().run(path, metadata, reader=reader, local_context=self.context,
                                  budget=budget, trigger='read')
        check()

    def consolidate(self, reference, *, trigger='manual', check=lambda: None, deadline):
        """Explicit events name an exact version; never choose a concurrent head."""
        artifact, version, file_id = parse_reference(reference)
        if version is None or file_id is not None or self.writer_factory is None:
            raise ContainerReadError('unauthorized')
        remaining = min(self.reader.limits.timeout_seconds, deadline-time.monotonic())
        if remaining <= 0:
            raise ContainerReadError('expired')
        def cancelled():
            check()
            return self.reader.limits.cancel_check is not None and self.reader.limits.cancel_check()
        budget = _Budget(replace(self.reader.limits, timeout_seconds=remaining, cancel_check=cancelled))
        result = self.reconcile(budget)
        self._collision(result, artifact, version)
        candidates = sorted((item for item in result.observations if (
            item.metadata.logical['artifact_id'], item.metadata.logical['version_id']) == (artifact, version)),
            key=lambda item: (item.identity, item.source_id, item.locator))
        if not candidates:
            raise ContainerReadError('unavailable')
        first = candidates[0]
        paths = tuple(result.paths[(item.source_id, item.locator)] for item in result.observations
                      if item.metadata.logical['artifact_id'] == artifact)
        return self.writer_factory().run(result.paths[(first.source_id, first.locator)], first.metadata,
            reader=self._bound(result, paths), local_context=self.context, budget=budget, trigger=trigger)

    def _refresh_configuration(self):
        if self.configuration is not None:
            self.config, self.reader = self.configuration.tune_reader(self.reader)

    def _pending(self):
        if self.store._anchor(self.context).pending_head is not None:
            raise ContainerReadError('pending')

    def reconcile(self, budget):
        """One serialized scan; uncertain catalog writes are never replayed."""
        try:
            self._refresh_configuration()
            budget.checkpoint()
            self.reader.check()
            lock = self.root/'reconcile'
            ensure_private_directory(lock, create=True)
            with runtime_use(lock, exclusive=True, timeout=0):
                self._pending()
                snapshot = self.store.load(self.context)
                self._pending()
                configuration_receipt = None if self.configuration is None else self.configuration.applied
                budget.checkpoint()
                inventory = scan_directories(tuple(zip(self.config.source_ids, self.config.source_directories)),
                    maximum=self.config.max_scan_objects, check=budget.checkpoint,
                    max_candidates=self.reader.max_sources)
                if len(inventory.candidates) > self.reader.max_sources:
                    raise ContainerReadError('limit_exceeded')
                prior = {}
                for row in snapshot.catalog['containers']:
                    for observation in row['observations']:
                        prior.setdefault((observation['source_id'], observation['locator']), set()).add(row['identity'])
                observations, paths, observed_bytes = [], {}, 0
                now = artifacts._clock()
                with tempfile.TemporaryDirectory(prefix='.catalog-scan-', dir=self.reader.policy.boundary) as temporary:
                    policy = replace(self.reader.policy, boundary=temporary)
                    for candidate in inventory.candidates:
                        budget.checkpoint()
                        key = candidate.source_id, candidate.locator
                        previous = prior.get(key, set())
                        cached = metadata_cache_for(snapshot, next(iter(previous))) if len(previous) == 1 else None
                        try:
                            with LocalFileRangeSource(candidate.path) as source:
                                if source.generation != candidate.generation:
                                    raise OSError('source changed')
                                observation = inspect_candidate(ContainerCandidate(*key, source),
                                    policy=policy, trust_state_path=str(self.reader.trust_state_path),
                                    gnupg_home=str(self.reader.gnupg_home),
                                    passphrase_reference=self.reader.passphrase_reference,
                                    secret_resolver=self.reader.secret_resolver, evaluation_time=now,
                                    limits=budget.limits.container_limits, cached=cached,
                                    _budget=budget, _workspace=Path(temporary))
                                if source.generation != candidate.generation:
                                    raise OSError('source changed')
                            paths[key] = candidate.path
                        except OSError:
                            budget.checkpoint()
                            observation = RejectedObservation(*key, 'UNAVAILABLE', now)
                        record = (_observation_record(observation) if isinstance(observation, CatalogObservation)
                                  else {'source_id': observation.source_id, 'locator': observation.locator,
                                        'error_code': observation.error_code, 'observed_at': observation.observed_at})
                        observed_bytes += len(canonical_bytes(record))
                        if observed_bytes > self.store.limits.transaction_bytes:
                            raise ContainerReadError('limit_exceeded')
                        observations.append(observation)
                budget.checkpoint()
                self.reader.check()
                source_configuration = [
                    [identity, str(path)] for identity, path in zip(self.config.source_ids, self.config.source_directories)
                ]
                if configuration_receipt is not None:
                    source_configuration = {'sources': source_configuration, 'configuration': configuration_receipt}
                source_digest = hashlib.sha256(canonical_bytes(source_configuration)).hexdigest()
                scan_id = hashlib.sha256(canonical_bytes([snapshot.head, source_digest, now,
                    [[item.source_id, item.locator, item.generation] for item in inventory.candidates]])).hexdigest()[:32]
                transition = build_transition(scan_id=scan_id, evaluation_time=now,
                    source_config_sha256=source_digest, policy=self.reader.policy,
                    coverage=tuple(ScanCoverage(*item) for item in inventory.coverage), observations=observations)
                proposed = apply_reconcile_transition(snapshot.catalog, transition, max_entities=self.store.limits.max_entities)
                if _stable(proposed) != _stable(snapshot.catalog):
                    # Checkpoint only a proven committed head, before reaching the
                    # existing chain bound. Any uncertain mutation stops this call.
                    if snapshot.chain_depth >= self.store.limits.max_journal_entries-1:
                        snapshot = self.store.checkpoint(snapshot.head, self.context)
                        budget.checkpoint()
                    snapshot = self.store.commit(snapshot.head, transition, self.context)
                budget.checkpoint()
                self.reader.check()
                return ScanResult(snapshot, tuple(item for item in observations if isinstance(item, CatalogObservation)), paths)
        except ContainerReadError:
            raise
        except TimeoutError:
            raise ContainerReadError('pending') from None
        except ScanLimitError:
            raise ContainerReadError('limit_exceeded') from None
        except CatalogError as error:
            raise ContainerReadError({'BUSY': 'pending', 'BOUNDED': 'limit_exceeded',
                'AUTHORIZATION_DENIED': 'unauthorized', 'CONFLICT': 'changed'}.get(error.code, 'unavailable')) from None
        except ReconcileError as error:
            raise ContainerReadError('limit_exceeded' if error.code == 'BOUNDED' else 'unavailable') from None
        except artifacts.ArtifactError:
            raise ContainerReadError('unauthorized') from None
        except Exception:
            raise ContainerReadError('unavailable') from None

    @staticmethod
    def _collision(result, artifact, version, commitment=None):
        rows = [row for row in result.snapshot.catalog['containers']
                if (row['artifact_id'], row['version_id']) == (artifact, version)]
        claims = {row['commitment_sha256'] for row in rows}
        if commitment is not None:
            claims.add(commitment)
        if len(claims) > 1 or any(row['claim_conflict'] for row in rows):
            raise ContainerReadError('conflict')
        from .container_versions import VersionNode, build_version_graph
        graph = build_version_graph(VersionNode(row['artifact_id'], row['version_id'],
            tuple(row['parents']), row['commitment_sha256'])
            for row in result.snapshot.catalog['containers'] if row['artifact_id'] == artifact)
        if any((artifact, version) in cycle for cycle in graph.cycles):
            raise ContainerReadError('conflict')

    def _bound(self, result, paths, metadata_checks=()):
        base = self.reader.check()
        head = result.snapshot.head
        def binding():
            if self.reader.check() != base:
                raise ContainerReadError('changed')
            self._pending()
            if self.store._read_head() != head:
                raise ContainerReadError('changed')
            for metadata in metadata_checks:
                artifacts._policy_logical(metadata.logical, self.reader.policy, historical=False,
                                          limits=self.reader.limits.container_limits)
            return hashlib.sha256(canonical_bytes([base, head])).hexdigest()
        def dependency_check(metadata, delta):
            self._collision(result, metadata.logical['artifact_id'], delta.base_version, delta.base_commitment)
        return replace(self.reader, source_paths=tuple(sorted(set(paths))), source_resolver=None,
                       metadata_resolver=None, reference_resolver=None, binding_check=binding,
                       dependency_check=dependency_check)

    def bind_metadata(self, metadata, budget):
        result = self.reconcile(budget)
        logical = metadata.logical
        self._collision(result, logical['artifact_id'], logical['version_id'], metadata.logical_commitment_sha256)
        paths = [result.paths[(item.source_id, item.locator)] for item in result.observations
                 if item.metadata.logical['artifact_id'] == logical['artifact_id']]
        bound = self._bound(result, paths)
        candidates = sorted((item for item in result.observations
            if item.metadata.logical_commitment_sha256 == metadata.logical_commitment_sha256),
            key=lambda item: (item.identity, item.source_id, item.locator))
        if bound.after_read is not None and candidates:
            first = candidates[0]
            canonical = result.paths[(first.source_id, first.locator)]
            def after_read(reader, _path, selected, budget, check):
                self.after_read(reader, canonical, selected, budget, check)
            bound = replace(bound, after_read=after_read)
        return bound

    def read_reference(self, name, *, check, deadline):
        try:
            return self._read_reference(name, check=check, deadline=deadline)
        except ContainerReadError:
            raise
        except Exception:
            raise ContainerReadError('unavailable') from None

    def _read_reference(self, name, *, check, deadline):
        check()
        self._refresh_configuration()
        artifact, version, selected = parse_reference(name)
        remaining = min(self.reader.limits.timeout_seconds, deadline-time.monotonic())
        if remaining <= 0:
            raise ContainerReadError('expired')
        def cancelled():
            check()
            return self.reader.limits.cancel_check is not None and self.reader.limits.cancel_check()
        budget = _Budget(replace(self.reader.limits, timeout_seconds=remaining, cancel_check=cancelled))
        result = self.reconcile(budget)
        if version is None:
            from .container_catalog_view import index_rows
            bound = self._bound(result, (), tuple(item.metadata for item in result.observations
                if artifact is None or item.metadata.logical['artifact_id'] == artifact))
            return stage_index(bound, index_rows(result, artifact), budget=budget, check=check, deadline=deadline)
        self._collision(result, artifact, version)
        candidates = [item for item in result.observations if (
            item.metadata.logical['artifact_id'], item.metadata.logical['version_id']) == (artifact, version)]
        if not candidates:
            raise ContainerReadError('unavailable')
        candidates.sort(key=lambda item: (item.identity, item.source_id, item.locator))
        paths = [result.paths[(item.source_id, item.locator)] for item in result.observations
                 if item.metadata.logical['artifact_id'] == artifact]
        bound = self._bound(result, paths)
        first = candidates[0]
        path = result.paths[(first.source_id, first.locator)]
        reference = name.rsplit('/@files/', 1)[0] if selected is not None else name
        with LocalFileRangeSource(path) as source:
            before = os.fstat(source._fd)
            return bound.prepare(path, reference, file_id=selected, expected_stat=before,
                check=check, deadline=deadline, expected_commitment=first.metadata.logical_commitment_sha256,
                _budget=budget)
