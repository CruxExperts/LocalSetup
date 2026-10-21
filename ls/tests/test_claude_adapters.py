"""Native Claude adapter preservation, without invoking Claude or its provider."""
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.claude_prerequisite import claude_personal_root
from ls.core.client_registry import load_client_registry
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.core.personal_repair import repair_personal
from ls.core.combined_repair import repair_combined
from ls.core.personal_detach import detach_personal
from ls.core.detach import detach_platforms
from ls.tests.test_install_flow import make_temp_repo


def test_native_override_static_check_is_provider_free(tmp_path, monkeypatch):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    assert claude_personal_root(tmp_path)['ok']
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / '.claude'))
    assert claude_personal_root(tmp_path)['ok']
    for override in ('', '.claude', str(tmp_path / 'alternate')):
        monkeypatch.setenv('CLAUDE_CONFIG_DIR', override)
        assert not claude_personal_root(tmp_path)['ok']
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_override_blocks_personal_writes_only(tmp_path, monkeypatch, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / 'alternate'))
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['claude-code'], skill_scope=scope)
    if scope == 'repo':
        apply_plan(root, plan, home)
        assert verify_install(root, home, target_root=root)['ok']
    else:
        with pytest.raises(RuntimeError, match='claude_personal_root'):apply_plan(root, plan, home)
        assert not (home / '.claude/skills').exists()
    assert not (home / 'alternate').exists()


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_claude_native_lifecycle_preserves_context_and_override_boundaries(tmp_path, monkeypatch, mode):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    preserved = [base / rel for base in (root, home) for rel in (
        '.claude/settings.json', '.claude/settings.local.json', '.claude/CLAUDE.md',
        '.claude/rules/custom.md', '.claude/skills/custom/SKILL.md', '.claude/sessions/fixture')]
    preserved += [root / 'CLAUDE.md', root / 'CLAUDE.local.md']
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True);path.write_text('preserve fixture')
    resource = root / 'ls/skills/ls-localsetup-project-context/references/claude-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('resource')
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['claude-code'],
                                      skill_scope='both', attach_mode=mode), home)
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):
        assert (base / '.claude/skills/ls-localsetup-project-context/references/claude-fixture.txt').read_text() == 'resource'
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / 'alternate'))
    assert not verify_install(root, home, target_root=root)['ok']
    # Canonical package refresh from another client also affects recorded Claude ownership.
    resource.write_text('changed canonical resource')
    other = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-cli'], attach_mode=mode)
    with pytest.raises(RuntimeError, match='claude_personal_root'):apply_plan(root, other, home)
    package = home / '.claude/skills/ls-localsetup-project-context'
    if package.is_symlink():package.unlink()
    else:
        import shutil
        shutil.rmtree(package)
    assert not repair_personal(root, home, ['claude-code'], apply=True)['ok']
    assert not repair_combined(root, home, root, ['claude-code'], apply=True)['ok']
    assert receipt.read_bytes() == before and not (home / 'alternate').exists()
    assert detach_personal(root, home, ['claude-code'], apply=True)['applied']
    detach_platforms(root, home, root, ['claude-code'])
    assert all(path.read_text() == 'preserve fixture' for path in preserved)


def test_goal_metadata_distinguishes_documented_command_from_hard_budgets():
    row = load_client_registry(Path(__file__).resolve().parents[2]).variant('claude-code', 'claude-code-cli').data
    assert row['goal']['status'] == 'supported'
    assert '/goal' in row['goal']['commands']
    assert row['goal']['limits']['status'] == 'unverified'
    assert row['integration']['qualification']['host'] == 'not-run'
