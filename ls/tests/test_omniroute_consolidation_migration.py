from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from ls.tests.test_install_flow import (
    apply_plan,
    build_install_plan,
    load_json,
    make_temp_repo,
    rollback,
    verify_install,
)


RETAINED = (
    "ls-omniroute-task-triage",
    "ls-omniroute-model-discovery",
    "ls-omniroute-admin-automation",
    "ls-omniroute-update",
)
REMOVED = (
    "ls-omniroute-codex",
    "ls-omniroute-context",
    "ls-omniroute-integrations",
    "ls-omniroute-observability",
)


def _write_old_eight_source(root: Path) -> None:
    config_path = root / "ls" / "config" / "pack.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["packs"]["omniroute"].extend(REMOVED)
    for name in REMOVED:
        config["extensions"]["skill_taxonomy"][name] = {
            "class": "integrations",
            "sort_priority": 50,
            "tags": ["omniroute", "legacy"],
            "owner_scope": "skill",
        }
        package = root / "ls" / "skills" / name
        package.mkdir()
        (package / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: Legacy migration fixture for {name}.\n"
            "---\n\n"
            f"# {name}\n",
            encoding="utf-8",
        )
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    ledger_path = root / "ls" / "config" / "dependency-ledger.yaml"
    ledger = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    ledger["nodes"].extend(
        {"id": f"skill:{name}", "kind": "skill", "name": name}
        for name in REMOVED
    )
    ledger_path.write_text(yaml.safe_dump(ledger, sort_keys=False), encoding="utf-8")


def _write_new_four_source(root: Path) -> None:
    config_path = root / "ls" / "config" / "pack.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["packs"]["omniroute"] = [
        name for name in config["packs"]["omniroute"] if name not in REMOVED
    ]
    for name in REMOVED:
        config["extensions"]["skill_taxonomy"].pop(name)
        shutil.rmtree(root / "ls" / "skills" / name)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    ledger_path = root / "ls" / "config" / "dependency-ledger.yaml"
    ledger = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    ledger["nodes"] = [
        row for row in ledger["nodes"] if row["name"] not in REMOVED
    ]
    ledger_path.write_text(yaml.safe_dump(ledger, sort_keys=False), encoding="utf-8")


def test_managed_old_eight_reconciles_to_four_and_rollback_preserves_custom(
    tmp_path: Path,
) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "lib", root / "ls" / "lib")
    home = tmp_path / "home"
    _write_old_eight_source(root)

    old_plan = build_install_plan(
        root,
        home=home,
        packs=["omniroute"],
        platform_ids=["codex"],
    )
    apply_plan(root, old_plan, home=home)
    adapter = root / ".agents" / "skills"
    global_root = home / ".local" / "share" / "localsetup" / "packages"
    assert all((adapter / name).exists() for name in (*RETAINED, *REMOVED))

    custom = adapter / "custom-omniroute-operator"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom OmniRoute operator\n", encoding="utf-8")

    _write_new_four_source(root)
    new_plan = build_install_plan(
        root,
        home=home,
        packs=["omniroute"],
        platform_ids=["codex"],
    )
    apply_plan(root, new_plan, home=home)

    assert verify_install(root, home, platform_ids=["codex"])["ok"] is True
    assert all((global_root / name / "SKILL.md").is_file() for name in RETAINED)
    assert all((adapter / name).exists() for name in RETAINED)
    assert all(not (global_root / name).exists() for name in REMOVED)
    assert all(not (adapter / name).exists() for name in REMOVED)
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == (
        "# Custom OmniRoute operator\n"
    )

    lock = load_json(root / ".localsetup" / "lock.json")
    registry = load_json(home / ".local" / "share" / "localsetup" / "registry.json")
    current_receipt = registry["targets"][str(root.resolve())]
    for name in REMOVED:
        assert name not in lock["skills"]
        assert name not in lock["repo_packages"]
        assert name not in registry["packages"]
        assert name not in current_receipt["packages"]

    helpers = {
        "ls-omniroute-task-triage": "scripts/omniroute_api.py",
        "ls-omniroute-model-discovery": "scripts/omniroute_discover.py",
        "ls-omniroute-admin-automation": "scripts/omniroute_admin.py",
        "ls-omniroute-update": "scripts/omniroute_update.py",
    }
    for name, relative in helpers.items():
        result = subprocess.run(
            [sys.executable, str(global_root / name / relative), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (name, result.stderr)
        assert "ModuleNotFoundError" not in result.stderr

    assert "tombstones" not in lock
    assert all(name not in lock["aliases"] for name in REMOVED)
    assert all(name not in lock["aliases"].values() for name in REMOVED)
    assert {Path(path).name for path in lock["pruned_packages"]} == set(REMOVED)

    rollback(root, home)
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == (
        "# Custom OmniRoute operator\n"
    )
    assert all(not (adapter / name).exists() for name in (*RETAINED, *REMOVED))
