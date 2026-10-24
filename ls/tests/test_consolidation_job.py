"""Durable exact approval recovery never repeats an uncertain signing request."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from ls.core.openpgp import artifacts, container_consolidation_job as jobs
from ls.core.openpgp.container_catalog import CatalogContext
from ls.core.openpgp.container_consolidation import PreparedSnapshot
from ls.core.openpgp.container_packing import PackingLimits
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits
from ls.core.openpgp.container_publication_job import publish_prepared_job
from ls.core.openpgp.container_reconstruction import extract_container_file
from ls.core.openpgp.container_snapshot_records import CoveredFile, SnapshotClaim
from ls.core.openpgp.container_sources import BytesRangeSource
from ls.core.openpgp.container_transfer import TransferError
from ls.tests.test_content_addressed_publication import Carrier
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, SCOPE, separate_machine_env


def test_invalid_job_cannot_create_private_state(tmp_path):
    with pytest.raises(TransferError, match='snapshot_job_invalid'):
        jobs.approve_snapshot_job(None, job_root=tmp_path/'job', job_id='invalid',
            source_commitment='c'*64, assignment_binding='d'*64,
            local_context=None, controller_context=None, policy=None, publication_check=lambda: None)
    assert not list(tmp_path.iterdir())


def test_completed_receipt_capacity_is_checked_before_approval(tmp_path):
    row = ['tenant/project/' + 'a'*32, str(tmp_path / ('b'*32 + '.lssc')),
           100, 'c'*64, 'b'*32, 1]
    record = dict(schema=jobs._SCHEMA, version=1, binding='d'*64, phase='prepared',
                  prepared=dict(artifact='e'*32, version='f'*32, commitment='a'*64,
                                checkpoint='b'*64, volumes=[row]), approved=None)
    limits = ContainerPreparationLimits()
    jobs._require_receipt_capacity(record, tmp_path/'approved', limits)
    # The initial encrypted journal fits; keeping both receipts would not.
    record['prepared']['volumes'] = [row] * 60
    assert len(json.dumps(record, separators=(',', ':')).encode()) < 32 * 1024
    with pytest.raises(TransferError, match='progress_limit'):
        jobs._require_receipt_capacity(record, tmp_path/'approved', limits)


def test_real_approval_recovery_preserves_exact_set_and_never_replays(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    data = b'known reconstructed content\n' * 1400
    source = tmp_path/'input'
    source.write_bytes(data)
    source.chmod(0o600)
    originals = tmp_path/'originals'
    originals.mkdir(mode=0o700)
    covered = CoveredFile('b'*32, 'c'*64, 'd'*32, len(data), hashlib.sha256(data).hexdigest())
    claim = SnapshotClaim(covered.version_id, covered.commitment_sha256, (covered,))
    prepared = artifacts.prepare_container((ContainerInput(source, covered.file_id),),
        output_dir=originals, namespace=ContainerNamespace('tenant', 'project', 'a'*32),
        policy=env.policies['origin'], trust_state_path=env.paths['origin'], gnupg_home=env.homes['origin'],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        parent_versions=(claim.source_version,), snapshot=claim,
        limits=ContainerPreparationLimits(packing_limits=PackingLimits(
            target_bytes=25000, tolerance=.1, max_split_iterations=12, max_measure_calls=200)))
    assert len(prepared.volumes) > 1
    snapshot = PreparedSnapshot(prepared, claim)
    original_bytes = {volume.path: volume.path.read_bytes() for volume in prepared.volumes}

    def context(role, trust_role, signer, catalog):
        state = env.snapshots[role]
        return CatalogContext(catalog, env.paths[role], state.store_id, SCOPE, trust_role,
            state.revision, state.epoch, signer, (signer,), env.homes[role], SECRET_REFERENCE, env.resolver)

    local = context('origin', 'publisher', env.publisher, 'e'*32)
    controller = context('controller', 'owner', env.owner, 'f'*32)
    args = dict(job_id='1'*32, source_commitment=claim.source_commitment,
        assignment_binding='2'*64, local_context=local, controller_context=controller,
        policy=env.policies['controller'], publication_check=lambda: None)
    invalid = tmp_path/'invalid-controller'
    with pytest.raises(TransferError, match='snapshot_job_invalid'):
        jobs.approve_snapshot_job(snapshot, job_root=invalid,
            **(args | {'controller_context': replace(controller, trust_revision=controller.trust_revision+1)}))
    assert not invalid.exists()
    denied = tmp_path/'denied'
    with pytest.raises(TransferError, match='snapshot_job_unauthorized'):
        jobs.approve_snapshot_job(snapshot, job_root=denied,
            **(args | {'publication_check': lambda: False}))
    assert not denied.exists()

    real_approve, calls = jobs.approve_snapshot, []
    def interrupted(*values, **options):
        calls.append(options['output_dir'])
        if options['output_dir'].parent.name == 'before':
            raise SystemExit('synthetic crash before approval returned any result')
        result = real_approve(*values, **options)
        raise SystemExit('synthetic crash after local outputs, before checkpoint')
    monkeypatch.setattr(jobs, 'approve_snapshot', interrupted)

    before = tmp_path/'before'
    with pytest.raises(SystemExit):
        jobs.approve_snapshot_job(snapshot, job_root=before, **args)
    journal = before/'consolidation.json'
    saved = journal.read_bytes()
    with pytest.raises(TransferError, match='snapshot_job_pending'):
        jobs.approve_snapshot_job(None, job_root=before, **args)
    assert journal.read_bytes() == saved and len(calls) == 1

    after = tmp_path/'after'
    with pytest.raises(SystemExit):
        jobs.approve_snapshot_job(snapshot, job_root=after, **args)
    journal = after/'consolidation.json'
    saved = journal.read_bytes()
    assert all(value.encode() not in saved for value in (
        str(source), prepared.logical_commitment_sha256, claim.source_commitment, 'snapshot-approval-job'))
    outputs = sorted((after/'approved').glob('*.lssc'))
    assert len(outputs) == len(prepared.volumes)
    # A partial set cannot be promoted or cause approval to run again.
    held = after/'held'
    outputs[-1].rename(held)
    with pytest.raises(TransferError, match='snapshot_job_pending'):
        jobs.approve_snapshot_job(None, job_root=after, **args)
    held.rename(outputs[-1])
    # Multiple candidates for one origin volume remain an explicit conflict.
    duplicate = after/'approved'/('9'*32+'.lssc')
    shutil.copyfile(outputs[0], duplicate)
    duplicate.chmod(0o600)
    with pytest.raises(TransferError, match='snapshot_job_conflict'):
        jobs.approve_snapshot_job(None, job_root=after, **args)
    assert duplicate.exists() and len(calls) == 2 and journal.read_bytes() == saved
    duplicate.unlink()
    approved = jobs.approve_snapshot_job(None, job_root=after, **args)
    assert approved.claim == claim and approved.prepared.logical_commitment_sha256 == prepared.logical_commitment_sha256
    assert jobs.approve_snapshot_job(None, job_root=after, **args) == approved
    assert len(calls) == 2 and all(path.read_bytes() == value for path, value in original_bytes.items())
    with pytest.raises(TransferError, match='snapshot_job_mismatch'):
        jobs.approve_snapshot_job(None, job_root=after, **(args | {'assignment_binding': '3'*64}))
    with pytest.raises(TransferError, match='snapshot_job_mismatch'):
        jobs.approve_snapshot_job(replace(snapshot, prepared=replace(prepared, version_id='4'*32)), job_root=after, **args)

    incoming = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    carrier = Carrier()
    delivery = publish_prepared_job(approved.prepared, job_root=after/'delivery', job_id='1'*32,
        destination_binding='5'*64, destinations={v.volume_id: carrier for v in approved.prepared.volumes},
        authorization_check=lambda: None, local_context=local, publication_mode='content_addressed', **incoming)
    assert delivery.completed_volumes == len(prepared.volumes) == carrier.calls
    sources = {v.key: BytesRangeSource(carrier.data[v.key+'.sha256-'+v.transport_sha256])
               for v in approved.prepared.volumes}
    extract_container_file(sources, covered.file_id, 'recovered', authorized=True, **incoming)
    assert (Path(env.policies['receiver'].boundary)/'recovered').read_bytes() == data
