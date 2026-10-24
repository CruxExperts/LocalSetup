"""Release assembly preserves authenticated native bytes and rejects stale wheels."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from ls.core.agent import native_bundle as native, native_sbom as sbom
from ls.core.agent import release_assets as assets, release_wheel
from ls.tests.test_native_bundle import bundle


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def native_set(tmp_path, monkeypatch, bundle):
    source = b'synthetic upstream source'
    components = copy.deepcopy(native.COMPONENTS)
    components['bubblewrap']['source_sha256'] = sha(source)
    monkeypatch.setattr(native, 'COMPONENTS', components)
    monkeypatch.setattr(sbom, 'LICENSE_HASHES', {
        'bubblewrap-COPYING': sha(b'license'), 'libcap-copyright': sha(b'copyright')})
    def update_manifest(contents):
        manifest = json.loads(contents['manifest.json'])
        manifest['components'] = components
        contents['manifest.json'] = json.dumps(manifest).encode()
    path, digest = bundle(update_manifest)
    policy = assets.native_policy(Path(__file__).resolve().parents[2])
    directory = tmp_path / 'native'
    directory.mkdir()
    files = {policy['bundle']: path.read_bytes(), policy['sbom']: sbom.encode(path, digest),
             'bubblewrap-0.12.0.tar.xz': source,
             'libcap2_2.66.orig.tar.xz': b'original',
             'libcap2_2.66-5ubuntu2.4.debian.tar.xz': b'patches',
             'libcap2_2.66-5ubuntu2.4.dsc': b'descriptor'}
    cap_sources = [{'name': name, 'sha256': sha(raw)} for name, raw in files.items() if name.startswith('libcap2_')]
    provenance = {'target': native.TARGET,
                  'artifacts': {'bundle_sha256': digest, 'sbom_sha256': sha(files[policy['sbom']]),
                                'executable_sha256': sha(native.read_static(path, digest)['bwrap'])},
                  'libcap_corresponding_source': cap_sources}
    files[policy['provenance']] = json.dumps(provenance).encode()
    policy['files'] = {name: sha(raw) for name, raw in files.items()}
    for name, raw in files.items():
        (directory / name).write_bytes(raw)
    monkeypatch.setattr(assets, 'native_policy', lambda _root: policy)
    wheel = tmp_path / 'localsetup-1.2.3-py3-none-any.whl'
    identity = {'version': '1.2.3', 'source_commit': 'a' * 40,
                'wheel': wheel.name, 'wheel_sha256': sha(b'wheel')}
    monkeypatch.setattr(assets, 'checked_wheel', lambda *_args: (b'wheel', identity))
    monkeypatch.setattr(assets, 'git', lambda _root, *args:
                        'a' * 40 if args[0] == 'rev-parse' else '')
    return directory, policy, files, wheel


def test_complete_deterministic_set_preserves_sources_and_checksums(native_set, tmp_path):
    directory, policy, files, wheel = native_set
    outputs = [tmp_path / 'one', tmp_path / 'two']
    for output in outputs:
        result = assets.prepare(tmp_path, wheel, output, native_directory=directory)
        assert result['ok'] and len(result['assets']) == 10
        for name, raw in files.items():
            assert (output / name).read_bytes() == raw
        manifest = json.loads((output / assets.MANIFEST).read_bytes())
        assert manifest['native_origin'] == {'repository': policy['repository'], 'tag': policy['tag']}
        for name, digest in manifest['files'].items():
            assert sha((output / name).read_bytes()) == digest
        lines = (output / assets.CHECKSUMS).read_text().splitlines()
        assert len(lines) == 9
        for line in lines:
            digest, name = line.split('  ')
            assert sha((output / name).read_bytes()) == digest
    assert {p.name: p.read_bytes() for p in outputs[0].iterdir()} == {
        p.name: p.read_bytes() for p in outputs[1].iterdir()}
    with pytest.raises(ValueError, match='fresh directory'):
        assets.prepare(tmp_path, wheel, outputs[0], native_directory=directory)


@pytest.mark.parametrize('kind', ['tamper', 'missing', 'symlink', 'provenance'])
def test_invalid_native_set_creates_no_release_output(native_set, tmp_path, kind):
    directory, policy, files, wheel = native_set
    target = directory / policy['provenance']
    if kind == 'tamper':
        target.write_bytes(b'tampered')
    elif kind == 'missing':
        (directory / 'libcap2_2.66.orig.tar.xz').unlink()
    elif kind == 'symlink':
        target.unlink()
        target.symlink_to(directory / policy['sbom'])
    else:
        value = json.loads(target.read_bytes())
        value['artifacts']['bundle_sha256'] = '0' * 64
        target.write_text(json.dumps(value))
        policy['files'][target.name] = sha(target.read_bytes())
    output = tmp_path / 'out'
    with pytest.raises(ValueError):
        assets.prepare(tmp_path, wheel, output, native_directory=directory)
    assert not output.exists()


def test_download_is_exact_pinned_set_and_failed_download_never_publishes(native_set, tmp_path, monkeypatch):
    directory, policy, files, wheel = native_set
    calls = []
    def download(command, **kwargs):
        calls.append(command)
        assert command[:4] == ['gh', 'release', 'download', policy['tag']]
        assert command[command.index('--repo') + 1] == policy['repository']
        assert command.count('--pattern') == 7 and kwargs['timeout'] == 120
        destination = Path(command[command.index('--dir') + 1])
        for name, raw in files.items():
            (destination / name).write_bytes(raw)
    monkeypatch.setattr(assets.subprocess, 'run', download)
    assert assets.prepare(tmp_path, wheel, tmp_path / 'downloaded')['ok']
    assert len(calls) == 1
    def failure(*_args, **_kwargs):
        raise subprocess.TimeoutExpired('gh', 120)
    monkeypatch.setattr(assets.subprocess, 'run', failure)
    with pytest.raises(subprocess.TimeoutExpired):
        assets.prepare(tmp_path, wheel, tmp_path / 'failed')
    assert not (tmp_path / 'failed').exists()


@pytest.fixture
def source_wheel(tmp_path, monkeypatch):
    (tmp_path / 'VERSION').write_text('1.2.3\n')
    (tmp_path / 'pyproject.toml').write_text('''[project]
requires-python = ">=3.12"
dependencies = ["requests>=2"]
[project.scripts]
lscli = "ls.core.agent.cli:main"
''')
    sdk = tmp_path / 'vendor/lscli/manifest.json'
    sdk.parent.mkdir(parents=True)
    sdk.write_bytes(b'fixture')
    module = 'ls/core/agent/cli.py'
    (tmp_path / module).parent.mkdir(parents=True)
    (tmp_path / module).write_bytes(b'code')
    monkeypatch.setattr(release_wheel, 'git', lambda _root, *args:
                        'a' * 40 if args[0] == 'rev-parse' else module if args[0] == 'ls-files' else '')
    monkeypatch.setattr(release_wheel, 'inspect_artifact', lambda _path:
                        {'manifest_sha256': sha(b'fixture')})
    path = tmp_path / 'localsetup-1.2.3-py3-none-any.whl'
    def write(content=b'code', version='1.2.3', omit=False,
              dependency='requests>=2', entrypoint='ls.core.agent.cli:main'):
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('localsetup-1.2.3.dist-info/METADATA',
                             f'Name: localsetup\nVersion: {version}\nRequires-Python: >=3.12\nRequires-Dist: {dependency}\n')
            archive.writestr('localsetup-1.2.3.dist-info/entry_points.txt', f'[console_scripts]\nlscli = {entrypoint}\n')
            archive.writestr('localsetup-1.2.3.dist-info/WHEEL', 'Root-Is-Purelib: true\nTag: py3-none-any\n')
            if not omit:
                archive.writestr(module, content)
    write()
    return path, write


def test_wheel_matches_complete_source_and_version(source_wheel, tmp_path):
    path, _write = source_wheel
    raw, identity = release_wheel.checked_wheel(tmp_path, path)
    assert identity['wheel_sha256'] == sha(raw)
    assert identity['source_commit'] == 'a' * 40


@pytest.mark.parametrize('change', ['stale', 'version', 'missing', 'dirty', 'foreign', 'dependencies', 'entrypoint'])
def test_stale_or_incomplete_wheel_is_rejected(source_wheel, tmp_path, monkeypatch, change):
    path, write = source_wheel
    if change == 'stale':
        write(content=b'older code')
    elif change == 'version':
        write(version='0.1.0')
    elif change == 'missing':
        write(omit=True)
    elif change == 'foreign':
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('foreign_package/__init__.py', b'foreign code')
    elif change == 'dependencies':
        write(dependency='unknown-package==9.9.9')
    elif change == 'entrypoint':
        write(entrypoint='unexpected.module:main')
    else:
        monkeypatch.setattr(release_wheel, 'git', lambda *_args: 'dirty')
    with pytest.raises(ValueError):
        release_wheel.checked_wheel(tmp_path, path)
