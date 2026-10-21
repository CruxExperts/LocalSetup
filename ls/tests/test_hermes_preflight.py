from pathlib import Path

import pytest

from ls.core.hermes_adapter import hermes_adapter_blockers
from ls.core.models import PlanAction


def fixture(tmp_path, personal=True):
    source = tmp_path / 'source';home = tmp_path / 'home';target = tmp_path / 'project'
    package = source / 'ls/skills/ls-fixture';package.mkdir(parents=True)
    (package / 'SKILL.md').write_text('fixture')
    action = PlanAction('attach_personal_path' if personal else 'attach_repo_path',
                        (home if personal else target) / '.hermes/skills',
                        {'platforms': ['hermes-agent'], 'mode': 'portable',
                         'mutable_copy': True, 'packages': ['ls-fixture']})
    return source, home, target, package, action


@pytest.mark.parametrize('personal', [True, False])
def test_native_binding_is_provider_free_and_does_not_create_state(tmp_path, monkeypatch, personal):
    monkeypatch.delenv('HERMES_HOME', raising=False)
    source, home, target, package, action = fixture(tmp_path, personal)
    assert not hermes_adapter_blockers(source, [action], home, target)
    assert not home.exists() and not target.exists()


@pytest.mark.parametrize('damage', ['mode', 'path', 'designation', 'resource-link', 'home'])
def test_unsafe_hermes_writes_fail_before_install(tmp_path, monkeypatch, damage):
    monkeypatch.delenv('HERMES_HOME', raising=False)
    source, home, target, package, action = fixture(tmp_path)
    if damage == 'mode':action.details['mode'] = 'symlink'
    elif damage == 'path':action.path = home / '.agents/skills'
    elif damage == 'designation':action.details.pop('mutable_copy')
    elif damage == 'resource-link':(package / 'linked.md').symlink_to(package / 'SKILL.md')
    else:monkeypatch.setenv('HERMES_HOME', str(home / 'other-profile'))
    result = hermes_adapter_blockers(source, [action], home, target)
    assert len(result) == 1 and result[0]['status_code'] == 'hermes_adapter_preservation'
    assert not home.exists() and not target.exists()


def test_other_clients_ignore_hermes_environment(tmp_path, monkeypatch):
    source, home, target, package, action = fixture(tmp_path)
    action.details['platforms'] = ['codex']
    monkeypatch.setenv('HERMES_HOME', 'other-profile')
    assert not hermes_adapter_blockers(source, [action], home, target)


def test_apply_rejects_authored_link_before_canonical_mutation(tmp_path, monkeypatch):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.tests.test_install_flow import make_temp_repo
    monkeypatch.delenv('HERMES_HOME', raising=False)
    source = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(source, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-cli'],
                              skill_scope='personal', attach_mode='portable')
    action = next(a for a in plan.actions if a.kind == 'attach_personal_path')
    action.path = home / '.hermes/skills'
    action.details.update(platforms=['hermes-agent'], mutable_copy=True)
    action.details['owners'][0]['client'] = 'hermes-agent'
    package = source / 'ls/skills/ls-localsetup-project-context'
    (package / 'unsafe-resource').symlink_to(package / 'SKILL.md')
    library = Path(action.details['global_root'])
    with pytest.raises(RuntimeError, match='hermes_adapter_preservation'):
        apply_plan(source, plan, home)
    assert not library.exists() and not action.path.exists()


def test_combined_repository_repair_obeys_native_binding(tmp_path):
    source, home, target, package, action = fixture(tmp_path, personal=False)
    action.kind = 'repair_repo_path'
    action.path = target / '.agents/skills'
    result = hermes_adapter_blockers(source, [action], home, target)
    assert len(result) == 1 and 'binding' in result[0]['reason']
