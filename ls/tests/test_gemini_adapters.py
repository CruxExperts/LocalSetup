"""Gemini filesystem ownership and home mapping, without native client startup."""
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.gemini_prerequisite import gemini_personal_root
from ls.core.client_registry import load_client_registry
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.core.personal_repair import repair_personal
from ls.core.combined_repair import repair_combined
from ls.core.personal_detach import detach_personal
from ls.core.detach import detach_platforms
from ls.tests.test_install_flow import make_temp_repo


def test_home_override_static_check_creates_no_state(tmp_path, monkeypatch):
    monkeypatch.delenv('GEMINI_CLI_HOME', raising=False)
    assert gemini_personal_root(tmp_path)['ok']
    for value in ('', str(tmp_path)):
        monkeypatch.setenv('GEMINI_CLI_HOME', value)
        assert gemini_personal_root(tmp_path)['ok']
    for value in ('.', ' ', str(tmp_path / 'alternate')):
        monkeypatch.setenv('GEMINI_CLI_HOME', value)
        report = gemini_personal_root(tmp_path)
        assert not report['ok'] and not report['host_verified']
        assert str(tmp_path / 'alternate') not in report['reason']
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_alternate_home_blocks_personal_writes_only(tmp_path, monkeypatch, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.setenv('GEMINI_CLI_HOME', str(home / 'alternate'))
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['gemini-cli'], skill_scope=scope)
    if scope == 'repo':
        apply_plan(root, plan, home)
        assert verify_install(root, home, target_root=root)['ok']
    else:
        with pytest.raises(RuntimeError, match='gemini_personal_root'):apply_plan(root, plan, home)
        assert not (home / '.agents/skills').exists()
    assert not (home / 'alternate').exists()


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_common_lifecycle_preserves_native_content_and_checks_affected_owners(tmp_path, monkeypatch, mode):
    monkeypatch.delenv('GEMINI_CLI_HOME', raising=False)
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    preserved = [base / rel for base in (root, home) for rel in (
        '.gemini/settings.json', '.gemini/GEMINI.md', '.gemini/trustedFolders.json',
        '.gemini/skills/ls-localsetup-project-context/SKILL.md', '.gemini/sessions/fixture')]
    preserved.append(root / 'GEMINI.md')
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True);path.write_text('preserve native fixture')
    resource = root / 'ls/skills/ls-localsetup-project-context/references/gemini-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('resource')
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['gemini-cli', 'github-copilot-cli'],
                                      skill_scope='both', attach_mode=mode), home)
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/references/gemini-fixture.txt').read_text() == 'resource'
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes()
    monkeypatch.setenv('GEMINI_CLI_HOME', str(home / 'alternate'))
    assert not verify_install(root, home, target_root=root)['ok']
    resource.write_text('changed resource')
    other = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-cli'], attach_mode=mode)
    with pytest.raises(RuntimeError, match='gemini_personal_root'):apply_plan(root, other, home)
    package = home / '.agents/skills/ls-localsetup-project-context'
    if package.is_symlink():package.unlink()
    else:
        import shutil
        shutil.rmtree(package)
    assert not repair_personal(root, home, ['gemini-cli'], apply=True)['ok']
    assert not repair_combined(root, home, root, ['gemini-cli'], apply=True)['ok']
    assert receipt.read_bytes() == before and not (home / 'alternate').exists()
    assert detach_personal(root, home, ['gemini-cli'], apply=True)['applied']
    detach_platforms(root, home, root, ['gemini-cli'])
    assert (root / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert repair_personal(root, home, ['github-copilot-cli'], apply=True)['ok']
    assert (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert all(path.read_text() == 'preserve native fixture' for path in preserved)


def test_registry_separates_aggregate_discovery_from_write_paths():
    row = load_client_registry(Path(__file__).resolve().parents[2]).variant('gemini', 'gemini-cli').data
    assert row['skills']['repo']['resolution'] == 'aggregate'
    assert row['skills']['repo']['paths'] == ('.agents/skills', '.gemini/skills')
    assert row['compatibility']['repo_write_paths'] == ('.agents/skills',)
    assert row['integration']['qualification']['host'] == 'not-run'
    assert row['goal']['status'] == 'unverified'


def test_repository_at_home_is_not_personal_ownership(tmp_path, monkeypatch):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.setenv('GEMINI_CLI_HOME', str(tmp_path / 'alternate'))
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['gemini-cli'],
                                      skill_scope='repo', target_root=home), home)
    assert verify_install(root, home, target_root=home)['ok']
    assert (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
