"""Persisted codec configuration supports ordinary reads without model procedures."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import time

import pytest

from ls.core.agent.cli import main
from ls.core.agent.container_authority import reader
from ls.core.agent.file_broker import FileBroker
from ls.core.agent.file_grants import FileGrant
from ls.core.openpgp import artifacts, container_runtime as runtime, keys, trust_state
from ls.core.openpgp.container_delta_codec import create_patch
from ls.core.openpgp.container_delta_records import FileDelta
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits, _Budget
from ls.core.openpgp.container_reader import ContainerReadError
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.tests.openpgp_key_helpers import _gpg, PASSPHRASE
from ls.tests.test_agent_container_reads import prepare, collect
from ls.tests.test_container_delta_codec import codec, evidence, private
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, SECRET_NAME, separate_machine_env


def test_configured_cli_and_indexed_delta_reads_keep_dependency_guards(
        tmp_path, codec, separate_machine_env, monkeypatch, capsys):
    env = separate_machine_env
    original = b'original content\n'*3000
    content = original[:300]+b'updated content'+original[310:]
    base = prepare(env, tmp_path, (original,))[0]
    common = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    with LocalFileRangeSource(base.path) as source:
        metadata = artifacts.inspect_container_metadata(source, **common)
    patch_path = tmp_path/'patch'
    claim = create_patch(private(tmp_path/'old', original), private(tmp_path/'new', content), patch_path,
        base=evidence(original), result=evidence(content), policy=codec, budget=_Budget(ContainerPreparationLimits()))
    assert claim is not None
    file_id = metadata.logical['files'][0]['file_id']
    delta = FileDelta(metadata.logical['version_id'], metadata.logical_commitment_sha256,
        file_id, evidence(original), claim, evidence(content), '8'*32)
    prepared = artifacts.prepare_container((ContainerInput(patch_path, file_id, delta=delta),),
        output_dir=tmp_path/'carriers', namespace=ContainerNamespace('tenant', 'project', 'a'*32),
        policy=env.policies['origin'], trust_state_path=env.paths['origin'], gnupg_home=env.homes['origin'],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        artifact_id=metadata.logical['artifact_id'], parent_versions=(delta.base_version,))
    with LocalFileRangeSource(prepared.volumes[0].path) as source:
        approved = artifacts.approve_container_distribution(source, output_dir=tmp_path/'carriers',
            policy=env.policies['controller'], trust_state_path=env.paths['controller'],
            gnupg_home=env.homes['controller'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)

    workspace = tmp_path/'work'; workspace.mkdir()
    shutil.copyfile(approved.path, workspace/'document')
    root = tmp_path/('f'*32)
    exact = env.policies['receiver']
    fields = asdict(exact)
    for key in ('content_revision', 'created_at', 'expires_at'):
        fields.pop(key)
    discovery = artifacts.ArtifactDiscoveryPolicy(**(fields | {
        'boundary': root/'reads', 'admit_from': exact.created_at-1, 'admit_until': exact.expires_at,
        'max_content_age_seconds': 3600, 'max_content_lifetime_seconds': 3600}))
    def selection(role, fingerprint):
        return runtime.KeySelection(fingerprint, _gpg(env.homes[role], '--armor', '--export', fingerprint),
                                    env.homes[role], SECRET_REFERENCE)
    monkeypatch.setenv(SECRET_NAME, PASSPHRASE)
    monkeypatch.setattr(trust_state, 'inspect_key', keys.inspect_key)
    config = runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(
        root.name, enabled=True, custom_publisher=selection('receiver', env.recipient),
        custom_owner=selection('controller', env.owner),
        remote_content=runtime.RemoteContentTrust(env.paths['receiver'], exact.trust_store_id, exact.scope),
        source_ids=('e'*32,), source_directories=(tmp_path/'carriers',), read_policy=discovery,
        delta_codec=codec, max_delta_depth=4))
    assert runtime.load_runtime_config(root) == config
    assert runtime.setup_identity(root).state == 'ready'
    identity = (root/'identity.json').read_bytes()
    grant_path = private(tmp_path/'grant.json', json.dumps({'schema_version': 1, 'read': ['document'],
        'write': [], 'disclose': ['document'], 'recipes': {}, 'container_runtime': str(root)}).encode())
    assert main(['read', '--workspace', str(workspace), '--grant', str(grant_path),
                 '--path', 'document', '--max-pages', '32']) == 0
    captured = capsys.readouterr()
    pages = [json.loads(line) for line in captured.out.splitlines()]
    assert pages[-1]['next_cursor'] is None and not captured.err
    assert ''.join(page['content'] for page in pages).encode() == content
    assert (root/'identity.json').read_bytes() == identity

    leases = tmp_path/'leases'; leases.mkdir(mode=0o700)
    expires = time.monotonic()+300
    grant = FileGrant('task', 'session', workspace, ('document', '@artifacts'), (),
                      ('document', '@artifacts'), expires)
    configured_reader = reader(str(root), workspace, deadline=expires)
    broker = FileBroker(grant, leases, container_reader=configured_reader)
    reference = f'@artifacts/{prepared.artifact_id}/{prepared.version_id}'
    assert collect(broker, reference) == content
    service = configured_reader.reference_resolver.__self__
    collision = service._collision
    def conflicting_base(result, artifact, version, commitment=None):
        if version == delta.base_version:
            raise ContainerReadError('conflict')
        return collision(result, artifact, version, commitment)
    with monkeypatch.context() as patch:
        patch.setattr(service, '_collision', conflicting_base)
        with pytest.raises(ContainerReadError, match='conflict'):
            collect(broker, reference)
    assert not list((root/'reads').glob('.lssc-read-*'))

    page = broker.read_page('task', 'session', 'document', for_provider=True)
    admission = artifacts._policy_logical
    def stale_base(logical, *args, **kwargs):
        if logical['version_id'] == delta.base_version:
            raise artifacts.ArtifactError(artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED)
        return admission(logical, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(artifacts, '_policy_logical', stale_base)
        with pytest.raises(ContainerReadError, match='unauthorized'):
            broker.read_page('task', 'session', 'document', page['next_cursor'], for_provider=True)

    page = broker.read_page('task', 'session', 'document', for_provider=True)
    assert page['next_cursor']
    held = tmp_path/'held-base'
    base.path.rename(held)
    with pytest.raises(ContainerReadError, match='changed'):
        broker.read_page('task', 'session', 'document', page['next_cursor'], for_provider=True)
    assert not list((root/'reads').glob('.lssc-read-*'))
    held.rename(base.path)

    page = broker.read_page('task', 'session', 'document', for_provider=True)
    config = runtime.configure_runtime(root, expected_revision=config.revision,
                                       proposed=replace(config, delta_codec=None))
    # The existing runtime binding revokes old readers on any config revision.
    with pytest.raises(ContainerReadError, match='unauthorized'):
        broker.read_page('task', 'session', 'document', page['next_cursor'], for_provider=True)
    disabled_codec = FileBroker(grant, leases, container_reader=reader(str(root), workspace, deadline=expires))
    with pytest.raises(ContainerReadError, match='unsupported'):
        collect(disabled_codec, 'document')
    assert not list((root/'reads').glob('.lssc-read-*'))
