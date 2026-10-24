"""Crash checkpoints are encrypted and never replay uncertain carrier writes."""
from dataclasses import replace
import json

import pytest

from ls.core.agent.runtime_lock import runtime_use
from ls.core.openpgp import artifacts, container_publication_job as jobs
from ls.core.openpgp import container_transfer as transfer
from ls.core.openpgp.artifact_records import canonical_container_key
from ls.core.openpgp.container_catalog import CatalogContext
from ls.core.openpgp.container_preparation import ContainerPreparationLimits, PreparedContainerSet, PreparedVolume
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.secrets import SecretResolver
from ls.tests.test_openpgp_artifacts import _prepare, separate_machine_env, SECRET_REFERENCE, SCOPE
from ls.tests.test_content_addressed_publication import Carrier


@pytest.fixture
def state_machine(tmp_path, monkeypatch):
    """Only ordering tests use inert crypto; the integration below uses GnuPG."""
    context = CatalogContext('a'*32, tmp_path/'trust', 'b'*64, ('fixture',), 'publisher',
        1, 1, 'C'*40, ('C'*40,), tmp_path/'home', SECRET_REFERENCE, SecretResolver(env_values={}))
    volumes = tuple(PreparedVolume(canonical_container_key('tenant', 'project', 'a'*32,
        'b'*32, 'c'*32, str(i)*32), tmp_path/str(i), i, 'd'*64, str(i)*32, 1) for i in (1, 2, 3))
    prepared = PreparedContainerSet('b'*32, 'c'*32, 'e'*64, volumes)
    monkeypatch.setattr(jobs, 'container_admission_policy_sha256', lambda _p: 'f'*64)
    monkeypatch.setattr(transfer, '_local_authority', lambda _c: None)
    monkeypatch.setattr(transfer, '_seal_progress', lambda v, _c, **_kw: {'fixture': json.loads(json.dumps(v))})
    monkeypatch.setattr(transfer, '_open_progress', lambda v, _c, **_kw: json.loads(json.dumps(v['fixture'])))
    args = dict(job_root=tmp_path/'job', job_id='1'*32, destination_binding='2'*64,
        destinations={v.volume_id: object() for v in volumes}, authorization_check=lambda: None,
        local_context=context, policy=object(), trust_state_path=tmp_path/'trust', gnupg_home=tmp_path/'home')
    return prepared, args


def test_resume_observes_interrupted_volume_and_continues_pending_only(state_machine, monkeypatch):
    prepared, args = state_machine
    calls, budgets = [], []

    def publish(volume, **kwargs):
        calls.append((volume.volume_id, kwargs['reconcile_only']))
        budgets.append(kwargs['_budget'])
        assert kwargs['expected_logical_commitment'] == prepared.logical_commitment_sha256
        if len(calls) == 2:
            raise SystemExit('synthetic process interruption after dispatch')

    monkeypatch.setattr(transfer, 'publish_prepared_volume', publish)
    with pytest.raises(SystemExit):
        jobs.publish_prepared_job(prepared, **args)
    assert budgets[0] is budgets[1]
    record = jobs._read(args['job_root']/'publication.json')['fixture']
    assert record['states'] == 'v?p'
    result = jobs.publish_prepared_job(prepared, **args)
    assert result.completed_volumes == result.total_volumes == 3
    assert calls == [('1'*32, False), ('2'*32, False), ('2'*32, True), ('3'*32, False)]
    assert budgets[2] is budgets[3] and budgets[0] is not budgets[2]
    jobs.publish_prepared_job(prepared, **args)
    assert len(calls) == 4  # Historical completion is not a new availability probe.
    for changed in ({'destination_binding': '3'*64}, {'job_id': '4'*32},
                    {'publication_mode': 'content_addressed'}):
        with pytest.raises(transfer.TransferError, match='publication_job_mismatch'):
            jobs.publish_prepared_job(prepared, **(args | changed))
    with pytest.raises(transfer.TransferError, match='publication_job_mismatch'):
        jobs.publish_prepared_job(replace(prepared, volumes=tuple(reversed(prepared.volumes))), **args)
    assert len(calls) == 4


@pytest.mark.parametrize('persisted', [False, True])
def test_checkpoint_failure_prevents_dispatch_and_preserves_uncertainty(state_machine, monkeypatch, persisted):
    prepared, args = state_machine
    write, calls = jobs._write, []

    def fail_checkpoint(path, sealed):
        if '?' in sealed['fixture']['states']:
            if persisted:
                write(path, sealed)
            raise OSError('synthetic checkpoint failure')
        write(path, sealed)

    monkeypatch.setattr(jobs, '_write', fail_checkpoint)
    monkeypatch.setattr(transfer, 'publish_prepared_volume', lambda _v, **kw: calls.append(kw['reconcile_only']))
    with pytest.raises(OSError):
        jobs.publish_prepared_job(prepared, **args)
    assert calls == []
    monkeypatch.setattr(jobs, '_write', write)
    jobs.publish_prepared_job(prepared, **args)
    assert calls == [persisted, False, False]


def test_authority_busy_and_tampered_state_never_dispatch(state_machine, monkeypatch):
    prepared, args = state_machine
    monkeypatch.setattr(transfer, 'publish_prepared_volume', lambda *_a, **_kw: pytest.fail('unexpected dispatch'))

    def deny():
        raise PermissionError('revoked')

    with pytest.raises(PermissionError):
        jobs.publish_prepared_job(prepared, **(args | {'authorization_check': deny}))
    assert not args['job_root'].exists()
    args['job_root'].mkdir(mode=0o700)
    with runtime_use(args['job_root'], exclusive=True, timeout=0):
        with pytest.raises(transfer.TransferError, match='busy'):
            jobs.publish_prepared_job(prepared, **args)
    assert not (args['job_root']/'publication.json').exists()
    jobs._write(args['job_root']/'publication.json', {'fixture': {'schema': 'wrong'}})
    with pytest.raises(transfer.TransferError, match='publication_job_mismatch'):
        jobs.publish_prepared_job(prepared, **args)


def test_real_encrypted_job_restart_does_not_repeat_uncertain_put(tmp_path, separate_machine_env):
    env = separate_machine_env
    original = _prepare(env, tmp_path)
    with LocalFileRangeSource(original.volumes[0].path) as source:
        approved = artifacts.approve_container_distribution(source, output_dir=original.volumes[0].path.parent,
            policy=env.policies['controller'], trust_state_path=env.paths['controller'],
            gnupg_home=env.homes['controller'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    prepared = replace(original, volumes=(approved,))
    local = env.snapshots['origin']
    context = CatalogContext('f'*32, env.paths['origin'], local.store_id, SCOPE, 'publisher',
        local.revision, local.epoch, env.publisher, (env.publisher,), env.homes['origin'], SECRET_REFERENCE, env.resolver)

    class InterruptedCarrier(Carrier):
        def put(self, *args, **kwargs):
            super().put(*args, **kwargs)
            raise SystemExit('synthetic crash after retained ciphertext')

    carrier = InterruptedCarrier()
    args = dict(job_root=tmp_path/'job', job_id='1'*32, destination_binding='2'*64,
        destinations={approved.volume_id: carrier}, authorization_check=lambda: None,
        local_context=context, policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        publication_mode='content_addressed')
    with pytest.raises(SystemExit):
        jobs.publish_prepared_job(prepared, **args)
    assert carrier.calls == 1 and list(carrier.data.values()) == [approved.path.read_bytes()]
    path = args['job_root']/'publication.json'
    encrypted = path.read_bytes()
    assert set(json.loads(encrypted)) == {'sealed'}
    assert all(value.encode() not in encrypted for value in (
        str(approved.path), prepared.logical_commitment_sha256, args['destination_binding'], 'publication-job'))
    carrier.visible = False
    with pytest.raises(transfer.TransferError, match='publish_uncertain'):
        jobs.publish_prepared_job(prepared, **args)
    assert path.read_bytes() == encrypted and carrier.calls == 1
    carrier.visible = True
    result = jobs.publish_prepared_job(prepared, **args)
    assert result.completed_volumes == result.total_volumes == 1 and carrier.calls == 1
    saved = path.read_bytes()
    corrupted = json.loads(saved)
    corrupted['sealed'] = corrupted['sealed'][:-4] + 'AAAA'
    jobs._write(path, corrupted)
    with pytest.raises(transfer.TransferError, match='progress_invalid'):
        jobs.publish_prepared_job(prepared, **args)
    assert carrier.calls == 1
    # Journal sealing and transfer verification draw from one job budget.
    limited = args | {'job_root': tmp_path/'bounded-job',
                      'limits': replace(ContainerPreparationLimits(), max_crypto_operations=1)}
    with pytest.raises(ValueError, match='crypto'):
        jobs.publish_prepared_job(prepared, **limited)
    assert carrier.calls == 1
    path.write_bytes(saved)
    with pytest.raises(transfer.TransferError, match='prepared_mismatch'):
        transfer.publish_prepared_volume(approved, destination=carrier, local_context=context,
            policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
            gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            expected_logical_commitment='0'*64, publication_mode='content_addressed')
    assert carrier.calls == 1
