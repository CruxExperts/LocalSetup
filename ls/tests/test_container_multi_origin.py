"""Real separate signing homes enter one catalog and ordinary read interface."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

import pytest

from ls.core.openpgp import artifacts, container_runtime as runtime, keys, trust_state
from ls.core.openpgp.artifact_records import ArtifactProvenance
from ls.core.openpgp.container_access import resolve_reader, resolve_catalog
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits, _Budget
from ls.core.openpgp.container_reader import ContainerReadError
from ls.core.agent.file_broker import FileBroker
from ls.core.agent.file_grants import FileGrant
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE, SECRET_NAME, _private_fingerprints
from ls.tests.openpgp_key_helpers import _gpg, PASSPHRASE
from ls.tests.test_agent_container_reads import collect
from ls.tests.test_container_delta_codec import codec, evidence, private
from ls.core.openpgp.container_delta_codec import create_patch
from ls.core.openpgp.container_delta_records import FileDelta


@pytest.mark.parametrize('mode', ['full', 'delta', 'cross_scope', 'cross_audience', 'fork_merge'])
def test_independent_origins_scan_read_restart_and_revoke(tmp_path, separate_machine_env, monkeypatch, request, mode):
    env = separate_machine_env
    selected_codec = request.getfixturevalue("codec") if mode != "full" else None
    monkeypatch.setattr(trust_state, 'inspect_key', keys.inspect_key)
    monkeypatch.setenv(SECRET_NAME, PASSPHRASE)
    root = tmp_path/('f'*32)
    sources = tmp_path/'carriers'; sources.mkdir(mode=0o700)
    second_path = tmp_path/'second-trust.db'
    second = trust_state.initialize_trust_state(second_path,
        scope=('other:scope',) if mode == 'cross_scope' else env.policies['origin'].scope,
        publisher_certificate=_gpg(env.homes['controller'], '--armor', '--export', env.owner),
        owner_certificate=_gpg(env.homes['origin'], '--armor', '--export', env.publisher))
    first_policy = replace(env.policies['origin'], controller_fingerprint=None, audience=(env.recipient,))
    second_policy = replace(first_policy, trust_store_id=second.store_id, trust_revision=second.revision,
        governance_epoch=second.epoch, origin_trust_store_id=second.store_id,
        origin_trust_revision=second.revision, origin_governance_epoch=second.epoch,
        origin_fingerprint=env.owner, scope=second.scope,
        audience=tuple(sorted((env.recipient, env.owner))) if mode == 'cross_audience' else (env.recipient,))
    def discovery(policy):
        values = asdict(policy)
        for key in ('content_revision', 'created_at', 'expires_at'): values.pop(key)
        return artifacts.ArtifactDiscoveryPolicy(**(values | dict(boundary=root/'reads',
            admit_from=policy.created_at-1, admit_until=policy.expires_at,
            max_content_age_seconds=3600, max_content_lifetime_seconds=3600)))
    def key(role, fingerprint):
        return runtime.KeySelection(fingerprint, _gpg(env.homes[role], '--armor', '--export', fingerprint),
                                    env.homes[role], SECRET_REFERENCE)
    first_read = discovery(replace(env.policies['receiver'], controller_fingerprint=None, audience=(env.recipient,)))
    second_read = discovery(second_policy)
    runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(root.name, enabled=True,
        custom_publisher=key('receiver', env.recipient), custom_owner=key('controller', env.owner),
        remote_content=runtime.RemoteContentTrust(env.paths['receiver'], first_read.trust_store_id, first_read.scope),
        read_policy=first_read, source_ids=('e'*32,), source_directories=(sources,), delta_codec=selected_codec,
        additional_admissions=(runtime.ContentAdmission(
            runtime.RemoteContentTrust(second_path, second.store_id, second.scope), second_read),)))
    assert runtime.setup_identity(root).state == 'ready'
    prepared = []
    original_content = b'first independent publisher\n'*1000
    contents = (original_content, original_content[:300]+b'second publisher change'+original_content[320:])
    for index, (policy, path, role) in enumerate(((first_policy, env.paths['origin'], 'origin'),
                                               (second_policy, second_path, 'controller'))):
        source = tmp_path/f'input-{index}'; source.write_bytes(contents[index])
        output = tmp_path/f'prepared-{index}'; output.mkdir(mode=0o700)
        delta = None
        if index == 1 and selected_codec is not None:
            patch_path = tmp_path/'patch'
            claim = create_patch(private(tmp_path/'old', contents[0]), private(tmp_path/'new', contents[1]),
                patch_path, base=evidence(contents[0]), result=evidence(contents[1]), policy=selected_codec,
                budget=_Budget(ContainerPreparationLimits()))
            assert claim is not None
            delta = FileDelta(prepared[0].version_id, prepared[0].logical_commitment_sha256,
                'b'*32, evidence(contents[0]), claim, evidence(contents[1]), '8'*32)
            source = patch_path
        prepared.append(artifacts.prepare_container(
            (ContainerInput(source, 'b'*32, ArtifactProvenance(original_name='document'), delta=delta),),
            output_dir=output, namespace=ContainerNamespace('tenant', 'project', 'a'*32),
            artifact_id='c'*32, parent_versions=() if delta is None else (delta.base_version,), policy=policy, trust_state_path=path, gnupg_home=env.homes[role],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver))
    # Arrival order is opposite the configured authority order.
    for index in (1, 0):
        shutil.copyfile(prepared[index].volumes[0].path, sources/f'carrier-{index}')
    original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.iterdir()}
    service = resolve_catalog(root, deadline=time.monotonic()+600)
    scan = service.reconcile(_Budget(ContainerPreparationLimits()))
    assert len(scan.observations) == 2
    assert {row['availability'] for row in scan.snapshot.catalog['containers']} == {'observed'}
    assert len(scan.snapshot.catalog['reconciliation_history']) == 2
    assert service.reconcile(_Budget(ContainerPreparationLimits())).snapshot.head == scan.snapshot.head
    # Overlapping explicit policies cannot choose a winner by configuration order.
    extra = service.additional_readers
    service.additional_readers = (*extra, replace(service.reader,
        policy=replace(service.reader.policy, admit_until=first_read.admit_until+1)))
    with pytest.raises(ContainerReadError, match='unauthorized'):
        service.reconcile(_Budget(ContainerPreparationLimits()))
    with pytest.raises(ContainerReadError, match='unauthorized'):
        service.prepare_selected(sources/'carrier-0', 'document', file_id=None,
            expected_stat=(sources/'carrier-0').stat(), check=lambda: None, deadline=time.monotonic()+60)
    assert service.store._read_head() == scan.snapshot.head
    service.additional_readers = extra
    shutil.copyfile(sources/'carrier-1', sources/'duplicate')
    original['duplicate'] = original['carrier-1']
    duplicate = service.reconcile(_Budget(ContainerPreparationLimits()))
    assert len(duplicate.observations) == 3
    assert len(duplicate.snapshot.catalog['containers']) == 2
    assert service.reconcile(_Budget(ContainerPreparationLimits())).snapshot.head == duplicate.snapshot.head
    assert _private_fingerprints(env.homes['receiver']) == {env.recipient}
    assert _private_fingerprints(env.homes['origin']) == {env.publisher}
    assert _private_fingerprints(env.homes['controller']) == {env.owner}
    workspace = tmp_path/'workspace'; workspace.mkdir(mode=0o700)
    for index in range(2): shutil.copyfile(sources/f'carrier-{index}', workspace/f'document-{index}')
    leases = tmp_path/'leases'; leases.mkdir(mode=0o700)
    deadline = time.monotonic()+600
    grant = FileGrant('task', 'session', workspace, ('document-0', 'document-1', '@artifacts'), (),
                     ('document-0', 'document-1', '@artifacts'), deadline)
    def broker():
        return FileBroker(grant, leases, container_reader=resolve_reader(root, workspace=workspace, deadline=deadline))
    active = broker()
    if mode in ('cross_scope', 'cross_audience'):
        with pytest.raises(ContainerReadError):
            collect(active, 'document-1')
        assert collect(active, 'document-0') == contents[0]
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.iterdir()} == original
        return
    assert collect(active, 'document-1') == contents[1]
    assert collect(active, 'document-0') == contents[0]
    if mode == 'delta':
        with sqlite3.connect(env.paths['receiver']) as db:
            base_receipts = {row[0] for row in db.execute(
                "SELECT value FROM consumed WHERE kind='container-selected-v1'")}
        with sqlite3.connect(second_path) as db:
            head_receipts = {row[0] for row in db.execute(
                "SELECT value FROM consumed WHERE kind='container-selected-v1'")}
        assert base_receipts and head_receipts and base_receipts.isdisjoint(head_receipts)
        recorded = trust_state._record_accepted_container
        checked = []
        def lose_receipt(path, **arguments):
            foreign = arguments.get('dependency_receipts', ())
            if foreign:
                assert foreign[0].path == env.paths['receiver']
                checked.append(True)
                arguments['dependency_receipts'] = (replace(foreign[0], selected_receipts=('f'*64,)), *foreign[1:])
            return recorded(path, **arguments)
        with monkeypatch.context() as patch:
            patch.setattr(trust_state, '_record_accepted_container', lose_receipt)
            with pytest.raises(ContainerReadError):
                collect(active, 'document-1')
        assert checked
    restarted = broker()
    for index, item in enumerate(prepared):
        assert collect(restarted, f'@artifacts/{item.artifact_id}/{item.version_id}') == contents[index]
    rows = [json.loads(line) for line in collect(restarted, '@artifacts').splitlines()]
    assert rows[1]['heads'] == (sorted(item.version_id for item in prepared) if mode == 'full' else [prepared[1].version_id])
    assert rows[1]['state'] == ('conflict' if mode == 'full' else 'indexed')
    if mode == 'fork_merge':
        branch_content = contents[0] + b'\nindependent A change\n'
        merge_content = contents[1] + b'\nindependent A change\n'
        def publish_version(name, content, parents):
            output = tmp_path/name; output.mkdir(mode=0o700)
            value = artifacts.prepare_container((ContainerInput(private(tmp_path/(name+'.plain'), content),
                'b'*32, ArtifactProvenance(original_name='document')),), output_dir=output,
                namespace=ContainerNamespace('tenant', 'project', 'a'*32),
                artifact_id=prepared[0].artifact_id, parent_versions=tuple(sorted(parents)),
                policy=first_policy, trust_state_path=env.paths['origin'], gnupg_home=env.homes['origin'],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
            shutil.copyfile(value.volumes[0].path, sources/name)
            original[name] = hashlib.sha256(value.volumes[0].path.read_bytes()).hexdigest()
            return value
        branch = publish_version('branch-a', branch_content, (prepared[0].version_id,))
        forked = resolve_catalog(root, deadline=time.monotonic()+600)
        fork_scan = forked.reconcile(_Budget(ContainerPreparationLimits()))
        assert forked.reconcile(_Budget(ContainerPreparationLimits())).snapshot.head == fork_scan.snapshot.head
        fork_rows = [json.loads(line) for line in collect(broker(), '@artifacts').splitlines()]
        assert fork_rows[1]['state'] == 'conflict'
        assert fork_rows[1]['heads'] == sorted((branch.version_id, prepared[1].version_id))
        for item, expected in ((branch, branch_content), (prepared[1], contents[1])):
            assert collect(broker(), f'@artifacts/{item.artifact_id}/{item.version_id}') == expected
        # Reopen a second receiver catalog after observing A before B, whereas
        # the original catalog observed B before the late A branch.
        reverse_root = tmp_path/('9'*32)
        reverse_sources = tmp_path/'reverse-carriers'; reverse_sources.mkdir(mode=0o700)
        config = runtime.load_runtime_config(root)
        runtime.configure_runtime(reverse_root, expected_revision=0, proposed=replace(config,
            runtime_id=reverse_root.name, revision=0, source_directories=(reverse_sources,),
            read_policy=replace(config.read_policy, boundary=reverse_root/'reads'),
            additional_admissions=tuple(replace(item, read_policy=replace(item.read_policy,
                boundary=reverse_root/'reads')) for item in config.additional_admissions)))
        assert runtime.setup_identity(reverse_root).state == 'ready'
        for name in ('carrier-0', 'branch-a'):
            shutil.copyfile(sources/name, reverse_sources/name)
        reverse = resolve_catalog(reverse_root, deadline=time.monotonic()+600)
        reverse.reconcile(_Budget(ContainerPreparationLimits()))
        shutil.copyfile(sources/'carrier-1', reverse_sources/'carrier-1')
        reverse = resolve_catalog(reverse_root, deadline=time.monotonic()+600)
        reversed_scan = reverse.reconcile(_Budget(ContainerPreparationLimits()))
        def graph(snapshot):
            return sorted((row['version_id'], tuple(row['parents']), row['commitment_sha256'])
                          for row in snapshot.catalog['containers'])
        assert graph(reversed_scan.snapshot) == graph(fork_scan.snapshot)
        # Merging requires an explicit new immutable object naming both heads.
        merged = publish_version('merge', merge_content, (branch.version_id, prepared[1].version_id))
        reopened = resolve_catalog(root, deadline=time.monotonic()+600)
        merged_scan = reopened.reconcile(_Budget(ContainerPreparationLimits()))
        merge_row = next(row for row in merged_scan.snapshot.catalog['containers']
                         if row['version_id'] == merged.version_id)
        assert merge_row['parents'] == sorted((branch.version_id, prepared[1].version_id))
        merge_rows = [json.loads(line) for line in collect(broker(), '@artifacts').splitlines()]
        assert merge_rows[1]['state'] == 'indexed' and merge_rows[1]['heads'] == [merged.version_id]
        assert reopened.reconcile(_Budget(ContainerPreparationLimits())).snapshot.head == merged_scan.snapshot.head
        for item, expected in ((prepared[0], contents[0]), (prepared[1], contents[1]),
                               (branch, branch_content), (merged, merge_content)):
            assert collect(broker(), f'@artifacts/{item.artifact_id}/{item.version_id}') == expected
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.iterdir()} == original
        return
    if mode == 'delta':
        from ls.core.openpgp.container_consolidation import prepare_snapshot
        from ls.core.openpgp.container_reconstruction import extract_container_file
        from ls.core.openpgp.container_sources import LocalFileRangeSource

        head = next(item.metadata for item in duplicate.observations
                    if item.metadata.logical_commitment_sha256 == prepared[1].logical_commitment_sha256)
        bound = service._bound(duplicate, tuple(duplicate.paths.values()), selected=head)
        # A library reader need not bind the whole catalog. Publication must
        # still recheck the base's authority after reconstruction has finished.
        bound = replace(bound, binding_check=None)
        snapshot_args = dict(reader=bound, authorized=True,
            namespace=ContainerNamespace('tenant', 'project', 'a'*32), publication_policy=first_policy,
            trust_state_path=env.paths['origin'], gnupg_home=env.homes['origin'],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
        output = tmp_path/'snapshot'; output.mkdir(mode=0o700)
        snapshot = prepare_snapshot(sources/'carrier-1', output_dir=output, **snapshot_args)
        assert {item.version_id for item in snapshot.claim.covered} == {item.version_id for item in prepared}
        read_args = dict(policy=first_read, trust_state_path=env.paths['receiver'],
            gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
        with LocalFileRangeSource(snapshot.prepared.volumes[0].path) as source:
            metadata = artifacts.inspect_container_metadata(source, **read_args)
            assert metadata.logical['parent_versions'] == [prepared[1].version_id]
            assert 'delta' not in metadata.logical['files'][0]
            extract_container_file({metadata.key: source}, 'b'*32, 'snapshot-plain', authorized=True, **read_args)
        assert (Path(first_read.boundary)/'snapshot-plain').read_bytes() == contents[1]
    # A current authority revision change invalidates already-resolved readers.
    page = restarted.read_page('task', 'session', 'document-1', None, for_provider=True)
    assert page['next_cursor'] is not None
    def revoke_base():
        with sqlite3.connect(env.paths['receiver'] if mode == 'delta' else second_path) as db:
            db.execute('UPDATE state SET revision=revision+1 WHERE singleton=1')
    if mode == 'delta':
        failed = tmp_path/'failed-snapshot'; failed.mkdir(mode=0o700)
        revoked = []
        def revoke_during_publication():
            if not revoked and list(failed.glob('.lssc-prep-*')):
                revoke_base()
                revoked.append(True)
        with pytest.raises((ContainerReadError, artifacts.ArtifactError)):
            prepare_snapshot(sources/'carrier-1', output_dir=failed,
                publication_check=revoke_during_publication, **snapshot_args)
        assert revoked and not list(failed.iterdir())
    else:
        revoke_base()
    with pytest.raises(ContainerReadError):
        restarted.read_page('task', 'session', 'document-1', page['next_cursor'], for_provider=True)
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.iterdir()} == original
