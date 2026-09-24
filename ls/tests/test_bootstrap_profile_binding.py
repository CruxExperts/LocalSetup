import argparse
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core.agent import run_cli, compact_cli, runtime_install
from ls.core.agent.profiles import load, wire
from ls.core.agent.coding_protocol import profile_digest


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    workspace = tmp_path/'workspace'; workspace.mkdir()
    source = tmp_path/'profiles.json'
    value = dict(base_url='https://first.invalid/v1/', api='chat_completions', model='fixture',
                 credential_env='KEY_A', timeout_seconds=10, capabilities=['tools','streaming'], allow_loopback_http=False)
    source.write_text(json.dumps(dict(schema_version=1, profiles={'coding':value})));source.chmod(0o600)
    grant = tmp_path/'grant'
    grant.write_text(json.dumps(dict(schema_version=1, read=[], write=[], disclose=[], recipes={})))
    grant.chmod(0o600)
    monkeypatch.setenv('KEY_A','fixture-first-key')
    @contextmanager
    def selected(*args, **kwargs):yield tmp_path/'release'
    monkeypatch.setattr(runtime_install,'selected',selected)
    captured = {}
    class Captured(Exception):pass
    def execve(path, argv, environment):
        captured.update(environment)
        raise Captured
    monkeypatch.setattr(run_cli.os,'execve',execve)
    return tmp_path, workspace, source, value, captured, Captured


def arguments(module, handoff):
    root, workspace, source, *_ = handoff
    argv = ['--profile','coding','--profiles',str(source),'--workspace',str(workspace),
            '--runtime-root',str(root/'runtime'),'--state-root',str(root/'state')]
    argv += (['--grant',str(root/'grant'),'--resource-parent',str(root/'resource'),'--prompt-stdin']
             if module is run_cli else ['--task','task','--session','session','--checkpoint','a'*64,'--disclose-history'])
    parser=argparse.ArgumentParser();module.arguments(parser)
    return argv, module.defaults(parser.parse_args(argv))


@pytest.mark.parametrize('module',[run_cli,compact_cli])
@pytest.mark.parametrize('change',['endpoint','credential','model','missing','unchanged'])
def test_profile_handoff_is_bound_before_input_or_state(handoff, monkeypatch, module, change):
    root, workspace, source, value, captured, Captured = handoff
    argv,args = arguments(module,handoff)
    before=load(source,'coding')
    with pytest.raises(Captured):module.launch(argv,args)
    credential = run_cli._CREDENTIAL if module is run_cli else compact_cli.CREDENTIAL
    binding = run_cli._PROFILE if module is run_cli else compact_cli.PROFILE
    assert captured[credential]=='fixture-first-key'
    assert captured[binding]==profile_digest(wire(before))
    assert set(captured)=={'PATH','LANG',credential,binding}
    if change=='endpoint':value['base_url']='https://second.invalid/v1/'
    elif change=='credential':value['credential_env']='KEY_B'
    elif change=='model':value['model']='different'
    source.write_text(json.dumps(dict(schema_version=1,profiles={'coding':value})))
    for key,item in captured.items():monkeypatch.setenv(key,item)
    if change=='missing':monkeypatch.delenv(binding)
    reached=[]
    class Boundary(Exception):pass
    def boundary(*args,**kwargs):reached.append(True);raise Boundary
    if module is run_cli:
        monkeypatch.setattr(run_cli,'_grant',boundary)
        if change=='unchanged':
            with pytest.raises(Boundary):run_cli.execute(args,None,None)
        else:
            with pytest.raises(ValueError,match='profile changed'):run_cli.execute(args,None,None)
    else:
        from ls.core.agent import session_owner
        monkeypatch.setattr(session_owner,'lease',boundary)
        monkeypatch.setattr(compact_cli,'sys',SimpleNamespace(flags=SimpleNamespace(isolated=1),
            dont_write_bytecode=True,prefix=str(Path(compact_cli.__file__).resolve().parents[3])))
        monkeypatch.setattr(run_cli,'failure',lambda *args,**kwargs:args[3])
        if change=='unchanged':
            with pytest.raises(Boundary):compact_cli.main(argv)
        else:assert compact_cli.main(argv)==2
    assert bool(reached)==(change=='unchanged')
    assert not (root/'state').exists()
