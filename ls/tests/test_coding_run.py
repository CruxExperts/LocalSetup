from dataclasses import replace
from pathlib import Path
import time

import pytest
from ls.core.agent import coding_run as coding
from ls.core.agent.file_grants import FileGrant
from ls.core.agent.resource_group import Limits
from ls.tests.test_coding_protocol import payload


@pytest.fixture
def inputs(tmp_path):
    directories=[tmp_path/name for name in ('runtime','sessions','leases','snapshots','scratch','workspace')]
    for p in directories:p.mkdir(mode=0o700)
    paths=coding.RunPaths(*directories[:5],Path('/sys/fs/cgroup/explicit'))
    files=FileGrant('task','session',directories[-1],('src',),('src',),('src',),time.monotonic()+5)
    value=payload();authority=coding.CodingGrant('task','session',coding.disclosure_digest(value),time.monotonic()+5)
    return paths,files,value,authority


def test_context_digest_binds_profile_context_and_limits_but_not_credential():
    value=payload();digest=coding.disclosure_digest(value)
    assert coding.disclosure_digest(value|{'credential':'rotated'})==digest
    for field,item in [('prompt','Different request'),('instructions','Different instructions'),('request_limit',4)]:
        assert coding.disclosure_digest(value|{field:item})!=digest


def test_disclosure_and_identity_refused_before_preflight(inputs,monkeypatch):
    paths,files,value,authority=inputs
    monkeypatch.setattr(coding,'qualified_tools',lambda *a,**k:pytest.fail('must refuse before preflight'))
    for packet,grant in ((value|{'prompt':'unapproved'},files),(value,replace(files,session='other'))):
        with pytest.raises(PermissionError):coding.run_coding(paths,packet,authority,grant,{},limits=Limits(),on_event=lambda x:None)


def test_layout_collision_refused_before_preflight(inputs,monkeypatch):
    paths,files,value,authority=inputs
    monkeypatch.setattr(coding,'qualified_tools',lambda *a,**k:pytest.fail('must refuse before preflight'))
    with pytest.raises(ValueError,match='separate'):
        coding.run_coding(replace(paths,scratch=files.root),value,authority,files,{},limits=Limits(),on_event=lambda x:None)


def test_revoked_file_grant_never_dispatches(inputs,monkeypatch):
    paths,files,value,authority=inputs;files.revoked.set()
    monkeypatch.setattr(coding,'qualified_tools',lambda *a,**k:pytest.fail('must not preflight'))
    result=coding.run_coding(paths,value,authority,files,{},limits=Limits(),on_event=lambda x:None)
    assert result.status=='cancelled' and result.data is None


def test_uncertain_session_blocks_worker_before_provider(inputs,monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from ls.core.agent.session_owner import lease
    paths,files,value,authority=inputs
    with lease(paths.sessions,task='task',session='session',workspace=files.root,expires=files.expires) as owner:
        owner._journal.begin('process',{'argv_sha256':'a'*64,'snapshot_sha256':'b'*64})
    @contextmanager
    def qualified(*a,**k):yield SimpleNamespace(release=paths.runtimes/'release')
    monkeypatch.setattr(coding,'qualified_tools',qualified)
    monkeypatch.setattr(coding,'supervise',lambda *a,**k:pytest.fail('must not launch worker'))
    with pytest.raises(PermissionError,match='reconciliation'):
        coding.run_coding(paths,value,authority,files,{},limits=Limits(),on_event=lambda x:None)


def test_retained_request_and_session_then_runtime_order(inputs,monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    paths,files,value,authority=inputs;order=[];original_lease=coding.lease
    class Stop(Exception):pass
    def check(digest):
        authority.check(digest)
        value['prompt']='changed by caller'
    wrapper=SimpleNamespace(task=authority.task,session=authority.session,expires=authority.expires,
                            revoked=authority.revoked,check=check)
    @contextmanager
    def owner(*args,**kwargs):
        with original_lease(*args,**kwargs) as held:
            order.append('session_enter')
            try:yield held
            finally:order.append('session_exit')
    @contextmanager
    def qualified(*a,**k):
        assert order==['session_enter'];order.append('runtime_enter')
        try:yield SimpleNamespace(release=paths.runtimes/'release')
        finally:order.append('runtime_exit')
    def handler(tools,packet,*args,**kwargs):
        assert packet['prompt']=='Edit fixture' and value['prompt']=='changed by caller'
        raise Stop()
    @contextmanager
    def control_scope(held, run_id):
        held._check()
        assert run_id == value['run_id']
        order.append('control_enter')
        try:
            yield
        finally:
            held._check()
            order.append('control_exit')
    monkeypatch.setattr(coding,'lease',owner);monkeypatch.setattr(coding,'qualified_tools',qualified)
    monkeypatch.setattr(coding,'CodingHandler',handler)
    from ls.core.agent.process_rpc import Recipe
    with pytest.raises(Stop):
        coding.run_coding(paths,value,wrapper,files,{'test':Recipe(('/usr/bin/true',),('src/a',),1)},limits=Limits(),on_event=lambda x:None,
                         control_scope=control_scope)
    assert order==['session_enter','runtime_enter','control_enter','control_exit','runtime_exit','session_exit']


def test_reselected_runtime_cannot_dispatch_protected_cli_worker(inputs,monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    paths,files,value,authority=inputs
    @contextmanager
    def qualified(*args,**kwargs):yield SimpleNamespace(release=paths.runtimes/'new-release')
    monkeypatch.setattr(coding,'qualified_tools',qualified)
    monkeypatch.setattr(coding,'supervise',lambda *args,**kwargs:pytest.fail('must not launch worker'))
    with pytest.raises(PermissionError,match='Selected runtime changed'):
        coding.run_coding(paths,value,authority,files,{},limits=Limits(),on_event=lambda event:None,
                          expected_release=paths.runtimes/'old-release',
                          control_scope=lambda *_: pytest.fail('must not admit control before release match'))


def test_atomic_fresh_session_refuses_concurrent_creator_at_child_lease(inputs, monkeypatch):
    paths, files, value, authority = inputs
    real_lease = coding.lease
    from contextlib import contextmanager
    @contextmanager
    def raced(*args, **kwargs):
        assert kwargs['new'] is True
        with real_lease(paths.sessions, task=files.task, session=files.session,
                        workspace=files.root, expires=files.expires) as owner:
            owner.save_checkpoint(b'[]', profile='a'*64, run_id='other', step=0, state='complete')
        with real_lease(*args, **kwargs) as owner:
            yield owner
    monkeypatch.setattr(coding, 'lease', raced)
    monkeypatch.setattr(coding, 'qualified_tools', lambda *a, **k: pytest.fail('must refuse before provider qualification'))
    with pytest.raises(FileExistsError):
        coding.run_coding(paths, value, authority, files, {}, limits=Limits(), on_event=lambda _: None, new_session=True)
