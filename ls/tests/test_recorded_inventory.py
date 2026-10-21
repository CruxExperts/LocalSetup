import json
from pathlib import Path

from ls.core.adapters import recorded_adapter_status
from ls.core.apply import apply_plan
from ls.core.inventory import install_inventory
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


def test_installed_views_keep_recorded_paths_after_catalog_change(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    # Preserve historical shared plus exclusive physical-target coverage.
    from ls.tests.test_preferred_path_retention import prefer_common
    prefer_common(root, historical=True)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor']), home)
    receipt = root / '.localsetup/lock.json';lock = json.loads(receipt.read_text())
    paths = {row['path'] for row in lock['adapter_targets']}
    catalog = root / 'ls/config/platforms.yaml'
    before_catalog = catalog.read_text()
    assert '.cursor/skills' in before_catalog
    catalog.write_text(before_catalog.replace('.cursor/skills', '.new-cursor/skills'))
    assert catalog.read_text() != before_catalog
    for clients in [None, ['cursor']]:
        inventory = install_inventory(root, home=home, platform_ids=clients)
        assert inventory['adapter_source'] == 'recorded'
        assert {row['repo_path'] for row in inventory['adapters']} == paths
        verified = verify_install(root, home, clients)
        assert verified['ok'], verified['issues']
        assert {row['repo_path'] for row in verified['adapters']} == paths
    assert not install_inventory(root, home=home, platform_ids=[])['adapters']
    assert not verify_install(root, home, [])['adapters']


def test_recorded_adapter_filters_respect_empty_and_legacy_membership(tmp_path):
    library = tmp_path / 'library';target = tmp_path / 'target'
    old = {'adapter_state': ['.old/skills'], 'platforms': ['cursor']}
    rows = recorded_adapter_status(old, library, ['cursor'], target_root=target)
    assert rows[0]['repo_path'] == str(target / '.old/skills')
    assert not recorded_adapter_status(old | {'adapter_targets': []}, library)
    row = {'path': str(target / '.old/skills'), 'platform': 'cursor', 'platforms': ['cursor'], 'owners': []}
    assert not recorded_adapter_status({'adapter_targets': [row]}, library, ['cursor'])
    assert recorded_adapter_status({'adapter_targets': [row]}, library)[0]['platforms'] == []
