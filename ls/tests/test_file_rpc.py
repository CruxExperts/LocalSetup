from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from ls.core.agent.broker_rpc import Channel
from ls.core.agent.file_broker import FileBroker
from ls.core.agent.file_rpc import FileHandler,METHODS
from ls.tests.test_session_owner import state,own,broker


def test_sdk_read_write_and_checkpoint_operation_mapping(state,broker):
    root=Path(__file__).resolve().parents[2]
    allowed=FileBroker(replace(broker.grant,disclose=('src',),expires=time.monotonic()+10),broker.lease_root)
    left,right=socket.socketpair()
    channel=Channel(left,task='task',session='session',methods=METHODS,expires=time.monotonic()+10)
    child=subprocess.Popen([sys.executable,'-I','-B',str(root/'ls/tests/sdk_file_fixture.py'),str(right.fileno()),str(root)],
                           pass_fds=(right.fileno(),),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    right.close()
    try:
        with own(state,broker,expires=time.monotonic()+10) as owner:
            handler=FileHandler(owner,allowed,profile='a'*64,run_id='run')
            while True:
                try:channel.serve_once(handler,check=owner._check)
                except ConnectionError:break
            output,error=child.communicate(timeout=5)
            assert child.returncode==0,error
            result=json.loads(output)
            operation,=owner.inspect().values()
            assert operation['outcome']=='applied' and operation['intent']['tool_call']['call_id']=='write-1'
            assert operation['intent']['checkpoint'] and operation['intent']['tool_call']['run_id']=='run'
            assert owner.resume_checkpoint(result['checkpoint'],profile='a'*64)==result['messages'].encode()
        assert (broker.grant.root/'src/a.txt').read_bytes()==b'changed'
    finally:
        channel.close()
        if child.poll() is None:child.kill();child.wait()


def test_read_disclosure_and_duplicate_write_refusal(state,broker):
    with own(state,broker) as owner:
        handler=FileHandler(owner,broker,profile='a'*64,run_id='run')
        with pytest.raises(PermissionError,match='disclosure'):
            handler('file.read',{'path':'src/a.txt','cursor':None})
        def checkpoint():return owner.save_checkpoint(b'[]',profile='a'*64,run_id='run',step=0,state='interrupted')
        args=dict(path='src/a.txt',content='changed',expected_before=hashlib.sha256(b'original').hexdigest(),checkpoint=checkpoint(),call_id='same')
        handler('file.write',args)
        args.update(content='replayed',expected_before=hashlib.sha256(b'changed').hexdigest(),checkpoint=checkpoint())
        with pytest.raises(ValueError,match='already has an operation'):
            handler('file.write',args)
        assert len(owner.inspect())==1 and (broker.grant.root/'src/a.txt').read_bytes()==b'changed'


def test_foreign_checkpoint_profile_and_payload_authority_refused(state,broker):
    with own(state,broker) as owner:
        handler=FileHandler(owner,broker,profile='a'*64,run_id='run')
        checkpoint=owner.save_checkpoint(b'[]',profile='b'*64,run_id='run',step=0,state='interrupted')
        args=dict(path='src/a.txt',content='changed',expected_before=None,checkpoint=checkpoint,call_id='call')
        with pytest.raises(PermissionError,match='matching'):
            handler('file.write',args)
        with pytest.raises(ValueError,match='schema'):
            handler('file.write',args|{'run_id':'other'})
        assert owner.inspect()=={}


def test_disclosure_revoked_during_page_construction(state,broker,monkeypatch):
    allowed=FileBroker(replace(broker.grant,disclose=('src',)),broker.lease_root)
    with own(state,broker) as owner:
        handler=FileHandler(owner,allowed,profile='a'*64,run_id='run')
        response = FileBroker._page_response
        def revoke_after_page(*args, **kwargs):
            result = response(*args, **kwargs)
            allowed.grant.revoked.set()
            return result
        monkeypatch.setattr(FileBroker, '_page_response', revoke_after_page)
        with pytest.raises(PermissionError,match='revoked'):
            handler('file.read',{'path':'src/a.txt','cursor':None})
        owner._check()


def test_controller_cannot_import_sdk_file_tools():
    from ls.core.agent.sdk_file_tools import file_tools
    with pytest.raises(RuntimeError,match='isolated worker'):
        file_tools(None,None)
    assert not any(name.startswith('pydantic_ai') for name in sys.modules)
