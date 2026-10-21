from __future__ import annotations

from pathlib import Path
from typing import Any

from .tmux_terminal_mode.cli import _default_tools_dir
from .tmux_terminal_mode.constants import DEFAULT_RULES_FILE
from .tmux_terminal_mode.layers import (
    default_shell_rc,
    detect_existing_settings_file,
    terminal_mode_status,
)

TMUX_WORKFLOWS = ("ls-wf-sudo-tmux-session", "ls-workflow-tmux-terminal-mode")


def _workflow_source_drift(workflow_path: Path, repo_root: Path) -> list[dict[str, str]]:
    drift: list[dict[str, str]] = []
    expected = str(repo_root.resolve(strict=False))
    for rel in ("SKILL.md", "workflow.yaml"):
        path = workflow_path / rel
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "/ls/" in line and expected not in line:
                drift.append({"path": str(path), "line": line.strip()})
                break
    return drift


def terminal_mode_health(
    repo_root: Path,
    *,
    home: Path,
    global_root: Path,
    lock: dict[str, Any] | None,
    adapters: list[dict[str, Any]],
    target_root: Path | None = None,
) -> dict[str, Any]:
    attachment_root = target_root or repo_root
    rules_path = attachment_root / DEFAULT_RULES_FILE
    status = terminal_mode_status(
        settings_path=detect_existing_settings_file(),
        rc_path=default_shell_rc(),
        rules_path=rules_path,
        tools_dir=_default_tools_dir(),
    )

    lock = lock or {}
    installed_workflows = set(lock.get("workflows", []))
    global_workflows: dict[str, dict[str, Any]] = {}
    source_drift: list[dict[str, str]] = []
    for workflow in TMUX_WORKFLOWS:
        workflow_path = global_root / workflow
        present = workflow_path.is_dir()
        global_workflows[workflow] = {"present": present, "path": str(workflow_path)}
        if present:
            source_drift.extend(_workflow_source_drift(workflow_path, repo_root))

    adapter_rows: list[dict[str, Any]] = []
    for adapter in adapters:
        visible = set(adapter.get("managed_visible_packages", adapter.get("visible_packages", [])))
        if not visible and not adapter.get("exists"):
            continue
        missing = [workflow for workflow in TMUX_WORKFLOWS if workflow not in visible]
        adapter_rows.append(
            {
                "platform": adapter.get("platform"),
                "path": adapter.get("repo_path"),
                "active": bool(adapter.get("exists")),
                "missing_workflows": missing,
            }
        )

    warnings: list[str] = []
    repair_hints: list[str] = []
    if status["layers"]["rules"]["active"] and not status["layers"]["rules"]["current"]:
        repair_hints.append("terminal-mode rules sentinel is present but not current; re-run tmux_terminal_mode enable after review")
    if not status["layers"]["tmux_ops"]["present"]:
        warnings.append("terminal-mode tmux_ops tool missing")
    for workflow in TMUX_WORKFLOWS:
        if workflow not in installed_workflows:
            repair_hints.append(f"terminal-mode workflow missing from lock selection: {workflow}")
        if not global_workflows[workflow]["present"]:
            repair_hints.append(f"terminal-mode workflow missing from managed package root: {workflow}")
    for row in adapter_rows:
        if row["active"] and row["missing_workflows"]:
            repair_hints.append(
                "terminal-mode workflows missing from adapter-visible packages "
                f"({row['path']}): {', '.join(row['missing_workflows'])}"
            )
    if source_drift:
        warnings.append("terminal-mode workflow package contains stale source-root references")

    return {
        "status": status,
        "workflows": {
            "expected": list(TMUX_WORKFLOWS),
            "lock_present": sorted(workflow for workflow in TMUX_WORKFLOWS if workflow in installed_workflows),
            "global": global_workflows,
            "adapters": adapter_rows,
        },
        "source_root_drift": source_drift,
        "warnings": warnings,
        "repair_hints": repair_hints,
    }
