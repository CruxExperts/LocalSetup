"""Tool-free context uses the same filesystem and disclosure authority as tools."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from ls.core.agent import completion_cli, completion_context
from ls.core.agent.coding_protocol import profile_digest
from ls.core.agent.completion_contract import envelope
from ls.core.agent.profiles import parse, wire
from ls.tests.test_agent_provider import config


@pytest.fixture
def context(tmp_path):
    tmp_path.chmod(0o700)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    text = 'Measured observation 日本語\n' * 500
    (workspace / 'health.txt').write_text(text)
    grant = tmp_path / 'grant.json'
    value = dict(schema_version=1, read=['health.txt'], write=['.'], disclose=['health.txt'], recipes={})
    grant.write_text(json.dumps(value)); grant.chmod(0o600)
    return workspace, grant, value, text


def attach(context, *, paths=None, cancelled=None):
    workspace, grant, *_ = context
    return json.loads(completion_context.attach(json.dumps({'input': 'Review observations'}),
        paths or ['health.txt'], grant, workspace, time.monotonic() + 5,
        cancelled or threading.Event()))


def test_context_reads_complete_pages_without_writes(context):
    workspace, grant, _, text = context
    before = (workspace / 'health.txt').read_bytes(), grant.read_bytes()
    request = attach(context)
    assert request['input']['instructions'] == 'Review observations'
    assert request['input']['context'] == [{'path': 'health.txt', 'text': text,
        'sha256': hashlib.sha256(text.encode()).hexdigest()}]
    assert before == ((workspace / 'health.txt').read_bytes(), grant.read_bytes())
    assert list(workspace.iterdir()) == [workspace / 'health.txt']


@pytest.mark.parametrize('failure', ['read', 'disclose', 'symlink', 'binary', 'bytes', 'pages', 'cancelled', 'protected'])
def test_context_rejects_unsafe_or_incomplete_snapshots(context, failure):
    workspace, grant, value, _ = context
    file = workspace / 'health.txt'
    cancelled = threading.Event()
    paths = ['health.txt']
    if failure in ('read', 'disclose'):
        value[failure] = []; grant.write_text(json.dumps(value))
    elif failure == 'symlink':
        file.rename(workspace / 'actual'); file.symlink_to(workspace / 'actual')
    elif failure == 'binary': file.write_bytes(b'\x00binary')
    elif failure == 'bytes': file.write_text('x' * (completion_context.MAX_CONTEXT_BYTES + 1))
    elif failure == 'pages': file.write_text('\n' * (completion_context.MAX_CONTEXT_PAGES * 200 + 1))
    elif failure == 'cancelled': cancelled.set()
    elif failure == 'protected':
        paths = ['.env']; value.update(read=['.'], disclose=['.'])
        grant.write_text(json.dumps(value)); (workspace / '.env').write_text('private')
    with pytest.raises((ValueError, OSError)):
        attach(context, paths=paths, cancelled=cancelled)


@pytest.mark.parametrize('paths', [[], ['../health.txt'], ['/health.txt'], ['health.txt'] * 2, ['x'] * 9, [None]])
def test_context_selection_is_bounded(paths):
    with pytest.raises((ValueError, OSError)):
        completion_context.validate_paths(paths)


def test_registered_complete_routes_through_existing_owner(tmp_path, monkeypatch):
    from ls.core.agent import cli
    seen = []
    monkeypatch.setattr(completion_cli, 'main', lambda args, **kwargs: seen.append((args, kwargs)) or 0)
    assert cli.main(['complete', '--profile', 'test', '--request', '-'], default_runtime_root=tmp_path) == 0
    assert seen == [(['complete', '--profile', 'test', '--request', '-'], {'default_runtime_root': tmp_path})]
    assert completion_cli.arguments(seen[0][0], **seen[0][1]).runtime_root == tmp_path


def test_registered_completion_keeps_redacted_argument_errors(capfd):
    from ls.core.agent.cli import main
    assert main(['complete', '--unexpected', 'private-sentinel']) == 2
    output = capfd.readouterr()
    assert output.err == '' and 'private-sentinel' not in output.out
    assert json.loads(output.out)['status'] == 'invalid_request'


def test_protected_completion_attaches_granted_context_before_dispatch(context, monkeypatch):
    from ls.core.agent import completion_run
    workspace, grant, _, text = context
    profiles = workspace.parent / 'profiles.json'
    profile = config()
    profiles.write_text(json.dumps({'schema_version': 1, 'profiles': {'test': profile}}))
    profiles.chmod(0o600)
    monkeypatch.setenv(completion_cli.PROFILE, profile_digest(wire(parse(profile))))
    monkeypatch.setenv(completion_cli.CREDENTIAL, 'synthetic')
    monkeypatch.setattr(completion_cli, 'sys', SimpleNamespace(flags=SimpleNamespace(isolated=1),
        dont_write_bytecode=True, prefix=str(Path(completion_cli.__file__).resolve().parents[3])))
    monkeypatch.setattr(completion_cli, 'read_request', lambda *_: json.dumps({'input': 'Review'}))
    observed = []
    def run(_root, payload, _authority, **_kwargs):
        observed.append(payload)
        return envelope('succeeded', model=profile['model'], data={}, attempts=1)
    monkeypatch.setattr(completion_run, 'run', run)
    monkeypatch.setattr(completion_cli, 'emit', lambda result: 0 if result['status'] == 'succeeded' else 2)
    args = ['complete', '--profile', 'test', '--profiles', str(profiles), '--request', '-',
            '--context', 'health.txt', '--workspace', str(workspace), '--grant', str(grant)]
    assert completion_cli.main(args, protected=True) == 0
    assert json.loads(observed[0]['request'])['input']['context'][0]['text'] == text
    observed.clear()
    value = json.loads(grant.read_text()); value['disclose'] = []; grant.write_text(json.dumps(value))
    assert completion_cli.main(args, protected=True) == 2
    assert observed == []
