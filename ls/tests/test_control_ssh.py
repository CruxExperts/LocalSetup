import os
import sys
import threading
import time

import pytest

from ls.core.agent import control_ssh
from ls.core.agent.control_transport import TransportError


def helper(monkeypatch, code):
    calls = []
    def command(target, argv, **kwargs):
        calls.append((target, tuple(argv)))
        return [sys.executable, '-c', code]
    monkeypatch.setattr(control_ssh, 'ssh_command', command)
    return calls


def test_owned_process_transports_exact_frame_and_is_collected(monkeypatch):
    monkeypatch.setenv('GENERIC_SUDO_KEY', 'fixture-private')
    monkeypatch.setenv('LOCALSETUP_RUN_CREDENTIAL', 'fixture-private')
    calls = helper(monkeypatch, '''
import os,sys
assert 'GENERIC_SUDO_KEY' not in os.environ and 'LOCALSETUP_RUN_CREDENTIAL' not in os.environ
from ls.core.agent.control_transport import HEADER, header, pack
raw = sys.stdin.buffer.read(HEADER.size)
kind, identifier, reply, size = header(raw)
body = sys.stdin.buffer.read(size)
assert not reply and body == b'opaque-ciphertext'
sys.stdout.buffer.write(pack(kind, identifier, body, reply=True))
sys.stdout.buffer.flush()
''')
    with control_ssh.SSH('fixture', ['/trusted/helper', '--stdio'], expires=time.monotonic()+5) as channel:
        assert channel.exchange('execute', 'a'*32, b'opaque-ciphertext') == b'opaque-ciphertext'
        process = channel.process
    assert calls == [('fixture', ('/trusted/helper', '--stdio'))]
    assert process.returncode is not None and channel.closed
    assert not channel.reader.is_alive() and not channel.lifetime._thread.is_alive()
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)
    with pytest.raises(TransportError) as caught:
        channel.exchange('execute', 'a'*32, b'opaque-ciphertext')
    assert caught.value.outcome == 'not_dispatched'


@pytest.mark.parametrize('mode', ['deadline', 'cancel', 'stderr', 'exit', 'acklost'])
def test_failures_collect_owned_child_without_replay(monkeypatch, mode):
    code = 'import time; time.sleep(30)'
    if mode == 'stderr':
        code = "import os,time; os.write(2, b'x'*70000); time.sleep(30)"
    elif mode == 'exit':
        code = 'import sys; sys.exit(7)'
    elif mode == 'acklost':
        code = 'import sys; from ls.core.agent.control_transport import HEADER,header; h=sys.stdin.buffer.read(HEADER.size); sys.stdin.buffer.read(header(h)[3]); sys.exit(0)'
    calls = helper(monkeypatch, code)
    cancelled = threading.Event()
    channel = control_ssh.SSH('fixture', ['/trusted/helper'],
        expires=time.monotonic()+(0.2 if mode == 'deadline' else 5), cancelled=cancelled)
    timer = threading.Timer(.1, cancelled.set) if mode == 'cancel' else None
    if timer is not None: timer.start()
    try:
        with pytest.raises(TransportError) as caught:
            channel.exchange('execute', 'b'*32, b'opaque-ciphertext')
        assert caught.value.outcome in {'uncertain', 'not_dispatched'}
        if mode == 'acklost': assert caught.value.outcome == 'uncertain'
    finally:
        if timer is not None: timer.join()
        channel.close()
    assert len(calls) == 1 and channel.process.returncode is not None
    assert not channel.reader.is_alive() and not channel.lifetime._thread.is_alive()
    if mode == 'stderr': assert 65536 < channel.diagnostic_bytes <= 69632


def test_cancelled_or_unqualified_launch_never_spawns(monkeypatch):
    cancelled = threading.Event(); cancelled.set()
    monkeypatch.setattr(control_ssh, 'ssh_command', lambda *_args, **_kwargs: pytest.fail('must not qualify'))
    with pytest.raises(TransportError) as caught:
        control_ssh.SSH('fixture', ['/trusted/helper'], expires=time.monotonic()+5, cancelled=cancelled)
    assert caught.value.outcome == 'not_dispatched'
    monkeypatch.setattr(control_ssh, 'ssh_command', lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError('unqualified')))
    monkeypatch.setattr(control_ssh.subprocess, 'Popen', lambda *_args, **_kwargs: pytest.fail('must not spawn'))
    with pytest.raises(ValueError, match='unqualified'):
        control_ssh.SSH('fixture', ['/trusted/helper'], expires=time.monotonic()+5)
