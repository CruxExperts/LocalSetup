"""Ordinary authorized file requests consume verified container contents."""
from dataclasses import replace
import base64
import json
from pathlib import Path
import shutil
import sqlite3
import time

import pytest

from ls.core.agent import file_broker
from ls.core.agent.file_broker import FileBroker
from ls.core.agent.file_grants import FileGrant
from ls.core.agent.session_owner import lease
from ls.core.openpgp import artifacts
from ls.core.openpgp import container_reader
from ls.core.openpgp.artifact_records import ArtifactProvenance
from ls.core.openpgp.container_packing import PackingLimits
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits
from ls.core.openpgp.container_reader import ContainerReader, ContainerReadError
from ls.core.openpgp.container_sources import LocalFileRangeSource, SourceChangedError
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


def prepare(env, root, contents, *, limits=ContainerPreparationLimits()):
    output = root / 'carriers'
    output.mkdir(mode=0o700)
    inputs = []
    for index, data in enumerate(contents):
        path = root / f'input-{index}'
        path.write_bytes(data)
        inputs.append(ContainerInput(path, f'{index+1:032x}',
                                     ArtifactProvenance(original_name=f'document-{index}')))
    prepared = artifacts.prepare_container(tuple(inputs), output_dir=output,
        namespace=ContainerNamespace('tenant', 'project', 'a'*32),
        policy=env.policies['origin'], trust_state_path=env.paths['origin'],
        gnupg_home=env.homes['origin'], passphrase_reference=SECRET_REFERENCE,
        secret_resolver=env.resolver, limits=limits)
    approved = []
    for volume in prepared.volumes:
        with LocalFileRangeSource(volume.path) as source:
            approved.append(artifacts.approve_container_distribution(source, output_dir=output,
                policy=env.policies['controller'], trust_state_path=env.paths['controller'],
                gnupg_home=env.homes['controller'], passphrase_reference=SECRET_REFERENCE,
                secret_resolver=env.resolver))
    return approved


def broker_for(env, root, approved, **reader_options):
    workspace = root / 'workspace'
    workspace.mkdir(mode=0o700)
    shutil.copyfile(approved[0].path, workspace / 'document')
    target_leases = root / 'target-leases'
    target_leases.mkdir(mode=0o700)
    grant = FileGrant('task', 'session', workspace, ('document',), (),
                      ('document',), time.monotonic()+300)
    reader = ContainerReader(env.policies['receiver'], env.paths['receiver'], env.homes['receiver'],
        SECRET_REFERENCE, env.resolver, tuple(volume.path for volume in reversed(approved)),
        **reader_options)
    return FileBroker(grant, target_leases, container_reader=reader)


def collect(broker, name, *, first=None):
    cursor, data = None, bytearray()
    while True:
        page = first if first is not None else broker.read_page('task', 'session', name, cursor, for_provider=True)
        first = None
        block = page['content'].encode() if page['encoding']=='utf-8' else base64.b64decode(page['content'])
        assert len(block) == page['bytes'] <= 8192
        assert len(json.dumps(page, ensure_ascii=True).encode()) <= 16384
        data.extend(block)
        cursor = page['next_cursor']
        if cursor is None:
            return bytes(data)


def test_member_index_and_normal_reads_retain_grants_and_live_authority(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    contents = [('reading 🗝 '*2000).encode(), b'\xff\x00binary']
    approved = prepare(env, tmp_path, contents)
    active = [True]
    generation = ['a'*64]
    def binding():
        if not active[0]:
            raise PermissionError('runtime disabled')
        return generation[0]
    broker = broker_for(env, tmp_path, approved, binding_check=binding)
    boundary = Path(env.policies['receiver'].boundary)
    with pytest.raises(ValueError, match='separate'):
        FileBroker(broker.grant, broker.lease_root, container_reader=replace(broker.container_reader,
            policy=replace(broker.container_reader.policy, boundary=broker.grant.root/'cache')))
    assert not (broker.grant.root/'cache').exists()
    denied = FileBroker(replace(broker.grant, disclose=()), broker.lease_root,
                        container_reader=broker.container_reader)
    with pytest.raises(PermissionError, match='disclosure'):
        denied.read_page('task', 'session', 'document', for_provider=True)
    assert not list(boundary.glob('.lssc-read-*'))
    with monkeypatch.context() as patch:
        patch.setattr(artifacts, '_copy_selected_file', lambda *_a, **_k: pytest.fail('index read decrypted payload'))
        index = [json.loads(line) for line in collect(broker, 'document').splitlines()]
    assert index[0]['metadata_verified'] and not index[0]['payload_verified']
    refs = [item['path'] for item in index[1:]]
    assert refs == ['document/@files/'+f'{index:032x}' for index in (1, 2)]
    assert [item['name'] for item in index[1:]] == ['document-0', 'document-1']
    assert collect(broker, refs[0]) == contents[0]
    assert collect(broker, refs[1]) == contents[1]
    assert not list(boundary.glob('.lssc-read-*'))
    ordinary = broker.grant.root / 'ordinary/@files' / ('a'*32)
    ordinary.parent.mkdir(parents=True)
    ordinary.write_bytes(b'ordinary file')
    normal = FileBroker(replace(broker.grant, read=('ordinary',), disclose=('ordinary',)),
                        broker.lease_root, container_reader=broker.container_reader)
    assert collect(normal, 'ordinary/@files/'+('a'*32)) == b'ordinary file'

    # A selected member grant does not permit the container index or other member.
    limited = FileBroker(replace(broker.grant, read=(refs[0],), disclose=(refs[0],)),
                         broker.lease_root, container_reader=broker.container_reader)
    assert collect(limited, refs[0]) == contents[0]
    for name in ('document', refs[1]):
        with pytest.raises(PermissionError):
            limited.read_page('task', 'session', name, for_provider=True)

    first = broker.read_page('task', 'session', refs[0], for_provider=True)
    active[0] = False
    with pytest.raises(ContainerReadError, match='unauthorized'):
        broker.read_page('task', 'session', refs[0], first['next_cursor'], for_provider=True)
    assert broker._container_page_cache is None and not list(boundary.glob('.lssc-read-*'))
    active[0] = True
    first = broker.read_page('task', 'session', refs[0], for_provider=True)
    generation[0] = 'b'*64
    with pytest.raises(ContainerReadError, match='changed'):
        broker.read_page('task', 'session', refs[0], first['next_cursor'], for_provider=True)
    assert not list(boundary.glob('.lssc-read-*'))
    inspect_output = container_reader._inspect_output
    def change_during_preparation(*args):
        result = inspect_output(*args)
        generation[0] = 'c'*64
        return result
    with monkeypatch.context() as patch:
        patch.setattr(container_reader, '_inspect_output', change_during_preparation)
        with pytest.raises(ContainerReadError, match='changed'):
            broker.read_page('task', 'session', refs[0], for_provider=True)
    assert not list(boundary.glob('.lssc-read-*'))
    first = broker.read_page('task', 'session', refs[0], for_provider=True)
    broker.read_page('task', 'session', refs[0], first['next_cursor'], for_provider=True)
    with pytest.raises(ContainerReadError, match='changed'):
        broker.read_page('task', 'session', refs[0], first['next_cursor'], for_provider=True)
    first = broker.read_page('task', 'session', refs[0], for_provider=True)
    cache_path = broker._container_page_cache.opened.opened.path
    cache_path.write_bytes(b'tampered')
    with pytest.raises(ContainerReadError, match='changed'):
        broker.read_page('task', 'session', refs[0], first['next_cursor'], for_provider=True)
    assert not cache_path.exists()

    sessions = tmp_path / 'sessions'
    sessions.mkdir(mode=0o700)
    with lease(sessions, task='task', session='session', workspace=broker.grant.root,
               expires=broker.grant.expires) as owner:
        first = owner.read_page(broker, refs[0], for_provider=True)
        assert first['next_cursor'] and owner._bound_broker.container_reader is broker.container_reader
    assert not list(boundary.glob('.lssc-read-*'))
    assert broker.read('task', 'session', refs[1], for_provider=True) == contents[1]
    first = broker.read_page('task', 'session', refs[0], for_provider=True)
    with sqlite3.connect(env.paths['receiver']) as database:
        database.execute('UPDATE state SET revision=revision+1 WHERE singleton=1')
    with pytest.raises(ContainerReadError, match='unauthorized'):
        broker.read_page('task', 'session', refs[0], first['next_cursor'], for_provider=True)
    assert not list(boundary.glob('.lssc-read-*'))


def test_normal_single_file_read_reassembles_selected_sources_before_legacy_cap(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    content = bytes(range(256))*120
    approved = prepare(env, tmp_path, (content,), limits=ContainerPreparationLimits(
        packing_limits=PackingLimits(target_bytes=25000, tolerance=.1, max_split_iterations=12, max_measure_calls=200)))
    assert len(approved)>1
    broker = broker_for(env, tmp_path, approved)
    monkeypatch.setattr(file_broker, 'MAX_FILE', 10)
    assert collect(broker, 'document') == content
    no_sources = replace(broker.container_reader, source_paths=())
    missing = FileBroker(broker.grant, broker.lease_root, container_reader=no_sources)
    with pytest.raises(ContainerReadError, match='incomplete'):
        missing.read_page('task', 'session', 'document', for_provider=True)
    assert not list(Path(env.policies['receiver'].boundary).glob('.lssc-read-*'))


def test_range_source_requires_same_opened_inode(tmp_path):
    path = tmp_path / 'carrier'
    path.write_bytes(b'original')
    before = path.stat()
    path.rename(tmp_path / 'old')
    path.write_bytes(b'replacement')
    with pytest.raises(SourceChangedError, match='authorized opened file'):
        LocalFileRangeSource(path, expected_stat=before)
