import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ls.core.aliases import collect_skill_aliases
from ls.core.apply import apply_plan
from ls.core.config import InstallConfig
from ls.core.personal_update import build_recorded_both_plan
from ls.core.path_contract import resolve_package_path, resolve_token
from ls.core.paths import PathValidationError
from ls.core.plan import build_install_plan
from ls.core.registry import load_registry
from ls.core.self_refresh import recorded_refresh
from ls.core.workflows import load_workflow_catalog
from ls.tests.test_install_flow import make_temp_repo


def _write_pack(root: Path, data: dict) -> None:
    (root / 'ls/config/pack.yaml').write_text(yaml.safe_dump(data))


def _legacy_adapter_after_rename(tmp_path: Path, *, mode: str = 'symlink', scope: str = 'repo',
                                target_root: Path | None = None):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    target = target_root or root
    old = 'ls-original-legacy-fixture'; new = 'ls-renamed-legacy-fixture'
    skill = root / 'ls/skills' / old
    skill.mkdir()
    (skill / 'SKILL.md').write_text(f'---\nname: {old}\ndescription: Legacy fixture.\n---\n')
    apply_plan(root, build_install_plan(
        root, home, skills=[old], platform_ids=['codex'], attach_mode=mode, skill_scope=scope,
        target_root=target,
    ), home, target_root=target)
    lock_path = target / '.localsetup/lock.json'
    lock = json.loads(lock_path.read_text())
    adapter = Path(lock['adapter_targets'][0]['path']) if lock.get('adapter_targets') else Path(
        lock['personal_adapter_targets'][0]['path']
    )
    skill.rename(skill.with_name(new))
    (root / 'ls/skills' / new / 'SKILL.md').write_text(
        (root / 'ls/skills' / new / 'SKILL.md').read_text().replace(f'name: {old}', f'name: {new}', 1)
    )
    pack_path = root / 'ls/config/pack.yaml'; pack = yaml.safe_load(pack_path.read_text())
    pack.setdefault('alias_policy', {})['skill_renames'] = {old: new}
    _write_pack(root, pack)
    marker_path = adapter / '.localsetup-adapter.json'
    marker = json.loads(marker_path.read_text())
    marker['packages'] = [name for name in marker.get('packages', []) if name != old]
    marker_path.write_text(json.dumps(marker, indent=2) + '\n')
    return root, home, old, new, adapter, lock_path


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_skill_rename_preserves_recorded_exposure_and_custom_neighbors(tmp_path, mode):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    old = 'ls-original-demo-skill'; new = 'ls-renamed-demo-skill'
    skill = root / 'ls/skills' / old
    skill.mkdir()
    (skill / 'SKILL.md').write_text(f'---\nname: {old}\ndescription: Demonstration skill.\n---\n')
    pack_path = root / 'ls/config/pack.yaml'
    pack = yaml.safe_load(pack_path.read_text())
    pack['packs']['experimental'].append(old)
    taxonomy = pack['extensions']['skill_taxonomy']
    taxonomy[old] = {'class': 'specialized', 'sort_priority': 70,
                     'tags': ['demo'], 'owner_scope': 'skill'}
    _write_pack(root, pack)

    apply_plan(root, build_install_plan(root, home, global_preset='custom',
        global_skills=[old], repo_preset='custom', repo_skills=[old],
        platform_ids=['codex'], attach_mode=mode, skill_scope='both'), home)
    receipt = root / '.localsetup/lock.json'
    before = json.loads(receipt.read_text())
    paths = [Path(row['path']) for row in
             [*before['adapter_targets'], *before['personal_adapter_targets']]]
    for path in paths:
        (path / 'custom-notes.txt').write_bytes(b'custom neighbor unchanged\n')

    skill.rename(skill.with_name(new))
    renamed = root / 'ls/skills' / new / 'SKILL.md'
    renamed.write_text(renamed.read_text().replace(f'name: {old}', f'name: {new}', 1))
    pack['packs']['experimental'] = [new if name == old else name
                                      for name in pack['packs']['experimental']]
    taxonomy[new] = taxonomy.pop(old)
    pack['alias_policy']['skill_renames'] = {old: new}
    _write_pack(root, pack)

    legacy = 'localsetup-original-demo-skill'
    library = home / '.local/share/localsetup/packages'
    for alias in (old, legacy):
        assert resolve_package_path(home, alias, 'SKILL.md') == library / alias / 'SKILL.md'
        assert resolve_package_path(home, alias, 'SKILL.md', source_root=root) == library / new / 'SKILL.md'
        assert resolve_token(f'localsetup://package/{alias}/SKILL.md',
                             source_root=root, home=home) == library / new / 'SKILL.md'
    aliases = collect_skill_aliases(root / 'ls/skills')
    assert aliases[old] == aliases[legacy] == new
    explicit = build_install_plan(root, home, global_preset='custom',
                                  global_skills=[old, legacy, new], platform_ids=[])
    assert explicit.rollback_metadata['skills'] == [new]
    assert explicit.rollback_metadata['aliases'][old] == new
    assert explicit.rollback_metadata['aliases'][legacy] == new
    recorded = build_recorded_both_plan(root, home, root)
    assert recorded.rollback_metadata['global_baseline_skills'] == [new]
    assert recorded.rollback_metadata['global_baseline_selectors']['skills'] == [new]
    assert recorded.rollback_metadata['repo_selectors']['skills'] == [new]
    assert old not in recorded.rollback_metadata['packages']

    plan = recorded_refresh(root, home, root, InstallConfig(), ['experimental'], None, False)
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
    assert new in after['skills'] and old not in after['skills']
    assert after['repo_skills'] == [new]
    assert new in after['global_baseline_skills'] and old not in after['global_baseline_skills']
    assert all(new in row['packages'] and old not in row['packages'] for row in
               [*after['adapter_targets'], *after['personal_adapter_targets']])
    assert all((path / new).exists() and not (path / old).exists() and
               (path / 'custom-notes.txt').read_bytes() == b'custom neighbor unchanged\n'
               for path in paths)
    registry = load_registry(home / '.local/share/localsetup/registry.json')
    assert all(new in owner['packages'] and old not in owner['packages']
               for owner in registry['personal_owners'].values())


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_recorded_update_retires_receipted_alias_missing_from_adapter_marker(tmp_path, mode):
    from ls.core.apply_preflight import preflight_install_plan

    root, home, old, new, adapter, lock_path = _legacy_adapter_after_rename(tmp_path, mode=mode)
    assert (adapter / old).exists()
    plan = recorded_refresh(root, home, root, InstallConfig(), ['experimental'], None, False)
    preflight = preflight_install_plan(root, plan, home, target_root=root)
    assert preflight['ok'], preflight['blockers']
    assert plan.actions[[a.kind for a in plan.actions].index('attach_repo_path')].details[
        'legacy_alias_retirements'
    ] == [old]
    apply_plan(root, plan, home)
    assert (adapter / new).exists()
    assert not (adapter / old).exists()
    assert old not in json.loads(lock_path.read_text())['repo_packages']

    second = recorded_refresh(root, home, root, InstallConfig(), ['experimental'], None, False)
    apply_plan(root, second, home)
    assert (adapter / new).exists() and not (adapter / old).exists()


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_personal_recorded_update_retires_proven_orphan_alias_at_recorded_target(tmp_path, mode):
    from ls.core.apply_preflight import preflight_install_plan

    root, home, old, new, adapter, lock_path = _legacy_adapter_after_rename(
        tmp_path, mode=mode, scope='personal', target_root=tmp_path / 'recorded-target'
    )
    target = lock_path.parent.parent
    plan = recorded_refresh(root, home, target, InstallConfig(), ['experimental'], None, False)
    preflight = preflight_install_plan(root, plan, home, target_root=target)
    assert preflight['ok'], preflight['blockers']
    assert any(action.kind == 'attach_personal_path'
               and action.details['legacy_alias_retirements'] == [old]
               for action in plan.actions)
    apply_plan(root, plan, home, target_root=target)
    assert (adapter / new).is_symlink() == (mode == 'symlink')
    assert not (adapter / old).exists()
    assert new in json.loads(lock_path.read_text())['skills']


def _canonicalize_personal_owner_for_overlap(lock_path: Path, adapter: Path, new: str) -> bytes:
    from ls.core.registry import load_registry
    from ls.core.lockfile import save_json

    lock = json.loads(lock_path.read_text())
    registry_path = Path(lock['registry_path'])
    registry = load_registry(registry_path)
    for owner in registry['personal_owners'].values():
        if str(adapter) in owner['paths']:
            owner['packages'] = [new if name.startswith('ls-original-legacy-fixture') else name
                                 for name in owner['packages']]
    save_json(registry_path, registry)
    return registry_path.read_bytes()


def test_repository_overlap_retires_proven_alias_for_canonical_personal_owner(tmp_path):
    from ls.core.apply_preflight import preflight_install_plan

    root, home, old, new, adapter, lock_path = _legacy_adapter_after_rename(
        tmp_path, scope='personal', target_root=tmp_path / 'home'
    )
    _canonicalize_personal_owner_for_overlap(lock_path, adapter, new)
    plan = build_install_plan(root, home, skills=[new], platform_ids=['cursor'], target_root=home,
                              skill_scope='repo')
    preflight = preflight_install_plan(root, plan, home, target_root=home)
    assert preflight['ok'], preflight['blockers']
    repo_action = next(action for action in plan.actions if action.kind == 'attach_repo_path')
    assert repo_action.path == adapter
    assert repo_action.details['legacy_alias_retirements'] == [old]

    apply_plan(root, plan, home, target_root=home)
    assert (adapter / new).is_symlink()
    assert not (adapter / old).exists()
    registry = load_registry(Path(json.loads((home / '.localsetup/lock.json').read_text())['registry_path']))
    assert all(new in owner['packages'] and old not in owner['packages']
               for owner in registry['personal_owners'].values())


def test_repository_overlap_rechecks_alias_proof_before_retirement(tmp_path, monkeypatch):
    import ls.core.apply as engine

    root, home, old, new, adapter, lock_path = _legacy_adapter_after_rename(
        tmp_path, scope='personal', target_root=tmp_path / 'home'
    )
    registry_bytes = _canonicalize_personal_owner_for_overlap(lock_path, adapter, new)
    lock_bytes = lock_path.read_bytes()
    plan = build_install_plan(root, home, skills=[new], platform_ids=['cursor'], target_root=home,
                              skill_scope='repo')
    original_preflight = engine.preflight_install_plan
    injected = {'done': False}

    def change_after_preflight(*args, **kwargs):
        result = original_preflight(*args, **kwargs)
        if result['ok'] and not injected['done']:
            library = home / '.local/share/localsetup/packages' / old / 'SKILL.md'
            library.write_text(library.read_text() + '\nchanged after proof\n')
            injected['done'] = True
        return result

    monkeypatch.setattr(engine, 'preflight_install_plan', change_after_preflight)
    with pytest.raises(ValueError, match='proof changed before adapter update'):
        apply_plan(root, plan, home, target_root=home)
    assert injected['done'] and (adapter / old).is_symlink()
    assert not (adapter / new).exists()
    assert lock_path.read_bytes() == lock_bytes
    assert Path(json.loads(lock_path.read_text())['registry_path']).read_bytes() == registry_bytes


def test_personal_alias_with_another_recorded_owner_is_preserved(tmp_path):
    from ls.core.installation_ownership import InstallationOwner
    from ls.core.personal_registry import owner_key
    from ls.core.registry import load_registry
    from ls.core.lockfile import save_json

    root, home, old, _new, adapter, lock_path = _legacy_adapter_after_rename(
        tmp_path, mode='symlink', scope='personal'
    )
    lock = json.loads(lock_path.read_text())
    registry_path = Path(lock['registry_path'])
    registry = load_registry(registry_path)
    foreign = InstallationOwner('personal', str(home.resolve()), 'claude-code')
    registry['personal_owners'][owner_key(foreign)] = {
        'owner': foreign.wire(), 'packages': [old], 'paths': [str(adapter)], 'mode': 'symlink',
    }
    save_json(registry_path, registry)

    with pytest.raises(ValueError, match='another recorded owner still uses this alias'):
        recorded_refresh(root, home, root, InstallConfig(), ['experimental'], None, False)
    assert (adapter / old).is_symlink()
    assert not (adapter / 'ls-renamed-legacy-fixture').exists()


def test_edited_alias_shadow_is_preserved_and_blocks_with_exact_path(tmp_path):
    from ls.core.apply_preflight import preflight_install_plan

    root, home, old, new, adapter, _ = _legacy_adapter_after_rename(tmp_path, mode='symlink')
    alias = adapter / old
    alias.unlink()
    shutil.copytree(home / '.local/share/localsetup/packages' / old, alias)
    contents = alias / 'SKILL.md'
    contents.write_text(contents.read_text() + '\nlocal edit to preserve\n')
    before = contents.read_bytes()
    plan = build_install_plan(root, home, skills=[new], platform_ids=['codex'])
    preflight = preflight_install_plan(root, plan, home, target_root=root)
    diagnostic = next(item for item in preflight['blockers']
                      if item['status_code'] == 'legacy_package_alias_shadow')
    assert diagnostic['path'] == str(alias)
    assert not preflight['ok']
    with pytest.raises(RuntimeError, match='legacy_package_alias_shadow'):
        apply_plan(root, plan, home)
    assert contents.read_bytes() == before
    assert not (adapter / new).exists()


def test_alias_proof_is_rechecked_after_preflight_and_transaction_rolls_back(tmp_path, monkeypatch):
    import ls.core.apply as engine

    root, home, old, new, adapter, lock_path = _legacy_adapter_after_rename(tmp_path, mode='symlink')
    alias = adapter / old
    before_lock = lock_path.read_bytes()
    plan = recorded_refresh(root, home, root, InstallConfig(), ['experimental'], None, False)
    original_preflight = engine.preflight_install_plan
    injected = {'done': False}

    def change_after_preflight(*args, **kwargs):
        result = original_preflight(*args, **kwargs)
        if result['ok'] and not injected['done']:
            (home / '.local/share/localsetup/packages' / old / 'SKILL.md').write_text('changed after proof')
            injected['done'] = True
        return result

    monkeypatch.setattr(engine, 'preflight_install_plan', change_after_preflight)
    with pytest.raises(ValueError, match='proof changed before adapter update'):
        apply_plan(root, plan, home)
    assert injected['done'] and alias.is_symlink()
    assert not (adapter / new).exists()
    assert lock_path.read_bytes() == before_lock


def test_adapter_alias_retirement_rolls_back_exact_entry_on_later_failure(tmp_path, monkeypatch):
    import ls.core.apply as engine

    root, home, old, new, adapter, _ = _legacy_adapter_after_rename(tmp_path, mode='symlink')
    alias = adapter / old
    before_target = alias.readlink()
    plan = recorded_refresh(root, home, root, InstallConfig(), ['experimental'], None, False)
    original = engine._write_scoped_adapter

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        if old in kwargs.get('retire_aliases', []):
            raise RuntimeError('injected failure after alias retirement')

    monkeypatch.setattr(engine, '_write_scoped_adapter', fail_after_write)
    with pytest.raises(RuntimeError, match='injected failure'):
        apply_plan(root, plan, home)
    assert alias.is_symlink() and alias.readlink() == before_target
    assert not (adapter / new).exists()


@pytest.mark.parametrize('renames,match', [
    ({'ls-old': 'ls-missing'}, 'unavailable'),
    ({'ls-current': 'ls-current'}, 'shadows'),
    ({'ls-old': 'ls-next', 'ls-next': 'ls-current'}, 'chain'),
    ({'../old': 'ls-current'}, 'invalid'),
    ({'ls-old': 'localsetup-current'}, 'invalid'),
])
def test_skill_rename_rejects_invalid_alias_maps(tmp_path, renames, match):
    root = tmp_path / 'ls'
    (root / 'skills/ls-current').mkdir(parents=True)
    (root / 'config').mkdir()
    (root / 'config/pack.yaml').write_text(yaml.safe_dump({
        'alias_policy': {'skill_renames': renames},
    }))
    with pytest.raises(ValueError, match=match):
        collect_skill_aliases(root / 'skills')


def test_current_legacy_prefix_alias_needs_no_pack_manifest(tmp_path):
    skills = tmp_path / 'ls/skills'
    (skills / 'ls-current').mkdir(parents=True)
    assert collect_skill_aliases(skills) == {'localsetup-current': 'ls-current'}


def test_published_workflow_path_aliases_and_cli_resolve_from_source(tmp_path):
    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    old = 'ls-workflow-ops-tmux-session'
    canonical = next(workflow.package for workflow in load_workflow_catalog(root)
                     if old in workflow.aliases)
    library = home / '.local/share/localsetup/packages'
    assert resolve_package_path(home, old, 'SKILL.md') == library / old / 'SKILL.md'
    assert resolve_package_path(home, old, 'SKILL.md', source_root=root) == library / canonical / 'SKILL.md'
    assert resolve_token(f'localsetup://package/{old}/SKILL.md',
                         source_root=root, home=home) == library / canonical / 'SKILL.md'
    assert resolve_package_path(home, 'custom-local-package', source_root=root) == library / 'custom-local-package'

    tool = root / 'ls/tools/localsetup.py'
    completed = subprocess.run([sys.executable, str(tool), '--source-root', str(root),
                                '--home', str(home), 'path', 'package', old, 'SKILL.md'],
                               text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(library / canonical / 'SKILL.md')
    with pytest.raises(PathValidationError):
        resolve_package_path(home, '../escape', source_root=root)
    with pytest.raises(PathValidationError):
        resolve_token(f'localsetup://package/{old}/../escape', source_root=root, home=home)


def test_alias_shadow_discovery_covers_registered_client_adapter_paths(tmp_path):
    from ls.core.adapter_alias_reconciliation import package_alias_shadows
    from ls.core.adapters import adapter_targets
    from ls.core.aliases import legacy_skill_name
    from ls.core.client_registry import load_client_registry, platform_rows, projection_matches

    root = make_temp_repo(tmp_path); home = tmp_path / 'home'
    registry = load_client_registry(root)
    assert projection_matches(root, registry)
    registrations = [row for row in platform_rows(registry) if row['repo_paths']]
    targets = adapter_targets(root, home, [row['id'] for row in registrations], target_root=root)
    expected_paths = {
        str((root / relative).resolve(strict=False))
        for row in registrations for relative in row['repo_paths']
    }
    assert {str(row['repo_path'].resolve(strict=False)) for row in targets} == expected_paths

    canonical = 'ls-localsetup-project-context'
    alias = legacy_skill_name(canonical)
    for raw_path in sorted(expected_paths):
        path = Path(raw_path); path.mkdir(parents=True, exist_ok=True)
        (path / alias).mkdir()
    shadows = package_alias_shadows(root, [
        {'path': raw_path, 'packages': [canonical]} for raw_path in sorted(expected_paths)
    ])
    assert {(row['path'], row['alias'], row['canonical']) for row in shadows} == {
        (str(Path(raw_path) / alias), alias, canonical) for raw_path in expected_paths
    }
