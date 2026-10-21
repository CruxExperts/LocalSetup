"""Filesystem success must not certify Kilo's native source trust."""
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.kilo_loading import kilo_loading_assessment
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_native_loading_is_separate_from_filesystem_and_shared_ownership(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    preserved = [base / rel for base in (root, home) for rel in (
        '.kilo/skills/custom/SKILL.md', '.kilo/skills-code/custom/SKILL.md',
        '.kilo/kilo.json', '.kilo/rules/custom.md', '.kilo/sessions/fixture')]
    preserved += [root / 'AGENTS.md', home / '.config/kilo/kilo.json']
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True);path.write_text('keep native content')
    resource = root / 'ls/skills/ls-localsetup-project-context/references/kilo-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('resource')
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['kilo', 'codex'],
                                      skill_scope='both', attach_mode=mode), home)
    report = verify_install(root, home, target_root=root)
    assert report['ok'], report['issues']
    repo = next(row for row in report['native_loading'] if row['scope'] == 'repo')
    assert repo['status'] == ('unsupported-project-source' if mode == 'symlink' else 'source-contained')
    assert not repo['host_verified']
    if mode == 'symlink':assert any('ordinary project-root policy' in warning for warning in report['warnings'])
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/references/kilo-fixture.txt').read_text() == 'resource'
        assert not (base / '.kilo/skills/ls-localsetup-project-context').exists()
    detach_platforms(root, home, root, ['kilo'])
    assert detach_personal(root, home, ['kilo'], apply=True)['applied']
    for base in (root, home):assert (base / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert all(path.read_text() == 'keep native content' for path in preserved)


def test_source_assessment_reports_missing_and_contained_link_without_reading_content(tmp_path, monkeypatch):
    target = tmp_path / 'project';target.mkdir()
    package = target / 'owned';package.mkdir();(package / 'SKILL.md').write_text('not parsed')
    adapter = target / '.agents/skills';adapter.mkdir(parents=True)
    (adapter / 'ls-localsetup-project-context').symlink_to(package, target_is_directory=True)
    rows = [{'platforms': ['kilo'], 'repo_path': str(adapter), 'expected_packages': ['ls-localsetup-project-context']}]
    assert kilo_loading_assessment(target, rows, {})[0]['status'] == 'source-contained'
    (package / 'SKILL.md').unlink()
    assert kilo_loading_assessment(target, rows, {})[0]['status'] == 'unqualified'
    monkeypatch.setenv('KILO_CONFIG_CONTENT', 'private fixture')
    report = kilo_loading_assessment(target, rows, {})
    assert 'KILO_CONFIG_CONTENT' in report[-1]['override_names']
    assert 'private fixture' not in str(report)
    assert kilo_loading_assessment(target, [], {}) == []


def test_explicit_portable_update_preserves_recorded_native_path(tmp_path, capsys):
    import json
    from ls.core import cli
    from ls.tests.test_preferred_path_retention import prefer_common
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    prefer_common(root, historical=True, client='kilo')
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['kilo']), home)
    native = root / '.kilo/skills';(native / 'custom.txt').write_text('keep')
    receipt = root / '.localsetup/lock.json';old = json.loads(receipt.read_text())
    prefer_common(root, client='kilo')
    args = ['--source-root', str(root), '--home', str(home)]
    options = ['--target-directory', str(root), '--mode', 'portable']
    assert cli.main(args + ['plan'] + options) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['auto_mode'] == 'recorded_repo'
    assert json.loads(receipt.read_text()) == old
    assert cli.main(args + ['update'] + options) == 0
    capsys.readouterr()
    new = json.loads(receipt.read_text())
    assert new['attach_mode'] == 'portable' and new['platforms'] == ['kilo']
    assert {row['path'] for row in new['adapter_targets']} == {row['path'] for row in old['adapter_targets']}
    assert not (native / 'ls-localsetup-project-context').is_symlink()
    assert (native / 'custom.txt').read_text() == 'keep'
    assert not (root / '.agents/skills').exists()
    assert verify_install(root, home)['native_loading'][0]['status'] == 'source-contained'
