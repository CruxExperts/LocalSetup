import errno
import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.rollback import rollback
from ls.core.personal_inventory import personal_inventory
from ls.tests.test_install_flow import make_temp_repo


def _ordinary_rollback_case(tmp_path, mode):
    root = make_temp_repo(tmp_path)
    home = tmp_path / 'home'
    apply_plan(
        root,
        build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'], attach_mode=mode),
        home,
    )
    receipt = root / '.localsetup/lock.json'
    lock = json.loads(receipt.read_text())
    registry = Path(lock['registry_path'])
    package = Path(lock['installed_skills'][0])
    adapter = Path(lock['adapter_state'][0])
    if not adapter.is_absolute():
        adapter = root / adapter
    assert not adapter.is_symlink()
    custom = adapter / 'custom.txt'
    custom.write_text('preserve custom neighbor')
    return root, home, receipt, registry, package, adapter, custom


def test_ordinary_rollback_preserves_unrelated_package_root_entries(tmp_path):
    root, home, _receipt, _registry, package, _adapter, _custom = _ordinary_rollback_case(tmp_path, 'symlink')
    global_root = package.parent
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sentinel.txt').write_text('preserve outside target')
    custom_link = global_root / 'custom-link'
    custom_link.symlink_to(outside, target_is_directory=True)
    backup_link = global_root / '.orphan.localsetup-backup-test'
    backup_link.symlink_to(outside, target_is_directory=True)
    custom_directory = global_root / 'custom-directory'
    custom_directory.mkdir()
    (custom_directory / 'sentinel.txt').write_text('preserve custom package')

    result = rollback(root, home)

    assert str(package) in result['removed']
    assert not package.exists()
    assert custom_link.is_symlink() and custom_link.readlink() == outside
    assert backup_link.is_symlink() and backup_link.readlink() == outside
    assert (custom_directory / 'sentinel.txt').read_text() == 'preserve custom package'
    assert (outside / 'sentinel.txt').read_text() == 'preserve outside target'


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
@pytest.mark.parametrize('failure', ['adapter', 'registry'])
def test_ordinary_rollback_restores_late_failure_and_retry(tmp_path, mode, failure, monkeypatch):
    import ls.core.shared_rollback as transaction

    root, home, receipt, registry, package, adapter, custom = _ordinary_rollback_case(tmp_path, mode)
    before = (receipt.read_bytes(), registry.read_bytes())
    original_remove = (
        transaction.remove_managed_adapter_entries if failure == 'adapter' else transaction.remove_target
    )
    error = OSError(errno.EIO, f'injected {failure} EIO')

    if failure == 'adapter':
        def fail(*args, **kwargs):
            if args[0] != adapter:
                return original_remove(*args, **kwargs)
            assert not package.exists()
            original_remove(*args, **kwargs)
            (adapter / 'arrived.txt').write_text('preserve new neighbor')
            raise error

        monkeypatch.setattr(transaction, 'remove_managed_adapter_entries', fail)
    else:
        def fail(*args, **kwargs):
            assert not package.exists()
            result = original_remove(*args, **kwargs)
            (adapter / 'arrived.txt').write_text('preserve new neighbor')
            raise error

        monkeypatch.setattr(transaction, 'remove_target', fail)

    with pytest.raises(OSError) as raised:
        rollback(root, home)

    assert raised.value is error
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert package.is_dir()
    managed = adapter / package.name
    assert managed.exists() or managed.is_symlink()
    assert custom.read_text() == 'preserve custom neighbor'
    assert (adapter / 'arrived.txt').read_text() == 'preserve new neighbor'
    journals = [
        path for path in (root / '.localsetup/install-journal').glob('*.json')
        if json.loads(path.read_text()).get('operation') == 'rollback'
    ]
    assert json.loads(max(journals, key=lambda path: path.stat().st_mtime_ns).read_text())['status'] == 'failed'

    if failure == 'adapter':
        monkeypatch.setattr(transaction, 'remove_managed_adapter_entries', original_remove)
    else:
        monkeypatch.setattr(transaction, 'remove_target', original_remove)
    result = rollback(root, home)
    assert str(package) in result['removed']
    assert not receipt.exists()
    assert not (adapter / package.name).exists()
    assert custom.read_text() == 'preserve custom neighbor'
    assert (adapter / 'arrived.txt').read_text() == 'preserve new neighbor'


def test_ordinary_rollback_retains_recovery_journal_when_restore_fails(tmp_path, monkeypatch):
    import ls.core.shared_rollback as transaction

    root, home, receipt, registry, package, adapter, custom = _ordinary_rollback_case(tmp_path, 'symlink')
    original_remove = transaction.remove_target

    def fail_remove(*args, **kwargs):
        original_remove(*args, **kwargs)
        raise OSError(errno.EIO, 'injected registry EIO')

    def fail_restore(*_args, **_kwargs):
        raise RuntimeError('injected restore failure')

    monkeypatch.setattr(transaction, 'remove_target', fail_remove)
    monkeypatch.setattr(transaction, 'restore_failed_mutations', fail_restore)
    with pytest.raises(OSError) as raised:
        rollback(root, home)

    assert any('recovery journal and backups' in note for note in getattr(raised.value, '__notes__', []))
    journals = [
        path for path in (root / '.localsetup/install-journal').glob('*.json')
        if json.loads(path.read_text()).get('operation') == 'rollback'
    ]
    journal_path = max(journals, key=lambda path: path.stat().st_mtime_ns)
    journal = json.loads(journal_path.read_text())
    assert journal['status'] == 'failed'
    assert 'injected restore failure' in journal['rollback_error']
    package_backup = next(
        Path(item['backup']) for item in journal['touched']
        if Path(item['path']) == package and item.get('backup')
    )
    assert package_backup.exists()
    assert str(journal_path) in raised.value.__notes__[0]
    assert not package.exists()
    assert receipt.exists()
    assert not registry.exists()
    assert custom.exists()


def test_ordinary_rollback_reports_postcommit_backup_cleanup_failure(tmp_path, monkeypatch):
    import ls.core.shared_rollback as transaction

    root, home, receipt, registry, package, adapter, custom = _ordinary_rollback_case(tmp_path, 'symlink')
    original_remove_path = transaction.remove_path

    def fail_cleanup(journal):
        backup = next(
            Path(item['backup']) for item in journal['touched']
            if Path(item['path']) == package and item.get('backup')
        )
        original_remove_path(backup)
        raise OSError('injected backup cleanup failure')

    def fail_restore(*_args, **_kwargs):
        pytest.fail('committed rollback must not attempt recovery during backup cleanup')

    monkeypatch.setattr(transaction, 'cleanup_backups', fail_cleanup)
    monkeypatch.setattr(transaction, 'restore_failed_mutations', fail_restore)
    result = rollback(root, home)

    assert any('rollback committed; backup cleanup failed' in warning for warning in result['warnings'])
    assert json.loads(Path(result['journal']).read_text())['status'] == 'committed'
    assert not receipt.exists()
    assert not registry.exists()
    assert not package.exists()
    assert custom.read_text() == 'preserve custom neighbor'


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_shared_rollback_restores_failed_transaction_and_retains_personal(tmp_path, mode, monkeypatch):
    import ls.core.shared_rollback as transaction
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    # Preserve historical shared plus exclusive physical-target coverage.
    from ls.tests.test_preferred_path_retention import prefer_common
    prefer_common(root, historical=True)
    repository = build_install_plan(root, home, skills=['ls-git-workflows'], platform_ids=['cursor'],
                                    target_root=home, attach_mode=mode)
    apply_plan(root, repository, home)
    personal = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['openclaw'],
                                  skill_scope='personal', attach_mode=mode)
    apply_plan(root, personal, home)
    receipt = home / '.localsetup/lock.json'
    lock = json.loads(receipt.read_text());registry = Path(lock['registry_path'])
    before = (receipt.read_bytes(), registry.read_bytes())
    owners = json.loads(registry.read_text())['personal_owners']
    shared = home / '.agents/skills';(shared / 'custom.txt').write_text('preserve')
    # A nonshared adapter participates in the same transaction. Marker-listed
    # dot-prefixed packages must receive the same recovery as visible packages.
    import shutil
    from ls.core.adapter_markers import ADAPTER_MARKER_JSON
    private_adapter = home / '.cursor/skills'
    hidden = private_adapter / '.hidden-package'
    global_root = Path(lock['global_root']) if 'global_root' in lock else Path(lock['installed_skills'][0]).parent
    shutil.copytree(global_root / 'ls-git-workflows', global_root / '.hidden-package')
    if mode == 'symlink':
        hidden.symlink_to(global_root / '.hidden-package', target_is_directory=True)
    else:
        shutil.copytree(global_root / 'ls-git-workflows', hidden)
    marker_path = private_adapter / ADAPTER_MARKER_JSON
    marker = json.loads(marker_path.read_text())
    marker['packages'].append('.hidden-package')
    marker_path.write_text(json.dumps(marker))

    remove = transaction.remove_target
    def fail(*args, **kwargs):
        assert not hidden.exists() and not hidden.is_symlink()
        remove(*args, **kwargs)
        (shared / 'arrived.txt').write_text('preserve new neighbor')
        raise RuntimeError('injected after registry removal')
    monkeypatch.setattr(transaction, 'remove_target', fail)
    with pytest.raises(RuntimeError, match='injected after registry removal'):
        rollback(root, home, target_root=home)
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert (shared / 'ls-git-workflows/SKILL.md').exists()
    assert (shared / 'arrived.txt').read_text() == 'preserve new neighbor'
    assert (hidden / 'SKILL.md').exists()
    monkeypatch.setattr(transaction, 'remove_target', remove)
    result = rollback(root, home, target_root=home)
    assert json.loads(Path(result['journal']).read_text())['status'] == 'committed'
    assert not receipt.exists()
    assert not (shared / 'ls-git-workflows').exists()
    assert (shared / 'ls-localsetup-project-context/SKILL.md').exists()
    assert (shared / 'custom.txt').read_text() == 'preserve'
    after = json.loads(registry.read_text())
    assert str(home.resolve()) not in after['targets']
    assert after['personal_owners'] == owners
    report = personal_inventory(root, home)
    assert report['ok'], report['issues']
