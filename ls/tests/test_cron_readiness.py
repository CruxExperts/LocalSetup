import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "ls" / "skills" / "ls-cron-job-scheduling" / "scripts" / "cron_ctl.py"


def _write_manifest(path: Path, command: list[str]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "triggers": {"nightly": {"schedule": "0 2 * * *"}},
                "tasks": [
                    {
                        "id": "report-task",
                        "trigger": "nightly",
                        "sequence_order": 1,
                        "enabled": False,
                        "command": command,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _readiness(
    manifest: Path,
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--manifest", str(manifest), "readiness", *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_readiness_is_read_only_and_keeps_unknowns_separate(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    marker = tmp_path / "workload-ran"
    _write_manifest(
        manifest,
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
    )
    original_manifest = manifest.read_bytes()

    proc = _readiness(manifest, "--repo-root", str(repo_root), "--trigger", "nightly", "--log-dir", str(log_dir))

    assert proc.returncode == 0
    assert "Automation readiness: unchecked" in proc.stdout
    assert "[READY] Manifest:" in proc.stdout
    assert "[READY] Task report-task executable:" in proc.stdout
    assert "[UNCHECKED] Task-specific requirements and recovery:" in proc.stdout
    assert "Activation state and run history are separate" in proc.stdout
    assert "no task was run, enabled, or installed" in proc.stdout
    assert manifest.read_bytes() == original_manifest
    assert not marker.exists()
    assert not log_dir.exists()


def test_readiness_blocks_missing_task_executable(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    missing_executable = tmp_path / "not-installed"
    _write_manifest(manifest, [str(missing_executable), "--version"])

    proc = _readiness(manifest, "--repo-root", str(repo_root), "--trigger", "nightly")

    assert proc.returncode == 0
    assert "Automation readiness: blocked" in proc.stdout
    assert "[BLOCKED] Task report-task executable:" in proc.stdout
    assert "Provide an existing executable path" in proc.stdout


def test_readiness_reports_insecure_log_path_without_repairing_it(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    manifest = tmp_path / "manifest.yaml"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    os.chmod(log_dir, 0o755)
    _write_manifest(manifest, [sys.executable, "--version"])

    proc = _readiness(manifest, "--repo-root", str(repo_root), "--trigger", "nightly", "--log-dir", str(log_dir))

    assert proc.returncode == 0
    assert "Automation readiness: blocked" in proc.stdout
    assert "[BLOCKED] Log permissions for nightly:" in proc.stdout
    assert "existing log directory allows group/other access" in proc.stdout
    assert log_dir.stat().st_mode & 0o777 == 0o755
    assert not (log_dir / "nightly.log").exists()


def test_readiness_resolves_relative_and_empty_path_entries_from_repo_root(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    manifest = tmp_path / "manifest.yaml"
    caller_root = tmp_path / "caller"
    repo_root = tmp_path / "repo"
    (repo_root / "bin").mkdir(parents=True)
    caller_root.mkdir()
    relative_tool = repo_root / "bin" / "relative-tool"
    current_dir_tool = repo_root / "current-dir-tool"
    for tool in (relative_tool, current_dir_tool):
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)

    original_path = os.environ.get("PATH", os.defpath)
    _write_manifest(manifest, [relative_tool.name])
    relative_proc = _readiness(
        manifest,
        "--repo-root",
        str(repo_root),
        "--trigger",
        "nightly",
        cwd=caller_root,
        env={**os.environ, "PATH": f"bin{os.pathsep}{original_path}"},
    )

    _write_manifest(manifest, [current_dir_tool.name])
    empty_entry_proc = _readiness(
        manifest,
        "--repo-root",
        str(repo_root),
        "--trigger",
        "nightly",
        cwd=caller_root,
        env={**os.environ, "PATH": f"{os.pathsep}{original_path}"},
    )

    assert relative_proc.returncode == 0
    assert "[READY] Task report-task executable:" in relative_proc.stdout
    assert str(relative_tool) in relative_proc.stdout
    assert empty_entry_proc.returncode == 0
    assert "[READY] Task report-task executable:" in empty_entry_proc.stdout
    assert str(current_dir_tool) in empty_entry_proc.stdout


def test_readiness_reports_path_probe_errors_without_tracebacks(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    manifest = tmp_path / "manifest.yaml"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    loop_path = tmp_path / "loop"
    loop_path.symlink_to(loop_path, target_is_directory=True)
    _write_manifest(manifest, [sys.executable, "--version"])

    root_proc = _readiness(manifest, "--repo-root", str(loop_path), "--trigger", "nightly")
    log_proc = _readiness(
        manifest,
        "--repo-root",
        str(repo_root),
        "--trigger",
        "nightly",
        "--log-dir",
        str(loop_path),
    )

    assert root_proc.returncode == 0
    assert "[BLOCKED] Working directory: could not inspect" in root_proc.stdout
    assert "Traceback" not in root_proc.stderr
    assert log_proc.returncode == 0
    assert "[BLOCKED] Log permissions for nightly: could not inspect" in log_proc.stdout
    assert "Traceback" not in log_proc.stderr
