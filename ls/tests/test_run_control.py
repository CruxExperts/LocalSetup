import json
import os
import socket
import threading
import time

import pytest

from ls.core.agent.run_control import listen, validate


def exchange(peer, identifier, method):
    peer.sendall(json.dumps({'schema_version':1,'id':identifier,'method':method}).encode()+b'\n')
    return json.loads(peer.recv(4096))


def test_status_cancel_and_descriptor_ownership():
    owner, child = socket.socketpair()
    cancelled=threading.Event()
    with owner, listen(child.detach(),cancelled,time.monotonic()+2):
        owner.settimeout(1)
        assert exchange(owner,1,'status') == {'schema_version':1,'id':1,'status':'active'}
        assert exchange(owner,2,'cancel')['status']=='cancellation_requested'
        assert cancelled.is_set()
        assert exchange(owner,3,'status')['status']=='cancellation_requested'


@pytest.mark.parametrize('payload', [b'', b'x'*16385, b'{"schema_version":1,"id":2,"method":"status"}\n',
    b'{"schema_version":1,"id":1,"method":"approve"}\n',
    b'{"schema_version":1,"id":1,"id":1,"method":"status"}\n'])
def test_owner_exit_or_invalid_protocol_revokes(payload):
    owner, child = socket.socketpair();cancelled=threading.Event()
    with listen(child.detach(),cancelled,time.monotonic()+2):
        if payload:owner.sendall(payload)
        else:owner.shutdown(socket.SHUT_WR)
        assert cancelled.wait(1)
    owner.close()


def test_fragmented_frames_and_close_are_bounded():
    owner, child = socket.socketpair();cancelled=threading.Event()
    started=time.monotonic()
    with owner, listen(child.detach(),cancelled,started+2):
        owner.settimeout(1)
        owner.sendall(b'{"schema_version":1,')
        owner.sendall(b'"id":1,"method":"status"}\n')
        assert json.loads(owner.recv(4096))['status']=='active'
    assert not cancelled.is_set() and time.monotonic()-started<1


def test_invalid_descriptor_and_deadline_do_not_cancel():
    with pytest.raises(ValueError):validate(0)
    read,write=os.pipe()
    try:
        with pytest.raises(OSError):validate(read)
    finally:os.close(read);os.close(write)
    owner,child=socket.socketpair();cancelled=threading.Event()
    with owner,listen(child.detach(),cancelled,time.monotonic()-1):
        time.sleep(0.03)
    assert not cancelled.is_set()


@pytest.mark.parametrize('seconds,revoked', [(0.1,False),(2,True)])
def test_reply_backpressure_preserves_run_deadline(seconds, revoked):
    owner,child=socket.socketpair();cancelled=threading.Event()
    child.setblocking(False)
    while True:
        try:child.send(b'x'*4096)
        except BlockingIOError:break
    child.setblocking(True)
    with owner,listen(child.detach(),cancelled,time.monotonic()+seconds):
        owner.sendall(b'{"schema_version":1,"id":1,"method":"status"}\n')
        assert cancelled.wait(0.4) is revoked


def test_steering_socket_queues_only_explicit_disclosure():
    from ls.core.agent.steering import Steering
    owner,child=socket.socketpair();cancelled=threading.Event();expires=time.monotonic()+2
    queue=Steering(cancelled,expires);queue.bind('task','session','profile')
    with owner,listen(child.detach(),cancelled,expires,queue):
        owner.settimeout(1)
        value={'schema_version':1,'id':1,'method':'steer','task':'task','session':'session','profile':'profile','text':'new direction','disclose':True}
        owner.sendall(json.dumps(value).encode()+b'\n')
        assert json.loads(owner.recv(4096))['status']=='queued'
        assert queue.take()==['new direction']


def test_dispatch_failure_after_effect_cannot_replay():
    from ls.core.agent.control_requests import Requests

    class Approvals:
        calls = 0

        def decide(self, value):
            self.calls += 1
            raise OSError('Acknowledgement unavailable after decision')

    approvals = Approvals()
    requests = Requests(threading.Event(), time.monotonic()+2, approvals=approvals)
    raw = b'{"schema_version":1,"id":1,"method":"approve"}'
    with pytest.raises(OSError):
        requests.dispatch(raw)
    with pytest.raises(PermissionError):
        requests.dispatch(raw)
    assert approvals.calls == 1 and requests.closed


def test_dispatch_rejects_reordered_requests_and_expired_authority():
    from ls.core.agent.control_requests import Requests

    cancelled = threading.Event()
    requests = Requests(cancelled, time.monotonic()+2)
    status = b'{"schema_version":1,"id":1,"method":"status"}'
    assert requests.dispatch(status)['status'] == 'active'
    with pytest.raises(ValueError):
        requests.dispatch(status)
    with pytest.raises(PermissionError):
        requests.dispatch(b'{"schema_version":1,"id":2,"method":"cancel"}')
    assert not cancelled.is_set()

    expired = Requests(cancelled, time.monotonic()-1)
    with pytest.raises(PermissionError):
        expired.dispatch(b'{"schema_version":1,"id":1,"method":"cancel"}')
    assert not cancelled.is_set()


def test_dispatch_owner_close_during_handler_cannot_acknowledge():
    from ls.core.agent.control_requests import Requests

    class Approvals:
        def decide(self, value):
            requests.closed = True

    requests = Requests(threading.Event(), time.monotonic()+2, approvals=Approvals())
    with pytest.raises(PermissionError):
        requests.dispatch(b'{"schema_version":1,"id":1,"method":"approve"}')
    assert requests.closed and requests.previous == 1
