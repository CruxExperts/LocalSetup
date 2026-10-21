"""Skill smoke matrix phase for run_framework_audit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

FormatFailure = Callable[[str, subprocess.CompletedProcess[str]], str]
NormalizeSmoke = Callable[[object], tuple[str, str] | None]
SanitizeCommand = Callable[[str], list[str]]


def phase_skill_matrix(
    root: Path,
    fw: Path,
    yaml_module: object,
    normalize_smoke_entry: NormalizeSmoke,
    sanitize_smoke_command: SanitizeCommand,
    format_subprocess_failure: FormatFailure,
) -> tuple[list[str], list[str]]:
    """Run sandbox smoke for each skill with a command. Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    smoke_file = fw / "tests" / "skill_smoke_commands.yaml"
    if not smoke_file.is_file():
        errors.append("Missing skill_smoke_commands.yaml")
        return (errors, warnings)
    try:
        data = yaml_module.safe_load(smoke_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml_module.YAMLError) as e:
        errors.append(f"Could not load smoke list: {e}")
        return (errors, warnings)
    if not isinstance(data, dict):
        errors.append("skill_smoke_commands.yaml must be a YAML map")
        return (errors, warnings)
    skills_dir = fw / "skills"
    if not skills_dir.is_dir():
        errors.append(f"Framework skills directory missing or invalid: {skills_dir}")
        return (errors, warnings)
    invalid_keys = [
        repr(key) for key in data if not isinstance(key, str) or not key.strip()
    ]
    if invalid_keys:
        errors.append(
            "skill_smoke_commands.yaml keys must be non-empty skill ids: "
            + ", ".join(invalid_keys)
        )
        return (errors, warnings)
    try:
        skill_ids = sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
    except OSError as exc:
        errors.append(
            f"Could not list framework skills directory {skills_dir}: "
            f"{type(exc).__name__}: {exc}"
        )
        return (errors, warnings)
    smoke_ids = set(data)
    missing_smoke_rows = [
        skill_id for skill_id in skill_ids if skill_id not in smoke_ids
    ]
    if missing_smoke_rows:
        errors.append(
            "skill_smoke_commands.yaml missing entries for skill dirs: "
            + ", ".join(missing_smoke_rows)
        )
        return (errors, warnings)
    create_sandbox = (
        fw
        / "skills"
        / "ls-skill-sandbox-tester"
        / "scripts"
        / "create_sandbox.py"
    )
    run_smoke = (
        fw / "skills" / "ls-skill-sandbox-tester" / "scripts" / "run_smoke.py"
    )
    if not create_sandbox.is_file() or not run_smoke.is_file():
        errors.append("Sandbox tooling (create_sandbox.py, run_smoke.py) not found")
        return (errors, warnings)
    for skill_id, entry in data.items():
        try:
            smoke = normalize_smoke_entry(entry)
        except ValueError as exc:
            errors.append(f"Skill matrix {skill_id}: invalid smoke entry: {exc}")
            continue
        if smoke is None:
            continue
        cwd_mode, cmd = smoke
        skill_path = skills_dir / skill_id
        if not skill_path.is_dir():
            warnings.append(f"Smoke list references missing skill dir: {skill_id}")
            continue
        try:
            if cwd_mode == "repo-root":
                argv = sanitize_smoke_command(cmd)
                cp = subprocess.run(
                    argv,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if cp.returncode != 0:
                    errors.append(
                        format_subprocess_failure(
                            f"Skill matrix {skill_id}: repo-root smoke",
                            cp,
                        )
                    )
                continue
            cp = subprocess.run(
                [
                    sys.executable, str(create_sandbox), "--skill-path", str(skill_path),
                    "--shared-deps", str(fw / "lib" / "deps.py"),
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp.returncode != 0:
                errors.append(
                    format_subprocess_failure(
                        f"Skill matrix {skill_id}: create_sandbox",
                        cp,
                    )
                )
                continue
            sandbox_dir = cp.stdout.strip().split("\n")[-1].strip()
            if not sandbox_dir:
                errors.append(f"Skill matrix {skill_id}: empty sandbox path")
                continue
            cp2 = subprocess.run(
                [
                    sys.executable,
                    str(run_smoke),
                    "--sandbox-dir",
                    sandbox_dir,
                    "--command",
                    cmd,
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if cp2.returncode != 0:
                errors.append(
                    format_subprocess_failure(
                        f"Skill matrix {skill_id}: smoke",
                        cp2,
                    )
                )
        except subprocess.TimeoutExpired:
            errors.append(f"Skill matrix {skill_id}: timeout")
        except Exception as e:
            errors.append(f"Skill matrix {skill_id}: {e}")
    return (errors, warnings)
