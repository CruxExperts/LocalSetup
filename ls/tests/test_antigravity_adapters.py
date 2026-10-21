from pathlib import Path
import pytest
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.core.personal_detach import detach_personal
from ls.core.client_registry import load_client_registry
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_application_projects_personal_skills_without_touching_other_variants(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    preserved = []
    for relative in ['.gemini/antigravity/skills/native/SKILL.md', '.gemini/antigravity-cli/skills/native.md',
                     '.gemini/GEMINI.md', '.gemini/antigravity/GEMINI.md', '.gemini/antigravity/state/fixture']:
        path = home / relative;path.parent.mkdir(parents=True, exist_ok=True);path.write_text('keep');preserved.append(path)
    native = home / '.gemini/config/skills/custom.md';native.parent.mkdir(parents=True);native.write_text('custom')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['antigravity-app','github-copilot-cli'],
                              skill_scope='both', attach_mode=mode)
    assert len([a for a in plan.actions if a.kind == 'attach_repo_path']) == 1
    assert len([a for a in plan.actions if a.kind == 'attach_personal_path']) == 2
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    assert (home / '.gemini/config/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert detach_personal(root, home, ['antigravity-app'], apply=True)['applied']
    assert not (home / '.gemini/config/skills/ls-localsetup-project-context').exists()
    assert (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert native.read_text() == 'custom'
    for path in preserved:assert path.read_text() == 'keep'


def test_antigravity_variants_have_separate_contracts_and_no_flat_package_export(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home';registry = load_client_registry(root)
    ide = registry.variant('antigravity','antigravity-ide').data
    app = registry.variant('antigravity','antigravity-app').data
    cli = registry.variant('antigravity','antigravity-cli').data
    assert tuple(ide['skills']['global']['paths']) == ('~/.gemini/antigravity/skills',)
    assert tuple(app['skills']['global']['paths']) == ('~/.gemini/config/skills',)
    assert tuple(cli['executables']) == ('agy',)
    assert tuple(ide['policy']['global']['paths']) == ('~/.gemini/GEMINI.md',)
    for row in [ide,cli]:
        assert 'compatibility' not in row and row['integration']['qualification']['catalog'] == 'bounded'
        with pytest.raises(ValueError, match='unknown platform'):
            build_install_plan(root, home, platform_ids=[row['id']])
    assert all(row['integration']['qualification']['host'] == 'not-run' for row in [ide,app,cli])
