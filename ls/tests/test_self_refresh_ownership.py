import json
from pathlib import Path

import pytest
import yaml

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.config import InstallConfig
from ls.core.plan import build_install_plan
from ls.core.personal_update import build_recorded_both_plan
from ls.core.registry import load_registry
from ls.core.self_refresh import recorded_refresh
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_recorded_refresh_keeps_owners_paths_and_exposure(tmp_path, scope, mode):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-context'], platform_ids=['codex'],
               attach_mode=mode, skill_scope=scope), home)
    before = json.loads((root / '.localsetup/lock.json').read_text())
    neighbors = []
    for row in [*before['adapter_targets'], *before['personal_adapter_targets']]:
        neighbor = Path(row['path']) / 'custom-notes.txt'
        neighbor.write_bytes(b'custom neighbor unchanged\n')
        neighbors.append(neighbor)
    plan = recorded_refresh(root, home, root, InstallConfig(), ['core'], None, False)
    assert plan.rollback_metadata['platforms'] == ['codex']
    assert plan.rollback_metadata['repo_packages'] == before['repo_packages']
    exposed = [a for a in plan.actions if a.kind in {'attach_repo_path', 'attach_personal_path'}]
    assert len(exposed) == (2 if scope == 'both' else 1)
    assert all(a.details['platforms'] == ['codex'] and a.details['mode'] == mode for a in exposed)
    expected = {row['path']: row['packages'] for row in
                [*before['adapter_targets'], *before['personal_adapter_targets']]}
    assert {str(a.path): a.details['packages'] for a in exposed} == expected
    apply_plan(root, plan, home)
    after = json.loads((root / '.localsetup/lock.json').read_text())
    assert after['platforms'] == ['codex']
    assert after['adapter_targets'] == before['adapter_targets']
    # The recorded planner enriches old rows with explicit per-owner packages.
    assert [{key: row[key] for key in original} for row, original in
            zip(after['personal_adapter_targets'], before['personal_adapter_targets'], strict=True)] == before['personal_adapter_targets']
    from ls.core.installation_ownership import InstallationOwner
    from ls.core.personal_registry import owner_key
    for row in after['personal_adapter_targets']:
        assert row['owner_packages'] == {owner_key(InstallationOwner(**owner)): row['packages']
                                         for owner in row['owners']}
    assert all(path.read_bytes() == b'custom neighbor unchanged\n' for path in neighbors)


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_recorded_refresh_migrates_legacy_workflow_package_exposure(tmp_path, mode):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    old = 'ls-workflow-ops-tmux-session'
    new = 'ls-wf-sudo-tmux-session'
    workflows = root / 'ls/workflows'
    old_source = workflows / old
    new_source = workflows / new
    if not old_source.exists():
        new_source.rename(old_source)
    manifest = old_source / 'workflow.yaml'
    data = yaml.safe_load(manifest.read_text())
    data['workflow_id'] = 'ops-tmux-session'
    data['display_name'] = 'Ops Tmux Session'
    data['aliases'] = [alias for alias in data['aliases']
                       if alias not in {old, new, 'ops-tmux-session'}]
    manifest.write_text(yaml.safe_dump(data))
    skill = old_source / 'SKILL.md'
    skill.write_text(skill.read_text().replace(f'name: {new}', f'name: {old}', 1))
    pack_path = root / 'ls/config/pack.yaml'
    pack = yaml.safe_load(pack_path.read_text())
    for names in pack['workflow_packs'].values():
        names[:] = [old if name == new else name for name in names]
    pack_path.write_text(yaml.safe_dump(pack))

    apply_plan(root, build_install_plan(root, home, global_preset='custom',
        global_workflows=[old], repo_preset='custom', repo_workflows=[old],
        platform_ids=['codex'], attach_mode=mode, skill_scope='both'), home)
    receipt = root / '.localsetup/lock.json'
    before = json.loads(receipt.read_text())
    paths = [Path(row['path']) for row in
             [*before['adapter_targets'], *before['personal_adapter_targets']]]
    for path in paths:
        (path / 'custom-notes.txt').write_bytes(b'custom neighbor unchanged\n')

    old_source.rename(new_source)
    manifest = new_source / 'workflow.yaml'
    data = yaml.safe_load(manifest.read_text())
    data['workflow_id'] = 'sudo-tmux-session'
    data['display_name'] = 'Sudo Tmux Session'
    data['aliases'].extend([old, 'ops-tmux-session'])
    manifest.write_text(yaml.safe_dump(data))
    skill = new_source / 'SKILL.md'
    skill.write_text(skill.read_text().replace(f'name: {old}', f'name: {new}', 1))
    pack = yaml.safe_load(pack_path.read_text())
    for names in pack['workflow_packs'].values():
        names[:] = [new if name == old else name for name in names]
    pack_path.write_text(yaml.safe_dump(pack))

    explicit = build_install_plan(root, home, global_preset='custom',
                                  global_workflows=[old], platform_ids=[])
    assert explicit.rollback_metadata['workflows'] == [new]
    recorded = build_recorded_both_plan(root, home, root)
    assert recorded.rollback_metadata['global_baseline_workflows'] == [new]
    assert new in recorded.rollback_metadata['global_baseline_packages']
    assert old not in recorded.rollback_metadata['packages']
    plan = recorded_refresh(root, home, root, InstallConfig(), ['core'], None, False)
    exposed = [action for action in plan.actions
               if action.kind in {'attach_repo_path', 'attach_personal_path'}]
    assert {action.path for action in exposed} == set(paths)
    assert all(action.details['platforms'] == ['codex'] and action.details['mode'] == mode
               and new in action.details['packages'] and old not in action.details['packages']
               for action in exposed)
    assert all(new in selection and old not in selection
               for action in exposed if action.kind == 'attach_personal_path'
               for selection in action.details['owner_packages'].values())
    apply_plan(root, plan, home)
    after = json.loads(receipt.read_text())
    assert new in after['workflows'] and old not in after['workflows']
    assert after['repo_workflows'] == [new]
    assert new in after['global_baseline_workflows'] and old not in after['global_baseline_workflows']
    assert new in after['repo_packages'] and old not in after['repo_packages']
    assert all(new in row['packages'] and old not in row['packages'] for row in
               [*after['adapter_targets'], *after['personal_adapter_targets']])
    assert all((path / new).exists() and not (path / old).exists() and
               (path / 'custom-notes.txt').read_bytes() == b'custom neighbor unchanged\n'
               for path in paths)
    registry = load_registry(home / '.local/share/localsetup/registry.json')
    assert all(new in owner['packages'] and old not in owner['packages']
               for owner in registry['personal_owners'].values())


def test_empty_recorded_selection_does_not_discover_shared_path(tmp_path):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, packs=['core'], platform_ids=[]), home)
    shared = root / '.agents/skills'; shared.mkdir(parents=True)
    (shared / 'notes.txt').write_text('keep')
    plan = recorded_refresh(root, home, root, InstallConfig(), ['core'], None, False)
    assert plan.rollback_metadata['platforms'] == []
    assert not any(a.kind.startswith('attach_') for a in plan.actions)
    with pytest.raises(ValueError, match='recorded adapter modes'):
        recorded_refresh(root, home, root, InstallConfig(attach_mode='portable'), ['core'], None, True)


def test_ambiguous_shared_path_fails_before_dependencies(tmp_path, monkeypatch):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    shared = root / '.agents/skills'; shared.mkdir(parents=True)
    (shared / '.localsetup-portable').write_text('managed_by=localsetup\n')
    monkeypatch.setattr(cli, 'ensure_dependencies', lambda *a, **kw: pytest.fail('dependency work'))
    with pytest.raises(ValueError, match='recorded ownership or explicit'):
        cli._run_self_refresh(root, InstallConfig(dependency_mode='uv-sync'), home)
    assert not (root / '.localsetup').exists()
    assert not home.exists()


def test_explicit_unrecorded_selection_is_supported(tmp_path):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    result = cli._run_self_refresh(root, InstallConfig(dependency_mode='prompt-only'), home,
                                   packs_override=['core'], platforms_override=['codex'])
    assert result['ok']
    assert result['selected']['platforms'] == ['codex']


def test_recorded_paths_survive_catalog_change(tmp_path):
    import yaml
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-context'], platform_ids=['codex']), home)
    path = root / 'ls/config/platforms.yaml'
    data = yaml.safe_load(path.read_text())
    for row in data['platforms']:
        if row['id'] == 'codex': row['repo_paths'] = ['.new-catalog/skills']
    path.write_text(yaml.safe_dump(data))
    plan = recorded_refresh(root, home, root, InstallConfig(), ['core'], None, False)
    assert [a.path for a in plan.actions if a.kind == 'attach_repo_path'] == [root / '.agents/skills']
    apply_plan(root, plan, home)
    assert not (root / '.new-catalog').exists()


@pytest.mark.parametrize('config,clients,explicit_mode,match', [
    (InstallConfig(), ['cursor'], False, 'change recorded clients'),
    (InstallConfig(attach_mode='portable'), None, True, 'recorded adapter modes'),
    (InstallConfig(repo_skills=['ls-context']), None, False, 'recorded exposure'),
    (InstallConfig(skill_scope='personal'), None, False, 'recorded scope'),
])
def test_recorded_override_requires_explicit_transition(tmp_path, config, clients, explicit_mode, match):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-context'], platform_ids=['codex']), home)
    receipt = root / '.localsetup/lock.json'; before = receipt.read_bytes()
    with pytest.raises(ValueError, match=match):
        recorded_refresh(root, home, root, config, ['core'], clients, explicit_mode)
    assert receipt.read_bytes() == before


def test_fresh_self_refresh_only_updates_library(tmp_path):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    result = cli._run_self_refresh(root, InstallConfig(), home, packs_override=['core'])
    assert result['ok'] and result['selected']['platforms'] == []
    assert (home / '.local/share/localsetup/packages/ls-context/SKILL.md').is_file()
    assert not (root / '.agents/skills').exists()


@pytest.mark.parametrize('receipt', [[], {'version': '1'}, {'platforms': 'codex'},
    {'skill_scope': 'bad'}, {'adapter_targets': [{'path': '../outside'}]}])
def test_explicit_selection_does_not_bypass_invalid_legacy_receipt(tmp_path, receipt):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    path = root / '.localsetup/lock.json'; path.parent.mkdir()
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        recorded_refresh(root, home, root, InstallConfig(), ['core'], ['codex'], False)
    assert not home.exists()


@pytest.mark.parametrize('override', ['packs', 'global'])
def test_library_override_preserves_recorded_target_selection(tmp_path, override):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, packs=['core'], platform_ids=['codex']), home)
    path = root / '.localsetup/lock.json'; before = json.loads(path.read_text())
    config = InstallConfig(global_preset='custom', global_skills=['ls-cloudflare-dns']) if override == 'global' else InstallConfig()
    plan = recorded_refresh(root, home, root, config, ['integrations'], None, False)
    baseline = plan.rollback_metadata
    assert 'ls-cloudflare-dns' in baseline['global_baseline_skills']
    if override == 'packs':
        assert baseline['global_baseline_packs'] == ['integrations']
    else:
        assert baseline['global_baseline_selectors']['skills'] == ['ls-cloudflare-dns']
        assert baseline['global_baseline_selectors']['preset'] == 'custom'
    assert baseline['repo_packages'] == before['repo_packages']
    apply_plan(root, plan, home)
    after = json.loads(path.read_text())
    assert after['adapter_targets'] == before['adapter_targets']
    assert (home / '.local/share/localsetup/packages/ls-cloudflare-dns/SKILL.md').is_file()
    assert not (root / '.agents/skills/ls-cloudflare-dns').exists()


def test_receipt_replacement_during_recorded_selection_fails(tmp_path, monkeypatch):
    from ls.core import self_refresh
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, packs=['core'], platform_ids=['codex']), home)
    path = root / '.localsetup/lock.json'
    original = self_refresh._build_recorded_plan
    def replaced(*args):
        path.write_bytes(path.read_bytes() + b'\n')
        return original(*args)
    monkeypatch.setattr(self_refresh, '_build_recorded_plan', replaced)
    with pytest.raises(ValueError, match='receipt changed during self-refresh selection'):
        recorded_refresh(root, home, root, InstallConfig(), ['core'], ['codex'], False)


def test_library_override_installs_first_workflow_without_exposing_it(tmp_path):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, global_preset='custom',
        global_skills=['ls-context'], repo_preset='custom', repo_skills=['ls-context'],
        platform_ids=['codex']), home)
    path = root / '.localsetup/lock.json'; before = json.loads(path.read_text())
    assert before['workflows'] == []
    name = 'ls-wf-repository-status-checkpoint'
    installed = home / '.local/share/localsetup/packages' / name
    assert not installed.exists()
    config = InstallConfig(global_preset='custom', global_workflows=[name])
    plan = recorded_refresh(root, home, root, config, [], None, False)
    apply_plan(root, plan, home)
    after = json.loads(path.read_text())
    assert after['global_baseline_workflows'] == [name]
    assert after['workflows'] == [name]
    assert (installed / 'SKILL.md').is_file()
    assert after['adapter_targets'] == before['adapter_targets']
    assert not (root / '.agents/skills' / name).exists()
