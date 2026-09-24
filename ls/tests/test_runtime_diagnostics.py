import json
from pathlib import Path
import sys

import pytest

from ls.core.agent import runtime_diagnostics as diagnostics
from ls.core.agent.runtime_install import _write_json
from ls.core.agent.runtime_integrity import seal
from ls.core.agent.runtime_lock import LOCK_NAME, runtime_use


@pytest.fixture
def runtime(tmp_path, synthetic_runtime_interpreter):
    root = tmp_path / 'runtimes'
    root.mkdir(mode=0o700)
    release = root / ('a' * 64)
    release.mkdir(mode=0o700)
    (release / 'venv').mkdir(mode=0o700)
    (release / 'venv/bin').mkdir(mode=0o700)
    (release / 'venv/bin/python').symlink_to(synthetic_runtime_interpreter)
    file = release / 'venv/fixture'
    file.write_bytes(b'installed')
    file.chmod(0o600)
    record = {'schema_version': 1, 'status': 'installed', 'sha256': release.name,
              'inventory_sha256': seal(release)}
    _write_json(release / 'status.json', record)
    _write_json(root / 'current.json', record)
    with runtime_use(root):
        pass
    return root, release


def test_missing_and_incomplete_runtime_never_creates_state(tmp_path):
    root = tmp_path / 'absent'
    assert diagnostics.runtime(root)['status'] == 'missing'
    assert not root.exists()
    root.mkdir(mode=0o700)
    assert diagnostics.runtime(root)['status'] == 'incomplete'
    assert list(root.iterdir()) == []
    with runtime_use(root):
        pass
    before = list(root.iterdir())
    assert diagnostics.runtime(root)['status'] == 'incomplete'
    assert list(root.iterdir()) == before


def test_verified_runtime_and_busy_upgrade(runtime):
    root, release = runtime
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
    report = diagnostics.runtime(root)
    assert report['status'] == 'verified'
    assert report['capabilities']['reader'] == {'status': 'missing', 'execution_tested': False}
    assert report['capabilities']['crypto_execution'] == {
        'status': 'not_tested', 'execution_tested': False}
    with runtime_use(root, exclusive=True):
        assert diagnostics.runtime(root)['status'] == 'busy'
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('damage', ['pointer', 'status', 'inventory', 'file', 'lock'])
def test_invalid_runtime_has_sanitized_result(runtime, damage):
    root, release = runtime
    targets = {'pointer': root / 'current.json', 'status': release / 'status.json',
               'inventory': release / 'inventory.json', 'file': release / 'venv/fixture',
               'lock': root / LOCK_NAME}
    if damage == 'lock':
        targets[damage].chmod(0o644)
    else:
        targets[damage].write_text('private-value')
    assert diagnostics.runtime(root) == {'status': 'invalid'}


def test_profiles_are_validated_without_credentials_or_value_disclosure(tmp_path):
    path = tmp_path / 'profiles.json'
    assert diagnostics.profiles(path) == {'status': 'missing', 'count': 0}
    path.write_text('private-value')
    path.chmod(0o600)
    assert diagnostics.profiles(path) == {'status': 'invalid', 'count': 0}
    def valid():
        return {'base_url': 'https://example.invalid/v1/', 'api': 'responses',
                'model': 'fixture', 'credential_env': 'MISSING_FIXTURE_CREDENTIAL',
                'timeout_seconds': 5, 'capabilities': [], 'allow_loopback_http': False}
    path.write_text(json.dumps({'schema_version': 1, 'profiles': {'private-name': valid()}}))
    path.chmod(0o644)
    assert diagnostics.profiles(path) == {'status': 'verified', 'count': 1}
    from ls.core.agent.profile_inventory import inventory
    before = path.read_bytes()
    for unsafe in ('file', 'ancestor'):
        target = path if unsafe == 'file' else tmp_path
        original = target.stat().st_mode & 0o7777
        target.chmod(0o666 if unsafe == 'file' else 0o777)
        try:
            assert diagnostics.profiles(path) == {'status': 'invalid', 'count': 0}
            assert len(inventory(path)['profiles']) == 1
            assert path.read_bytes() == before
            assert target.stat().st_mode & 0o7777 == (0o666 if unsafe == 'file' else 0o777)
        finally:
            target.chmod(original)


def test_doctor_static_success_is_separate_from_execution(runtime, tmp_path, monkeypatch):
    from ls.core.agent import diagnostics as report
    root, release = runtime
    package = tmp_path / 'package'
    (package / '_sdk_payload').mkdir(parents=True)
    monkeypatch.setattr(report, 'verify', lambda path: None)
    from ls.core.agent import installed_capabilities
    monkeypatch.setattr(installed_capabilities, 'dependencies', lambda release: {'status': 'verified'})
    monkeypatch.setattr(diagnostics, 'profiles', lambda path: {'status': 'verified', 'count': 1})
    result = report.inspect(package_root=package, home=tmp_path / 'absent-home', runtime_root=root)
    assert result['status'] == 'static_verified'
    assert result['execution_available'] is False
    assert result['runtime']['status'] == 'verified'
    assert not (tmp_path / 'absent-home').exists()


def test_doctor_inspection_does_not_import_providers(tmp_path):
    import subprocess
    code = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ls.core.agent.diagnostics import inspect
inspect(package_root=Path(sys.argv[2]), home=Path(sys.argv[2]))
assert not any(n.startswith(('pydantic_ai', 'pydantic_graph', 'openai', 'httpx')) for n in sys.modules)
assert not Path(sys.argv[2]).exists()
"""
    subprocess.run([sys.executable, '-I', '-S', '-c', code,
                    str(Path(__file__).resolve().parents[2]), str(tmp_path / 'absent')],
                   check=True, timeout=10, capture_output=True)


def test_doctor_cli_overrides_exit_and_output_failure(tmp_path, monkeypatch, capfd):
    from ls.core.agent import cli, doctor_output
    report = {'schema_version': 1, 'status': 'static_verified', 'execution_available': False}
    seen = {}
    def inspect(**options):
        seen.update(options)
        return report
    monkeypatch.setattr(cli, 'inspect', inspect)
    assert cli.main(['doctor', '--format', 'json', '--runtime-root', str(tmp_path),
                     '--profiles', str(tmp_path / 'profiles')]) == 0
    assert json.loads(capfd.readouterr().out) == report
    assert seen == {'runtime_root': tmp_path, 'profiles_path': tmp_path / 'profiles'}
    def blocked(*args):
        raise TimeoutError('private diagnostic detail')
    monkeypatch.setattr(doctor_output, 'emit', blocked)
    assert cli.main(['doctor', '--format', 'json']) == 2
    assert capfd.readouterr() == ('', '')


@pytest.mark.parametrize('name', ['pointer', 'status'])
@pytest.mark.parametrize('damage', ['missing', 'oversized'])
def test_missing_and_oversized_records_are_bounded(runtime, name, damage):
    root, release = runtime
    path = root / 'current.json' if name == 'pointer' else release / 'status.json'
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b' ' * (64 * 1024 + 1))
    expected = 'incomplete' if damage == 'missing' else 'invalid'
    assert diagnostics.runtime(root) == {'status': expected}
    assert path.exists() == (damage != 'missing')
