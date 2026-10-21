"""Common Codex adapters remain separate from native profile and nested context."""
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.client_registry import load_client_registry
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_codex_common_ownership_does_not_follow_native_profile_home(tmp_path, monkeypatch, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    native = home / 'codex-profile'
    monkeypatch.setenv('CODEX_HOME', str(native))
    preserved = [native / rel for rel in ('config.toml', 'AGENTS.md', 'AGENTS.override.md', 'sessions/fixture')]
    preserved += [root / rel for rel in ('.codex/config.toml', 'AGENTS.override.md',
        'nested/AGENTS.md', 'nested/.agents/skills/ls-localsetup-project-context/SKILL.md', '.codex/skills/custom/SKILL.md')]
    for base in (root, home):preserved.append(base / '.agents/skills/custom/SKILL.md')
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True);path.write_text('preserve fixture')
    resource = root / 'ls/skills/ls-localsetup-project-context/references/codex-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('package resource')
    clients = ['codex', 'github-copilot-cli']
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=clients,
                              skill_scope='both', attach_mode=mode)
    personal = [a for a in plan.actions if a.kind == 'attach_personal_path']
    assert len(personal) == 1 and personal[0].path == home / '.agents/skills'
    assert {o['client'] for o in personal[0].details['owners']} == set(clients)
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/references/codex-fixture.txt').read_text() == 'package resource'
    detach_platforms(root, home, root, ['codex'])
    assert detach_personal(root, home, ['codex'], apply=True)['applied']
    for base in (root, home):assert (base / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert verify_install(root, home, target_root=root)['ok']
    assert all(path.read_text() == 'preserve fixture' for path in preserved)
    assert not (native / 'skills').exists()


def test_codex_discovery_metadata_does_not_invent_duplicate_winner():
    row = load_client_registry(Path(__file__).resolve().parents[2]).variant('codex', 'codex-cli').data
    assert row['skills']['repo']['resolution'] == 'hierarchy'
    assert row['skills']['repo']['precedence_status'] == 'unverified'
    assert row['skills']['global']['paths'] == ('~/.agents/skills',)
    assert row['integration']['qualification']['host'] == 'not-run'

    assert row['policy']['global']['resolution'] == 'first-nonempty'
    assert row['config']['repo']['resolution'] == 'hierarchy'
    assert '/goal pause' in row['goal']['commands']
