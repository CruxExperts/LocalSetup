"""Dependency acquisition keeps authentication, ordering and effects explicit."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from ls.core.agent import cli, dependency_artifacts as dependencies


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    build, runtime = b'build-wheel', b'runtime-source'
    exports = {'sdk-build.lock': f'build==1.0 --hash=sha256:{sha(build)}\n'.encode(),
               'sdk-runtime.lock': f'runtime==2.0 --hash=sha256:{sha(runtime)}\n'.encode()}
    receipt = {'schema_version': 1, 'source_sha256': 'a' * 64,
               'files': {name: sha(data) for name, data in exports.items()}}
    path = tmp_path / 'framework.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('localsetup-1.2.3.dist-info/METADATA', 'Name: localsetup\nVersion: 1.2.3\n')
        for name, data in exports.items():
            archive.writestr('ls/config/' + name, data)
        archive.writestr('ls/config/sdk-dependency-receipt.json', json.dumps(receipt))
    monkeypatch.setattr(dependencies, 'inspect_artifact', lambda _path: {})
    return path, sha(path.read_bytes()), exports, build, runtime


@pytest.fixture
def execution(wheel, monkeypatch):
    path, digest, exports, build, runtime = wheel
    calls = []
    monkeypatch.setenv('GH_TOKEN', 'private-fixture')
    monkeypatch.setenv('GENERIC_SUDO_KEY', 'private-fixture')
    monkeypatch.setenv('PIP_INDEX_URL', 'https://private-fixture.invalid')
    monkeypatch.setattr(dependencies.shutil, 'which', lambda _name: '/trusted/uv')
    def probe(command, **kwargs):
        assert command[-3:] == ['-m', 'pip', '--version']
        assert not {'GH_TOKEN', 'GENERIC_SUDO_KEY', 'PIP_INDEX_URL'} & kwargs['env'].keys()
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(dependencies.subprocess, 'run', probe)
    def run(command, *, directory, deadline, environment):
        calls.append(command)
        assert deadline > dependencies.time.monotonic()
        assert not {'GH_TOKEN', 'GENERIC_SUDO_KEY', 'PIP_INDEX_URL'} & environment.keys()
        assert environment['UV_PYTHON_DOWNLOADS'] == 'never'
        if 'download' in command:
            assert '--require-hashes' in command and '--no-build-isolation' in command and '--no-deps' in command
            if 'sdk-build.lock' in command[-1]:
                assert '--only-binary' in command and len(calls) == 1
                (directory / 'wheelhouse/build.whl').write_bytes(build)
            else:
                assert len(calls) == 4 and '--python' in command
                assert command[command.index('--python') + 1] == str(directory / 'download-env/bin/python')
                (directory / 'wheelhouse/runtime.tar.gz').write_bytes(runtime)
        elif 'install' in command:
            assert '--no-index' in command and '--require-hashes' in command and '--only-binary' in command
            assert len(calls) == 3 and (directory / 'wheelhouse/build.whl').is_file()
        else:
            assert 'venv' in command and '--offline' in command and len(calls) == 2
    monkeypatch.setattr(dependencies.runtime_install, '_run', run)
    return calls


def test_preparation_authenticates_and_preserves_order_without_runtime_selection(wheel, execution, tmp_path):
    path, digest, exports, build, runtime = wheel
    destination = tmp_path / 'prepared'
    result = dependencies.prepare(path, digest, destination, python='/chosen/python')
    assert result['status'] == 'prepared'
    assert result['artifacts'] == {'build.whl': sha(build), 'runtime.tar.gz': sha(runtime)}
    assert result['lock_sha256'] == {name: sha(data) for name, data in exports.items()}
    assert json.loads((destination / 'status.json').read_bytes()) == result
    assert not (destination / 'current.json').exists()
    with pytest.raises(ValueError, match='fresh directory'):
        dependencies.prepare(path, digest, destination)
    assert len(execution) == 4


@pytest.mark.parametrize('fault', ['digest', 'receipt', 'pip', 'uv'])
def test_preflight_failure_does_not_create_destination(wheel, execution, tmp_path, monkeypatch, fault):
    path, digest, exports, build, runtime = wheel
    if fault == 'digest':
        digest = '0' * 64
    elif fault == 'receipt':
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('ls/config/sdk-dependency-receipt.json', '{}')
        digest = sha(path.read_bytes())
    elif fault == 'pip':
        monkeypatch.setattr(dependencies.subprocess, 'run', lambda *_a, **_k: subprocess.CompletedProcess([], 1))
    else:
        monkeypatch.setattr(dependencies.shutil, 'which', lambda _name: None)
    destination = tmp_path / 'failed'
    with pytest.raises((RuntimeError, ValueError)):
        dependencies.prepare(path, digest, destination)
    assert not destination.exists() and not execution


@pytest.mark.parametrize('phase', range(4))
def test_phase_failure_is_retained_and_never_retried(wheel, execution, tmp_path, monkeypatch, phase):
    path, digest, exports, build, runtime = wheel
    runner = dependencies.runtime_install._run
    count = 0
    def fail(command, **kwargs):
        nonlocal count
        count += 1
        if count == phase + 1:
            raise RuntimeError('private transport diagnostic')
        runner(command, **kwargs)
    monkeypatch.setattr(dependencies.runtime_install, '_run', fail)
    destination = tmp_path / 'failed'
    with pytest.raises(RuntimeError) as result:
        dependencies.prepare(path, digest, destination)
    assert 'private transport' not in str(result.value) and count == phase + 1
    receipt = json.loads((destination / 'status.json').read_bytes())
    assert receipt['status'] == 'incomplete'
    assert receipt['stage'] == dependencies.recipe(destination, '/chosen/python')[phase]['stage']


def test_unlisted_artifact_is_rejected_even_after_successful_commands(wheel, execution, tmp_path, monkeypatch):
    path, digest, exports, build, runtime = wheel
    runner = dependencies.runtime_install._run
    def insert(command, **kwargs):
        runner(command, **kwargs)
        (kwargs['directory'] / 'wheelhouse/extra.whl').write_bytes(b'not approved')
    monkeypatch.setattr(dependencies.runtime_install, '_run', insert)
    destination = tmp_path / 'extra'
    with pytest.raises(ValueError, match='not authenticated'):
        dependencies.prepare(path, digest, destination)
    assert json.loads((destination / 'status.json').read_bytes())['status'] == 'incomplete'


def test_cli_explicit_preparation_and_default_runtime_binding_are_separate(wheel, execution, tmp_path, capsys):
    path, digest, exports, build, runtime = wheel
    assert cli.main(['setup', '--prepare-dependencies', '--wheel', str(path), '--sha256', digest,
                     '--assets-dir', str(tmp_path / 'prepared'), '--python', '/chosen/python'],
                    default_runtime_root=tmp_path / 'runtime') == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'prepared'
    assert not (tmp_path / 'runtime').exists()


@pytest.mark.parametrize('options', [[], ['--wheelhouse', 'x'], ['--sdk-only'], ['--runtime-root', 'x'],
                                   ['--bin-dir', 'x'], ['--profile-input', 'x']])
def test_invalid_cli_combinations_never_reach_preparation(tmp_path, monkeypatch, options):
    monkeypatch.setattr(dependencies, 'prepare', lambda *_a, **_k: pytest.fail('invalid dispatch'))
    base = [] if not options else ['--assets-dir', str(tmp_path / 'new'), '--wheel', 'x', '--sha256', 'a' * 64]
    with pytest.raises(SystemExit) as result:
        cli.main(['setup', '--prepare-dependencies', *base, *options])
    assert result.value.code == 2
