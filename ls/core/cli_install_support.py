from __future__ import annotations

import sys
from .cli_handler_sync import sync


def sync_from_facade(cli) -> None:
    sync(globals(), cli)


def _ensure_synced() -> None:
    cli = sys.modules.get("ls.core.cli")
    if cli is not None:
        sync_from_facade(cli)


def _all_configured_packs(repo_root: Path) -> list[str]:
    _ensure_synced()
    pack = load_pack_config(repo_root)
    return list(pack.packs.keys())


def _policy_findings(root: Path, skill_names: list[str], mode: str, *, bindings: dict | None = None) -> dict:
    _ensure_synced()
    by_name = {skill.name: skill for skill in load_skill_catalog(root)}
    warnings: list[str] = []
    blockers: list[str] = []
    for skill_name in skill_names:
        skill = by_name.get(skill_name)
        binding = (bindings or {}).get(skill_name)
        if binding:
            from .skills_external import validate_source_binding
            source = validate_source_binding(binding).path
        elif skill:
            source = skill.path
        else:
            continue
        frontmatter = parse_skill_frontmatter(source / "SKILL.md")
        risk = str(frontmatter.get("risk", "low"))
        permissions = frontmatter.get("permissions", [])
        invalid_metadata = False
        if risk not in {"low", "medium", "high"}:
            invalid_metadata = True
            warnings.append(f"{skill_name}: invalid risk metadata: {risk}")
        if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
            invalid_metadata = True
            warnings.append(f"{skill_name}: invalid permissions metadata")
            permissions = []
        if risk in {"medium", "high"} or permissions:
            warnings.append(f"{skill_name}: risk={risk}; permissions={permissions}")
        if mode in {"strict", "ci"} and invalid_metadata:
            blockers.append(f"invalid skill policy metadata blocked by {mode} policy: {skill_name}")
        if mode in {"strict", "ci"} and risk == "high":
            blockers.append(f"high-risk skill blocked by {mode} policy: {skill_name}")
    return {"mode": mode, "warnings": warnings, "blockers": blockers}

def _existing_target_platforms(repo_root: Path, target_root: Path, home: Path) -> list[dict[str, str]]:
    _ensure_synced()
    global_root = expand_user_path(load_pack_config(repo_root).global_root, home)
    selected: list[dict[str, str]] = []
    for platform in load_platforms(repo_root):
        for rel in platform.repo_paths:
            candidate = target_root / rel
            state = adapter_path_state(candidate, global_root)
            if state["points_to_global"] or state["is_portable_copy"]:
                selected.append(
                    {
                        "platform": platform.platform_id,
                        "mode": "portable" if state["is_portable_copy"] else "symlink",
                    }
                )
                break
    return sorted(selected, key=lambda item: item["platform"])


_SELECTOR_CONFIG_FIELDS = (
    "platforms",
    "packs",
    "preset",
    "skills",
    "skill_classes",
    "skill_tags",
    "exclude_skills",
    "workflows",
    "global_packs",
    "global_preset",
    "global_skills",
    "global_skill_classes",
    "global_skill_tags",
    "global_exclude_skills",
    "global_workflows",
    "repo_packs",
    "repo_preset",
    "repo_skills",
    "repo_skill_classes",
    "repo_skill_tags",
    "repo_exclude_skills",
    "repo_workflows",
)


def _selector_free(config: InstallConfig) -> bool:
    _ensure_synced()
    return all(getattr(config, field_name) is None for field_name in _SELECTOR_CONFIG_FIELDS)


def _repair_detected_existing_state(repair: dict) -> bool:
    _ensure_synced()
    shape = repair.get("detected_shape", {})
    if any(
        shape.get(key)
        for key in (
            "modern_lockfile",
            "legacy_lockfile",
            "adapter_paths",
            "historical_adapter_paths",
            "stale_framework_path",
            "partial_adapters",
            "protected_source_root",
        )
    ):
        return True
    inferred = repair.get("inferred", {})
    return bool(inferred.get("platforms"))


def _global_selector_kwargs_from_lock(target_root: Path) -> dict:
    _ensure_synced()
    lock = load_json(target_root / ".localsetup" / "lock.json")
    selectors = lock.get("global_baseline_selectors") if isinstance(lock, dict) else {}
    if not isinstance(selectors, dict):
        selectors = {}
    kwargs = {
        "global_packs": selectors.get("packs") if isinstance(selectors.get("packs"), list) else None,
        "global_preset": selectors.get("preset") if isinstance(selectors.get("preset"), str) else None,
        "global_skills": selectors.get("skills") if isinstance(selectors.get("skills"), list) else None,
        "global_workflows": selectors.get("workflows") if isinstance(selectors.get("workflows"), list) else None,
        "global_skill_classes": selectors.get("skill_classes") if isinstance(selectors.get("skill_classes"), list) else None,
        "global_skill_tags": selectors.get("skill_tags") if isinstance(selectors.get("skill_tags"), list) else None,
        "global_exclude_skills": selectors.get("exclude_skills") if isinstance(selectors.get("exclude_skills"), list) else None,
    }
    if any(value is not None for value in kwargs.values()):
        return kwargs
    if isinstance(lock.get("global_baseline_workflows"), list):
        return {
            "global_preset": "custom",
            "global_workflows": lock["global_baseline_workflows"],
            "global_skills": lock.get("global_baseline_skills") if isinstance(lock.get("global_baseline_skills"), list) else None,
        }
    if isinstance(lock.get("global_baseline_packs"), list):
        return {"global_packs": lock["global_baseline_packs"], "global_preset": "custom"}
    return {"global_preset": "core"}


def _build_auto_inferred_plan(root: Path, home: Path, target_root: Path, repair: dict):
    _ensure_synced()
    inferred = repair.get("inferred", {})
    return build_install_plan(
        root,
        home=home,
        **_global_selector_kwargs_from_lock(target_root),
        repo_preset="custom",
        repo_skills=list(inferred.get("repo_skills") or inferred.get("repo_packages") or []),
        repo_workflows=list(inferred.get("repo_workflows") or []),
        attach_mode=str(inferred.get("attach_mode") or "symlink"),
        platform_ids=list(inferred.get("platforms") or []),
        target_root=target_root,
    )


def _build_auto_new_repo_plan(root: Path, home: Path, target_root: Path):
    _ensure_synced()
    return build_install_plan(
        root,
        home=home,
        global_preset="normal",
        platform_ids=[],
        target_root=target_root,
    )


def _auto_plan_payload(
    root: Path,
    home: Path,
    config: InstallConfig,
    target_root: Path,
    plan,
    policy: dict,
    *,
    mode: str,
    repair: dict,
) -> dict:
    _ensure_synced()
    return {
        "auto_mode": mode,
        "actions": [{"kind": a.kind, "path": str(a.path), "details": a.details} for a in plan.actions],
        "config": config_to_dict(config),
        "attachment": {
            "target_root": str(target_root),
            "platforms": plan.rollback_metadata.get("platforms", []),
            "global_only": plan.rollback_metadata.get("global_only", False),
        },
        "inventory": install_inventory(root, home=home, target_root=target_root, platform_ids=plan.rollback_metadata.get("platforms", [])),
        "warnings": [],
        "policy": policy,
        "rollback": plan.rollback_metadata,
        "repair": repair,
    }


def _apply_install_plan(
    root: Path,
    home: Path,
    config: InstallConfig,
    target_root: Path,
    plan,
    policy: dict,
    *,
    mode: str | None = None,
) -> tuple[dict, int]:
    _ensure_synced()
    git_pre = git_status_snapshot(target_root)
    adapter_plan_paths: list[str] = []
    for action in plan.actions:
        if action.kind != "attach_repo_path":
            continue
        try:
            adapter_plan_paths.append(str(action.path.relative_to(target_root)))
        except ValueError:
            continue
    planned_paths = [
        ".localsetup/lock.json",
        ".localsetup/health.json",
        ".localsetup/AGENT_STATUS.md",
        *adapter_plan_paths,
    ]
    if policy["blockers"]:
        payload = {"ok": False, "policy": policy, "blockers": policy["blockers"]}
        if mode:
            payload["auto_mode"] = mode
        git_post = git_status_snapshot(target_root)
        _record_health_for_payload(
            root=root,
            home=home,
            target_root=target_root,
            operation="install",
            mode=mode or "explicit",
            payload=payload,
            git_pre=git_pre,
            git_post=git_post,
            planned_paths=planned_paths,
        )
        return payload, 1
    dependency_info = (
        ensure_dependencies(root, mode=config.dependency_mode, data_root=_config_data_root(config, home), target_root=target_root)
        if config.dependency_mode != "prompt-only"
        else None
    )
    try:
        result = apply_plan(root, plan, home=home, dry_run=False, dependency_info=dependency_info, target_root=target_root)
    except PackageRootLockTimeout as exc:
        payload = {"ok": False, "status_code": exc.status_code, "blockers": [str(exc)]}
        if mode:
            payload["auto_mode"] = mode
        git_post = git_status_snapshot(target_root)
        _record_health_for_payload(
            root=root,
            home=home,
            target_root=target_root,
            operation="install",
            mode=mode or "explicit",
            payload=payload,
            git_pre=git_pre,
            git_post=git_post,
            planned_paths=planned_paths,
        )
        return payload, 1
    if dependency_info:
        result["dependencies"] = dependency_info
    result["attachment"] = {
        "target_root": str(target_root),
        "platforms": plan.rollback_metadata.get("platforms", []),
        "global_only": plan.rollback_metadata.get("global_only", False),
    }
    if policy["warnings"]:
        result.setdefault("warnings", []).extend(policy["warnings"])
    result["policy"] = policy
    if mode:
        result["auto_mode"] = mode
    result["ok"] = True
    git_post = git_status_snapshot(target_root)
    _record_health_for_payload(
        root=root,
        home=home,
        target_root=target_root,
        operation="install",
        mode=mode or "explicit",
        payload=result,
        git_pre=git_pre,
        git_post=git_post,
        planned_paths=planned_paths,
    )
    return result, 0


def _auto_default_context(root: Path, home: Path, config: InstallConfig, target_root: Path) -> dict:
    _ensure_synced()
    from .retained_update import retained_repository_plan
    retained_plan = retained_repository_plan(root, home, target_root)
    if retained_plan is not None:
        return {"mode": "recorded_repo", "repair": {"ok": True, "actions": []}, "plan": retained_plan}
    repair = run_repair(
        root,
        home=home,
        target_root=target_root,
        platform_ids=None,
        backup_dir=Path(config.backup_dir).expanduser().resolve() if config.backup_dir else None,
        dependency_mode=config.dependency_mode,
        apply=False,
    )
    if repair.get("skill_scope") == "personal" and repair.get("ok") and not repair.get("actions"):
        from .personal_update import build_recorded_personal_plan
        return {"mode": "recorded_personal", "repair": repair,
                "plan": build_recorded_personal_plan(root, home, target_root)}
    if repair.get("skill_scope") == "both" and repair.get("ok") and not repair.get("actions"):
        from .personal_update import build_recorded_both_plan
        return {"mode": "recorded_both", "repair": repair,
                "plan": build_recorded_both_plan(root, home, target_root)}
    if repair.get("skill_scope") in {"personal", "both"}:
        return {"mode": "repair_required", "repair": repair, "plan": None}
    if repair.get("blockers") or repair.get("decisions"):
        return {"mode": "repair_required", "repair": repair, "plan": None}
    if not _repair_detected_existing_state(repair):
        return {"mode": "default_new_repo", "repair": repair, "plan": _build_auto_new_repo_plan(root, home, target_root)}
    non_resolver_actions = [
        action for action in repair.get("actions", [])
        if action.get("kind") != "refresh_paths_manifest"
    ]
    if non_resolver_actions:
        return {"mode": "repair_required", "repair": repair, "plan": None}
    return {"mode": "inferred_existing", "repair": repair, "plan": _build_auto_inferred_plan(root, home, target_root, repair)}


def _run_self_refresh(
    root: Path,
    config: InstallConfig,
    home: Path,
    *,
    packs_override: list[str] | None = None,
    platforms_override: list[str] | None = None,
    attach_mode_explicit: bool = False,
) -> dict:
    _ensure_synced()
    target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else root
    packs = packs_override if packs_override is not None else _all_configured_packs(root)
    from .self_refresh import recorded_refresh
    plan = recorded_refresh(root, home, target_root, config, packs,
                            platforms_override, attach_mode_explicit)
    platforms = list(plan.rollback_metadata['platforms']) if plan else (platforms_override or [])
    attach_mode = plan.rollback_metadata['attach_mode'] if plan else config.attach_mode
    if plan is None and not attach_mode_explicit:
        existing = _existing_target_platforms(root, target_root, home)
        modes = {item['mode'] for item in existing if item['platform'] in set(platforms)}
        if len(modes) > 1:
            raise ValueError('self-refresh found mixed adapter modes; select an explicit --mode')
        if modes:
            attach_mode = modes.pop()
    if plan is None:
        plan = build_install_plan(
            root,
            home=home,
            packs=packs,
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
            attach_mode=attach_mode,
            platform_ids=platforms,
            target_root=target_root,
            skill_scope=config.skill_scope,
        )
    from .apply_preflight import preflight_install_plan
    preflight = preflight_install_plan(root, plan, home, target_root=target_root)
    if not preflight['ok']:
        raise RuntimeError(f"install preflight failed: {preflight['blockers']}")
    dependency_info = (
        ensure_dependencies(root, mode=config.dependency_mode, data_root=_config_data_root(config, home), target_root=target_root)
        if config.dependency_mode != "prompt-only" else None
    )
    result = apply_plan(root, plan, home=home, dry_run=False, dependency_info=dependency_info, target_root=target_root)
    verify = verify_install(root, home=home, platform_ids=platforms, target_root=target_root)
    if dependency_info:
        result["dependencies"] = dependency_info
    return {
        "ok": verify["ok"],
        "selected": {
            "packs": packs,
            "platforms": platforms,
            "target_root": str(target_root),
            "attach_mode": attach_mode,
        },
        "apply": result,
        "verify": verify,
    }
