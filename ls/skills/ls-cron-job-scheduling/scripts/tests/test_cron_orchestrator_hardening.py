import subprocess
import sys
import os
import stat
import time
from pathlib import Path

import yaml


SCRIPT_DIR = Path(__file__).resolve().parents[1]
ROOT = SCRIPT_DIR.parents[3]
CRON_CTL = SCRIPT_DIR / "cron_ctl.py"
RUN_TRIGGER = SCRIPT_DIR / "run_trigger.py"


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _run_cron_ctl(
    manifest: Path,
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CRON_CTL), "--manifest", str(manifest), *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_trigger(manifest: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUN_TRIGGER), "--manifest", str(manifest), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_validate_rejects_unsafe_schedule_characters(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"bad": {"schedule": "0 0 * * *%/bin/sh"}},
            "tasks": [{"id": "safe", "trigger": "bad", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_cron_ctl(manifest, "validate")

    assert proc.returncode == 1
    assert "triggers.bad.schedule" in proc.stderr
    assert "unsupported cron characters" in proc.stderr


def test_validate_allows_literal_shell_looking_argv_args(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "literal", "trigger": "nightly", "sequence_order": 1, "command": ["printf", "a && b"]}],
        },
    )

    proc = _run_cron_ctl(manifest, "validate")

    assert proc.returncode == 0
    assert proc.stdout.strip() == "OK"


def test_install_quotes_runner_args_without_shell_chaining(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo root %safe"
    repo_root.mkdir()
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {
                "nightly": {"schedule": "0 2 * * *"},
                "after-boot": {"on_boot_delay_minutes": 5},
            },
            "tasks": [
                {"id": "night-task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]},
                {"id": "boot-task", "trigger": "after-boot", "sequence_order": 1, "command": ["python3", "--version"]},
            ],
        },
    )

    proc = _run_cron_ctl(manifest, "install", "--repo-root", str(repo_root))

    expected_repo_root = str(repo_root).replace("%", r"\%")
    assert proc.returncode == 0
    assert f"--repo-root '{expected_repo_root}'" in proc.stdout
    assert "--delay-seconds 300" in proc.stdout
    assert " && " not in proc.stdout
    assert "\tcd " not in proc.stdout
    assert "\tsleep " not in proc.stdout


def test_install_can_pass_log_dir_to_runner(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "cron logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "night-task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_cron_ctl(manifest, "install", "--repo-root", str(repo_root), "--log-dir", str(log_dir))

    assert proc.returncode == 0
    assert f"--log-dir '{log_dir}'" in proc.stdout


def test_run_trigger_appends_durable_log(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "log-task",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; print('stdout marker'); print('stderr marker', file=sys.stderr)",
                    ],
                }
            ],
        },
    )

    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")

    log_text = (log_dir / "nightly.log").read_text(encoding="utf-8")
    assert proc.returncode == 0
    assert "stdout marker" in proc.stdout
    assert "stderr marker" in proc.stderr
    assert "runner_start trigger=nightly" in log_text
    assert "task_start id=log-task" in log_text
    assert "task_exit id=log-task exit_code=0" in log_text
    assert "stdout marker" in log_text
    assert "stderr marker" in log_text
    assert "runner_exit trigger=nightly exit_code=0" in log_text


def test_run_trigger_creates_private_durable_log_permissions(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "log-task", "trigger": "nightly", "sequence_order": 1, "command": [sys.executable, "-c", "print('secret-ish tail')"]}],
        },
    )

    old_umask = os.umask(0)
    try:
        proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")
    finally:
        os.umask(old_umask)

    log_path = log_dir / "nightly.log"
    assert proc.returncode == 0
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_run_trigger_rejects_public_existing_log_dir(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    os.chmod(log_dir, 0o755)
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")

    assert proc.returncode == 1
    assert "permissions must not allow group/other access" in proc.stderr
    assert not (log_dir / "nightly.log").exists()


def test_run_trigger_rejects_public_existing_log_file(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o700)
    log_path = log_dir / "nightly.log"
    log_path.write_text("existing\n", encoding="utf-8")
    os.chmod(log_path, 0o644)
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")

    assert proc.returncode == 1
    assert "permissions must not allow group/other access" in proc.stderr
    assert log_path.read_text(encoding="utf-8") == "existing\n"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o644


def test_run_trigger_logs_runner_exit_for_manifest_validation_failure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "bad task id", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")

    log_text = (log_dir / "nightly.log").read_text(encoding="utf-8")
    assert proc.returncode == 1
    assert "tasks[0].id" in proc.stderr
    assert "runner_start trigger=nightly" in log_text
    assert "runner_exit trigger=nightly exit_code=1 reason=manifest_validation_failed" in log_text


def test_run_trigger_log_dir_error_is_controlled(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "not-a-directory"
    log_dir.write_text("already a file\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")

    assert proc.returncode == 1
    assert "[run_trigger] Invalid log directory" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_reorder_rejects_duplicate_order_ids(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "a",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "command": ["python3", "--version"],
                },
                {
                    "id": "b",
                    "trigger": "nightly",
                    "sequence_order": 2,
                    "command": ["python3", "--version"],
                },
            ],
        },
    )

    proc = _run_cron_ctl(manifest, "reorder", "--trigger", "nightly", "--order", "a,a")

    assert proc.returncode == 1
    assert "Duplicate task id(s) in order for trigger nightly: a" in proc.stderr
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert [task["id"] for task in data["tasks"]] == ["a", "b"]


def test_install_output_write_error_is_controlled(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    manifest = tmp_path / "manifest.yaml"
    output_dir = tmp_path / "existing-dir"
    output_dir.mkdir()
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "task",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "command": ["python3", "--version"],
                }
            ],
        },
    )

    proc = _run_cron_ctl(
        manifest,
        "install",
        "--repo-root",
        str(repo_root),
        "--output",
        str(output_dir),
    )

    assert proc.returncode == 1
    assert "[cron_ctl] Failed to write output:" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_missing_pyyaml_uses_shared_dependency_error(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("triggers: {}\n", encoding="utf-8")
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import importlib\n"
        "_real_import_module = importlib.import_module\n"
        "def _patched_import_module(name, package=None):\n"
        "    if name == 'yaml':\n"
        "        raise ImportError('simulated missing yaml')\n"
        "    return _real_import_module(name, package)\n"
        "importlib.import_module = _patched_import_module\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)

    proc = _run_cron_ctl(manifest, "validate", env=env)

    assert proc.returncode == 2
    assert "[FATAL] Missing Python packages: yaml" in proc.stderr
    assert "uv sync --locked --no-dev" in proc.stderr


def test_validate_enforces_linux_cron_field_semantics(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    valid_payload = {
        "triggers": {"nightly": {"schedule": "*/15 0-23 1,15 1-12 0-6"}},
        "tasks": [{"id": "task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
    }
    _write_manifest(manifest, valid_payload)

    assert _run_cron_ctl(manifest, "validate").returncode == 0

    for schedule in ("60 0 * * *", "*/0 * * * *", "0 0 0 * *", "0 0 * 13 *", "0 0 * * 8"):
        _write_manifest(
            manifest,
            {
                "triggers": {"nightly": {"schedule": schedule}},
                "tasks": [{"id": "task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
            },
        )

        proc = _run_cron_ctl(manifest, "validate")

        assert proc.returncode == 1
        assert "triggers.nightly.schedule" in proc.stderr


def test_validate_requires_typed_task_order_and_enabled_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    payload = {
        "triggers": {"nightly": {"schedule": "0 2 * * *"}},
        "tasks": [{"id": "task", "trigger": "nightly", "command": ["python3", "--version"]}],
    }
    _write_manifest(manifest, payload)

    missing_order = _run_cron_ctl(manifest, "validate")

    assert missing_order.returncode == 1
    assert "tasks[0].sequence_order: is required" in missing_order.stderr
    payload["tasks"][0]["sequence_order"] = 1.0
    payload["tasks"][0]["enabled"] = "true"
    _write_manifest(manifest, payload)

    wrong_order_type = _run_cron_ctl(manifest, "validate")

    assert wrong_order_type.returncode == 1
    assert "tasks[0].sequence_order: must be an integer" in wrong_order_type.stderr

    payload["tasks"][0]["sequence_order"] = 1
    _write_manifest(manifest, payload)

    wrong_enabled_type = _run_cron_ctl(manifest, "validate")

    assert wrong_enabled_type.returncode == 1
    assert "tasks[0].enabled: must be a boolean" in wrong_enabled_type.stderr


def test_run_trigger_rejects_negative_delay_without_type_coercion(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--delay-seconds", "-1", "nightly")

    assert proc.returncode == 1
    assert "delay_seconds: must be in the range 0..86400" in proc.stderr


def test_install_emits_reviewable_user_crontab_fragment(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    manifest = tmp_path / "manifest.yaml"
    output = tmp_path / "cron" / "fragment"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "task", "trigger": "nightly", "sequence_order": 1, "command": ["python3", "--version"]}],
        },
    )

    proc = _run_cron_ctl(manifest, "install", "--repo-root", str(repo_root), "--output", str(output))
    fragment = output.read_text(encoding="utf-8")

    assert proc.returncode == 0
    assert fragment.startswith("# Generated user-crontab fragment; merge manually after inspecting the current crontab.\n")
    assert "0 2 * * *\t" in fragment
    assert "/etc/cron.d" not in fragment


def test_run_trigger_streams_output_and_bounds_log_tails(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "noisy",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; print('A' * 5000, flush=True); print('B' * 5000, file=sys.stderr, flush=True)",
                    ],
                }
            ],
        },
    )

    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")
    log_text = (log_dir / "nightly.log").read_text(encoding="utf-8")

    assert proc.returncode == 0
    assert "A" * 5000 in proc.stdout
    assert "A" * 3999 in log_text
    assert "B" * 3999 in log_text
    assert "A" * 4000 not in log_text
    assert "B" * 4000 not in log_text
    assert "B" * 4001 not in log_text


def test_run_trigger_timeout_kills_process_and_logs_outcome(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "slow",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "timeout_seconds": 1,
                    "command": [sys.executable, "-c", "import time; print('starting', flush=True); time.sleep(30)"],
                }
            ],
        },
    )

    started = time.monotonic()
    proc = _run_trigger(manifest, "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly")
    elapsed = time.monotonic() - started
    log_text = (log_dir / "nightly.log").read_text(encoding="utf-8")

    assert proc.returncode == 124
    assert elapsed < 10
    assert "starting" in proc.stdout
    assert "task_timeout id=slow timeout_seconds=1" in log_text
    assert "runner_exit trigger=nightly exit_code=124" in log_text


def test_run_trigger_reports_closed_output_sink_without_stalling(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "noisy",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "command": [sys.executable, "-c", "print('x' * 100000, flush=True)"],
                }
            ],
        },
    )

    runner = subprocess.Popen(
        [sys.executable, str(RUN_TRIGGER), "--manifest", str(manifest), "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert runner.stdout is not None
    assert runner.stderr is not None
    runner.stdout.close()
    stderr = runner.stderr.read()

    assert runner.wait(timeout=10) == 1
    assert "Task noisy output relay failed" in stderr
    log_text = (log_dir / "nightly.log").read_text(encoding="utf-8")
    assert "task_output_error id=noisy detail=stdout relay failed: BrokenPipeError:" in log_text
    assert "runner_exit trigger=nightly exit_code=1 reason=output_relay_failed" in log_text


def test_add_task_rejects_out_of_range_sequence_order_without_writing(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "last", "trigger": "nightly", "sequence_order": 86400, "command": ["python3", "--version"]}],
        },
    )
    before = manifest.read_text(encoding="utf-8")

    explicit = _run_cron_ctl(
        manifest,
        "add-task",
        "--trigger",
        "nightly",
        "--id",
        "too-high",
        "--sequence-order",
        "86401",
        "--command",
        "python3 --version",
    )
    computed = _run_cron_ctl(
        manifest,
        "add-task",
        "--trigger",
        "nightly",
        "--id",
        "after-last",
        "--command",
        "python3 --version",
    )

    assert explicit.returncode == 1
    assert computed.returncode == 1
    assert "sequence_order: must be in the range 0..86400" in explicit.stderr
    assert "sequence_order: must be in the range 0..86400" in computed.stderr
    assert manifest.read_text(encoding="utf-8") == before


def test_run_trigger_relay_failure_takes_precedence_over_timeout(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "slow-noisy",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "timeout_seconds": 1,
                    "command": [sys.executable, "-c", "import time; print('x' * 100000, flush=True); time.sleep(30)"],
                }
            ],
        },
    )

    runner = subprocess.Popen(
        [sys.executable, str(RUN_TRIGGER), "--manifest", str(manifest), "--repo-root", str(repo_root), "--log-dir", str(log_dir), "nightly"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert runner.stdout is not None
    assert runner.stderr is not None
    runner.stdout.close()
    stderr = runner.stderr.read()

    assert runner.wait(timeout=10) == 1
    assert "Task slow-noisy output relay failed" in stderr
    log_text = (log_dir / "nightly.log").read_text(encoding="utf-8")
    assert "task_output_error id=slow-noisy detail=stdout relay failed: BrokenPipeError:" in log_text
    assert "runner_exit trigger=nightly exit_code=1 reason=output_relay_failed" in log_text
    assert "task_timeout id=slow-noisy timeout_seconds=1" not in log_text
