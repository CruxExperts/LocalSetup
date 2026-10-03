from __future__ import annotations

from pathlib import Path

from .provenance import load_package_marker, marker_public_snapshot
from .source import source_commit
from .installation_ownership import repository_owners


def build_lock_payload(
    *,
    repo_root: Path,
    home: Path,
    attachment_root: Path,
    pack,
    plan,
    installed_skills: list[str],
    installed_workflows: list[str],
    installed_codex_agents: list[str],
    preserved_codex_agents: list[dict[str, str]],
    dependency_info: dict | None,
) -> dict:
    adapter_actions = [action for action in plan.actions if action.kind == "attach_repo_path"]
    transition_actions = [action for action in plan.actions if action.kind == "retire_historical_adapter"]
    return {
        "version": 2,
        "skill_scope": plan.rollback_metadata.get("skill_scope", "repo"),
        "pack": pack.pack_id,
        "namespace": pack.namespace,
        "source_commit": source_commit(repo_root),
        "source_root": str(repo_root),
        "localsetup_home": str(home / ".local" / "share" / "localsetup"),
        "target_root": str(attachment_root),
        "aliases": plan.rollback_metadata.get("aliases", {}),
        "skills": plan.rollback_metadata.get("skills", []),
        "workflows": plan.rollback_metadata.get("workflows", []),
        "codex_agents": plan.rollback_metadata.get("codex_agents", []),
        "global_baseline_selectors": plan.rollback_metadata.get("global_baseline_selectors", {}),
        "global_baseline_packs": plan.rollback_metadata.get("global_baseline_packs", []),
        "global_baseline_skills": plan.rollback_metadata.get("global_baseline_skills", []),
        "global_baseline_workflows": plan.rollback_metadata.get("global_baseline_workflows", []),
        "global_baseline_packages": plan.rollback_metadata.get("global_baseline_packages", []),
        "repo_selectors": plan.rollback_metadata.get("repo_selectors", {}),
        "repo_packs": plan.rollback_metadata.get("repo_packs", []),
        "repo_skills": plan.rollback_metadata.get("repo_skills", []),
        "repo_workflows": plan.rollback_metadata.get("repo_workflows", []),
        "repo_packages": plan.rollback_metadata.get("repo_packages", []),
        "adapter_state": [state for state in plan.rollback_metadata.get("repo_links", [])],
        "adapter_targets": [
            {
                "platform": action.details.get("platform"),
                "platforms": action.details.get("platforms", [action.details.get("platform")]),
                "path": str(action.path),
                "owners": repository_owners(attachment_root, action.details.get("platforms", [action.details.get("platform")])),
                "mode": action.details.get("mode", "symlink"),
                "global_root": action.details.get("global_root"),
                "packages": action.details.get("packages", []),
                "verify_rules": action.details.get("verify_rules", []),
            }
            for action in adapter_actions
        ],
        "personal_adapter_targets": [
            {"path": str(action.path), "owners": action.details["owners"],
             "platforms": action.details["platforms"], "packages": action.details["packages"],
             "global_root": action.details["global_root"], "mode": action.details.get("mode", "symlink"),
             **({"owner_packages": action.details["owner_packages"]} if "owner_packages" in action.details else {})}
            for action in plan.actions if action.kind == "attach_personal_path"
        ],
        "adapter_transitions": [
            {
                "id": action.details.get("id"),
                "platform": action.details.get("platform"),
                "from": str(action.path),
                "to": action.details.get("replacement"),
                "disposition": action.details.get("disposition", "planned"),
                "removed": action.details.get("removed", []),
            }
            for action in transition_actions
        ] if transition_actions else plan.rollback_metadata.get("recorded_adapter_transitions", []),
        "platforms": plan.rollback_metadata.get("platforms", []),
        "global_only": plan.rollback_metadata.get("global_only", False),
        "attach_mode": plan.rollback_metadata.get("attach_mode", "symlink"),
        "installed_skills": installed_skills,
        "installed_workflows": installed_workflows,
        "installed_codex_agents": installed_codex_agents,
        "preserved_codex_agents": preserved_codex_agents,
        "adapter_packages": plan.rollback_metadata.get("adapter_packages", []),
        "dependency_mode": (dependency_info or {}).get("mode"),
        "python_interpreter": (dependency_info or {}).get("interpreter"),
        "dependency_state": (dependency_info or {}).get("lock"),
        "package_provenance": {
            Path(path).name: marker_public_snapshot(load_package_marker(Path(path)))
            for path in [*installed_skills, *installed_workflows]
        },
    }
