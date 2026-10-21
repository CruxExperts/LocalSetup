from ls.core.kimi_prerequisite import kimi_personal_root


def test_empty_preferred_root_masks_common_home_without_mutation(tmp_path):
    preferred = tmp_path / '.config/agents/skills';preferred.mkdir(parents=True)
    assert kimi_personal_root(tmp_path)['status'] == 'masked'
    assert list(preferred.iterdir()) == [] and not (tmp_path / '.agents').exists()


def test_same_physical_root_is_not_masked(tmp_path):
    common = tmp_path / '.agents/skills';common.mkdir(parents=True)
    preferred = tmp_path / '.config/agents/skills';preferred.parent.mkdir(parents=True)
    preferred.symlink_to(common, target_is_directory=True)
    assert kimi_personal_root(tmp_path)['ok']


def test_absent_or_regular_file_preferred_root_does_not_mask(tmp_path):
    assert kimi_personal_root(tmp_path)['ok']
    preferred = tmp_path / '.config/agents/skills';preferred.parent.mkdir(parents=True);preferred.write_text('keep')
    assert kimi_personal_root(tmp_path)['ok'] and preferred.read_text() == 'keep'


import pytest
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.core.personal_repair import repair_personal
from ls.core.combined_repair import repair_combined
from ls.core.personal_detach import detach_personal
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_kimi_mask_blocks_only_personal_writes_before_install(tmp_path, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    preferred = home / '.config/agents/skills';preferred.mkdir(parents=True)
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['kimi-cli'], skill_scope=scope)
    if scope != 'repo':
        with pytest.raises(RuntimeError, match='kimi_personal_mask'):apply_plan(root, plan, home)
        assert not (home / '.agents/skills').exists()
    else:
        apply_plan(root, plan, home)
        assert verify_install(root, home, target_root=root)['ok']
    assert preferred.is_dir() and list(preferred.iterdir()) == []


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_kimi_masks_are_reported_after_install_and_detach_remains_available(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    native = home / '.kimi/config.toml';native.parent.mkdir(parents=True)
    native.write_text('merge_all_available_skills = true\n')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['kimi-cli'], skill_scope='both', attach_mode=mode)
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    preferred = home / '.config/agents/skills';preferred.mkdir(parents=True)
    assert not verify_install(root, home, target_root=root)['ok']
    # A different client touching the same recorded path cannot hide the mask.
    other = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-cli'], skill_scope='both', attach_mode=mode)
    with pytest.raises(RuntimeError, match='kimi_personal_mask'):apply_plan(root, other, home)
    copy = home / '.agents/skills/ls-localsetup-project-context'
    if copy.is_symlink():copy.unlink()
    else:
        import shutil
        shutil.rmtree(copy)
    assert not repair_personal(root, home, ['kimi-cli'], apply=True)['ok']
    assert not repair_combined(root, home, root, ['kimi-cli'], apply=True)['ok']
    assert detach_personal(root, home, ['kimi-cli'], apply=True)['applied']
    assert native.read_text() == 'merge_all_available_skills = true\n' and preferred.is_dir()


def test_unreadable_root_is_unknown_not_unmasked(tmp_path, monkeypatch):
    from pathlib import Path
    original = Path.stat
    def denied(path, *args, **kwargs):
        if path == tmp_path / '.config/agents/skills':raise PermissionError('fixture')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', denied)
    assert kimi_personal_root(tmp_path)['status'] == 'unknown'
