import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "ls" / "skills" / "ls-cron-job-scheduling" / "scripts" / "run_trigger.py"


def _run(manifest: Path, trigger: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--manifest", str(manifest), "--repo-root", str(ROOT), trigger],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_default_repo_root_is_manifest_parent(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifest-home"
    manifest_dir.mkdir()
    output = manifest_dir / "cwd.txt"
    manifest = manifest_dir / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "pwd",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('cwd.txt').write_text(str(Path.cwd()))",
                    ],
                }
            ],
        },
    )

    proc = subprocess.run(
        [sys.executable, str(TOOL), "--manifest", str(manifest), "nightly"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert proc.returncode == 0
    assert output.read_text(encoding="utf-8") == str(manifest_dir)


def test_rejects_malformed_task_command_type(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "bad", "trigger": "nightly", "sequence_order": 1, "command": {"bad": "shape"}}],
        },
    )

    proc = _run(manifest, "nightly")

    assert proc.returncode == 1
    assert "command must be a string or list" in proc.stderr


def test_rejects_unsafe_shell_operator_shape(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [{"id": "unsafe", "trigger": "nightly", "sequence_order": 1, "command": "echo ok && echo bad"}],
        },
    )

    proc = _run(manifest, "nightly")

    assert proc.returncode == 1
    assert "unsupported shell operators" in proc.stderr


def test_timeout_reports_task_id_and_limit(tmp_path: Path) -> None:
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
                    "command": [sys.executable, "-c", "import time; time.sleep(2)"],
                    "timeout_seconds": 1,
                }
            ],
        },
    )

    proc = _run(manifest, "nightly")

    assert proc.returncode == 124
    assert "Task slow timed out after 1s" in proc.stderr


def test_nonzero_task_surfaces_stderr_and_exit_code(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {
                    "id": "fails",
                    "trigger": "nightly",
                    "sequence_order": 1,
                    "command": [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); raise SystemExit(7)"],
                }
            ],
        },
    )

    proc = _run(manifest, "nightly")

    assert proc.returncode == 7
    assert "boom" in proc.stderr
    assert "Task fails exited 7" in proc.stderr


def test_runs_tasks_in_sequence_for_trigger(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    _write_manifest(
        manifest,
        {
            "triggers": {"nightly": {"schedule": "0 2 * * *"}},
            "tasks": [
                {"id": "second", "trigger": "nightly", "sequence_order": 2, "command": [sys.executable, "-c", "print('second')"]},
                {"id": "first", "trigger": "nightly", "sequence_order": 1, "command": [sys.executable, "-c", "print('first')"]},
                {"id": "skip", "trigger": "nightly", "sequence_order": 3, "enabled": False, "command": [sys.executable, "-c", "print('nope')"]},
            ],
        },
    )

    proc = _run(manifest, "nightly")

    assert proc.returncode == 0
    assert proc.stderr == ""
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert lines == ["first", "second"]
