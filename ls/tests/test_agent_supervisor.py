import sys
import threading
import json
import signal

import pytest

from ls.core.agent.supervisor import supervise
from ls.core.agent.worker_protocol import event, probe_request


def run(tmp_path, code, *, timeout=2, cancel=None):
    return supervise([sys.executable,'-I','-B','-c',code], b'{}',cwd=tmp_path,environment={},timeout=timeout,cancel=cancel)


def valid():
    return event(0,'ready',{}) + event(1,'result',{'schema_version':1,'status':'qualified','origins':{'pydantic_ai':'/fixture/__init__.py'}})


def test_success_requires_protocol_and_process(tmp_path):
    data=valid()
    outcome=run(tmp_path,f'import sys;sys.stdin.read();sys.stdout.buffer.write({data!r})')
    assert outcome.status=='completed' and outcome.returncode==0
    outcome=run(tmp_path,f'import sys;sys.stdout.buffer.write({data!r});sys.exit(4)')
    assert outcome.status=='failed' and outcome.data is None


@pytest.mark.parametrize('data',[b'',b'not json\n',event(0,'result',{}),valid()+event(2,'result',{}),valid().replace(b'"sequence": 1',b'"sequence": true'),valid().replace(b'"sequence": 1',b'"sequence": 9, "sequence": 1')])
def test_invalid_protocol(tmp_path,data):
    outcome=run(tmp_path,f'import sys;sys.stdout.buffer.write({data!r})')
    assert outcome.status=='protocol_error'


@pytest.mark.parametrize('stream,amount',[('stdout',1024*1024+1),('stderr',64*1024+1)])
def test_output_limits(tmp_path,stream,amount):
    outcome=run(tmp_path,f'import sys;sys.{stream}.buffer.write(b"x"*{amount});sys.{stream}.flush();import time;time.sleep(30)')
    assert outcome.status=='output_limit' and outcome.returncode is not None


def test_timeout_and_active_cancellation(tmp_path):
    assert run(tmp_path,'import time;time.sleep(30)',timeout=.05).status=='timed_out'
    cancel=threading.Event()
    timer=threading.Timer(.05,cancel.set);timer.start()
    try:
        outcome=run(tmp_path,'import time;time.sleep(30)',cancel=cancel)
    finally:
        timer.cancel();timer.join()
    assert outcome.status=='cancelled' and outcome.returncode is not None


def test_precancel_never_spawns(tmp_path):
    cancel=threading.Event();cancel.set()
    assert run(tmp_path,'raise AssertionError()',cancel=cancel).returncode is None


@pytest.mark.parametrize('failure', [False, True])
def test_owner_tick_runs_while_worker_is_silent_and_failure_collects_worker(tmp_path, monkeypatch, failure):
    from ls.core.agent import supervisor

    processes, ticks = [], []
    original = supervisor.subprocess.Popen
    owner = threading.get_ident()
    cancel = threading.Event()
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    def tick():
        assert threading.get_ident() == owner
        ticks.append(True)
        if len(ticks) == 3:
            if failure:
                raise ValueError('Rejected live configuration')
            cancel.set()
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launch)
    def execute():
        return supervise([sys.executable, '-I', '-B', '-c', 'import time;time.sleep(30)'],
                         b'', cwd=tmp_path, environment={}, timeout=2, cancel=cancel, on_tick=tick)
    if failure:
        with pytest.raises(ValueError, match='Rejected live configuration'): execute()
    else:
        assert execute().status == 'cancelled'
    assert len(ticks) == 3
    assert len(processes) == 1 and processes[0].poll() == -signal.SIGKILL
    assert all(stream.closed for stream in (processes[0].stdin, processes[0].stdout, processes[0].stderr))


@pytest.mark.parametrize('disposition', [signal.SIG_IGN, lambda *_args: None])
def test_shared_child_reaper_is_refused_before_launch(tmp_path, monkeypatch, disposition):
    from ls.core.agent import worker_lifetime
    from ls.core.agent import supervisor

    monkeypatch.setattr(worker_lifetime.signal, 'getsignal', lambda *_args: disposition)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *_args, **_kwargs: pytest.fail('must not spawn'))
    with pytest.raises(RuntimeError, match='exclusive non-reaping'):
        run(tmp_path, 'raise AssertionError()')


@pytest.mark.parametrize('failure', ['start', 'monitor'])
def test_lifetime_failure_stops_and_reaps_worker(tmp_path, monkeypatch, failure):
    from ls.core.agent import supervisor, worker_lifetime

    created = []
    original = supervisor.subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        created.append(process)
        return process

    def cannot_start(_self):
        raise RuntimeError('Monitor unavailable')

    class Cancellation:
        def is_set(self):
            if threading.current_thread().name == 'ls-worker-lifetime':
                raise RuntimeError('Flag unavailable')
            return False

    monkeypatch.setattr(supervisor.subprocess, 'Popen', launch)
    if failure == 'start':
        monkeypatch.setattr(worker_lifetime.WorkerLifetime, 'start', cannot_start)
    with pytest.raises(RuntimeError, match='Monitor unavailable|monitoring failed'):
        run(tmp_path, 'import time;time.sleep(30)', cancel=Cancellation())
    assert len(created) == 1 and created[0].poll() == -signal.SIGKILL
    assert all(stream.closed for stream in (created[0].stdin, created[0].stdout, created[0].stderr))
    assert not any(thread.name == 'ls-worker-lifetime' for thread in threading.enumerate())


def test_lost_child_wait_ownership_never_signals_a_reused_id(monkeypatch):
    from types import SimpleNamespace
    from ls.core.agent import worker_lifetime

    def already_reaped(*_args):
        raise ChildProcessError()

    monkeypatch.setattr(worker_lifetime.os, 'waitid', already_reaped)
    monkeypatch.setattr(worker_lifetime.os, 'killpg', lambda *_args: pytest.fail('must not signal'))
    lifetime = worker_lifetime.WorkerLifetime(SimpleNamespace(pid=123), 0, None)
    with pytest.raises(RuntimeError, match='ownership was lost'):
        lifetime.kill()
    lifetime.close()


def test_parent_exit_tears_down_pipe_holding_descendant(tmp_path):
    code="import os,time,sys;pid=os.fork();time.sleep(30) if pid==0 else sys.exit(2)"
    outcome=run(tmp_path,code,timeout=1)
    assert outcome.status=='failed' and outcome.returncode==2


def test_probe_request_strict_version():
    probe_request(b'{"schema_version":1,"operation":"probe"}')
    for raw in [b'',b'{"schema_version":true,"operation":"probe"}',b'{"schema_version":1,"operation":"run"}',b'{"schema_version":2,"schema_version":1,"operation":"probe"}']:
        with pytest.raises(ValueError):probe_request(raw)
