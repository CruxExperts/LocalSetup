import json
import os
from pathlib import Path
import threading
import time

import pytest

from ls.core.agent.run_cli import _grant, _state
from ls.core.agent.run_io import Streams, safe


def test_private_grant_and_workspace_boundary(tmp_path):
    tmp_path.chmod(0o700)
    workspace=tmp_path/'project';workspace.mkdir()
    path=tmp_path/'grant.json'
    path.write_text(json.dumps({'schema_version':1,'read':['src'],'write':['src'],'disclose':[], 'recipes':{}}));path.chmod(0o600)
    value, recipes=_grant(path,workspace)
    assert value['disclose']==[] and recipes=={}
    path.chmod(0o644)
    with pytest.raises(ValueError,match='private'):_grant(path,workspace)
    path.chmod(0o600)
    with pytest.raises(ValueError,match='separate'):_grant(path,tmp_path)
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):_grant(link,workspace)

def test_openpgp_read_authority_requires_selected_private_home_and_envman_binary(tmp_path):
    from ls.core.agent.run_cli import _openpgp_authority
    from ls.core.openpgp import SecretProvider, SecretReference

    tmp_path.chmod(0o700)
    workspace = tmp_path / 'project'
    workspace.mkdir()
    home = tmp_path / 'keys'
    grant = tmp_path / 'grant.json'
    authority = {
        'home': str(home),
        'expected_signers': ['A' * 40],
        'expected_recipients': ['B' * 40],
        'trusted_fingerprints': ['A' * 40],
        'owner_fingerprint': None,
        'publisher_fingerprint': None,
        'passphrase': {'provider': 'envman', 'name': 'SELECTED_KEY',
                       'executable': '/private/bin/envman'},
    }
    value = {'schema_version': 1, 'read': ['src'], 'write': [],
             'disclose': ['src'], 'recipes': {}, 'openpgp': authority}
    grant.write_text(json.dumps(value))
    grant.chmod(0o600)
    selected, _ = _grant(grant, workspace)
    parsed = _openpgp_authority(selected['openpgp'])
    assert parsed is not None
    reference = parsed['passphrase_reference']
    assert isinstance(reference, SecretReference)
    assert reference.provider is SecretProvider.ENVMAN
    grant.write_text(json.dumps(value | {'openpgp': authority | {'home': str(workspace / 'keys')}}))
    with pytest.raises(ValueError, match='separate'):
        _grant(grant, workspace)
    grant.write_text(json.dumps(value | {
        'openpgp': authority | {'passphrase': {
            'provider': 'envman', 'name': 'SELECTED_KEY', 'executable': 'envman'}},
    }))
    with pytest.raises(ValueError, match='absolute'):
        _grant(grant, workspace)
    grant.write_text(json.dumps(value | {
        'openpgp': authority | {'passphrase': {
            'provider': 'envman', 'name': 'SELECTED_KEY',
            'executable': str(workspace / 'envman')}},
    }))
    with pytest.raises(ValueError, match='separate'):
        _grant(grant, workspace)


def test_read_cli_default_is_one_successful_page(tmp_path, capsys):
    from ls.core.agent.cli import main
    tmp_path.chmod(0o700)
    workspace = tmp_path / 'project'; workspace.mkdir()
    payload = 'x' * 9000
    (workspace / 'data.txt').write_text(payload)
    grant = tmp_path / 'grant.json'
    grant.write_text(json.dumps({'schema_version': 1, 'read': ['data.txt'],
                                'write': [], 'disclose': ['data.txt'], 'recipes': {}}))
    grant.chmod(0o600)
    args = ['read', '--workspace', str(workspace), '--grant', str(grant), '--path', 'data.txt']
    assert main(args) == 0
    captured = capsys.readouterr()
    page = json.loads(captured.out)
    assert page['content'] == payload[:8192] and page['next_cursor']
    assert 'More content remains' in captured.err
    assert main(args + ['--max-pages', '2']) == 0
    captured = capsys.readouterr()
    assert ''.join(json.loads(line)['content'] for line in captured.out.splitlines()) == payload


def test_state_preserves_custom_content_and_refuses_shared_child(tmp_path):
    tmp_path.chmod(0o700)
    root=tmp_path/'state'
    _state(root)
    custom=root/'custom';custom.write_text('retain')
    _state(root);assert custom.read_text()=='retain'
    (root/'sessions').chmod(0o755)
    with pytest.raises(ValueError,match='private'):_state(root)


def test_prompt_eof_bounds_cancellation_and_safe_rendering():
    read,write=os.pipe()
    try:
        os.write(write,b'hello\n');os.close(write);write=None
        assert Streams(time.monotonic()+1,threading.Event(),input_fd=read).prompt()=='hello\n'
    finally:
        os.close(read)
        if write is not None:os.close(write)
    cancelled=threading.Event();cancelled.set()
    with pytest.raises(InterruptedError):Streams(time.monotonic()+1,cancelled).prompt()
    assert safe('\x1b[31m\u202eevil')=='\\u001b[31m\\u202eevil'


def test_blocked_output_deadline_restores_descriptor_mode():
    read,write=os.pipe()
    try:
        os.set_blocking(write,False)
        while True:
            try:os.write(write,b'x'*4096)
            except BlockingIOError:break
        os.set_blocking(write,True)
        start=time.monotonic()
        with pytest.raises(TimeoutError):Streams(start+0.05,threading.Event(),output_fd=write).write('blocked')
        assert time.monotonic()-start<1 and os.get_blocking(write)
    finally:os.close(read);os.close(write)


@pytest.mark.parametrize('with_openpgp', [False, True])
def test_loader_named_credential_never_becomes_exec_environment(monkeypatch,tmp_path,with_openpgp):
    import argparse
    from contextlib import contextmanager
    from ls.core.agent import run_cli
    from ls.core.agent.profiles import Profile
    from ls.core.agent.run_options import arguments
    from ls.core.agent import runtime_install
    parser=argparse.ArgumentParser();arguments(parser)
    args=parser.parse_args(['--profile','coding','--grant',str(tmp_path/'grant'),'--resource-parent',str(tmp_path/'resource'),'--prompt-stdin'])
    grant=tmp_path/'grant'
    grant.write_text(json.dumps({'schema_version':1,'read':['src'],'write':[],
                                 'disclose':['src'],'recipes':{}}))
    grant.chmod(0o600)
    if with_openpgp:
        value = json.loads(grant.read_text())
        value['openpgp'] = {
            'home': str(tmp_path / 'keys'), 'expected_signers': ['A' * 40],
            'expected_recipients': ['B' * 40], 'trusted_fingerprints': ['A' * 40],
            'owner_fingerprint': None, 'publisher_fingerprint': None,
            'passphrase': {'provider': 'env', 'name': 'GCONV_PATH'},
        }
        grant.write_text(json.dumps(value))
        monkeypatch.setenv('GCONV_PATH', 'selected-passphrase')
    profile=Profile('https://example.invalid/v1/','chat_completions','fixture','LD_PRELOAD',1,frozenset())
    monkeypatch.setattr(run_cli,'load',lambda *args:profile)
    monkeypatch.setenv('LD_PRELOAD','fixture-not-a-library')
    @contextmanager
    def selected(*args,**kwargs):yield tmp_path/'release'
    monkeypatch.setattr(runtime_install,'selected',selected)
    class Captured(Exception):pass
    def execute(path,argv,environment):
        expected = {'PATH','LANG',run_cli._CREDENTIAL,run_cli._PROFILE}
        if with_openpgp:
            expected.add(run_cli._OPENPGP_SECRET)
            assert environment[run_cli._OPENPGP_SECRET] == 'selected-passphrase'
            with monkeypatch.context() as patch:
                patch.setattr(os, 'environ', environment)
                authority = run_cli._openpgp_authority(value['openpgp'], protected=True)
                assert authority['secret_resolver'].resolve(authority['passphrase_reference']) == 'selected-passphrase'
        assert set(environment)==expected
        from ls.core.agent.coding_protocol import profile_digest
        from ls.core.agent.profiles import wire
        assert environment[run_cli._PROFILE]==profile_digest(wire(profile))
        assert environment[run_cli._CREDENTIAL]=='fixture-not-a-library'
        assert argv[1:4]==['-I','-B','-m']
        raise Captured
    monkeypatch.setattr(os,'execve',execute)
    with pytest.raises(Captured):run_cli.launch([],args)


def test_full_stderr_does_not_block_terminal_failure():
    from ls.core.agent.run_cli import failure
    read,write=os.pipe();out_read,out_write=os.pipe()
    try:
        os.set_blocking(write,False)
        while True:
            try:os.write(write,b'x'*4096)
            except BlockingIOError:break
        os.set_blocking(write,True)
        start=time.monotonic()
        assert failure('jsonl',0,'timed_out',124,'deadline expired',output_fd=out_write,diagnostic_fd=write)==124
        assert time.monotonic()-start<1 and os.get_blocking(write)
        assert json.loads(os.read(out_read,4096))['data']['status']=='timed_out'
    finally:
        for fd in (read,write,out_read,out_write):os.close(fd)


def test_atomic_fresh_mode_excludes_history_options():
    import argparse
    from ls.core.agent.run_options import arguments
    parser = argparse.ArgumentParser()
    arguments(parser)
    base = ['--profile', 'fixture', '--grant', '/private/grant', '--resource-parent', '/private/resource', '--prompt-stdin']
    assert parser.parse_args(base+['--require-new-session']).require_new_session is True
    for option in ('--resume', '--recover-from'):
        with pytest.raises(SystemExit): parser.parse_args(base+['--require-new-session', option, 'a'*64])
