"""Configured consolidation through the existing bounded snapshot job owner."""
from dataclasses import replace
import hashlib
from itertools import islice
from pathlib import Path

from ..agent.runtime_lock import runtime_use
from . import artifacts, container_runtime as runtime
from .artifact_records import canonical_bytes
from .container_catalog import CatalogContext
from .container_consolidation_config import read_binding
from .container_local_publisher import LocalDirectoryPublisher
from .container_preparation import ContainerNamespace
from .container_reader import ContainerReadError
from .container_storage import ensure_private_directory, read_private_file
from .container_transfer import _local_authority
from .secrets import SecretResolver


def _context(value, runtime_id, role, env_values, paths):
    if type(value) is not dict or set(value) != {'policy', 'trust_state_path', 'key'}:
        raise ValueError('Invalid snapshot authority')
    policy = artifacts.ArtifactPolicy(**{key: tuple(item) if key in ('scope', 'audience') else item
                                       for key, item in value['policy'].items()})
    key = runtime._parse_key(value['key'])
    signer = policy.origin_fingerprint if role == 'publisher' else policy.controller_fingerprint
    if key is None or key.fingerprint != signer:
        raise ValueError('Snapshot signer mismatch')
    trust = runtime._absolute(Path(value['trust_state_path']))
    paths.extend((trust, key.home, Path(policy.boundary)))
    if key.protected_file_root is not None:
        paths.append(key.protected_file_root)
    context = CatalogContext(runtime_id, trust, policy.trust_store_id, policy.scope, role,
        policy.trust_revision, policy.governance_epoch, signer, policy.audience, key.home,
        key.reference, SecretResolver(protected_file_root=key.protected_file_root,
                                      env_values=env_values))
    return context, policy


class ConsolidationRuntime:
    """An explicit private write grant; a reader or carrier path cannot create it."""
    def __init__(self, root, config, *, env_values=None, workspace=None, current=lambda: None):
        if config.consolidation is None:
            raise PermissionError('No consolidation write authority')
        self.root, self.binding, self.current = root, config.consolidation, current
        self.value = read_binding(self.binding)
        self.mode = self.value['mode']
        paths = [root, self.binding.path]
        self.publisher, self.publication_policy = _context(self.value['publisher'], root.name,
                                                          'publisher', env_values, paths)
        self.controller, self.approval_policy = _context(self.value['controller'], root.name,
                                                       'owner', env_values, paths)
        admissions = ((config.remote_content, config.read_policy), *(
            (item.remote_content, item.read_policy) for item in config.additional_admissions))
        self.output_admissions = tuple((remote, policy) for remote, policy in admissions
            if remote is not None and policy is not None and all(
                getattr(policy, key) == getattr(self.publication_policy, key)
                for key in ('origin_trust_store_id', 'origin_trust_revision', 'origin_governance_epoch',
                            'origin_fingerprint', 'controller_fingerprint', 'scope', 'audience', 'purpose')))
        for remote, policy in self.output_admissions:
            paths.extend((remote.path, Path(policy.boundary)))
        self.namespace = ContainerNamespace(**self.value['namespace'])
        destination = self.value['destination']
        self.destination = LocalDirectoryPublisher(runtime._absolute(Path(destination['path'])))
        paths.append(self.destination.root)
        self.destination_binding = hashlib.sha256(canonical_bytes(
            [self.binding.sha256, str(self.destination.root), self.destination.identity])).hexdigest()
        self.worker = None
        worker = self.value['worker']
        if worker is not None:
            if type(worker) is not dict or set(worker) != {'authority_file', 'authority_sha256', 'owner', 'key', 'home'}:
                raise ValueError('Invalid worker authority')
            from ..live_configuration import ConfigurationAuthority
            from ..live_configuration_cli import bound_runtime
            from ..live_configuration_files import editable_json
            from .container_worker_authority import AssignedWorker, AssignmentController
            authority_file = runtime._absolute(Path(worker['authority_file']))
            worker_home = runtime._absolute(Path(worker['home']))
            raw = read_private_file(authority_file, maximum=65536)
            if hashlib.sha256(raw).hexdigest() != worker['authority_sha256']:
                raise PermissionError('Worker authority changed')
            authority = editable_json(raw)
            paths.extend((authority_file, worker_home, Path(authority['root']), Path(authority['gnupg_home']),
                          Path(authority['trust']['path'])))
            if authority['secret']['protected_file_root'] is not None:
                paths.append(Path(authority['secret']['protected_file_root']))
            selected = runtime._parse_key(worker['key'])
            if selected is None:
                raise ValueError('Worker key is required')
            paths.append(selected.home)
            if selected.protected_file_root is not None:
                paths.append(selected.protected_file_root)
            if workspace is not None:
                from ..agent.session_owner import _separate
                for path in paths:
                    _separate(path, workspace)
            shared, _defaults, _digest, _write = bound_runtime(authority_file,
                source_root=Path(__file__).resolve().parents[3], home=worker_home, env_values=env_values)
            actor = ConfigurationAuthority(selected.fingerprint, shared.store.authority.recipients,
                selected.home, selected.reference,
                SecretResolver(protected_file_root=selected.protected_file_root, env_values=env_values),
                shared.store.authority.check)
            endpoint = AssignmentController(shared.store, worker['owner'], authorized=True)
            self.worker = AssignedWorker(shared.store, worker['owner'], actor, endpoint)
            if (shared.store.authority.signer != self.approval_policy.controller_fingerprint
                    or self.worker.assignment.artifact_id not in self.value['artifacts']
                    or not any(policy.require_worker_snapshots for _remote, policy in self.output_admissions)):
                raise PermissionError('Worker admission policy mismatch')
        if workspace is not None:
            from ..agent.session_owner import _separate
            for path in paths:
                _separate(path, workspace)
        self.check()

    def check(self):
        self.current()
        if read_binding(self.binding) != self.value:
            raise PermissionError('Snapshot authority changed')
        _local_authority(self.publisher)
        artifacts._current_authorities(self.controller.trust_state_path, self.approval_policy,
                                       include_controller=True)
        for policy in (self.publication_policy, self.approval_policy):
            artifacts._require_fresh_policy(policy)
        self.current()

    def run(self, path, metadata, *, reader, local_context, budget, trigger):
        self.check()
        if trigger == 'read':
            if self.mode != 'client':
                return {'state': 'read_only'}
        elif trigger not in self.value['events']:
            raise PermissionError('Snapshot event is not authorized')
        if metadata.logical['artifact_id'] not in self.value['artifacts']:
            if trigger == 'read':
                return {'state': 'read_only'}
            raise PermissionError('Snapshot artifact is not authorized')
        if 'snapshot' in metadata.logical:
            return {'state': 'already_snapshot'}
        # Check the prospective publication policy against the independent
        # receiving policy before creating a job or signing anything.
        output_reader = self._output_reader(reader, metadata, budget)
        assignment = hashlib.sha256(canonical_bytes([self.binding.sha256,
            None if self.worker is None else self.worker.binding])).hexdigest()
        job_id = hashlib.sha256(canonical_bytes([self.root.name, assignment,
            metadata.logical_commitment_sha256])).hexdigest()[:32]
        jobs = self.root/'consolidations'
        ensure_private_directory(jobs, create=True)
        with runtime_use(jobs, exclusive=True, timeout=0):
            existing = tuple(islice(jobs.iterdir(), self.value['max_jobs']+2))
            count = sum(item.name != '.runtime-use.lock' for item in existing)
            if not (jobs/job_id).exists() and count >= self.value['max_jobs']:
                raise ContainerReadError('limit_exceeded')
            ensure_private_directory(jobs/job_id, create=True)
        from .container_consolidation_job import run_snapshot_job
        reader.check()
        # A catalog may gain the just-published snapshot between reads. Keep
        # the durable job bound to stable runtime authority rather than the
        # transient catalog head. The shared budget still checks the read grant;
        # final plaintext release separately rechecks the read observation.
        bound = replace(reader, limits=budget.limits, after_read=None,
                        metadata_resolver=None, reference_resolver=None, binding_check=self.current)
        approved, delivered = run_snapshot_job(path, reader=bound, authorized=True, job_root=jobs/job_id,
            job_id=job_id, source_commitment=metadata.logical_commitment_sha256,
            assignment_binding=assignment, namespace=self.namespace, local_context=local_context,
            publisher_context=self.publisher, controller_context=self.controller,
            publication_policy=self.publication_policy, approval_policy=self.approval_policy,
            publication_check=self.check, destination_binding=self.destination_binding,
            destination=self.destination, publication_mode=self.value['destination']['mode'],
            worker_authority=self.worker, output_reader=output_reader, _budget=budget)
        return {'state': 'published', 'job_id': job_id, 'version_id': approved.prepared.version_id,
                'completed_volumes': delivered.completed_volumes}

    def _output_reader(self, reader, metadata, budget):
        prospective = metadata.logical | artifacts._logical_policy(self.publication_policy)
        matches = []
        for remote, policy in self.output_admissions:
            budget.checkpoint()
            try:
                artifacts._policy_logical(prospective, policy, historical=False,
                                          limits=budget.limits.container_limits)
            except artifacts.ArtifactError as error:
                if error.code != artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED:
                    raise
                continue
            matches.append((remote, policy))
        if len(matches) != 1:
            raise PermissionError('Snapshot output admission is missing or ambiguous')
        remote, policy = matches[0]
        if (any(getattr(policy, key) != getattr(reader.policy, key) for key in ('scope', 'audience', 'purpose'))
                or self.worker is not None and not policy.require_worker_snapshots):
            raise PermissionError('Snapshot output admission mismatch')
        selected = replace(reader, policy=policy, trust_state_path=remote.path, binding_check=self.current,
                           after_read=None, limits=budget.limits)
        selected.check()
        if (remote.path == reader.trust_state_path
                and artifacts.container_admission_policy_sha256(policy) == artifacts.container_admission_policy_sha256(reader.policy)):
            return None  # Preserve the existing single-reader lifecycle identity.
        return selected


def bind_consolidation(root, path, *, expected_revision, authorized):
    """Select exact private authority bytes; never infer a grant from a read."""
    if authorized is not True:
        raise PermissionError('Snapshot binding is not authorized')
    from .container_consolidation_config import ConsolidationBinding
    raw = read_private_file(path, maximum=128*1024)
    config = runtime.load_runtime_config(root)
    proposed = replace(config, consolidation=ConsolidationBinding(path, hashlib.sha256(raw).hexdigest()))
    ConsolidationRuntime(root, proposed)
    return runtime.configure_runtime(root, expected_revision=expected_revision, proposed=proposed)
