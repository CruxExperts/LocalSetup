"""Cursor owns a common projection without suppressing other native owners."""
import pytest

from ls.core.apply import apply_plan
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_cursor_mixed_client_layout_preserves_native_custom_content(tmp_path, monkeypatch, mode):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    for base in (root, home):
        custom = base / '.cursor/skills/custom/SKILL.md'
        custom.parent.mkdir(parents=True)
        custom.write_text('---\nname: custom\ndescription: Keep me\n---\nCustom')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['cursor', 'claude-code'], skill_scope='both', attach_mode=mode)
    apply_plan(root, plan, home)
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
        assert (base / '.claude/skills/ls-localsetup-project-context/SKILL.md').is_file()
        assert not (base / '.cursor/skills/ls-localsetup-project-context').exists()
    detach_platforms(root, home, root, ['cursor'])
    assert detach_personal(root, home, ['cursor'], apply=True)['applied']
    for base in (root, home):
        assert (base / '.claude/skills/ls-localsetup-project-context/SKILL.md').is_file()
        assert (base / '.cursor/skills/custom/SKILL.md').read_text().endswith('Custom')
