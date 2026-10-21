from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_qwen_common_packages_preserve_native_collisions_and_shared_owners(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    clients = ['qwen-code-cli', 'github-copilot-cli']
    native_files = {}
    for base in (root, home):
        native = base / '.qwen/skills/ls-localsetup-project-context/SKILL.md';native.parent.mkdir(parents=True)
        native.write_text('native same-name fixture')
        native_files[native] = native.read_bytes()
        common = base / '.agents/skills';common.mkdir(parents=True)
        (common / 'custom.txt').write_text('keep')
    settings = home / '.qwen/settings.json';settings.write_text('{"fixture":true}')
    session = home / '.qwen/sessions/fixture';session.parent.mkdir();session.write_text('native session')
    resource = root / 'ls/skills/ls-localsetup-project-context/references/qwen-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('resource')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=clients,
                              skill_scope='both', attach_mode=mode)
    assert len([a for a in plan.actions if a.kind == 'attach_repo_path']) == 1
    assert len([a for a in plan.actions if a.kind == 'attach_personal_path']) == 1
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/references/qwen-fixture.txt').read_text() == 'resource'
    detach_platforms(root, home, root, ['qwen-code-cli'])
    assert detach_personal(root, home, ['qwen-code-cli'], apply=True)['applied']
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
        assert (base / '.agents/skills/custom.txt').read_text() == 'keep'
    for path, before in native_files.items():assert path.read_bytes() == before
    assert settings.read_text() == '{"fixture":true}' and session.read_text() == 'native session'


def test_qwen_catalog_does_not_claim_active_host_or_native_insertion():
    from ls.core.client_registry import load_client_registry
    row = load_client_registry(Path(__file__).resolve().parents[2]).variant('qwen-code', 'qwen-code-cli').data
    assert tuple(row['executables']) == ('qwen',)
    assert row['integration']['qualification']['host'] == 'not-run'
    assert row['insertion']['repo']['status'] == 'unverified'
    assert tuple(row['skills']['global']['paths']) == ('~/.agents/skills',)
