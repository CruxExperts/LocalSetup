"""Authenticated manifests adapt to the existing installer without implicit effects."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

from ls.core.agent import cli, native_bundle, release_download, release_manifest, runtime_install
from ls.tests.test_native_bundle import bundle
from ls.tests.test_runtime_install import installation


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def release(tmp_path, bundle):
    bundle_path, digest = bundle()
    directory = tmp_path / 'assets'
    directory.mkdir()
    raw = io.BytesIO()
    wheel = 'localsetup-1.2.3-py3-none-any.whl'
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('localsetup-1.2.3.dist-info/METADATA', 'Name: localsetup\nVersion: 1.2.3\n')
    files = {wheel: raw.getvalue(), 'native.zip': bundle_path.read_bytes(),
             **{f'source-{i}.txt': f'source fixture {i}'.encode() for i in range(6)}}
    for name, content in files.items():
        (directory / name).write_bytes(content)
    value = {'schema_version': 1, 'version': '1.2.3', 'source_commit': 'a' * 40,
             'wheel': wheel, 'wheel_sha256': sha(files[wheel]), 'native_bundle': 'native.zip',
             'native_sha256': digest, 'native_origin': {'repository': 'CruxExperts/LocalSetup', 'tag': 'v1.0.0'},
             'files': {name: sha(content) for name, content in files.items()}}
    path = directory / release_manifest.NAME
    path.write_text(json.dumps(value))
    return path, sha(path.read_bytes()), value, files


def test_resolve_is_noncreating_and_sdk_only_is_explicit(release, monkeypatch):
    path, digest, value, files = release
    monkeypatch.setattr(native_bundle, '_platform', lambda: pytest.fail('static resolution must not require a supported host'))
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    normal = release_manifest.resolve(path, digest)
    assert normal['native_inputs']['sandbox_sha256'] == value['native_sha256']
    assert normal['release']['source_commit'] == 'a' * 40
    sdk = release_manifest.resolve(path, digest, sdk_only=True)
    assert sdk['native_inputs'] == {} and sdk['release']['sdk_only'] is True
    assert before == {p.name: p.read_bytes() for p in path.parent.iterdir()}


@pytest.mark.parametrize('change', ['digest', 'extra', 'traversal', 'duplicate', 'roles', 'version', 'schema'])
def test_manifest_rejects_untrusted_or_ambiguous_input(release, change):
    path, digest, value, files = release
    if change == 'digest':
        digest = '0' * 64
    else:
        if change == 'extra':
            value['extra'] = 'ignored?'
        elif change == 'traversal':
            value['files']['../escape'] = value['files'].pop('source-0.txt')
        elif change == 'roles':
            value['wheel_sha256'] = '0' * 64
        elif change == 'version':
            value['version'] = '01.2.3'
        elif change == 'schema':
            value['schema_version'] = True
        path.write_text(json.dumps(value))
        if change == 'duplicate':
            path.write_text(path.read_text()[:-1] + ',"version":"1.2.3"}')
        digest = sha(path.read_bytes())
    with pytest.raises(ValueError):
        release_manifest.resolve(path, digest)


@pytest.mark.parametrize('change', ['tamper', 'missing', 'symlink', 'fifo', 'wheel-version'])
def test_payload_errors_are_rejected_before_installer(release, change):
    path, digest, value, files = release
    target = path.parent / 'source-0.txt'
    if change == 'tamper':
        target.write_bytes(b'changed')
    elif change == 'missing':
        target.unlink()
    elif change == 'symlink':
        target.unlink()
        target.symlink_to(path)
    elif change == 'fifo':
        target.unlink()
        os.mkfifo(target)
    else:
        target = path.parent / value['wheel']
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('localsetup-1.2.3.dist-info/METADATA', 'Name: localsetup\nVersion: 9.9.9\n')
        value['wheel_sha256'] = value['files'][target.name] = sha(target.read_bytes())
        path.write_text(json.dumps(value)); digest = sha(path.read_bytes())
    with pytest.raises((ValueError, OSError)):
        release_manifest.resolve(path, digest)


@pytest.mark.parametrize('apply,sdk_only', [(False, False), (True, False), (True, True)])
def test_cli_uses_existing_plan_or_installer_with_authenticated_inputs(release, tmp_path, monkeypatch, capsys, apply, sdk_only):
    path, digest, value, files = release
    calls = []
    def chosen(*args, **kwargs):
        calls.append((args, kwargs)); return {'status': 'installed' if apply else 'planned'}
    monkeypatch.setattr(runtime_install, 'install' if apply else 'plan', chosen)
    monkeypatch.setattr(runtime_install, 'plan' if apply else 'install', lambda *_a, **_k: pytest.fail('wrong operation'))
    argv = ['setup', '--apply' if apply else '--plan', '--release-manifest', str(path),
            '--release-sha256', digest, '--wheelhouse', str(tmp_path / 'wheelhouse'),
            '--runtime-root', str(tmp_path / 'runtime')]
    if sdk_only:
        argv.append('--sdk-only')
    assert cli.main(argv) == 0
    assert len(calls) == 1 and calls[0][0][1:3] == (path.parent / value['wheel'], value['wheel_sha256'])
    assert ('sandbox_bundle' in calls[0][1]) is not sdk_only
    result = json.loads(capsys.readouterr().out)
    assert result['release_manifest_sha256'] == digest and result['sdk_only'] is sdk_only
    assert not (tmp_path / 'runtime').exists()


@pytest.mark.parametrize('extra', [['--wheel', 'x.whl'], ['--bin-dir', 'bin'], ['--profile-input', 'profile.json'], ['--assets-dir', 'assets']])
def test_cli_rejects_mixed_release_authority(release, tmp_path, extra):
    path, digest, value, files = release
    with pytest.raises(SystemExit) as result:
        cli.main(['setup', '--plan', '--release-manifest', str(path), '--release-sha256', digest,
                  '--wheelhouse', str(tmp_path), *extra])
    assert result.value.code == 2


def test_manifest_apply_preserves_previous_runtime_and_uses_existing_sealing(installation, release, monkeypatch, capsys):
    root, wheel, digest, wheelhouse, workspace = installation
    previous = runtime_install.install(*installation)
    path, manifest_digest, value, files = release
    monkeypatch.chdir(workspace)
    assert cli.main(['setup', '--apply', '--sdk-only', '--release-manifest', str(path),
                     '--release-sha256', manifest_digest, '--wheelhouse', str(wheelhouse),
                     '--runtime-root', str(root)]) == 0
    with runtime_install.selected(root) as selected_path:
        assert selected_path.name == value['wheel_sha256']
    selected = json.loads((root / 'current.json').read_bytes())
    assert selected['previous'] == previous['sha256']
    assert selected['sha256'] == value['wheel_sha256']
    assert (root / previous['sha256']).is_dir()
    assert json.loads(capsys.readouterr().out)['release_manifest_sha256'] == manifest_digest


def test_missing_github_cli_has_actionable_error_and_creates_nothing(release, tmp_path, monkeypatch):
    path, digest, value, files = release
    monkeypatch.setattr(release_download.shutil, 'which', lambda _name: None)
    destination = tmp_path / 'no-gh'
    with pytest.raises(RuntimeError, match='manually acquired verified assets'):
        release_download.download('v1.2.3', digest, destination)
    assert not destination.exists()


def mock_download(release, monkeypatch, *, fail=False):
    path, digest, value, files = release
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[:4] == ['gh', 'release', 'download', 'v1.2.3']
        assert command[command.index('--repo') + 1] == 'CruxExperts/LocalSetup'
        assert kwargs['env']['GH_PROMPT_DISABLED'] == '1' and kwargs['stdin'] == subprocess.DEVNULL
        if fail and len(calls) == 2:
            raise subprocess.TimeoutExpired('sensitive transport diagnostic', 1)
        directory = Path(command[command.index('--dir') + 1])
        names = [command[i + 1] for i, item in enumerate(command) if item == '--pattern']
        for name in names:
            (directory / name).write_bytes(path.read_bytes() if name == path.name else files[name])
    monkeypatch.setattr(release_download.subprocess, 'run', run)
    return calls


def test_explicit_download_authenticates_manifest_before_payload_and_retains_receipt(release, tmp_path, monkeypatch, capsys):
    path, digest, value, files = release
    calls = mock_download(release, monkeypatch)
    destination = tmp_path / 'downloaded'
    assert cli.main(['setup', '--download-release', 'v1.2.3', '--release-sha256', digest,
                     '--assets-dir', str(destination)], default_runtime_root=tmp_path / 'unrelated') == 0
    assert len(calls) == 2 and calls[0][-2:] == ['--pattern', path.name]
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'downloaded' and (destination.stat().st_mode & 0o777) == 0o700
    assert json.loads((destination / 'status.json').read_bytes()) == result
    with pytest.raises(ValueError, match='fresh directory'):
        release_download.download('v1.2.3', digest, destination)
    assert len(calls) == 2


@pytest.mark.parametrize('failure', ['manifest-digest', 'payload-download', 'tag-version'])
def test_download_failure_never_installs_or_replays(release, tmp_path, monkeypatch, failure):
    path, digest, value, files = release
    if failure == 'tag-version':
        value['version'] = '2.0.0'
        value['files']['localsetup-2.0.0-py3-none-any.whl'] = value['files'].pop(value['wheel'])
        value['wheel'] = 'localsetup-2.0.0-py3-none-any.whl'
        path.write_text(json.dumps(value)); digest = sha(path.read_bytes())
    calls = mock_download(release, monkeypatch, fail=failure == 'payload-download')
    if failure == 'manifest-digest':
        digest = '0' * 64
    destination = tmp_path / 'failed'
    with pytest.raises((ValueError, RuntimeError)) as result:
        release_download.download('v1.2.3', digest, destination)
    assert 'sensitive transport' not in str(result.value)
    assert len(calls) == (2 if failure == 'payload-download' else 1)
    assert json.loads((destination / 'status.json').read_bytes())['status'] == 'incomplete'
