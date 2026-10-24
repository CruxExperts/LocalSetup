"""One signed revision from captured memory in separate admitted processes."""
import multiprocessing
import socket
import threading
import time

import pytest

from ls.core import live_configuration as config
from ls.core.agent.broker_rpc import Channel
from ls.core.live_configuration_channels import METHODS, flush_connected
from ls.core.live_configuration_group import ConfigurationGroup, flush_all
from ls.core.live_configuration_participant import ConfigurationParticipant, scope
from ls.core.live_configuration_runtime import ConfigurationRuntime
from ls.tests.test_live_configuration import storage_authority, validators
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


def runtime_for(store, names=None, clock=time.monotonic):
    checks = validators()
    return ConfigurationRuntime(store, checks if names is None else {name: checks[name] for name in names},
                                actor='2'*64, machine='3'*64, operation='4'*64, clock=clock)


def initial(store):
    return store.commit({'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'}},
                        expected_digest=None, authorized=True)


def child_participant(spec, connection, output, owner, value, later=None, recovery=None):
    # The test launcher deliberately passes only this connected endpoint; no
    # discoverable server or shared filesystem proposal transport exists.
    from ls.core.openpgp.secrets import SecretReference, SecretProvider, SecretResolver
    authority = config.ConfigurationAuthority(spec['signer'], spec['recipients'], spec['home'],
        SecretReference(SecretProvider(spec['provider']), spec['name']),
        SecretResolver(env_values=spec['environment']), lambda: None)
    if spec['synthetic']:
        def open_fixture(self, serialized):
            if not serialized.startswith(b'fixture-envelope:'):
                raise config.ConfigurationError('fixture_signature_invalid')
            return serialized[len(b'fixture-envelope:'):]
        config.ConfigurationAuthority.open = open_fixture
    store = config.ConfigurationStore(spec['root'], spec['store_id'], authority)
    runtime = runtime_for(store, (owner,))
    runtime.stage(owner, value)
    participant = ConfigurationParticipant(runtime)
    channel = Channel(connection, task=scope(runtime), session='a'*64,
                      methods=METHODS, expires=time.monotonic()+180)
    try:
        try:
            for index in range(3):
                participant.serve_once(channel)
                if index == 0 and later is not None:
                    runtime.stage(owner, later)
        except ConnectionError:
            if recovery is None:
                raise
            channel.close()
            channel = Channel(recovery, task=scope(runtime), session='a'*64,
                              methods=METHODS, expires=time.monotonic()+180)
            participant.serve_once(channel)
        output.send(dict(value=runtime.value(owner), staged=runtime.staged,
                         digest=runtime.accepted.digest, pending=participant.pending is not None))
    except Exception as error:
        output.send({'error': type(error).__name__})
    finally:
        channel.close()
        output.close()


def launch(store, owner, value, later=None, *, synthetic=True, reconnect=False):
    context = multiprocessing.get_context('spawn')
    parent, child = socket.socketpair()
    incoming, outgoing = context.Pipe(duplex=False)
    recovery_parent, recovery_child = socket.socketpair() if reconnect else (None, None)
    authority = store.authority
    spec = dict(root=store.root, store_id=store.store_id, signer=authority.signer,
        recipients=authority.recipients, home=authority.home, provider=authority.reference.provider.value,
        name=authority.reference.name, environment=authority.resolver._env_values, synthetic=synthetic)
    process = context.Process(target=child_participant,
                              args=(spec, child, outgoing, owner, value, later, recovery_child))
    process.start()
    child.close(); outgoing.close()
    if recovery_child is not None:
        recovery_child.close()
    return (process, parent, incoming, recovery_parent) if reconnect else (process, parent, incoming)


def collect(process, channel, incoming):
    try:
        assert incoming.poll(10), 'participant did not acknowledge reload'
        result = incoming.recv()
        process.join(10)
        assert process.exitcode == 0 and 'error' not in result, result
        return result
    finally:
        channel.close(); incoming.close()
        if process.is_alive():
            process.terminate(); process.join(5)


def test_cli_flush_collects_two_processes_and_retains_later_edits(tmp_path, storage_authority, monkeypatch, capsys):
    from ls.core import live_configuration_cli as cli
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    first = initial(store)
    controller = runtime_for(store)
    peers = [launch(store, 'terminal', {'minimum_rows': 12}, {'minimum_rows': 14}),
             launch(store, 'containers', {'mode': 'designated_worker'})]
    monkeypatch.setattr(cli, 'bound_runtime', lambda *args, **kwargs: (controller, {}, 'b'*64, True))
    try:
        args = ['--authority-file', str(tmp_path/'authority'), 'flush-all', '--cohort', 'a'*64]
        for _process, channel, _incoming in peers:
            args.extend(('--participant-fd', str(channel.fileno())))
        assert cli.main(args, source_root=tmp_path) == 0
        capsys.readouterr()
        outcomes = [collect(*peer) for peer in peers]
        committed = store.load()
        assert committed.revision == first.revision+1 and len(signatures) == 2
        assert committed.record['values'] == {'terminal': {'minimum_rows': 12}, 'containers': {'mode': 'designated_worker'}}
        assert {item['digest'] for item in outcomes} == {committed.digest}
        assert outcomes[0]['staged'] == {'terminal': config._encode({'minimum_rows': 14})}
        assert outcomes[1]['staged'] == {} and not any(item['pending'] for item in outcomes)
    finally:
        for process, channel, incoming in peers:
            channel.close(); incoming.close()
            if process.is_alive():
                process.terminate(); process.join(5)


def test_real_signed_process_reload(tmp_path, separate_machine_env):
    env = separate_machine_env
    authority = config.ConfigurationAuthority(env.owner, (env.owner,), env.homes['controller'],
                                             SECRET_REFERENCE, env.resolver, lambda: None)
    store = config.ConfigurationStore(tmp_path/'real-store', '1'*32, authority)
    initial(store)
    controller = runtime_for(store)
    controller.stage('containers', {'mode': 'designated_worker'})
    peer = launch(store, 'terminal', {'minimum_rows': 18}, synthetic=False)
    try:
        committed = flush_connected(controller, [peer[1].fileno()], cohort='a'*64,
                                    timeout_seconds=180, authorized=True)
        result = collect(*peer)
        assert committed.revision == 2 and result['digest'] == committed.digest
        assert not controller.dirty and committed.record['values']['terminal']['minimum_rows'] == 18
    finally:
        peer[1].close(); peer[2].close()
        if peer[0].is_alive():
            peer[0].terminate(); peer[0].join(5)


def test_conflicting_memory_or_external_revision_never_partially_commits(tmp_path, storage_authority):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    first = initial(store)
    controller, other = runtime_for(store), runtime_for(store)
    controller.stage('terminal', {'minimum_rows': 12})
    other.stage('terminal', {'minimum_rows': 14})
    participant = ConfigurationParticipant(other)
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        flush_all(controller, (participant,), authorized=True, deadline=time.monotonic()+10)
    assert store.load().digest == first.digest and len(signatures) == 1
    assert controller.dirty and other.dirty and participant.pending is None
    external = store.commit(first.record['values'], expected_digest=first.digest, authorized=True)
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        flush_all(controller, (participant,), authorized=True, deadline=time.monotonic()+10)
    assert store.load().digest == external.digest and len(signatures) == 2


def test_lost_publication_ack_recovers_exact_revision_at_existing_cadence(tmp_path, storage_authority, monkeypatch):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    initial(store)
    controller, other = runtime_for(store), runtime_for(store, clock=lambda: 0)
    other.stage('terminal', {'minimum_rows': 12})
    participant = ConfigurationParticipant(other)
    original = config.write_private_file
    def lost_ack(path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path.name == 'HEAD':
            raise OSError('synthetic lost acknowledgement')
    monkeypatch.setattr(config, 'write_private_file', lost_ack)
    with pytest.raises(config.ConfigurationError, match='configuration_uncertain') as failed:
        flush_all(controller, (participant,), authorized=True, deadline=time.monotonic()+10)
    committed = store.load()
    assert committed.digest == failed.value.candidate and len(signatures) == 2
    other.stage('terminal', {'minimum_rows': 16})
    other.clock = lambda: 60
    assert other.poll(authorized=False)
    assert other.value('terminal')['minimum_rows'] == 12
    assert other.staged == {'terminal': config._encode({'minimum_rows': 16})}
    assert participant.pending is None
    capture = dict(scope=scope(controller), transaction='e'*64)
    assert controller.participant.request('capture', capture)['base'] == committed.digest
    controller.participant.request('abort', capture)
    assert len(signatures) == 2 and controller.accepted.digest == committed.digest
    # A reload receipt can be observed again after an autonomous cadence reload.
    assert participant.request('finish', dict(scope=scope(other),
        transaction=committed.record['provenance']['operation'], digest=committed.digest))['ok']


def test_group_uses_existing_cadence_and_reports_missing_reload_ack(tmp_path, storage_authority):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    initial(store)
    controller, other = runtime_for(store, clock=lambda: 0), runtime_for(store)
    other.stage('terminal', {'minimum_rows': 12})
    participant = ConfigurationParticipant(other)
    class LostReload:
        def request(self, method, data):
            result = participant.request(method, data)
            if method == 'finish':
                raise ConnectionError('synthetic lost reload acknowledgement')
            return result
    ConfigurationGroup(controller, (LostReload(),))
    assert not controller.poll(authorized=True)
    controller.clock = lambda: 60
    with pytest.raises(config.ConfigurationError, match='configuration_reload_pending') as failed:
        controller.poll(authorized=True)
    assert failed.value.candidate == store.load().digest
    assert controller.accepted.digest == other.accepted.digest and len(signatures) == 2
    assert other.participant.pending is None


def test_no_authority_or_channel_scope_mismatch_does_not_capture(tmp_path, storage_authority):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    runtime = runtime_for(store)
    participant = ConfigurationParticipant(runtime)
    with pytest.raises(config.ConfigurationError):
        flush_all(runtime, (), authorized=False, deadline=time.monotonic()+10)
    with pytest.raises(config.ConfigurationError):
        participant.request('capture', dict(transaction='a'*64, scope='b'*64))
    assert participant.pending is None and not signatures and not store.root.exists()


def test_contending_participant_is_busy_without_waiting_or_writing(tmp_path, storage_authority):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    first = initial(store)
    controller, other = runtime_for(store), runtime_for(store)
    other.stage('terminal', {'minimum_rows': 12})
    participant = ConfigurationParticipant(other)
    ready, release = threading.Event(), threading.Event()
    def hold():
        with other.lock:
            ready.set()
            release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        with pytest.raises(config.ConfigurationError, match='configuration_busy'):
            flush_all(controller, (participant,), authorized=True, deadline=time.monotonic()+10)
        assert store.load().digest == first.digest and len(signatures) == 1
        assert other.dirty and controller.participant.pending is None
    finally:
        release.set(); thread.join(5)


def test_retained_candidate_recovery_and_explicit_capture_cancellation(tmp_path, storage_authority, monkeypatch):
    from ls.core.live_configuration_channels import reconcile_channels
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    initial(store)
    controller, other = runtime_for(store), runtime_for(store)
    participant = ConfigurationParticipant(other)
    other.stage('terminal', {'minimum_rows': 12})
    original = config.write_private_file
    def before_head(path, *args, **kwargs):
        if path.name == 'HEAD':
            raise OSError('synthetic interruption before pointer publication')
        original(path, *args, **kwargs)
    monkeypatch.setattr(config, 'write_private_file', before_head)
    with pytest.raises(config.ConfigurationError, match='configuration_uncertain') as failed:
        flush_all(controller, (participant,), authorized=True, deadline=time.monotonic()+10)
    with pytest.raises(config.ConfigurationError, match='configuration_pending'):
        participant.request('abort', dict(scope=scope(other), transaction=failed.value.transaction))
    monkeypatch.setattr(config, 'write_private_file', original)
    recovered = store.recover(candidate_digest=failed.value.candidate, authorized=True)
    controller.reload()
    reconcile_channels(controller, (participant,), snapshot=recovered)
    assert len(signatures) == 2 and not other.dirty and participant.pending is None
    other.stage('terminal', {'minimum_rows': 14})
    request = dict(scope=scope(other), transaction='f'*64)
    participant.request('capture', request)
    participant.request('abort', request)
    assert participant.pending is None and other.dirty and len(signatures) == 2


def test_cli_recovers_retained_bytes_and_reloads_reconnected_process(tmp_path, storage_authority, monkeypatch, capsys):
    from ls.core import live_configuration_cli as cli
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    initial(store)
    controller = runtime_for(store)
    process, channel, incoming, recovery = launch(store, 'terminal', {'minimum_rows': 20}, reconnect=True)
    original = config.write_private_file
    def before_head(path, *args, **kwargs):
        if path.name == 'HEAD':
            raise OSError('synthetic lost controller publication')
        original(path, *args, **kwargs)
    try:
        with monkeypatch.context() as failure:
            failure.setattr(config, 'write_private_file', before_head)
            with pytest.raises(config.ConfigurationError, match='configuration_uncertain') as failed:
                flush_connected(controller, [channel.fileno()], cohort='a'*64,
                                timeout_seconds=60, authorized=True)
        channel.close()
        monkeypatch.setattr(cli, 'bound_runtime', lambda *args, **kwargs: (controller, {}, 'b'*64, True))
        assert cli.main(['--authority-file', str(tmp_path/'authority'), 'recover',
            '--candidate', failed.value.candidate, '--cohort', 'a'*64,
            '--participant-fd', str(recovery.fileno())], source_root=tmp_path) == 0
        capsys.readouterr()
        result = collect(process, recovery, incoming)
        assert result['digest'] == failed.value.candidate and not result['pending']
        assert result['value']['minimum_rows'] == 20 and len(signatures) == 2
    finally:
        channel.close(); recovery.close(); incoming.close()
        if process.is_alive():
            process.terminate(); process.join(5)
