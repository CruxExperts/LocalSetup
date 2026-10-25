from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import InstallConfig, config_to_dict
from .dependencies import dependency_status
from .doctor import run_doctor
from .health import read_health_status
from .migration import detect_legacy_artifacts
from .plan import build_install_plan


def _action_dicts(plan_actions) -> list[dict[str, Any]]:
    return [{"kind": action.kind, "path": str(action.path), "details": action.details} for action in plan_actions]


def build_agent_context(repo_root: Path, *, home: Path, config: InstallConfig) -> dict:
    target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
    data_root = Path(config.data_root).expanduser().resolve() if config.data_root else home / ".local" / "share" / "localsetup"
    plan = build_install_plan(
        repo_root,
        home=home,
        packs=config.packs,
        preset=config.preset,
        skills=config.skills,
        workflows=config.workflows,
        skill_classes=config.skill_classes,
        skill_tags=config.skill_tags,
        exclude_skills=config.exclude_skills,
        global_packs=config.global_packs,
        global_preset=config.global_preset,
        global_skills=config.global_skills,
        global_workflows=config.global_workflows,
        global_skill_classes=config.global_skill_classes,
        global_skill_tags=config.global_skill_tags,
        global_exclude_skills=config.global_exclude_skills,
        repo_packs=config.repo_packs,
        repo_preset=config.repo_preset,
        repo_skills=config.repo_skills,
        repo_workflows=config.repo_workflows,
        repo_skill_classes=config.repo_skill_classes,
        repo_skill_tags=config.repo_skill_tags,
        repo_exclude_skills=config.repo_exclude_skills,
        attach_mode=config.attach_mode,
        skill_scope=config.skill_scope,
        platform_ids=config.platforms,
        target_root=target_root,
    )
    doctor = run_doctor(
        repo_root,
        home=home,
        packs=config.packs,
        platform_ids=config.platforms,
        dependency_mode=config.dependency_mode,
        data_root=data_root,
        target_root=target_root,
    )
    dependencies = dependency_status(repo_root, mode=config.dependency_mode, data_root=data_root, target_root=target_root).to_dict()
    selected_platforms = plan.rollback_metadata.get("platforms", [])
    selected_packs = plan.rollback_metadata.get("packs", config.packs)
    migration_artifacts = detect_legacy_artifacts(repo_root, home=home, platform_ids=config.platforms, target_root=target_root)
    health = read_health_status(home=home, target_root=target_root or repo_root)

    commands = {
        "doctor": "localsetup doctor",
        "plan": "localsetup plan",
        "install": "localsetup install --yes",
        "verify": "localsetup verify",
        "rollback": "localsetup rollback",
    }
    suffix_parts: list[str] = []
    if config.target_directory:
        suffix_parts.extend(["--target-directory", config.target_directory])
    if config.platforms:
        suffix_parts.extend(["--platforms", *config.platforms])
    if suffix_parts:
        suffix = " " + " ".join(suffix_parts)
        commands = {key: value + suffix for key, value in commands.items()}

    return {
        "environment": doctor["environment"],
        "config": config_to_dict(config),
        "selected_platforms": selected_platforms,
        "selected_packs": selected_packs,
        "dependencies": dependencies,
        "migration": {
            "mode": config.migration_mode,
            "artifacts": migration_artifacts,
            "backup_dir": config.backup_dir,
        },
        "actions": _action_dicts(plan.actions),
        "blockers": doctor["blockers"],
        "warnings": doctor["warnings"],
        "health": health.get("event"),
        "commands": commands,
        "rollback": plan.rollback_metadata,
        "verification": [
            "uv run --locked pytest <focused-test-file-or-node> -q",
            "uv run --locked python ls/tools/localsetup.py validate-catalog",
            "uv run --locked python ls/tools/localsetup.py validate-package-surface",
            "uv run --locked python ls/tools/localsetup.py scan-migration",
            "uv run --locked ./ls/tests/automated_test.sh",
            "uv run --locked pytest -n \"$(uv run --locked python ls/tools/localsetup.py --source-root . test-workers)\" ls/tests -q",
        ],
    }


def render_markdown_report(context: dict) -> str:
    lines = [
        "# LocalSetup Install Context",
        "",
        f"- Repo: `{context['environment']['repo_root']}`",
        f"- Home: `{context['environment']['home']}`",
        f"- Platforms: {', '.join(context['selected_platforms']) or 'none'}",
        f"- Packs: {', '.join(context['selected_packs']) or 'none'}",
        f"- Dependency mode: `{context['dependencies']['mode']}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = context.get("blockers") or []
    lines.extend([f"- {item}" for item in blockers] or ["- None"])
    lines.extend(["", "## Warnings", ""])
    warnings = context.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] or ["- None"])
    health = context.get("health") or {}
    lines.extend(["", "## Health", ""])
    if health:
        lines.append(f"- Status: `{health.get('status', 'unknown')}`")
        lines.append(f"- Last operation: `{health.get('operation', 'unknown')}`")
        actions = health.get("next_actions") or []
        if actions:
            lines.append(f"- Next repair command: `{actions[0]}`")
    else:
        lines.append("- No health status recorded; run `localsetup health --json`.")
    lines.extend(["", "## Planned Actions", ""])
    for action in context.get("actions", []):
        lines.append(f"- `{action['kind']}` -> `{action['path']}`")
    if not context.get("actions"):
        lines.append("- None")
    lines.extend(["", "## Commands", ""])
    for key, value in context.get("commands", {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Verification", ""])
    for command in context.get("verification", []):
        lines.append(f"- `{command}`")
    return "\n".join(lines) + "\n"
