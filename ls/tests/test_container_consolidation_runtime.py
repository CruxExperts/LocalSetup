"""Configured modes execute beneath normal reads with separate write authority."""
from dataclasses import asdict, replace
import hashlib
import io
import json
from pathlib import Path
import time

import pytest

from ls.core.openpgp import artifacts, container_runtime as runtime
from ls.core.openpgp.container_consolidation_config import ConsolidationBinding
from ls.core.openpgp.container_consolidation_runtime import bind_consolidation
from ls.core.openpgp.container_local_publisher import LocalDirectoryPublisher
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.container_storage import write_private_file
from ls.core.openpgp.container_transfer import TransferError
from ls.tests.test_agent_container_reads import prepare
from ls.tests.test_container_worker_authority import configured_worker
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE, SECRET_NAME


def test_local_carrier_preserves_bytes_collision_and_cancel(tmp_path):
    tmp_path.chmod(0o700)
    carrier = LocalDirectoryPublisher(tmp_path)
    value = b'opaque ciphertext'*10000
    digest = hashlib.sha256(value).hexdigest()
    args = dict(length=len(value), sha256=digest, timeout_seconds=10, cancel_check=lambda: False)
    key = 'opaque/provider/key'
    assert carrier.observe(key, timeout_seconds=10, cancel_check=lambda: False) is None
    assert carrier.create_if_absent(key, io.BytesIO(value), **args) == 'created'
    assert carrier.create_if_absent(key, io.BytesIO(value), **args) == 'exists'
    assert carrier.observe(key, timeout_seconds=10, cancel_check=lambda: False) == (len(value), digest)
    carrier.put(key, io.BytesIO(value), **args)
    with pytest.raises(TransferError, match='collision'):
        carrier.put(key, io.BytesIO(b'changed'), **(args | {'length': 7, 'sha256': hashlib.sha256(b'changed').hexdigest()}))
    assert len(list(tmp_path.iterdir())) == 1
    with pytest.raises(TransferError, match='cancelled'):
        carrier.create_if_absent('other', io.BytesIO(value), **(args | {'cancel_check': lambda: True}))
    with pytest.raises(TransferError, match='invalid'):
        carrier.create_if_absent('invalid', io.BytesIO(b'truncated'), **args)
    assert len(list(tmp_path.iterdir())) == 1
    old = tmp_path.with_name(tmp_path.name+'-old')
    tmp_path.rename(old); tmp_path.mkdir(mode=0o700)
    with pytest.raises(TransferError, match='destination_changed'):
        carrier.observe(key, timeout_seconds=10, cancel_check=lambda: False)


def test_consolidation_config_cannot_be_silently_downgraded(tmp_path):
    with pytest.raises(ValueError):
        ConsolidationBinding(Path('relative'), 'a'*64)
    with pytest.raises(runtime.RuntimeErrorCode):
        runtime.RuntimeConfig('a'*32, consolidation=ConsolidationBinding(tmp_path/'grant', 'b'*64))
    assert not list(tmp_path.iterdir())


def test_protected_supervisor_accepts_only_bounded_secret_data(tmp_path, monkeypatch):
    from ls.core.agent import container_authority as authority
    from ls.core.openpgp.container_reader import ContainerReadError
    root, workspace = tmp_path/('a'*32), tmp_path/'work'
    workspace.mkdir()
    values = {'CATALOG_SECRET': 'one', 'PUBLISHER_SECRET': 'two', 'CONTROLLER_SECRET': 'three'}
    monkeypatch.setenv(authority.SECRET_ENV, json.dumps(values))
    monkeypatch.setattr(authority, 'resolve_reader', lambda *args, **kwargs: kwargs['env_values'])
    assert authority.reader(str(root), workspace, deadline=time.monotonic()+10, protected=True) == values
    monkeypatch.setenv(authority.SECRET_ENV, json.dumps({f'EXTRA_{index}': 'no' for index in range(9)}))
    with pytest.raises(ContainerReadError, match='unauthorized'):
        authority.reader(str(root), workspace, deadline=time.monotonic()+10, protected=True)


def test_worker_configuration_home_is_outside_workspace(tmp_path, monkeypatch):
    from ls.core.openpgp import container_consolidation_config as config
    private, workspace = tmp_path/'private', tmp_path/'workspace'
    private.mkdir(); workspace.mkdir()
    key = dict(home=str(private/'keys'), protected_file_root=None)
    role = dict(key=key, policy=dict(boundary=str(private/'boundary')),
                trust_state_path=str(private/'trust'))
    worker = dict(authority_file=str(private/'authority'), home=str(private/'home'), key=key)
    value = dict(publisher=role, controller=role, worker=worker,
                 destination=dict(path=str(private/'destination')))
    monkeypatch.setattr(config, 'read_binding', lambda binding: value)
    monkeypatch.setattr(config, '_worker_authority', lambda worker: dict(
        root=str(private/'store'), gnupg_home=str(private/'keys'),
        trust=dict(path=str(private/'trust')), secret=dict(protected_file_root=None)))
    binding = ConsolidationBinding(private/'binding', 'a'*64)
    config.check_workspace(binding, workspace)
    worker['home'] = str(workspace/'config-home')
    with pytest.raises(ValueError, match='separate'):
        config.check_workspace(binding, workspace)


def setup_runtime(env, tmp_path, monkeypatch, mode):
    from ls.core.openpgp import keys, trust_state
    from ls.tests.openpgp_key_helpers import _gpg, PASSPHRASE
    content = b'normal read with explicit snapshot authorization'
    source = prepare(env, tmp_path, (content,))[0]
    incoming = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    with LocalFileRangeSource(source.path) as stream:
        metadata = artifacts.inspect_container_metadata(stream, **incoming)
    root = tmp_path/('f'*32)
    destination = tmp_path/'published'; destination.mkdir(mode=0o700)
    workspace = tmp_path/'workspace'; workspace.mkdir(mode=0o700)
    (workspace/'document').write_bytes(source.path.read_bytes())
    def key(role, fingerprint):
        return runtime.KeySelection(fingerprint, _gpg(env.homes[role], '--armor', '--export', fingerprint),
                                    env.homes[role], SECRET_REFERENCE)
    selected = {role: key(role, fingerprint) for role, fingerprint in (
        ('origin', env.publisher), ('controller', env.owner), ('receiver', env.recipient))}
    fields = asdict(env.policies['receiver'])
    for name in ('content_revision', 'created_at', 'expires_at'):
        fields.pop(name)
    policy = env.policies['receiver']
    discovery = artifacts.ArtifactDiscoveryPolicy(**(fields | dict(boundary=root/'reads',
        require_worker_snapshots=mode=='worker', admit_from=policy.created_at-1,
        admit_until=policy.expires_at, max_content_age_seconds=3600, max_content_lifetime_seconds=3600)))
    monkeypatch.setenv(SECRET_NAME, PASSPHRASE)
    monkeypatch.setattr(trust_state, 'inspect_key', keys.inspect_key)
    config = runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(root.name,
        enabled=True, custom_publisher=selected['receiver'], custom_owner=selected['controller'],
        remote_content=runtime.RemoteContentTrust(env.paths['receiver'], discovery.trust_store_id, discovery.scope),
        source_ids=('d'*32, 'e'*32), source_directories=(tmp_path/'carriers', destination), read_policy=discovery))
    assert runtime.setup_identity(root).state == 'ready'
    worker = None
    if mode == 'worker':
        store, assignment, _view, _actor, _endpoint, _worker = configured_worker(env, tmp_path/'assignments', metadata.logical['artifact_id'])
        state = env.snapshots['controller']
        authority = dict(schema='localsetup.configuration-authority', version=1,
            root=str(store.root), store_id=store.store_id, signer=env.owner,
            recipients=list(store.authority.recipients), gnupg_home=str(env.homes['controller']), write=True,
            secret=dict(provider='env', name=SECRET_NAME, protected_file_root=None),
            trust=dict(path=str(env.paths['controller']), store_id=state.store_id,
                       revision=state.revision, epoch=state.epoch, scope=list(state.scope)),
            owners={'worker': dict(kind='snapshot-worker', **{k: v for k, v in assignment.record['values']['worker'].items() if k != 'enabled'})})
        authority_file = tmp_path/'assignment-authority.json'
        raw = json.dumps(authority).encode()
        write_private_file(authority_file, raw, replace=False)
        worker = dict(authority_file=str(authority_file), authority_sha256=hashlib.sha256(raw).hexdigest(),
                      owner='worker', key=runtime._key_dict(selected['receiver']), home=str(tmp_path/'home'))
    def authority(role):
        return dict(policy=asdict(env.policies[role]), trust_state_path=str(env.paths[role]),
                    key=runtime._key_dict(selected[role]))
    value = dict(schema='localsetup.snapshot-consolidation', version=1, mode=mode,
        artifacts=[metadata.logical['artifact_id']], events=['change', 'manual'],
        namespace=dict(tenant_id='tenant', project_id='project', publisher_opaque_id='a'*32),
        publisher=authority('origin'), controller=authority('controller'),
        destination=dict(kind='local-directory', path=str(destination), mode='conditional'),
        worker=worker, max_jobs=4)
    authority_file = tmp_path/'snapshot-authority.json'
    write_private_file(authority_file, json.dumps(value).encode(), replace=False)
    return root, config, authority_file, workspace, source, metadata, destination, content


@pytest.mark.parametrize('mode', ['ad-hoc', 'client', 'worker'])
def test_configured_modes_normal_read_exact_job_and_revocation(tmp_path, separate_machine_env, monkeypatch, capsys, mode):
    from ls.core.agent.cli import main as read_main
    from ls.core.openpgp.container_cli import main
    from ls.core.openpgp.container_access import resolve_catalog
    env = separate_machine_env
    root, config, authority, workspace, source, metadata, destination, content = setup_runtime(env, tmp_path, monkeypatch, mode)
    grant = tmp_path/'read-grant.json'
    write_private_file(grant, json.dumps(dict(schema_version=1, read=['document'], write=[],
        disclose=['document'], recipes={}, container_runtime=str(root))).encode(), replace=False)
    read_args = ['read', '--workspace', str(workspace), '--grant', str(grant), '--path', 'document']
    assert read_main(read_args) == 0
    assert ''.join(json.loads(line)['content'] for line in capsys.readouterr().out.splitlines()).encode() == content
    assert not list(destination.iterdir()) and not (root/'consolidations').exists()
    configured = bind_consolidation(root, authority, expected_revision=config.revision, authorized=True)
    encoded = runtime._config_dict(configured)
    assert encoded['version'] == 5 and runtime._parse_config(encoded, root.name) == configured
    with pytest.raises(runtime.RuntimeErrorCode):
        runtime._parse_config(dict(encoded, version=4), root.name)
    from ls.core.openpgp.container_consolidation_config import check_workspace
    unsafe = json.loads(authority.read_text())
    unsafe['destination']['path'] = str(workspace/'untrusted-destination')
    unsafe_path = tmp_path/'unsafe-authority.json'
    raw = json.dumps(unsafe).encode()
    write_private_file(unsafe_path, raw, replace=False)
    with pytest.raises(ValueError, match='separate'):
        check_workspace(ConsolidationBinding(unsafe_path, hashlib.sha256(raw).hexdigest()), workspace)
    if mode == 'client':
        # The carrier retained bytes but lost its acknowledgement and initial
        # readback. A new read must observe them without another create/sign.
        create, writes = LocalDirectoryPublisher.create_if_absent, []
        def lost_ack(self, *args, **kwargs):
            result = create(self, *args, **kwargs)
            writes.append(result)
            raise OSError('synthetic lost acknowledgement')
        with monkeypatch.context() as patch:
            patch.setattr(LocalDirectoryPublisher, 'create_if_absent', lost_ack)
            with monkeypatch.context() as offline:
                def unavailable(*args, **kwargs):
                    raise OSError('synthetic readback outage')
                offline.setattr(LocalDirectoryPublisher, 'observe', unavailable)
                assert read_main(read_args) == 2
                capsys.readouterr()
            assert read_main(read_args) == 0
            capsys.readouterr()
        assert writes == ['created']
    assert read_main(read_args) == 0
    assert ''.join(json.loads(line)['content'] for line in capsys.readouterr().out.splitlines()).encode() == content
    reference = '@artifacts/'+metadata.logical['artifact_id']+'/'+metadata.logical['version_id']
    if mode != 'client':
        assert not list(destination.iterdir())
        assert main(['--runtime-root', str(root), 'consolidate', '--reference', reference],
                    source_root=Path(__file__).resolve().parents[2]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['state'] == 'published'
    outputs = tuple(destination.glob('*.lssc'))
    assert len(outputs) == 1
    original = {path: path.read_bytes() for path in outputs}
    jobs = tuple(path for path in (root/'consolidations').iterdir() if path.is_dir())
    assert len(jobs) == 1
    # A new catalog head after publication does not restart the same source job.
    assert read_main(read_args) == 0
    capsys.readouterr()
    service = resolve_catalog(root, deadline=time.monotonic()+300)
    repeated = service.consolidate(reference, trigger='change', deadline=time.monotonic()+300)
    assert repeated['state'] == 'published' and repeated['job_id'] == jobs[0].name
    assert {path: path.read_bytes() for path in destination.glob('*.lssc')} == original
    with pytest.raises(PermissionError):
        service.consolidate(reference, trigger='scheduled', deadline=time.monotonic()+300)
    with LocalFileRangeSource(outputs[0]) as stream:
        checked = artifacts.inspect_container_metadata(stream, policy=configured.read_policy,
            trust_state_path=env.paths['receiver'], gnupg_home=env.homes['receiver'],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    assert ('worker' in checked.logical['snapshot']) is (mode == 'worker')
    assert checked.logical['snapshot']['source_commitment'] == metadata.logical_commitment_sha256
    if mode == 'worker':
        from ls.core.openpgp.container_worker_authority import WorkerAuthorityError
        controller = service.writer_factory().worker.endpoint
        before = controller.store.load()
        controller.store.commit({'worker': before.record['values']['worker'] | {'enabled': False}},
                                expected_digest=before.digest, authorized=True)
        with pytest.raises(WorkerAuthorityError, match='worker_disabled'):
            service.consolidate(reference, deadline=time.monotonic()+300)
        assert read_main(read_args) == 0
        capsys.readouterr()
    assert main(['--runtime-root', str(root), 'disable-consolidation', '--expected-revision', str(configured.revision)],
                source_root=Path(__file__).resolve().parents[2]) == 0
    capsys.readouterr()
    with pytest.raises(Exception):
        service.consolidate(reference, deadline=time.monotonic()+300)
    assert read_main(read_args) == 0
    assert ''.join(json.loads(line)['content'] for line in capsys.readouterr().out.splitlines()).encode() == content
    assert {path: path.read_bytes() for path in destination.glob('*.lssc')} == original
