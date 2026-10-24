"""Selected secret sources remain private across bootstrap and static inspection."""
import json
from pathlib import Path
import sys

import pytest

from ls.core.agent import compact_cli, completion_cli, profile_credentials, run_cli
from ls.core.agent.coding_protocol import profile_digest
from ls.core.agent.profile_inventory import inventory
from ls.core.agent.profiles import load, parse, wire
from ls.core.openpgp.secrets import SecretResolutionError
from ls.tests.test_agent_provider import config
from ls.tests.test_bootstrap_profile_binding import handoff, arguments


def test_legacy_profiles_keep_wire_identity_and_explicit_env_resolution(monkeypatch):
    profile=parse(config())
    assert 'credential_source' not in wire(profile)
    monkeypatch.setenv('TASK_KEY','original')
    assert profile_credentials.resolve(profile)=='original'
    selected=parse(config(credential_source={'provider':'env','name':'OTHER_KEY'}))
    assert profile_digest(wire(selected))!=profile_digest(wire(profile))
    with pytest.raises(SecretResolutionError):profile_credentials.resolve(selected)
    monkeypatch.setenv('OTHER_KEY','selected')
    assert profile_credentials.resolve(selected)=='selected'
    assert parse(wire(selected))==selected


def test_envman_exact_argv_without_environment_secret_or_static_resolution(tmp_path,monkeypatch):
    executable=tmp_path/'envman';counter=tmp_path/'called'
    executable.write_text(f'#!{sys.executable}\n'+
        'import json,os,sys\nfrom pathlib import Path\n'+
        "assert sys.argv[1:]==['get','--json','--reveal','SOURCE_KEY']\n"+
        "assert 'TASK_KEY' not in os.environ\n"+
        f'Path({str(counter)!r}).write_text("called")\n'+
        'print(json.dumps({"variable":{"name":"SOURCE_KEY","value":"synthetic-private-token"}}))\n')
    executable.chmod(0o700)
    value=config(credential_source={'provider':'envman','name':'SOURCE_KEY','envman_binary':str(executable)})
    source=tmp_path/'profiles.json';source.write_text(json.dumps({'schema_version':1,'profiles':{'test':value}}));source.chmod(0o600)
    monkeypatch.setenv('TASK_KEY','ambient-ignored')
    report=inventory(source)
    assert not counter.exists() and 'SOURCE_KEY' not in json.dumps(report)
    assert profile_credentials.resolve(load(source,'test'))=='synthetic-private-token'
    assert counter.read_text()=='called'
    executable.write_text(f'#!{sys.executable}\nimport sys\nprint("synthetic-private-token",file=sys.stderr)\nraise SystemExit(1)\n')
    with pytest.raises(SecretResolutionError) as error:profile_credentials.resolve(load(source,'test'))
    assert 'synthetic' not in str(error.value) and str(executable) not in str(error.value)


def test_protected_file_is_selected_without_fallback_or_following_links(tmp_path,monkeypatch):
    root=tmp_path/'keys';root.mkdir(mode=0o700)
    key=root/'API_KEY';key.write_text('private-key');key.chmod(0o600)
    profile=parse(config(credential_source={'provider':'protected-file','name':'API_KEY','protected_file_root':str(root)}))
    monkeypatch.setenv('TASK_KEY','ambient')
    assert profile_credentials.resolve(profile)=='private-key'
    key.rename(root/'actual');key.symlink_to(root/'actual')
    with pytest.raises(SecretResolutionError):profile_credentials.resolve(profile)
    key.unlink()
    with pytest.raises(SecretResolutionError):profile_credentials.resolve(profile)


def test_setup_preserves_source_without_resolving_or_revealing_it(tmp_path, monkeypatch):
    from ls.core.agent import profile_setup
    from ls.core.openpgp.secrets import SecretResolver

    monkeypatch.setattr(SecretResolver, 'resolve', lambda *_: pytest.fail('setup must not resolve secrets'))
    descriptor = {'provider': 'envman', 'name': 'SOURCE_KEY', 'envman_binary': '/private/bin/envman'}
    source = tmp_path / 'input.json'
    source.write_text(json.dumps({'schema_version': 1, 'profiles': {
        'test': config(credential_source=descriptor)}}))
    target = tmp_path / 'config/profiles.json'
    plan = profile_setup.plan(source, target)
    result = profile_setup.apply(source, target, plan['sha256'])
    assert wire(load(target, 'test'))['credential_source'] == descriptor
    for report in (plan, result, inventory(target)):
        assert 'SOURCE_KEY' not in json.dumps(report)
        assert '/private/bin/envman' not in json.dumps(report)


@pytest.mark.parametrize('source',[
    None, {'provider':[],'name':'KEY'}, {'provider':'unknown','name':'KEY'},
    {'provider':'envman','name':'KEY','envman_binary':'relative'},
    {'provider':'env','name':'KEY','envman_binary':'/bin/tool'},
    {'provider':'env','name':'KEY','protected_file_root':'/private'},
    {'provider':'protected-file','name':'KEY'},
    {'provider':'protected-file','name':'../KEY','protected_file_root':'/private'},
    {'provider':'protected-file','name':'KEY','protected_file_root':'/private/../keys'},
    {'provider':'env','name':'KEY','value':'do-not-store'},
])
def test_invalid_source_declarations_are_rejected(source):
    with pytest.raises(ValueError):parse(config(credential_source=source))


@pytest.mark.parametrize('module',[run_cli,compact_cli,completion_cli])
def test_all_public_bootstraps_resolve_selected_source_once_and_pin_it(handoff,monkeypatch,module):
    root,workspace,source,value,captured,Captured=handoff
    value['credential_source']={'provider':'envman','name':'SOURCE_KEY'}
    source.write_text(json.dumps({'schema_version':1,'profiles':{'coding':value}}))
    calls=[]
    def resolve(self,reference):
        calls.append((reference.provider,reference.name));return 'selected-token'
    from ls.core.openpgp.secrets import SecretResolver
    monkeypatch.setattr(SecretResolver,'resolve',resolve)
    if module is completion_cli:
        with pytest.raises(Captured):
            module.main(['complete','--profile','coding','--profiles',str(source),
                '--runtime-root',str(root/'runtime'),'--request','-'])
        credential,binding=module.CREDENTIAL,module.PROFILE
    else:
        argv,args=arguments(module,handoff)
        with pytest.raises(Captured):module.launch(argv,args)
        credential,binding=(run_cli._CREDENTIAL,run_cli._PROFILE) if module is run_cli else (module.CREDENTIAL,module.PROFILE)
    assert len(calls)==1 and calls[0][1]=='SOURCE_KEY'
    assert captured[credential]=='selected-token'
    assert captured[binding]==profile_digest(wire(load(source,'coding')))
    assert set(captured)=={'PATH','LANG',credential,binding}
