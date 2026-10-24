"""Editable sets preserve concurrent edits and use pinned CLI authority."""
import json
from pathlib import Path

import pytest

from ls.core import cli, live_configuration as config
from ls.core.live_configuration_files import export_edit, flush_edit, _pretty
from ls.core.live_configuration_runtime import ConfigurationRuntime
from ls.core.openpgp.container_storage import write_private_file
from ls.tests.test_live_configuration import storage_authority, validators
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


def runtime_for(tmp_path, authority):
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    return ConfigurationRuntime(store, validators(), actor='2'*64, machine='3'*64, operation='4'*64)


def test_edit_set_repeated_flush_and_external_revision_conflict(tmp_path, storage_authority):
    authority, signatures = storage_authority
    runtime = runtime_for(tmp_path, authority)
    defaults = {'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'}}
    directory = export_edit(runtime, tmp_path/'edit', binding_digest='5'*64, defaults=defaults)
    source = directory/'owners/terminal.json'
    write_private_file(source, _pretty({'minimum_rows': 12}), replace=True)
    write_private_file(directory/'cadence.json', _pretty({'seconds': 1}), replace=True)
    first = flush_edit(runtime, directory, binding_digest='5'*64, authorized=True)
    assert first.revision == 1 and first.record['values']['terminal']['minimum_rows'] == 12
    assert runtime.cadence == 1 and len(signatures) == 1
    assert flush_edit(runtime, directory, binding_digest='5'*64, authorized=True).digest == first.digest
    assert len(signatures) == 1 and json.loads(source.read_text())['minimum_rows'] == 12
    stale = runtime_for(tmp_path, authority)
    write_private_file(source, _pretty({'minimum_rows': 14}), replace=True)
    second = flush_edit(runtime, directory, binding_digest='5'*64, authorized=True)
    assert second.revision == 2 and len(signatures) == 2
    assert flush_edit(stale, directory, binding_digest='5'*64, authorized=True).digest == second.digest
    assert stale.value('terminal')['minimum_rows'] == 14 and len(signatures) == 2
    other = runtime.store.commit(dict(defaults, terminal={'minimum_rows': 16}),
        expected_digest=second.digest, authorized=True)
    write_private_file(source, _pretty({'minimum_rows': 18}), replace=True)
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        flush_edit(runtime, directory, binding_digest='5'*64, authorized=True)
    assert runtime.store.load().digest == other.digest and len(signatures) == 3
    assert json.loads(source.read_text())['minimum_rows'] == 18
    with pytest.raises(config.ConfigurationError, match='configuration_edit_exists'):
        export_edit(runtime, directory, binding_digest='5'*64, defaults=defaults)


def test_edit_during_signing_never_overwrites_new_input_or_publishes_head(tmp_path, storage_authority, monkeypatch):
    authority, signatures = storage_authority
    runtime = runtime_for(tmp_path, authority)
    defaults = {'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'}}
    directory = export_edit(runtime, tmp_path/'edit', binding_digest='5'*64, defaults=defaults)
    source = directory/'owners/terminal.json'
    original = config.ConfigurationAuthority.seal
    def changed(self, payload):
        result = original(self, payload)
        write_private_file(source, _pretty({'minimum_rows': 19}), replace=True)
        return result
    monkeypatch.setattr(config.ConfigurationAuthority, 'seal', changed)
    with pytest.raises(config.ConfigurationError, match='configuration_uncertain'):
        flush_edit(runtime, directory, binding_digest='5'*64, authorized=True)
    assert runtime.store.load() is None and len(signatures) == 1
    assert json.loads(source.read_text()) == {'minimum_rows': 19}


@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'symlink'])
def test_invalid_edit_files_stop_before_signing(tmp_path, storage_authority, failure):
    authority, signatures = storage_authority
    runtime = runtime_for(tmp_path, authority)
    defaults = {'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'}}
    directory = export_edit(runtime, tmp_path/'edit', binding_digest='5'*64, defaults=defaults)
    source = directory/'owners/terminal.json'
    if failure == 'duplicate':
        write_private_file(source, b'{"minimum_rows":10,"minimum_rows":15}', replace=True)
    else:
        source.unlink()
        if failure == 'symlink':
            source.symlink_to(directory/'cadence.json')
    expected = 'configuration_edit_incomplete' if failure == 'missing' else 'configuration_edit_invalid'
    with pytest.raises(config.ConfigurationError, match=expected):
        flush_edit(runtime, directory, binding_digest='5'*64, authorized=True)
    assert not signatures and runtime.store.load() is None


def test_private_cli_binding_real_signatures_and_read_only_authority(tmp_path, separate_machine_env, monkeypatch, capsys):
    env = separate_machine_env
    state = env.snapshots['controller']
    binding = dict(schema='localsetup.configuration-authority', version=1,
        root=str(tmp_path/'configuration'), store_id='6'*32, signer=env.owner,
        recipients=[env.owner], gnupg_home=str(env.homes['controller']), write=True,
        secret=dict(provider='env', name=SECRET_REFERENCE.name, protected_file_root=None),
        trust=dict(path=str(env.paths['controller']), store_id=state.store_id,
                   revision=state.revision, epoch=state.epoch, scope=list(state.scope)),
        owners={'terminal': {'kind': 'application-profile', 'application': 'tmux', 'name': 'agent-shell'},
                'containers': {'kind': 'container-runtime', 'runtime_id': '7'*32}})
    path = tmp_path/'authority.json'
    write_private_file(path, _pretty(binding), replace=False)
    monkeypatch.setenv(SECRET_REFERENCE.name, env.resolver.resolve(SECRET_REFERENCE))
    root = Path(__file__).resolve().parents[2]
    prefix = ['--source-root', str(root), '--home', str(tmp_path/'home'),
              'config', '--authority-file', str(path)]
    def invoke(*args):
        code = cli.main(prefix+list(args))
        out = capsys.readouterr().out
        assert 'history_limit' not in out and SECRET_REFERENCE.name not in out
        return code, json.loads(out)
    code, result = invoke('status')
    assert code == 0 and not result['configured']
    directory = tmp_path/'working-set'
    code, result = invoke('edit', '--directory', str(directory))
    assert code == 0 and result['edit_directory'] == str(directory)
    profile = json.loads((directory/'owners/terminal.json').read_text())
    profile['history_limit'] = 25000
    write_private_file(directory/'owners/terminal.json', _pretty(profile), replace=True)
    code, result = invoke('flush', '--directory', str(directory))
    assert code == 0 and result['revision'] == 1 and result['owner_count'] == 2
    code, same = invoke('flush', '--directory', str(directory))
    assert code == 0 and same['digest'] == result['digest'] and same['revision'] == 1
    binding['write'] = False
    write_private_file(path, _pretty(binding), replace=True)
    code, denied = invoke('flush', '--directory', str(directory))
    assert code == 2 and denied['code'] == 'configuration_unauthorized'
    binding['write'] = True
    binding['trust']['revision'] += 1
    write_private_file(path, _pretty(binding), replace=True)
    code, denied = invoke('status')
    assert code == 2 and denied['code'] == 'configuration_unauthorized'
