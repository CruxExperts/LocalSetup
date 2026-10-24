import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import threading
import time

import pytest
from ls.core.agent.broker_rpc import Channel
from ls.core.agent.supervisor import supervise


@pytest.mark.skipif(not hasattr(os, 'pidfd_open'), reason='requires Linux process exit handles')
@pytest.mark.parametrize('reason', ['cancelled', 'timed_out'])
def test_stalled_callback_does_not_delay_worker_termination(tmp_path, monkeypatch, reason):
    """Observe real process exit before releasing the owner-thread callback."""
    from ls.core.agent import supervisor

    left, right = socket.socketpair()
    cancel, entered, release = threading.Event(), threading.Event(), threading.Event()
    channel = Channel(left, task='task', session='session', methods=frozenset({'read'}),
                      expires=time.monotonic() + 10)
    owner = threading.get_ident()
    created, observed, errors = [], [], []
    popen = subprocess.Popen
    sibling = popen([sys.executable, '-c', 'import time;time.sleep(30)'], start_new_session=True)

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        created.append(process)
        return process

    def handler(*_args):
        assert threading.get_ident() == owner
        entered.set()
        assert release.wait(4), 'Observer did not release callback'
        return {}

    def observe():
        descriptor = None
        try:
            assert entered.wait(2), 'Callback did not start'
            descriptor = os.pidfd_open(created[0].pid)
            poll = select.poll()
            poll.register(descriptor, select.POLLIN)
            if reason == 'cancelled':
                cancel.set()
            observed.append(bool(poll.poll(2000)))
            if observed[-1]:
                # Exit is visible, but only supervisor teardown may reap the ID.
                assert os.waitid(os.P_PID, created[0].pid,
                                 os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            assert sibling.poll() is None, 'Unrelated process was terminated'
        except BaseException as error:
            errors.append(error)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            release.set()

    code = '''import json,socket,struct,sys,time
s=socket.socket(fileno=int(sys.argv[1]))
raw=json.dumps(dict(schema_version=1,task='task',session='session',sequence=0,type='request',method='read',data={})).encode()
s.sendall(struct.pack('!I',len(raw))+raw)
time.sleep(30)
'''
    observer = threading.Thread(target=observe)
    try:
        monkeypatch.setattr(supervisor.subprocess, 'Popen', launch)
        observer.start()
        outcome = supervise([sys.executable, '-I', '-B', '-c', code, str(right.fileno())], b'',
                            cwd=tmp_path, environment={}, timeout=1 if reason == 'timed_out' else 5,
                            cancel=cancel, capture=True, pass_fds=(right.fileno(),),
                            broker=(channel, handler, lambda: None))
        observer.join(3)
        assert not observer.is_alive() and not errors, errors
        assert observed == [True], 'Worker stayed alive throughout the stalled callback'
        assert outcome.status == reason and outcome.returncode is not None
        assert created[0].poll() is not None
        assert not any(thread.name == 'ls-worker-lifetime' for thread in threading.enumerate())
    finally:
        release.set()
        observer.join(5)
        sibling.kill()
        sibling.wait()
        channel.close()
        right.close()


def test_supervisor_services_broker_on_owning_thread(tmp_path):
    left,right=socket.socketpair();deadline=time.monotonic()+3
    channel=Channel(left,task='task',session='session',methods=frozenset({'read'}),expires=deadline)
    owner=threading.get_ident();calls=[]
    def handler(method,data):
        assert threading.get_ident()==owner
        calls.append(data)
        return {'answer':42}
    script="""import socket,sys,json,struct
s=socket.socket(fileno=int(sys.argv[1]))
raw=json.dumps({'schema_version':1,'task':'task','session':'session','sequence':0,'type':'request','method':'read','data':{}}).encode()
s.sendall(struct.pack('!I',len(raw))+raw)
size=struct.unpack('!I',s.recv(4))[0]
response=json.loads(s.recv(size));print(json.dumps(response))
s.close()
"""
    try:
        outcome=supervise(['/usr/bin/python3','-I','-B','-c',script,str(right.fileno())],b'',cwd=tmp_path,
                          environment={},timeout=2,capture=True,pass_fds=(right.fileno(),),broker=(channel,handler,lambda:None))
        assert outcome.status=='completed' and len(calls)==1
        assert json.loads(outcome.data['stdout'])['data']=={'answer':42}
    finally:channel.close();right.close()


def test_broker_contract_refused_before_launch(tmp_path):
    with pytest.raises(ValueError,match='channel'):
        supervise(['/usr/bin/true'],b'',cwd=tmp_path,environment={},timeout=1,broker=(None,None,None))


def test_cancellation_interrupts_partial_broker_frame(tmp_path):
    left,right=socket.socketpair();cancel=threading.Event()
    channel=Channel(left,task='task',session='session',methods=frozenset({'read'}),expires=time.monotonic()+30)
    code="import socket,sys,time;s=socket.socket(fileno=int(sys.argv[1]));s.sendall(b'xx');time.sleep(30)"
    timer=threading.Timer(.1,cancel.set);timer.start();start=time.monotonic()
    try:
        outcome=supervise(['/usr/bin/python3','-I','-B','-c',code,str(right.fileno())],b'',cwd=tmp_path,environment={},
                          timeout=2,cancel=cancel,pass_fds=(right.fileno(),),broker=(channel,lambda *a:pytest.fail('must not dispatch'),lambda:None))
        assert outcome.status=='cancelled' and outcome.data is None
        assert time.monotonic()-start<1
    finally:timer.cancel();timer.join();channel.close();right.close()


def test_partial_frame_worker_exit_does_not_wait_for_retained_peer(tmp_path):
    left,right=socket.socketpair()
    channel=Channel(left,task='task',session='session',methods=frozenset({'read'}),expires=time.monotonic()+30)
    code="import socket,sys,time;s=socket.socket(fileno=int(sys.argv[1]));s.sendall(b'xx');time.sleep(.05)"
    start=time.monotonic()
    try:
        with pytest.raises(ConnectionError):
            supervise(['/usr/bin/python3','-I','-B','-c',code,str(right.fileno())],b'',cwd=tmp_path,environment={},
                      timeout=10,pass_fds=(right.fileno(),),broker=(channel,lambda *a:pytest.fail('must not dispatch'),lambda:None))
        assert time.monotonic()-start<1
    finally:channel.close();right.close()
