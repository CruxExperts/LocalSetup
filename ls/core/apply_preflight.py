from __future__ import annotations

import stat
from pathlib import Path

from .adapters import adapter_path_state, legacy_global_roots
from .provenance import is_managed_package

SAFE_ADAPTER_STATUS_CODES = {
    "absent",
    "custom_repo_skills",
    "managed_scoped_adapter",
    "managed_portable_adapter",
    "legacy_monolithic_symlink",
    "mixed_managed_custom_adapter",
    "shared_adapter_directory",
}


def unsafe_same_name_adapter_entries(action, state: dict) -> list[str]:
    selected = {str(name) for name in action.details.get("packages", [])}
    unsafe = set(state.get("custom_entries", [])) | set(state.get("unknown_entries", []))
    return sorted(selected & unsafe)


def codex_agent_source(repo_root: Path, agent_name: str) -> Path:
    return repo_root / "ls" / "adapters" / "codex" / "agents" / f"{agent_name}.toml"


def _codex_agent_conflicts(repo_root: Path, plan, policy: str) -> tuple[list[dict], list[dict[str, str]]]:
    blockers: list[dict] = []
    preserved: list[dict[str, str]] = []
    for action in plan.actions:
        if action.kind != "install_codex_agents":
            continue
        for raw_name in action.details.get("agents", []):
            name = str(raw_name)
            source = codex_agent_source(repo_root, name)
            destination = action.path / f"{name}.toml"
            try:
                source_bytes = source.read_bytes()
            except FileNotFoundError:
                blockers.append(
                    {"path": str(source), "status_code": "missing_source_agent",
                     "reason": "selected Codex agent source is missing"}
                )
                continue
            except OSError as exc:
                blockers.append(
                    {"path": str(source), "status_code": "unreadable_source_agent",
                     "reason": f"selected Codex agent source is unreadable: {exc}"}
                )
                continue
            try:
                target_stat = destination.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                blockers.append(
                    {"path": str(destination), "status_code": "codex_agent_conflict",
                     "reason": f"refusing to inspect existing Codex agent path: {exc}"}
                )
                continue
            if not stat.S_ISREG(target_stat.st_mode):
                blockers.append(
                    {"path": str(destination), "status_code": "codex_agent_conflict",
                     "reason": "refusing to overwrite existing Codex agent path that is not a regular file"}
                )
                continue
            try:
                existing_bytes = destination.read_bytes()
            except OSError as exc:
                blockers.append(
                    {"path": str(destination), "status_code": "codex_agent_conflict",
                     "reason": f"refusing to overwrite unreadable existing Codex agent file: {exc}"}
                )
                continue
            if existing_bytes == source_bytes:
                continue
            reason = "existing readable Codex agent file differs from selected source"
            if policy == "preserve":
                preserved.append({"name": name, "path": str(destination), "reason": reason})
            else:
                blockers.append(
                    {"path": str(destination), "status_code": "codex_agent_conflict",
                     "reason": "refusing to overwrite existing Codex agent file with different content"}
                )
    return blockers, preserved


def preflight_install_plan(repo_root: Path, plan, home: Path, *, target_root: Path | None = None) -> dict:
    blockers: list[dict] = []
    codex_agent_conflict = getattr(plan, "codex_agent_conflict", "error")
    if codex_agent_conflict not in {"error", "preserve"}:
        raise ValueError(f"unsupported Codex agent conflict policy: {codex_agent_conflict}")
    codex_blockers, preserved_codex_agents = _codex_agent_conflicts(repo_root, plan, codex_agent_conflict)
    blockers.extend(codex_blockers)
    from .mutable_ownership import require_owned_copies
    try:require_owned_copies(repo_root, home, [a.path for a in plan.actions], target=target_root or repo_root)
    except ValueError as exc:
        blockers.append({"path": str(target_root or repo_root), "status_code": "mutable_copy_preservation", "reason": str(exc)})
    from .hermes_adapter import hermes_adapter_blockers
    blockers.extend(hermes_adapter_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .claude_prerequisite import claude_prerequisite_blockers
    blockers.extend(claude_prerequisite_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .gemini_prerequisite import gemini_prerequisite_blockers
    blockers.extend(gemini_prerequisite_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .openclaw_prerequisite import openclaw_prerequisite_blockers
    blockers.extend(openclaw_prerequisite_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .kimi_prerequisite import kimi_prerequisite_blockers
    blockers.extend(kimi_prerequisite_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .factory_preflight import factory_skill_blockers
    blockers.extend(factory_skill_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .amp_preflight import amp_skill_blockers
    blockers.extend(amp_skill_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .opencode_preflight import opencode_skill_blockers
    blockers.extend(opencode_skill_blockers(repo_root, plan.actions, home, target_root or repo_root))
    from .goose_prerequisite import goose_prerequisite_blockers
    blockers.extend(goose_prerequisite_blockers(repo_root, plan.actions, home, target_root or repo_root))
    import hashlib
    for raw_path, expected in plan.rollback_metadata.get("recorded_state_hashes", {}).items():
        try:unchanged = hashlib.sha256(Path(raw_path).read_bytes()).hexdigest() == expected
        except OSError:unchanged = False
        if not unchanged:
            blockers.append({"path": raw_path, "status_code": "stale_recorded_plan",
                             "reason": "recorded ownership changed; rebuild the update plan"})
    from .personal_registry import validate_personal_selection_consistency
    try:
        validate_personal_selection_consistency([
            action.details for action in plan.actions if action.kind == "attach_personal_path"
        ])
    except ValueError as exc:
        blockers.append({"path": str(target_root or repo_root),
                         "status_code": "personal_selection_conflict", "reason": str(exc)})
    from .adapter_coalescing import paired_repository_actions
    try:pairs = paired_repository_actions(plan)
    except ValueError as exc:
        return {"ok": False, "blockers": [*blockers, {"path": str(target_root or repo_root),
                "status_code": "overlapping_scope_actions", "reason": str(exc)}],
                "preserved_codex_agents": preserved_codex_agents}
    for action in plan.actions:
        if action.kind == "attach_personal_path":
            from .personal_adapter import selection
            pair = pairs.get(action.path)
            retiring = any(a.kind == 'retire_historical_adapter' and a.path == action.path for a in plan.actions)
            try:selection(repo_root, home, action,
                          repository_target=(target_root or repo_root) if pair or retiring else None,
                          repository_packages=pair.details.get("packages", []) if pair else [] if retiring else None)
            except (ValueError, OSError) as exc:
                blockers.append({"path": str(action.path), "status_code": "personal_adapter_unsafe", "reason": str(exc)})
            continue
        if action.kind in {"install_skills", "install_workflows"}:
            source_subdir = "skills" if action.kind == "install_skills" else "workflows"
            names = action.details.get("skills", action.details.get("workflows", []))
            for name in names:
                src = repo_root / "ls" / source_subdir / str(name)
                dest = action.path / str(name)
                if not src.is_dir():
                    blockers.append(
                        {"path": str(src), "status_code": "missing_source_package", "reason": "selected package source is missing"}
                    )
                elif dest.exists() and not is_managed_package(dest):
                    blockers.append(
                        {
                            "path": str(dest),
                            "status_code": "unmanaged_package_path",
                            "reason": "refusing to overwrite unmanaged package path",
                        }
                    )
        elif action.kind == "install_codex_agents":
            continue
        elif action.kind == "attach_repo_path":
            if action.path in pairs:continue
            from .repository_overlap import check_overlap
            try:
                if check_overlap(repo_root, home, target_root or repo_root, action):continue
            except (ValueError, OSError) as exc:
                blockers.append({"path": str(action.path), "status_code": "personal_owner_overlap", "reason": str(exc)})
                continue
            global_root = Path(action.details["global_root"])
            state = adapter_path_state(
                action.path,
                global_root,
                known_global_roots=legacy_global_roots(home),
                target_root=target_root,
            )
            if state["status_code"] not in SAFE_ADAPTER_STATUS_CODES:
                blockers.append(
                    {
                        "path": str(action.path),
                        "status_code": state["status_code"],
                        "reason": state["collision_reason"] or "adapter target is not safe to mutate",
                    }
                )
                continue
            unsafe_entries = unsafe_same_name_adapter_entries(action, state)
            if unsafe_entries:
                blockers.append(
                    {
                        "path": str(action.path),
                        "status_code": "adapter_custom_package_name_collision",
                        "reason": "adapter contains custom or unknown entries with selected LocalSetup package names",
                        "entries": unsafe_entries,
                    }
                )
        elif action.kind == "retire_historical_adapter":
            if any(a.kind == 'attach_personal_path' and a.path == action.path for a in plan.actions):
                continue
            from .historical_ownership import retained_historical_action
            try:
                retained = retained_historical_action(repo_root, home, target_root or repo_root,
                                                      action.path, Path(action.details['global_root']))
                if retained is not None:continue
            except (ValueError, OSError, TypeError, KeyError) as exc:
                blockers.append({'path': str(action.path), 'status_code': 'historical_current_owner', 'reason': str(exc)})
                continue
            global_root = Path(action.details["global_root"])
            state = adapter_path_state(
                action.path,
                global_root,
                known_global_roots=legacy_global_roots(home),
                target_root=target_root,
            )
            platform = str(action.details.get("platform", "unknown"))
            if action.path.exists() and not action.path.is_symlink() and not action.path.is_dir():
                blockers.append(
                    {
                        "path": str(action.path),
                        "status_code": "unsupported_historical_adapter_node",
                        "reason": f"historical {platform} adapter is not a supported symlink or directory",
                    }
                )
            elif action.path.is_symlink() and not (
                state["points_to_global"]
                or state["points_to_legacy_global"]
                or state.get("managed_visible_packages")
            ):
                blockers.append(
                    {
                        "path": str(action.path),
                        "status_code": (
                            "unproven_legacy_codex_symlink"
                            if platform == "codex"
                            else f"unproven_historical_{platform}_symlink"
                        ),
                        "reason": (
                            f"historical {action.path} symlink is not proven LocalSetup-managed; "
                            f"preserve it and review or remove it before installing the {platform} adapter"
                        ),
                    }
                )
    return {
        "ok": not blockers,
        "blockers": blockers,
        "preserved_codex_agents": preserved_codex_agents,
    }
