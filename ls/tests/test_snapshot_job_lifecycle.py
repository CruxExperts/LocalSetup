"""One bounded snapshot lifecycle survives preparation and delivery crashes."""
from pathlib import Path
import shutil

import pytest

from ls.core.openpgp import artifacts, container_consolidation_job as jobs, container_publication_job as publication
from ls.core.openpgp.container_catalog import CatalogContext
from ls.core.openpgp.container_preparation import ContainerNamespace
from ls.core.openpgp.container_reader import ContainerReader
from ls.core.openpgp.container_reconstruction import extract_container_file
from ls.core.openpgp.container_sources import BytesRangeSource, LocalFileRangeSource
from ls.core.openpgp.container_transfer import TransferError
from ls.tests.test_agent_container_reads import prepare
from ls.tests.test_content_addressed_publication import Carrier
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, SCOPE, separate_machine_env


def test_recover_preparation_then_approval_and_uncertain_delivery(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    content = b'complete retained source\n' * 250
    source = prepare(env, tmp_path, (content,))[0]
    original_bytes = source.path.read_bytes()
    incoming = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    with LocalFileRangeSource(source.path) as stream:
        metadata = artifacts.inspect_container_metadata(stream, **incoming)
    reader = ContainerReader(**incoming, source_paths=(source.path,), binding_check=lambda: '9'*64)

    def context(role, trust_role, signer, catalog):
        state = env.snapshots[role]
        return CatalogContext(catalog, env.paths[role], state.store_id, SCOPE, trust_role,
            state.revision, state.epoch, signer, (signer,), env.homes[role], SECRET_REFERENCE, env.resolver)

    publisher = context('origin', 'publisher', env.publisher, 'e'*32)
    controller = context('controller', 'owner', env.owner, 'f'*32)
    carrier = Carrier(lose_ack=True)
    carrier.visible = False
    args = dict(reader=reader, authorized=True, job_id='1'*32,
        source_commitment=metadata.logical_commitment_sha256, assignment_binding='2'*64,
        namespace=ContainerNamespace('tenant', 'project', 'a'*32), local_context=publisher,
        publisher_context=publisher, controller_context=controller,
        publication_policy=env.policies['origin'], approval_policy=env.policies['controller'],
        publication_check=lambda: None, destination_binding='3'*64,
        destination=carrier, publication_mode='content_addressed')
    denied = tmp_path/'denied'
    with pytest.raises(TransferError, match='snapshot_job_invalid'):
        jobs.run_snapshot_job(source.path, job_root=denied, **(args | {'authorized': False}))
    assert not denied.exists()
    with pytest.raises(TransferError, match='snapshot_job_unauthorized'):
        jobs.run_snapshot_job(source.path, job_root=denied, **(args | {'publication_check': lambda: False}))
    assert not denied.exists()

    real_prepare, real_approve, real_publish = jobs.prepare_snapshot, jobs.approve_snapshot, publication.publish_prepared_job
    calls, budgets = [], []
    def interrupted(*values, **options):
        calls.append('prepare')
        budgets.append(options['_budget'])
        if options['output_dir'].parent.parent.name == 'before':
            raise SystemExit('before any preparation output')
        real_prepare(*values, **options)
        raise SystemExit('after preparation output, before checkpoint')
    def approve(*values, **options):
        calls.append('approve')
        budgets.append(options['_budget'])
        return real_approve(*values, **options)
    def publish(*values, **options):
        calls.append('publish')
        budgets.append(options['_budget'])
        return real_publish(*values, **options)
    monkeypatch.setattr(jobs, 'prepare_snapshot', interrupted)
    monkeypatch.setattr(jobs, 'approve_snapshot', approve)
    monkeypatch.setattr(publication, 'publish_prepared_job', publish)

    before = tmp_path/'before'
    with pytest.raises(SystemExit):
        jobs.run_snapshot_job(source.path, job_root=before, **args)
    with pytest.raises(TransferError, match='snapshot_job_pending'):
        jobs.run_snapshot_job(source.path, job_root=before, **args)
    assert calls == ['prepare'] and carrier.calls == 0

    root = tmp_path/'after'
    with pytest.raises(SystemExit):
        jobs.run_snapshot_job(source.path, job_root=root, **args)
    saved = (root/'prepare/preparation.json').read_bytes()
    assert str(source.path).encode() not in saved and metadata.logical_commitment_sha256.encode() not in saved
    outputs = tuple((root/'prepare/prepared').glob('*.lssc'))
    assert len(outputs) == 1
    duplicate = outputs[0].parent / ('8'*32+'.lssc')
    shutil.copyfile(outputs[0], duplicate)
    duplicate.chmod(0o600)
    with pytest.raises(TransferError, match='snapshot_job_conflict'):
        jobs.run_snapshot_job(source.path, job_root=root, **args)
    assert duplicate.exists() and (root/'prepare/preparation.json').read_bytes() == saved
    duplicate.unlink()
    # Even before delivery starts, its destination is bound into the job.
    with pytest.raises(TransferError, match='snapshot_job_mismatch'):
        jobs.run_snapshot_job(source.path, job_root=root, **(args | {'destination_binding': '4'*64}))
    with pytest.raises(TransferError, match='publish_uncertain'):
        jobs.run_snapshot_job(source.path, job_root=root, **args)
    assert calls == ['prepare', 'prepare', 'approve', 'publish']
    assert budgets[-1] is budgets[-2]  # Approval and upload share this invocation's budget.
    assert carrier.calls == 1
    carrier.visible = True
    carrier.put = None  # Recovery must work without another write capability.
    approved, delivery = jobs.run_snapshot_job(source.path, job_root=root, **args)
    assert calls == ['prepare', 'prepare', 'approve', 'publish', 'publish']
    assert delivery.completed_volumes == 1 and carrier.calls == 1
    assert all(budget.temporary == 0 for budget in budgets)
    assert source.path.read_bytes() == original_bytes and outputs[0].exists()
    sources = {v.key: BytesRangeSource(carrier.data[v.key+'.sha256-'+v.transport_sha256])
               for v in approved.prepared.volumes}
    extract_container_file(sources, metadata.logical['files'][0]['file_id'], 'recovered-lifecycle',
                           authorized=True, **incoming)
    assert (Path(reader.policy.boundary)/'recovered-lifecycle').read_bytes() == content
