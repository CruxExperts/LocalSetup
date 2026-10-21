import json
from pathlib import Path
import pytest
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.paths import global_layout
from ls.core.personal_repair import repair_personal
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize("mode", ["symlink", "portable"])
def test_personal_repair_preserves_selection_and_neighbors(tmp_path, mode, monkeypatch):
    import ls.core.personal_repair as repair
    root = make_temp_repo(tmp_path);home = tmp_path / "home"
    plan = build_install_plan(root, home, skills=["ls-localsetup-project-context"], platform_ids=["cursor"], skill_scope="personal", attach_mode=mode)
    apply_plan(root, plan, home)
    lock_path = root / ".localsetup/lock.json";registry = Path(json.loads(lock_path.read_text())["registry_path"])
    adapter = home / ".agents/skills";package = adapter / "ls-localsetup-project-context"
    (adapter / "custom.txt").write_text("preserve")
    if mode == "symlink":package.unlink()
    else:(package / "SKILL.md").write_text("corrupt")
    before = (lock_path.read_bytes(), registry.read_bytes())
    report = repair_personal(root, home, ["cursor"])
    assert report["ok"] and report["actions"] and not report["applied"]
    assert not (global_layout(home).state_root / "personal-repair").exists()
    assert before == (lock_path.read_bytes(), registry.read_bytes())
    write = repair.write
    def fail(*args, **kwargs):
        write(*args, **kwargs)
        (adapter / "new-custom.txt").write_text("arrived")
        raise RuntimeError("injected repair failure")
    monkeypatch.setattr(repair, "write", fail)
    failed = repair_personal(root, home, ["cursor"], apply=True)
    assert not failed["ok"] and failed["recovery_ok"]
    if mode == "symlink":assert not package.exists()
    else:assert (package / "SKILL.md").read_text() == "corrupt"
    assert (adapter / "new-custom.txt").read_text() == "arrived"
    monkeypatch.setattr(repair, "write", write)
    result = repair_personal(root, home, ["cursor"], apply=True)
    assert result["ok"] and result["applied"] and result["verification"]["ok"], result
    assert before == (lock_path.read_bytes(), registry.read_bytes())
    assert (adapter / "custom.txt").read_text() == "preserve"
    assert not repair_personal(root, home, ["cursor"])["actions"]


def test_personal_repair_empty_and_unknown_selection_do_not_install(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / "home"
    assert repair_personal(root, home, [], apply=True)["ok"]
    report = repair_personal(root, home, ["cursor"], apply=True)
    assert not report["ok"] and "no recorded personal owner" in report["blockers"][0]
    assert not home.exists()
