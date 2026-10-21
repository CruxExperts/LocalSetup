import json
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
