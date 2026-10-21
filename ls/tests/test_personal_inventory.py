import json
from pathlib import Path
import pytest
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.inventory import install_inventory
from ls.core.verify import verify_install
from ls.core.personal_inventory import personal_inventory
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize("mode", ["symlink", "portable"])
def test_personal_inventory_verifies_recorded_union_and_detects_drift(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / "home"
    selections = {}
    for client, skill in [("cursor", "ls-localsetup-project-context"), ("openclaw", "ls-git-workflows")]:
        plan = build_install_plan(root, home, skills=[skill], platform_ids=[client], skill_scope="personal", attach_mode=mode)
        selections[client] = plan.rollback_metadata["repo_packages"]
        apply_plan(root, plan, home)
    lock_path = root / ".localsetup/lock.json";lock = json.loads(lock_path.read_text());registry = Path(lock["registry_path"])
    before = (lock_path.read_bytes(), registry.read_bytes())
    inventory = install_inventory(root, home=home, platform_ids=["openclaw"])
    assert not inventory["adapters"] and inventory["personal"]["ok"]
    shared = next(a for a in inventory["personal"]["adapters"] if a["path"] == str(home / ".agents/skills"))
    assert shared["requested_packages"] == sorted(selections["openclaw"])
    assert shared["expected_visible_packages"] == sorted(set(selections["cursor"] + selections["openclaw"]))
    verified = verify_install(root, home, platform_ids=["openclaw"])
    assert verified["ok"], verified["issues"]
    assert not verified["adapters"] and verified["personal"]["owners"]
    assert before == (lock_path.read_bytes(), registry.read_bytes())
    assert not personal_inventory(root, home, [])["adapters"]
    marker = home / ".agents/skills/.localsetup-adapter.json";marker_bytes = marker.read_bytes()
    for malformed in ([], None):
        marker.write_text(json.dumps(malformed))
        assert not personal_inventory(root, home)["ok"]
    marker.write_bytes(marker_bytes)
    registry_bytes = registry.read_bytes();value = json.loads(registry_bytes)
    value["personal_owners"] = [];registry.write_text(json.dumps(value))
    assert not verify_install(root, home)["ok"]
    registry.write_bytes(registry_bytes)
    entry = home / ".agents/skills/ls-git-workflows"
    if mode == "symlink":entry.unlink()
    else:(entry / "SKILL.md").write_text("corrupt portable content")
    assert not verify_install(root, home)["ok"]
    value = json.loads(registry.read_text());value["personal_owners"] = {};registry.write_text(json.dumps(value))
    assert any("missing personal owner" in issue for issue in verify_install(root, home)["issues"])


def test_personal_inventory_rejects_recorded_path_escape_without_following(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / "home"
    apply_plan(root, build_install_plan(root, home, skills=["ls-localsetup-project-context"], platform_ids=["cursor"], skill_scope="personal"), home)
    lock = json.loads((root / ".localsetup/lock.json").read_text());path = Path(lock["registry_path"])
    registry = json.loads(path.read_text())
    for owner in registry["personal_owners"].values():owner["paths"] = [str(tmp_path / "outside")]
    path.write_text(json.dumps(registry))
    result = personal_inventory(root, home)
    assert not result["ok"] and not result["adapters"]
    assert not (tmp_path / "outside").exists()
