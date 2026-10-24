"""Fresh signed worker proof, generation changes and partition refusal."""
from dataclasses import replace

import pytest

from ls.core.live_configuration import ConfigurationAuthority, ConfigurationStore
from ls.core.openpgp.container_worker_authority import (
    AssignedWorker, AssignmentController, WorkerAssignment, WorkerAuthorityError,
)
from ls.core.openpgp.container_preparation import ContainerPreparationLimits, _Budget
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE


def configured_worker(env, root, artifact='a'*32):
    audience = tuple(sorted((env.owner, env.publisher, env.recipient)))
    controller = ConfigurationAuthority(env.owner, audience, env.homes['controller'],
        SECRET_REFERENCE, env.resolver, lambda: None)
    store = ConfigurationStore(root, '1'*32, controller)
    assignment = WorkerAssignment('2'*32, artifact, env.recipient)
    snapshot = store.commit({'worker': assignment.record()}, expected_digest=None, authorized=True)
    view = ConfigurationStore(store.root, store.store_id, replace(controller, home=env.homes['receiver']))
    actor = replace(view.authority, signer=env.recipient)
    endpoint = AssignmentController(store, 'worker', authorized=True)
    worker = AssignedWorker(view, 'worker', actor, endpoint, timeout_seconds=60)
    return store, snapshot, view, actor, endpoint, worker


def test_real_worker_proof_replay_generation_partition_and_wrong_key(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    store, first, view, actor, controller, worker = configured_worker(env, tmp_path/'assignments')
    arguments = dict(job_id='3'*32, source_commitment='4'*64, destination_binding='5'*64,
                     budget=_Budget(ContainerPreparationLimits(timeout_seconds=300)))
    exchanges = []
    class Capture:
        def respond(self, proof, **kwargs):
            response = controller.respond(proof, **kwargs)
            exchanges.append((proof, response))
            return response
    worker.endpoint = Capture()
    worker.authorize('prepare', **arguments)
    assert len(exchanges) == 1
    assert all(b'snapshot-worker-check' not in value and b'assignment' not in value for value in exchanges[0])
    class Replay:
        def respond(self, _proof, **_kwargs):
            return exchanges[0][1]
    worker.endpoint = Replay()
    with pytest.raises(WorkerAuthorityError, match='worker_proof_mismatch'):
        worker.authorize('prepare', **arguments)
    class Offline:
        def respond(self, _proof, **_kwargs):
            raise TimeoutError
    worker.endpoint = Offline()
    with pytest.raises(WorkerAuthorityError, match='worker_authority_unavailable'):
        worker.authorize('approve', **arguments)
    import time
    real_clock = time.monotonic
    class Delayed:
        def respond(self, proof, **kwargs):
            response = controller.respond(proof, **kwargs)
            monkeypatch.setattr(time, 'monotonic', lambda: real_clock()+61)
            return response
    worker.endpoint = Delayed()
    try:
        with pytest.raises(WorkerAuthorityError, match='worker_check_expired'):
            worker.authorize('prepare', **arguments)
    finally:
        monkeypatch.setattr(time, 'monotonic', real_clock)
    # Matching fingerprint text without the assigned private key is insufficient.
    impostor = AssignedWorker(view, 'worker', replace(actor, home=env.homes['origin']), controller)
    with pytest.raises(WorkerAuthorityError):
        impostor.authorize('prepare', **arguments)
    second = store.commit(first.record['values'], expected_digest=first.digest, authorized=True)
    assert second.revision == first.revision+1
    worker.endpoint = controller
    with pytest.raises(WorkerAuthorityError, match='assignment_changed'):
        worker.authorize('deliver', **arguments)
    renewed = AssignedWorker(view, 'worker', actor, controller, timeout_seconds=60)
    assert renewed.binding != worker.binding
    renewed.authorize('prepare', **arguments)
    disabled = {'worker': dict(first.record['values']['worker'], enabled=False)}
    store.commit(disabled, expected_digest=second.digest, authorized=True)
    with pytest.raises(WorkerAuthorityError, match='worker_disabled'):
        renewed.authorize('approve', **arguments)
    assert arguments['budget'].crypto >= 4


def test_assignment_shape_and_explicit_controller_authority(tmp_path):
    with pytest.raises(ValueError):
        WorkerAssignment('0'*32, 'a'*32, 'B'*40)
    with pytest.raises(WorkerAuthorityError):
        AssignmentController(None, 'worker', authorized=False)
    assert not list(tmp_path.iterdir())


def test_snapshot_job_checks_each_phase_and_binds_artifact(tmp_path, separate_machine_env):
    from ls.core.openpgp import artifacts
    from ls.core.openpgp.container_catalog import CatalogContext
    from ls.core.openpgp.container_consolidation_job import run_snapshot_job
    from ls.core.openpgp.container_preparation import ContainerNamespace
    from ls.core.openpgp.container_reader import ContainerReader, ContainerReadError
    from ls.core.openpgp.container_sources import LocalFileRangeSource
    from ls.core.openpgp.container_transfer import TransferError
    from ls.tests.test_agent_container_reads import prepare
    from ls.tests.test_content_addressed_publication import Carrier
    from ls.tests.test_openpgp_artifacts import SCOPE

    env = separate_machine_env
    source = prepare(env, tmp_path, (b'worker snapshot content',))[0]
    incoming = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    with LocalFileRangeSource(source.path) as stream:
        metadata = artifacts.inspect_container_metadata(stream, **incoming)
    reader = ContainerReader(**incoming, source_paths=(source.path,))
    store, first, view, actor, endpoint, worker = configured_worker(env, tmp_path/'assignments',
                                                                metadata.logical['artifact_id'])
    def context(role, trust_role, signer, catalog):
        state = env.snapshots[role]
        return CatalogContext(catalog, env.paths[role], state.store_id, SCOPE, trust_role,
            state.revision, state.epoch, signer, (signer,), env.homes[role], SECRET_REFERENCE, env.resolver)
    publisher = context('origin', 'publisher', env.publisher, 'e'*32)
    controller = context('controller', 'owner', env.owner, 'f'*32)
    carrier, changed, root = Carrier(), [], tmp_path/'job'
    def reassign_after_prepare():
        if not changed and list((root/'prepare/prepared').glob('*.lssc')):
            changed.append(store.commit(first.record['values'], expected_digest=first.digest, authorized=True))
    arguments = dict(reader=reader, authorized=True, job_id='3'*32,
        source_commitment=metadata.logical_commitment_sha256, assignment_binding='4'*64,
        namespace=ContainerNamespace('tenant', 'project', 'a'*32), local_context=publisher,
        publisher_context=publisher, controller_context=controller,
        publication_policy=env.policies['origin'], approval_policy=env.policies['controller'],
        publication_check=reassign_after_prepare, destination_binding='5'*64,
        destination=carrier, publication_mode='content_addressed', worker_authority=worker)
    with pytest.raises(WorkerAuthorityError, match='assignment_changed'):
        run_snapshot_job(source.path, job_root=root, **arguments)
    assert changed and carrier.calls == 0 and not (root/'approve').exists()
    assert list((root/'prepare/prepared').glob('*.lssc'))
    renewed = AssignedWorker(view, 'worker', actor, endpoint, timeout_seconds=60)
    with pytest.raises(TransferError, match='snapshot_job_mismatch'):
        run_snapshot_job(source.path, job_root=root, **(arguments | {'worker_authority': renewed}))
    approved, published = run_snapshot_job(source.path, job_root=tmp_path/'new-job',
        **(arguments | {'worker_authority': renewed, 'job_id': '6'*32}))
    assert published.completed_volumes == 1 and carrier.calls == 1
    assert approved.prepared.artifact_id == worker.assignment.artifact_id
    wrong = {'worker': dict(first.record['values']['worker'], artifact_id='b'*32)}
    store.commit(wrong, expected_digest=changed[0].digest, authorized=True)
    other = AssignedWorker(view, 'worker', actor, endpoint, timeout_seconds=60)
    denied = tmp_path/'wrong-artifact'
    with pytest.raises(ContainerReadError, match='unauthorized'):
        run_snapshot_job(source.path, job_root=denied,
            **(arguments | {'worker_authority': other, 'job_id': '7'*32}))
    assert not list(denied.glob('**/*.lssc')) and carrier.calls == 1
