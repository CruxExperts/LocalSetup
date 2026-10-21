from pathlib import Path

import pytest

from ls.core import goose_prerequisite as goose
from ls.core.apply import apply_plan
from ls.core.apply_preflight import preflight_install_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo

ENABLED = 'extensions:\n  skills:\n    enabled: true\n    type: platform\n    name: skills\n'


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    for key in ('XDG_CONFIG_HOME', 'GOOSE_PATH_ROOT', 'GOOSE_ADDITIONAL_CONFIG_FILES', 'EXTENSIONS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(goose, 'SYSTEM_CONFIG', tmp_path / 'system-config')
    monkeypatch.setattr(goose.sys, 'platform', 'linux')


def configure(home, content=ENABLED):
    path = home / '.config/goose/config.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.mark.parametrize('content,status', [
    (ENABLED, 'configured'), (ENABLED.replace('true', 'false'), 'disabled'),
    ('', 'unknown'), ('extensions: {}', 'unknown'), ('extensions: [secret]', 'unknown'),
    (ENABLED.replace('true', 'yes'), 'unknown'),
    (ENABLED.replace('platform', 'stdio').replace('true', 'false'), 'unknown'),
    (ENABLED + '    name: duplicate\n', 'unknown'),
    (ENABLED.replace('skills:\n', 'skills: &alias\n'), 'unknown'),
    (ENABLED + '    available_tools: [restricted]\n', 'unknown'),
    (ENABLED + '    available_tools: []\n', 'configured'),
])
def test_static_configuration_is_explicit_and_never_host_attestation(tmp_path, content, status):
    path = configure(tmp_path, content)
    result = goose.goose_skills_configuration(tmp_path)
    assert result['status'] == status
    assert result['ok'] == (status == 'configured')
    assert not result['host_verified'] and result['scope'] == 'static-configuration'
    assert path.read_text() == content
    assert 'secret' not in result['reason']


@pytest.mark.parametrize('key,value', [
    ('GOOSE_PATH_ROOT', '/custom'), ('GOOSE_ADDITIONAL_CONFIG_FILES', '/layer'),
    ('EXTENSIONS', ''), ('XDG_CONFIG_HOME', '/custom'),
])
def test_overrides_require_effective_state_qualification(tmp_path, monkeypatch, key, value):
    configure(tmp_path)
    monkeypatch.setenv(key, value)
    assert goose.goose_skills_configuration(tmp_path)['status'] == 'unknown'


def test_missing_oversized_and_system_configuration_remain_untouched(tmp_path):
    assert goose.goose_skills_configuration(tmp_path)['status'] == 'unknown'
    assert not (tmp_path / '.config').exists()
    path = configure(tmp_path, ENABLED + '#' * 262144)
    assert goose.goose_skills_configuration(tmp_path)['status'] == 'unknown'
    path.write_text(ENABLED)
    goose.SYSTEM_CONFIG.write_text('extensions: {}')
    assert goose.goose_skills_configuration(tmp_path)['status'] == 'unknown'


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_install_resource_projection_and_verify_recheck(tmp_path, mode, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    resource = root / 'ls/skills/ls-localsetup-project-context/references/goose-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('resource fixture')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['goose-cli'], skill_scope=scope, attach_mode=mode)
    assert not (home / '.config').exists()
    assert not preflight_install_plan(root, plan, home)['ok']
    with pytest.raises(RuntimeError, match='Goose'):apply_plan(root, plan, home)
    assert not (root / '.localsetup/lock.json').exists()
    config = configure(home)
    apply_plan(root, plan, home)
    for base in ([root, home] if scope == 'both' else [home if scope == 'personal' else root]):
        assert (base / '.agents/skills/ls-localsetup-project-context/references/goose-fixture.txt').read_text() == 'resource fixture'
    assert verify_install(root, home, target_root=root)['ok']
    config.write_text(ENABLED.replace('true', 'false'))
    assert not verify_install(root, home, target_root=root)['ok']
    assert config.read_text() == ENABLED.replace('true', 'false')


@pytest.mark.parametrize('scope', ['personal', 'both'])
def test_repair_cannot_bypass_disabled_skills(tmp_path, scope):
    from ls.core.combined_repair import repair_combined
    from ls.core.personal_repair import repair_personal
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    config = configure(home)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['goose-cli'], skill_scope=scope), home)
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes()
    (home / '.agents/skills/ls-localsetup-project-context').unlink()
    config.write_text(ENABLED.replace('true', 'false'))
    result = repair_combined(root, home, root, apply=True) if scope == 'both' else repair_personal(root, home, ['goose-cli'], apply=True)
    assert not result['ok'] and not result['applied']
    assert any('Goose' in b for b in result['blockers'])
    assert receipt.read_bytes() == before
    assert not (home / '.agents/skills/ls-localsetup-project-context').exists()


def test_other_client_write_checks_recorded_goose_owner(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    config = configure(home)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['goose-cli'], skill_scope='both'), home)
    config.write_text(ENABLED.replace('true', 'false'))
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['github-copilot-cli'], skill_scope='both')
    assert not preflight_install_plan(root, plan, home)['ok']
    with pytest.raises(RuntimeError, match='Goose'):apply_plan(root, plan, home)


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_shared_owner_detach_preserves_custom_and_native_content(tmp_path, mode):
    from ls.core.detach import detach_platforms
    from ls.core.personal_detach import detach_personal
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    config = configure(home)
    for base in (root, home):
        adapter = base / '.agents/skills';adapter.mkdir(parents=True)
        (adapter / 'custom.txt').write_text('keep custom')
    native = home / '.local/share/goose/sessions/fixture'
    native.parent.mkdir(parents=True);native.write_text('keep session')
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['goose-cli', 'github-copilot-cli'], skill_scope='both', attach_mode=mode), home)
    config.write_text(ENABLED.replace('true', 'false'))
    # Detaching exposure must remain possible when the prerequisite is disabled.
    detach_platforms(root, home, root, ['goose-cli'])
    assert detach_personal(root, home, ['goose-cli'], apply=True)['applied']
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
        assert (base / '.agents/skills/custom.txt').read_text() == 'keep custom'
    assert config.read_text() == ENABLED.replace('true', 'false')
    assert native.read_text() == 'keep session'
