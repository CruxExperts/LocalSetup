"""Configured runtime access stays beneath ordinary granted reads."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import time

import pytest

from ls.core.openpgp import container_runtime as runtime
from ls.core.openpgp.container_reader import ContainerReadError
from ls.core.openpgp.container_scan import scan_directories, ScanLimitError
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE, SECRET_NAME


def test_v1_configuration_is_read_without_rewriting_or_enabling(tmp_path):
    root = tmp_path / ('a'*32)
    configured = runtime.configure_runtime(root, expected_revision=0,
        proposed=runtime.RuntimeConfig(root.name, source_ids=('b'*32,)))
    legacy = runtime._config_dict(configured)
    legacy.pop('read_policy')
    legacy.pop('source_directories')
    legacy['version'] = 1
    runtime._write(root/'runtime.json', legacy)
    before = (root/'runtime.json').read_bytes()
    assert runtime.load_runtime_config(root) == configured
    assert (root/'runtime.json').read_bytes() == before
    assert not (root/'identity.json').exists()
    with pytest.raises(runtime.RuntimeErrorCode, match='config_invalid'):
        runtime.RuntimeConfig(root.name, source_directories=(tmp_path,))


def test_scan_is_bounded_deterministic_and_does_not_follow_links(tmp_path, monkeypatch):
    root = tmp_path/'source'
    root.mkdir(mode=0o700)
    root.chmod(0o775)  # Shared carrier input is not private trusted state.
    magic = b'format: localsetup.secure-container\n'
    (root/'b').write_bytes(magic)
    (root/'a').write_bytes(magic)
    (root/'plain').write_bytes(b'plain')
    outside = tmp_path/'outside'
    outside.write_bytes(magic)
    (root/'linked').symlink_to(outside)
    sources = (('b'*32, tmp_path/'missing'), ('a'*32, root))
    inventory = scan_directories(sources, maximum=10, check=lambda: None)
    assert [item.locator for item in inventory.candidates] == ['a', 'b']
    assert inventory.coverage == (('a'*32, 'complete'), ('b'*32, 'unavailable'))
    with pytest.raises(ScanLimitError):
        scan_directories(sources, maximum=2, check=lambda: None)
    with pytest.raises(ScanLimitError, match='candidate'):
        scan_directories(sources, maximum=10, max_candidates=1, check=lambda: None)
    def cancelled():
        raise ContainerReadError('expired')
    with pytest.raises(ContainerReadError, match='expired'):
        scan_directories(sources, maximum=10, check=cancelled)
    from ls.core.openpgp import container_scan
    original = container_scan.LocalFileRangeSource
    def replaced(path, **options):
        opened = original(path, **options)
        root.rename(tmp_path/'old-source')
        root.mkdir()
        return opened
    monkeypatch.setattr(container_scan, 'LocalFileRangeSource', replaced)
    changed = scan_directories(sources, maximum=10, check=lambda: None)
    assert not changed.candidates and changed.coverage[0] == ('a'*32, 'unavailable')


def test_grant_runtime_selection_and_supervisor_secret_names(tmp_path, monkeypatch):
    from ls.core.agent import container_authority as authority
    from ls.core.agent.run_cli import _grant
    from ls.core.openpgp.secrets import SecretProvider, SecretReference
    workspace = tmp_path/'work'
    workspace.mkdir()
    root = tmp_path/('a'*32)
    key = runtime.KeySelection('A'*40, b'public fixture', tmp_path/'keys',
                               SecretReference(SecretProvider.ENV, 'LD_PRELOAD'))
    runtime.configure_runtime(root, expected_revision=0,
        proposed=runtime.RuntimeConfig(root.name, custom_publisher=key))
    monkeypatch.setenv('LD_PRELOAD', 'test-only-passphrase')
    monkeypatch.setenv('UNRELATED_SECRET', 'not selected')
    forwarded = authority.launch_environment(str(root), workspace)
    assert set(forwarded) == {authority.SECRET_ENV}
    assert json.loads(forwarded[authority.SECRET_ENV]) == {'LD_PRELOAD': 'test-only-passphrase'}
    grant = tmp_path/'grant'
    value = {'schema_version': 1, 'read': ['document'], 'write': [],
             'disclose': ['document'], 'recipes': {}, 'container_runtime': str(root)}
    grant.write_text(json.dumps(value)); grant.chmod(0o600)
    assert _grant(grant, workspace)[0] == value
    grant.write_text(json.dumps(value | {'container_runtime': str(workspace/('a'*32))}))
    with pytest.raises(ValueError, match='separate'):
        _grant(grant, workspace)
    with pytest.raises(ContainerReadError, match='unauthorized'):
        authority.reader(str(root), workspace, deadline=time.monotonic()+10)
    assert not (root/'identity.json').exists() and not (root/'reads').exists()


def test_ordinary_cli_reads_configured_runtime_and_rejects_live_disable(
        tmp_path, separate_machine_env, monkeypatch, capsys):
    from ls.core.agent.cli import main
    from ls.core.agent.container_authority import reader
    from ls.core.agent.file_broker import FileBroker
    from ls.core.agent.file_grants import FileGrant
    from ls.core.openpgp import artifacts, keys, trust_state
    from ls.core.openpgp.container_preparation import ContainerPreparationLimits
    from ls.core.openpgp.container_packing import PackingLimits
    from ls.tests.test_agent_container_reads import prepare, collect
    from ls.tests.openpgp_key_helpers import _gpg, PASSPHRASE
    env = separate_machine_env
    content = b'configured ordinary file\n'*1500
    approved = prepare(env, tmp_path, (content,), limits=ContainerPreparationLimits(
        packing_limits=PackingLimits(target_bytes=25000, tolerance=.1, max_split_iterations=12, max_measure_calls=200)))
    assert len(approved) > 1
    workspace = tmp_path/'work'; workspace.mkdir()
    shutil.copyfile(approved[0].path, workspace/'document')
    root = tmp_path/('f'*32)
    policy = env.policies['receiver']
    fields = asdict(policy)
    for field in ('content_revision', 'created_at', 'expires_at'):
        fields.pop(field)
    discovery = artifacts.ArtifactDiscoveryPolicy(**(fields | {
        'boundary': root/'reads', 'admit_from': policy.created_at-1,
        'admit_until': policy.expires_at, 'max_content_age_seconds': 3600,
        'max_content_lifetime_seconds': 3600}))
    def selection(role, fingerprint):
        return runtime.KeySelection(fingerprint, _gpg(env.homes[role], '--armor', '--export', fingerprint),
                                    env.homes[role], SECRET_REFERENCE)
    monkeypatch.setenv(SECRET_NAME, PASSPHRASE)
    monkeypatch.setattr(trust_state, 'inspect_key', keys.inspect_key)
    config = runtime.configure_runtime(root, expected_revision=0,
        proposed=runtime.RuntimeConfig(root.name, enabled=True,
            custom_publisher=selection('receiver', env.recipient),
            custom_owner=selection('controller', env.owner),
            remote_content=runtime.RemoteContentTrust(env.paths['receiver'], policy.trust_store_id, policy.scope),
            source_ids=('e'*32,), source_directories=(tmp_path/'carriers',), read_policy=discovery))
    assert runtime.setup_identity(root).state == 'ready'
    grant_path = tmp_path/'grant.json'
    grant_path.write_text(json.dumps({'schema_version': 1, 'read': ['document'],
        'write': [], 'disclose': ['document'], 'recipes': {}, 'container_runtime': str(root)}))
    grant_path.chmod(0o600)
    args = ['read', '--workspace', str(workspace), '--grant', str(grant_path),
            '--path', 'document', '--max-pages', '10']
    assert main(args) == 0
    captured = capsys.readouterr()
    assert ''.join(json.loads(line)['content'] for line in captured.out.splitlines()).encode() == content
    assert not list((root/'reads').glob('.lssc-read-*'))
    # A new normal CLI invocation resolves the same persisted identity without
    # a different model procedure, key generation or enrollment.
    identity = (root/'identity.json').read_bytes()
    assert main(args) == 0
    capsys.readouterr()
    assert (root/'identity.json').read_bytes() == identity
    target_leases = tmp_path/'leases'; target_leases.mkdir(mode=0o700)
    expires = time.monotonic()+300
    broker = FileBroker(FileGrant('task', 'session', workspace, ('document',), (),
        ('document',), expires), target_leases,
        container_reader=reader(str(root), workspace, deadline=expires))
    # A fresh read must retain the identity resolved by this reader, not silently
    # adopt a replacement binding with its previously selected private key.
    recorded = runtime._read(root/'identity.json')
    runtime._write(root/'identity.json', recorded | {'year': recorded['year']+1})
    with pytest.raises(ContainerReadError, match='changed'):
        broker.read_page('task', 'session', 'document', for_provider=True)
    runtime._write(root/'identity.json', recorded)
    # Workspace-wide authority does not disclose the external catalog.
    broad = FileBroker(replace(broker.grant, read=('.',), disclose=('.',)), target_leases,
                       container_reader=broker.container_reader)
    with pytest.raises(PermissionError, match='outside'):
        broad.read_page('task', 'session', '@artifacts', for_provider=True)
    indexed = FileBroker(replace(broker.grant, read=('@artifacts',), disclose=('@artifacts',)),
                         target_leases, container_reader=broker.container_reader)
    service = broker.container_reader.reference_resolver.__self__
    with monkeypatch.context() as patch:
        patch.setattr(artifacts, '_copy_selected_file', lambda *_a, **_k: pytest.fail('catalog index decrypted payload'))
        catalog = [json.loads(line) for line in collect(indexed, '@artifacts').splitlines()]
    artifact_ref = catalog[1]['path']
    before_head = service.store._read_head()
    versions = [json.loads(line) for line in collect(indexed, artifact_ref).splitlines()]
    assert service.store._read_head() == before_head  # No journal churn for unchanged scans.
    version_ref = versions[1]['path']
    assert versions[1]['metadata_verified'] and not versions[1]['payload_verified']
    assert collect(indexed, version_ref) == content
    indexed_page = indexed.read_page('task', 'session', version_ref, for_provider=True)
    assert indexed_page['next_cursor']
    held = tmp_path/'late-volume'
    approved[-1].path.rename(held)
    with pytest.raises(ContainerReadError, match='incomplete'):
        broker.read_page('task', 'session', 'document', for_provider=True)
    with pytest.raises(ContainerReadError, match='changed'):
        indexed.read_page('task', 'session', version_ref, indexed_page['next_cursor'], for_provider=True)
    held.rename(approved[-1].path)
    first = broker.read_page('task', 'session', 'document', for_provider=True)
    assert first['next_cursor']
    # Execute the exact generated cron bootstrap in a fresh interpreter. This
    # exercises persisted metadata proofs without an in-process cache or model.
    import shlex
    import subprocess
    from ls.core.terminal_ops import command_scheduler as scheduler
    from ls.core.openpgp.container_schedule import schedule_id
    from ls.tests.test_shell_scheduler import FakeCrontab, SOURCE_ROOT
    cron = FakeCrontab('# unrelated\n', present=True)
    schedule_state = tmp_path/'schedule-state'
    installed = scheduler.install_schedule(schedule_id(root), source_root=SOURCE_ROOT,
        state_root=schedule_state, grant_id='fixture-indexing',
        job={'runtime_root': str(root)}, backend=cron)
    assert installed['ok']
    line = next(line for line in cron.snapshot.content.splitlines() if line.startswith('* * * * *'))
    command = shlex.split(line.split(' ', 5)[5])
    catalog_head = service.store._read_head()
    for expected in ('reconciled', 'not_due'):
        completed = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=120)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert json.loads(completed.stdout)['result']['state'] == expected
    assert service.store._read_head() == catalog_head
    assert (root/'identity.json').read_bytes() == identity
    assert not list((root/'reads').glob('.catalog-scan-*'))
    runtime.configure_runtime(root, expected_revision=config.revision, proposed=replace(config, enabled=False))
    stopped = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert json.loads(stopped.stdout)['state'] == 'job_drift'
    assert not json.loads(stopped.stdout)['dispatched']
    assert scheduler.disable_schedule(schedule_id(root), state_root=schedule_state, backend=cron)['ok']
    assert cron.snapshot.content == '# unrelated\n'
    with pytest.raises(ContainerReadError, match='unauthorized'):
        broker.read_page('task', 'session', 'document', first['next_cursor'], for_provider=True)
    assert not list((root/'reads').glob('.lssc-read-*'))
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)['state'] == 'unauthorized'
