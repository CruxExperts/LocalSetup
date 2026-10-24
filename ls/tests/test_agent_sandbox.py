from contextlib import contextmanager
import os
from pathlib import Path
import time

import pytest

from ls.core.agent import sandbox


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    root = tmp_path / 'runtimes'
    release = root / 'release'
    binary = release / 'venv/lscli-native/bwrap'
    binary.parent.mkdir(parents=True)
    binary.write_text('fixture')
    binary.chmod(0o700)
    helper=release/'venv/lib/python3.12/site-packages/ls/core/agent/sandbox_copy.py'
    helper.parent.mkdir(parents=True);helper.write_text('fixture')
    stage = tmp_path / 'snapshot'
    stage.mkdir(mode=0o700)
    held = []
    @contextmanager
    def selected(path, **kwargs):
        assert path == root
        held.append(True)
        try:
            yield release
        finally:
            held.pop()
    monkeypatch.setattr(sandbox, 'selected', selected)
    monkeypatch.setattr(sandbox, '_platform', lambda: None)
    grant = sandbox.ProcessGrant('task', 'session', stage, ('/usr/bin/python3', '-c', 'print(1)'), time.monotonic()+30)
    return root, grant, held, binary


def test_invocation_keeps_lease_and_exact_authorized_command(invocation):
    root, grant, held, binary = invocation
    with sandbox.invocation(root, grant, task='task', session='session') as launch:
        command = list(launch.command)
        assert launch.cwd == binary.parents[2]
        assert dict(launch.environment) == {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        with pytest.raises(TypeError):
            launch.environment['LD_PRELOAD'] = 'untrusted'
        assert held
        assert command[0] == str(binary)
        assert command[command.index('--')+1:] == ['/usr/bin/python3','-I','-B','/lscli-copy.py',*grant.command]
        assert all(x in command for x in ('--unshare-all', '--unshare-user', '--disable-userns'))
        assert '--clearenv' in command
        assert '--bind' not in command
        assert command[command.index(str(grant.staging))+1]=='/inputs'
        assert command.count('--size')==2 and '--remount-ro' in command
        assert not grant.disclose_output
    assert not held


def test_coding_invocation_exposes_only_selected_runtime_and_rpc(invocation):
    root, grant, _, binary = invocation
    release = binary.parents[2]
    worker = release / 'venv/bin/python'
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_text('fixture')
    worker.chmod(0o700)
    launch = sandbox.coding_invocation(release, 7, task='task', session='session',
                                       expires=time.monotonic()+30)
    command = list(launch.command)
    separator = command.index('--')
    assert launch.pass_fds == (7,)
    assert '--preserve-fds' not in command
    assert command[separator+1:] == ['/runtime/venv/bin/python', '-I', '-B', '-m',
        'ls.core.agent.coding_worker', '7', 'task', 'session', command[-1]]
    assert command.count('--ro-bind') == 2
    runtime_bind = command.index(str(release))
    assert command[runtime_bind-1:runtime_bind+2] == ['--ro-bind', str(release), '/runtime']
    assert str(worker) not in command
    assert str(grant.staging) not in command
    assert '/home' not in command and '/run' not in command
    assert '--unshare-all' in command and '--clearenv' in command
    assert launch.environment == {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}


def test_coding_invocation_fails_closed_without_sandbox(invocation):
    root, grant, _, binary = invocation
    binary.unlink()
    with pytest.raises(RuntimeError, match='sandbox'):
        sandbox.coding_invocation(binary.parents[2], 4, task='task', session='session',
                                  expires=time.monotonic()+30)


@pytest.mark.parametrize('name', ['.git', '.env', '.env.secret', 'AGENTS.md', '.agents'])
def test_protected_snapshot_entries_refused(invocation, name):
    root, grant, _, _ = invocation
    (grant.staging / name).write_text('private')
    with pytest.raises(ValueError, match='protected'):
        with sandbox.invocation(root, grant, task='task', session='session'):
            pytest.fail('must refuse before spawn')


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'shared_mode'])
def test_unsafe_snapshot_entries_refused(invocation, kind):
    root, grant, _, _ = invocation
    entry = grant.staging / 'entry'
    if kind == 'symlink':
        entry.symlink_to('/etc/passwd')
    elif kind == 'hardlink':
        other = grant.staging / 'other'
        other.write_text('x')
        os.link(other, entry)
    elif kind == 'fifo':
        os.mkfifo(entry)
    else:
        entry.write_text('x')
        entry.chmod(0o666)
    with pytest.raises(ValueError, match='regular'):
        with sandbox.invocation(root, grant, task='task', session='session'):
            pytest.fail('must refuse before spawn')


def test_revocation_mismatch_and_missing_backend(invocation):
    root, grant, _, binary = invocation
    with pytest.raises(PermissionError):
        with sandbox.invocation(root, grant, task='other', session='session'):
            pass
    binary.unlink()
    with pytest.raises(RuntimeError, match='sealed'):
        with sandbox.invocation(root, grant, task='task', session='session'):
            pass
    grant.revoked.set()
    with pytest.raises(PermissionError):
        with sandbox.invocation(root, grant, task='task', session='session'):
            pass


@pytest.mark.parametrize('command', [('/bin/sh',), ('/usr/bin/../bin/sh',), ('/usr/bin/sh', '\x00'), ['/usr/bin/sh'], ()])
def test_exact_system_command_contract(tmp_path, command):
    with pytest.raises(ValueError):
        sandbox.ProcessGrant('task', 'session', tmp_path, command, time.monotonic()+1)


def test_runtime_under_exposed_system_tree_is_refused(monkeypatch):
    monkeypatch.setattr(Path, 'resolve', lambda self, **kwargs: self)
    for root in (Path('/usr/local/runtimes'), Path('/usr'), Path('/')):
        with pytest.raises(ValueError, match='outside'):
            sandbox._system_boundary(root)
    sandbox._system_boundary(Path('/private/runtimes'))
