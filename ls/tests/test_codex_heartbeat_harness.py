from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ls.core.apply import apply_plan
from ls.core.harness import HEARTBEAT_TASK_ID
from ls.core.harness import budget as harness_budget
from ls.core.harness import disable as harness_disable
from ls.core.harness import enable as harness_enable
from ls.core.harness import init as harness_init
from ls.core.harness import plan as harness_plan
from ls.core.harness import run as harness_run
from ls.core.harness import status as harness_status
from ls.core.plan import build_install_plan
from ls.core.skills import selected_skill_names
from ls.core.workflows import selected_workflow_names
from ls.tests.codex_heartbeat_test_helpers import ROOT, write_config


def test_harness_pack_selects_heartbeat_skill_and_finalizer() -> None:
    assert "ls-codex-heartbeat" in selected_skill_names(ROOT, ["harness"])
    assert "ls-cron-orchestrator" in selected_skill_names(ROOT, ["harness"])
    assert selected_workflow_names(ROOT, ["harness"]) == ["ls-wf-repository-status-checkpoint"]


def test_normal_install_of_harness_pack_does_not_activate_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    home = tmp_path / "home"
    target.mkdir()
    plan = build_install_plan(ROOT, home=home, packs=["harness"], target_root=target)
    result = apply_plan(ROOT, plan, home=home, target_root=target)

    assert result["dry_run"] is False
    assert (home / ".local/share/localsetup/packages/ls-codex-heartbeat").is_dir()
    assert not (target / "HEARTBEAT.md").exists()
    assert not (target / "config" / "codex_heartbeat.yaml").exists()
    assert not (target / "cron" / "manifest.yaml").exists()
    assert not (target / ".localsetup" / "state" / "codex-heartbeat").exists()


def test_harness_init_enable_run_status_disable_are_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    first_init = harness_init(ROOT, target)
    second_init = harness_init(ROOT, target)
    enable_one = harness_enable(ROOT, target)
    enable_two = harness_enable(ROOT, target)
    run_payload = harness_run(ROOT, target, no_agent=True)
    status_payload = harness_status(ROOT, target)
    disable_one = harness_disable(ROOT, target)
    disable_two = harness_disable(ROOT, target)

    assert first_init["created"]["HEARTBEAT.md"] is True
    assert second_init["created"]["HEARTBEAT.md"] is False
    assert enable_one["enabled"] is True
    assert enable_two["enabled"] is True
    assert run_payload["ok"] is True
    assert status_payload["latest"]["status"] == "succeeded"
    assert disable_one["enabled"] is False
    assert disable_two["enabled"] is False
    config = yaml.safe_load((target / "config" / "codex_heartbeat.yaml").read_text(encoding="utf-8"))
    assert config["heartbeat"]["enabled"] is False
    manifest = yaml.safe_load((target / "cron" / "manifest.yaml").read_text(encoding="utf-8"))
    task = next(task for task in manifest["tasks"] if task["id"] == HEARTBEAT_TASK_ID)
    assert task["enabled"] is False


def test_harness_cron_upsert_preserves_unrelated_entries_and_validates(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    manifest = target / "cron" / "manifest.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        yaml.safe_dump(
            {
                "triggers": {"nightly": {"schedule": "0 2 * * *"}},
                "tasks": [
                    {
                        "id": "keep",
                        "trigger": "nightly",
                        "sequence_order": 1,
                        "command": [sys.executable, "-c", "print('keep')"],
                        "enabled": True,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    harness_enable(ROOT, target)

    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert "nightly" in data["triggers"]
    assert "codex-heartbeat" in data["triggers"]
    assert any(task["id"] == "keep" for task in data["tasks"])
    assert any(task["id"] == HEARTBEAT_TASK_ID for task in data["tasks"])
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "ls" / "skills" / "ls-cron-orchestrator" / "scripts" / "cron_ctl.py"),
            "--manifest",
            str(manifest),
            "validate",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_harness_live_crontab_requires_yes(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(RuntimeError, match="requires --install-crontab and --yes"):
        harness_enable(ROOT, target, install_crontab=True, yes=False)
    assert not (target / "config" / "codex_heartbeat.yaml").exists()
    assert not (target / "cron" / "manifest.yaml").exists()


def test_harness_plan_reports_launcher_from_source_checkout(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    payload = harness_plan(ROOT, target)

    command = payload["launcher_command"]
    assert command[0].endswith("localsetup") or str(ROOT / "ls" / "tools" / "localsetup.py") in command
    assert "ls/skills/ls-codex-heartbeat" not in " ".join(command)


def test_harness_budget_missing_queue_uses_safe_defaults(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    payload = harness_budget(ROOT, target)

    assert payload["ok"] is True
    assert payload["policy"]["effective_runtime_seconds"] == 720
    assert payload["policy"]["max_parallel"] == 1
    assert payload["policy"]["max_total"] == 1
    assert payload["policy"]["max_depth"] == 1
    assert payload["policy"]["execution_order"] == "serial"
    assert payload["policy"]["allow_git_writes"] is False
    assert payload["policy"]["allow_destructive"] is False
    assert payload["summary"]["task_count"] == 0
    assert payload["tasks"] == []


def test_harness_budget_caps_runtime_from_interval_and_queue_policy(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    write_config(target, task_queue_path="queues/heartbeat-tasks.yaml")
    queue = target / "queues" / "heartbeat-tasks.yaml"
    queue.parent.mkdir()
    queue.write_text(
        yaml.safe_dump(
            {
                "policy": {
                    "max_subagent_occupancy_ratio": 0.5,
                    "max_parallel": 2,
                    "max_total": 3,
                    "max_depth": 2,
                },
                "tasks": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    payload = harness_budget(ROOT, target)

    assert payload["policy"]["effective_runtime_seconds"] == 450
    assert payload["policy"]["max_parallel"] == 2
    assert payload["policy"]["max_total"] == 3
    assert payload["policy"]["max_depth"] == 2


def test_harness_budget_serial_carryover_uses_actual_for_completed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    write_config(
        target,
        task_queue_path="queues/heartbeat-tasks.yaml",
        agent={"enabled": False, "profile": "lower-cost", "timeout_seconds": 120},
        agent_profiles={"lower-cost": {"timeout_seconds": 300}},
    )
    queue = target / "queues" / "heartbeat-tasks.yaml"
    queue.parent.mkdir()
    queue.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {"id": "done", "status": "completed", "allocated_runtime_seconds": 300, "actual_runtime_seconds": 45},
                    {"id": "pending", "status": "planned", "allocated_runtime_seconds": 300},
                    {"id": "unknown-time", "status": "assigned"},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    payload = harness_budget(ROOT, target)

    assert payload["tasks"][0]["reserved_runtime_seconds"] == 45
    assert payload["tasks"][0]["reservation_rule"] == "actual"
    assert payload["tasks"][1]["reserved_runtime_seconds"] == 300
    assert payload["tasks"][2]["reserved_runtime_seconds"] == 120
    assert payload["summary"]["reserved_runtime_seconds"] == 465


def test_harness_budget_uses_selected_agent_profile_timeout(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    write_config(
        target,
        agent={"enabled": False, "profile": "alternate"},
        agent_profiles={"heartbeat": {"timeout_seconds": 60}, "alternate": {"timeout_seconds": 90}},
    )

    payload = harness_budget(ROOT, target)

    assert payload["policy"]["default_timeout_seconds"] == 90


def test_harness_budget_clamps_hostile_numeric_values(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    write_config(
        target,
        task_queue_path="queues/heartbeat-tasks.yaml",
        agent={"enabled": False, "profile": "heartbeat"},
        agent_profiles={"heartbeat": {"timeout_seconds": -5}},
    )
    queue = target / "queues" / "heartbeat-tasks.yaml"
    queue.parent.mkdir()
    queue.write_text(
        yaml.safe_dump(
            {
                "policy": {
                    "max_subagent_occupancy_ratio": 2,
                    "max_parallel": 0,
                    "max_total": -3,
                    "max_depth": 0,
                },
                "tasks": [
                    {"id": "bad-allocation", "status": "planned", "allocated_runtime_seconds": -1},
                    {"id": "bad-timeout", "status": "planned", "timeout_seconds": -5},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    payload = harness_budget(ROOT, target)

    assert payload["policy"]["max_subagent_occupancy_ratio"] == 1.0
    assert payload["policy"]["max_parallel"] == 1
    assert payload["policy"]["max_total"] == 1
    assert payload["policy"]["max_depth"] == 1
    assert payload["policy"]["default_timeout_seconds"] == 1800
    assert all(task["reserved_runtime_seconds"] > 0 for task in payload["tasks"])
    assert payload["summary"]["remaining_runtime_seconds"] <= payload["policy"]["effective_runtime_seconds"]


def test_harness_budget_defaults_non_finite_occupancy_ratio(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    write_config(target, task_queue_path="queues/heartbeat-tasks.yaml")
    queue = target / "queues" / "heartbeat-tasks.yaml"
    queue.parent.mkdir()
    queue.write_text(
        "policy:\n"
        "  max_subagent_occupancy_ratio: .nan\n"
        "tasks:\n"
        "  - id: one\n"
        "    status: planned\n"
        "    allocated_runtime_seconds: -1\n",
        encoding="utf-8",
    )

    nan_payload = harness_budget(ROOT, target)
    assert nan_payload["policy"]["max_subagent_occupancy_ratio"] == 0.8
    assert nan_payload["policy"]["effective_runtime_seconds"] == 720

    queue.write_text(
        "policy:\n"
        "  max_subagent_occupancy_ratio: .inf\n"
        "tasks: []\n",
        encoding="utf-8",
    )
    inf_payload = harness_budget(ROOT, target)
    assert inf_payload["policy"]["max_subagent_occupancy_ratio"] == 0.8
    assert inf_payload["policy"]["effective_runtime_seconds"] == 720


def test_harness_budget_defaults_non_finite_integer_fields(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    write_config(target, task_queue_path="queues/heartbeat-tasks.yaml")
    queue = target / "queues" / "heartbeat-tasks.yaml"
    queue.parent.mkdir()
    queue.write_text(
        "policy:\n"
        "  max_parallel: .inf\n"
        "  max_total: .nan\n"
        "  max_depth: -.inf\n"
        "tasks:\n"
        "  - id: one\n"
        "    status: planned\n"
        "    allocated_runtime_seconds: .inf\n"
        "  - id: two\n"
        "    status: planned\n"
        "    timeout_seconds: .nan\n",
        encoding="utf-8",
    )

    payload = harness_budget(ROOT, target)

    assert payload["ok"] is True
    assert payload["policy"]["max_parallel"] == 1
    assert payload["policy"]["max_total"] == 1
    assert payload["policy"]["max_depth"] == 1
    assert [task["reserved_runtime_seconds"] for task in payload["tasks"]] == [1800, 1800]


def test_harness_budget_rejects_queue_path_outside_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside" / "queue.yaml"
    outside.parent.mkdir()
    outside.write_text("tasks: []\n", encoding="utf-8")
    write_config(target, task_queue_path="../outside/queue.yaml")

    traversal_payload = harness_budget(ROOT, target)
    assert traversal_payload["ok"] is False
    assert traversal_payload["summary"]["task_queue_exists"] is False
    assert "under target_root" in traversal_payload["summary"]["queue_error"]

    config = yaml.safe_load((target / "config" / "codex_heartbeat.yaml").read_text(encoding="utf-8"))
    config["heartbeat"]["task_queue_path"] = str(outside)
    (target / "config" / "codex_heartbeat.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    absolute_payload = harness_budget(ROOT, target)
    assert absolute_payload["ok"] is False
    assert "repo-local" in absolute_payload["summary"]["queue_error"]


def test_harness_budget_cli_dispatch(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "ls" / "tools" / "localsetup.py"),
            "--source-root",
            str(ROOT),
            "--target-directory",
            str(target),
            "harness",
            "codex-heartbeat",
            "budget",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["summary"]["task_count"] == 0


@pytest.mark.parametrize('minutes,schedule', [
    (1, '*/1 * * * *'), (15, '*/15 * * * *'), (30, '*/30 * * * *'),
    (60, '0 */1 * * *'), (120, '0 */2 * * *'), (480, '0 */8 * * *'),
    (720, '0 */12 * * *'), (1440, '0 0 * * *'),
])
def test_faithful_heartbeat_cron_intervals(minutes, schedule):
    from ls.core.harness import _interval_schedule
    assert _interval_schedule(minutes) == schedule


@pytest.mark.parametrize('minutes', [0, 7, 35, 59, 61, 90, 420, 1500, True, 15.0, '15'])
def test_invalid_cadence_does_not_enable_or_write_manifest(tmp_path, minutes):
    from ls.core.harness import HEARTBEAT_CONFIG
    harness_init(ROOT, tmp_path)
    path = tmp_path / HEARTBEAT_CONFIG
    config = yaml.safe_load(path.read_text())
    config['heartbeat']['interval_minutes'] = minutes
    path.write_text(yaml.safe_dump(config))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        harness_enable(ROOT, tmp_path)
    assert path.read_bytes() == before
    assert not (tmp_path / 'cron/manifest.yaml').exists()


def test_disabling_preserves_historical_trigger_even_with_unsupported_interval(tmp_path):
    from ls.core.harness import HEARTBEAT_CONFIG, HEARTBEAT_TRIGGER_ID
    harness_init(ROOT, tmp_path)
    harness_enable(ROOT, tmp_path)
    manifest_path = tmp_path / 'cron/manifest.yaml'
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest['triggers'][HEARTBEAT_TRIGGER_ID] = {'schedule': '7,42 * * * *'}
    manifest_path.write_text(yaml.safe_dump(manifest))
    config_path = tmp_path / HEARTBEAT_CONFIG
    config = yaml.safe_load(config_path.read_text())
    config['heartbeat']['interval_minutes'] = 35
    config_path.write_text(yaml.safe_dump(config))
    harness_disable(ROOT, tmp_path)
    after = yaml.safe_load(manifest_path.read_text())
    assert after['triggers'][HEARTBEAT_TRIGGER_ID] == {'schedule': '7,42 * * * *'}
    assert not yaml.safe_load(config_path.read_text())['heartbeat']['enabled']
