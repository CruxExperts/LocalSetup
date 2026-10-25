from __future__ import annotations

from pathlib import Path

from .adapters import adapter_path_state, adapter_status, legacy_global_roots, recorded_adapter_status
from .lockfile import load_json
from .manifests import load_pack_config, load_platforms
from .paths import expand_user_path, legacy_target_lockfile_path, repo_path, target_lockfile_path
from .provenance import is_managed_package, package_digest, provenance_report
from .reference_materializer import validate_materialized_package
from .registry import load_registry
from .personal_inventory import personal_inventory
from .repair_safety import _protected_target_reasons
from .terminal_mode_health import terminal_mode_health
from .workflows import validate_workflow_catalog
from .client_registry.historical import HISTORICAL_ADAPTERS


SUPPORTED_LEVELS = {"filesystem"}


def verify_install(
    repo_root: Path,
    home: Path,
    platform_ids: list[str] | None = None,
    *,
    target_root: Path | None = None,
    level: str = "filesystem",
) -> dict:
    if level not in SUPPORTED_LEVELS:
        raise ValueError(f"unsupported verify level: {level}")
    pack = load_pack_config(repo_root)
    attachment_root = target_root or repo_root
    lock_path = repo_path(attachment_root, pack.lockfile, "repo.lockfile")
    if lock_path.name != "lock.json" or lock_path.parent.name != ".localsetup":
        lock_path = target_lockfile_path(attachment_root)
    lock = load_json(lock_path)
    global_root = expand_user_path(pack.global_root, home)

    issues: list[str] = []
    if not lock:
        issues.append("missing lockfile")
    legacy_lock = legacy_target_lockfile_path(attachment_root)
    if legacy_lock.exists():
        issues.append(f"legacy root lockfile remains; migrate to .localsetup/lock.json: {legacy_lock}")
    target_framework = attachment_root / "ls"
    protected_reasons = _protected_target_reasons(repo_root, home, attachment_root)
    if target_framework.exists() and attachment_root.resolve(strict=False) != repo_root.resolve(strict=False) and not protected_reasons:
        issues.append(f"stale target framework source is not supported: {target_framework}")
    attach_mode = lock.get("attach_mode", "symlink") if isinstance(lock, dict) else "symlink"

    if not global_root.is_dir():
        issues.append(f"missing global skill library: {global_root}")

    aliases = lock.get("aliases", {}) if isinstance(lock, dict) else {}
    bindings = lock.get("package_bindings", {}) if isinstance(lock, dict) else {}
    from .package_bindings import package_source, verify_binding
    for name, binding in bindings.items():
        try:
            verify_binding(global_root, binding)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(f"external skill invalid: {name}: {exc}")
    for skill_name in sorted(set(aliases.values())):
        skill_path = global_root / skill_name
        if not skill_path.is_dir():
            issues.append(f"missing managed skill: {skill_path}")
        elif not is_managed_package(skill_path):
            issues.append(f"managed marker missing: {skill_path}")
        else:
            package_validation = validate_materialized_package(skill_path, repo_root=repo_root)
            if not package_validation["ok"]:
                issues.extend(f"managed skill package invalid: {skill_path}: {issue}" for issue in package_validation["issues"])

    workflows = lock.get("workflows", []) if isinstance(lock, dict) else []
    for workflow_name in sorted(set(workflows)):
        workflow_path = global_root / workflow_name
        if not workflow_path.is_dir():
            issues.append(f"missing managed workflow: {workflow_path}")
        elif not is_managed_package(workflow_path):
            issues.append(f"managed marker missing: {workflow_path}")
        else:
            package_validation = validate_materialized_package(workflow_path, repo_root=repo_root)
            if not package_validation["ok"]:
                issues.extend(f"managed workflow package invalid: {workflow_path}: {issue}" for issue in package_validation["issues"])

    scope = lock.get("skill_scope", "repo")
    adapters = [] if scope == "personal" else (
        recorded_adapter_status(lock, global_root, platform_ids, target_root=attachment_root)
        if lock or platform_ids is None
        else adapter_status(repo_root, home, global_root, platform_ids=platform_ids, target_root=attachment_root)
    )
    personal = personal_inventory(repo_root, home, platform_ids, expected=lock.get("personal_adapter_targets", [])) if scope in {"personal", "both"} else {"ok": True, "owners": [], "adapters": [], "issues": []}
    issues.extend(personal["issues"])
    expected_by_path = {
        str(item.get("path")): item.get("packages", lock.get("repo_packages", lock.get("adapter_packages", [])))
        for item in lock.get("adapter_targets", [])
        if isinstance(item, dict)
    }
    for adapter in adapters:
        adapter.setdefault(
            "expected_packages",
            expected_by_path.get(adapter["repo_path"], lock.get("repo_packages", lock.get("adapter_packages", []))),
        )
    from .models import PlanAction
    from .repository_overlap import expected_overlap
    for adapter in adapters:
        try:
            visible = expected_overlap(repo_root, home, attachment_root, PlanAction(
                "attach_repo_path", Path(adapter["repo_path"]), {
                    "mode": adapter.get("expected_mode", attach_mode), "global_root": str(global_root),
                    "packages": adapter["expected_packages"],
                    "package_bindings": adapter.get("package_bindings", {}),
                }))
            if visible is not None:
                adapter["requested_packages"] = adapter["expected_packages"]
                adapter["expected_packages"] = visible
        except (ValueError, OSError, TypeError, KeyError) as exc:
            issues.append(f"invalid shared adapter ownership: {exc}")
    platform_rules = {platform.platform_id: platform.verify_rules for platform in load_platforms(repo_root)}
    rule_results: list[dict] = []
    if scope in {"personal", "both"} and any(row["owner"]["client"] == "claude-code" for row in personal["owners"]):
        from .claude_prerequisite import claude_personal_root
        prerequisite = claude_personal_root(home)
        rule_results.append(prerequisite)
        if not prerequisite["ok"]:issues.append(prerequisite["reason"])
    if scope in {"personal", "both"} and any(row["owner"]["client"] == "gemini-cli" for row in personal["owners"]):
        from .gemini_prerequisite import gemini_personal_root
        prerequisite = gemini_personal_root(home)
        rule_results.append(prerequisite)
        if not prerequisite["ok"]:issues.append(prerequisite["reason"])
    if scope in {"personal", "both"} and any(row["owner"]["client"] == "openclaw" for row in personal["owners"]):
        from .openclaw_prerequisite import openclaw_personal_root
        prerequisite = openclaw_personal_root(home)
        rule_results.append(prerequisite)
        if not prerequisite["ok"]:issues.append(prerequisite["reason"])
    if scope in {"personal", "both"} and any(row["owner"]["client"] == "kimi-cli" for row in personal["owners"]):
        from .kimi_prerequisite import kimi_personal_root
        prerequisite = kimi_personal_root(home)
        rule_results.append(prerequisite)
        if not prerequisite["ok"]:issues.append(prerequisite["reason"])
    if "goose-cli" in lock.get("platforms", []) and (platform_ids is None or "goose-cli" in platform_ids):
        from .goose_prerequisite import goose_skills_configuration
        prerequisite = goose_skills_configuration(home)
        rule_results.append(prerequisite)
        if not prerequisite["ok"]:issues.append(prerequisite["reason"])
    for adapter in adapters:
        expected_mode = adapter.get("expected_mode", attach_mode)
        platform_evidence = {
            "platform": adapter.get("platform"),
            "platforms": list(adapter.get("platforms", [adapter.get("platform")] if adapter.get("platform") else [])),
        }
        rules = list(adapter.get("verify_rules", []))
        for platform_id in adapter.get("platforms", []):
            for rule in platform_rules.get(str(platform_id), []):
                if rule not in rules:
                    rules.append(rule)
        if not rules:
            rules = platform_rules.get(str(adapter.get("platform")), [])
        if "adapter_path_exists" in rules or rules:
            ok = bool(adapter["exists"])
            rule_results.append({"rule": "adapter_path_exists", **platform_evidence, "ok": ok, "path": adapter["repo_path"]})
            if not ok:
                issues.append(f"verify rule adapter_path_exists failed: {adapter['repo_path']}")
        if "adapter_points_to_managed_root" in rules or expected_mode != "portable":
            ok = (
                bool(
                    adapter["points_to_global"]
                    or adapter.get("is_scoped_symlink_adapter")
                    or adapter.get("is_repo_local_symlink_adapter")
                )
                if expected_mode != "portable"
                else True
            )
            rule_results.append({"rule": "adapter_points_to_managed_root", **platform_evidence, "ok": ok, "path": adapter["repo_path"]})
            if not ok:
                issues.append(f"verify rule adapter_points_to_managed_root failed: {adapter['repo_path']}")
        if "portable_marker_valid" in rules or expected_mode == "portable":
            ok = bool(adapter["is_portable_copy"]) if expected_mode == "portable" else True
            rule_results.append({"rule": "portable_marker_valid", **platform_evidence, "ok": ok, "path": adapter["repo_path"]})
            if not ok:
                issues.append(f"verify rule portable_marker_valid failed: {adapter['repo_path']}")
        if not adapter["exists"]:
            issues.append(f"missing adapter path: {adapter['repo_path']}")
        elif expected_mode == "portable" and not adapter["is_portable_copy"]:
            issues.append(f"adapter is not a managed portable copy: {adapter['repo_path']}")
        elif expected_mode != "portable" and not (
            adapter["points_to_global"] or adapter.get("is_scoped_symlink_adapter") or adapter.get("is_repo_local_symlink_adapter")
        ):
            issues.append(f"adapter does not point at global library: {adapter['repo_path']}")
        expected_packages = sorted(str(name) for name in adapter.get("expected_packages", []) if name)
        for name, binding in adapter.get("package_bindings", {}).items():
            try:
                if binding.get("disposition") == "external_present":
                    from .skills_external import validate_source_binding
                    validate_source_binding(binding)
                else:
                    verify_binding(global_root, binding, projection=Path(adapter["repo_path"]) / name)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append(f"external adapter skill invalid: {name}: {exc}")
        if expected_packages or "requested_packages" in adapter:
            visible_packages = sorted(str(name) for name in adapter.get("managed_visible_packages", adapter.get("visible_packages", [])))
            visible_packages = sorted(set(visible_packages) | {
                name for name, binding in adapter.get("package_bindings", {}).items()
                if binding.get("disposition") == "external_present"
            })
            ok = visible_packages == expected_packages
            rule_results.append(
                {
                    "rule": "adapter_visible_packages_match_selection",
                    **platform_evidence,
                    "ok": ok,
                    "visible_count": len(visible_packages),
                    "expected_count": len(expected_packages),
                }
            )
            if not ok:
                issues.append(f"adapter visible packages do not match selection: {adapter['repo_path']}")
        integrity_failures = adapter.get("package_integrity_failures", [])
        if integrity_failures:
            rule_results.append(
                {
                    "rule": "adapter_package_targets_match_managed_root",
                    **platform_evidence,
                    "ok": False,
                    "failure_count": len(integrity_failures),
                    "path": adapter["repo_path"],
                }
            )
            issues.append(f"adapter package target mismatch: {adapter['repo_path']}")
        elif adapter.get("is_scoped_symlink_adapter") or adapter.get("is_repo_local_symlink_adapter") or adapter.get("is_portable_copy"):
            rule_results.append(
                {
                    "rule": "adapter_package_targets_match_managed_root",
                    **platform_evidence,
                    "ok": True,
                    "path": adapter["repo_path"],
                }
            )
        if expected_mode == "portable" and adapter.get("is_portable_copy"):
            digest_mismatches: list[str] = []
            adapter_path = Path(str(adapter["repo_path"]))
            for package_name in expected_packages or adapter.get("managed_visible_packages", []):
                package = str(package_name)
                local_digest = package_digest(adapter_path / package)
                global_digest = package_digest(package_source(
                    global_root, package, bindings=adapter.get("package_bindings", {})))
                if local_digest and global_digest and local_digest != global_digest:
                    digest_mismatches.append(package)
            rule_results.append(
                {
                    "rule": "portable_package_digests_match_global",
                    **platform_evidence,
                    "ok": not digest_mismatches,
                    "mismatches": digest_mismatches,
                    "path": adapter["repo_path"],
                }
            )
            if digest_mismatches:
                issues.append(
                    f"portable adapter package digest differs from managed library: {adapter['repo_path']}"
                )
        if "namespace_ls" in rules:
            ok = all(Path(path).name.startswith("ls-") or Path(path).name in bindings
                     for path in [*lock.get("installed_skills", []), *lock.get("installed_workflows", [])])
            rule_results.append({"rule": "namespace_ls", **platform_evidence, "ok": ok})
            if not ok:
                issues.append(f"verify rule namespace_ls failed: {adapter.get('platform')}")
        if "skills_visible" in rules or "skills_visible_filesystem" in rules:
            visible = [path for path in lock.get("installed_skills", []) if (Path(path) / "SKILL.md").is_file()]
            ok = len(visible) == len(lock.get("installed_skills", []))
            rule_results.append({"rule": "skills_visible_filesystem", **platform_evidence, "ok": ok, "visible_count": len(visible)})
            if not ok:
                issues.append(f"verify rule skills_visible_filesystem failed: {adapter.get('platform')}")

    workflow_issues = validate_workflow_catalog(repo_root, validate_references=False) if lock.get("workflows") else []
    rule_results.append({"rule": "workflow_manifest_valid", "ok": not workflow_issues, "issue_count": len(workflow_issues)})
    if workflow_issues:
        issues.extend(f"workflow manifest validation failed: {issue}" for issue in workflow_issues)

    historical_transitions: dict[str, list[dict]] = {}
    requested_platforms = set(platform_ids) if platform_ids is not None else None
    installed_platforms = set(lock.get("platforms", [])) if scope != "personal" else set()
    for platform_id, transitions in HISTORICAL_ADAPTERS.items():
        rows: list[dict] = []
        for transition in transitions:
            historical_path = attachment_root / transition["path"]
            state = adapter_path_state(
                historical_path,
                global_root,
                known_global_roots=legacy_global_roots(home),
                target_root=attachment_root,
            )
            managed_exposure = bool(
                state["points_to_global"]
                or state["points_to_legacy_global"]
                or state.get("managed_visible_packages")
            )
            in_scope = requested_platforms is None or platform_id in requested_platforms
            current_owned = False
            if platform_id in installed_platforms and in_scope and managed_exposure:
                from .historical_ownership import retained_historical_action
                try:
                    retained = retained_historical_action(repo_root, home, attachment_root, historical_path, global_root)
                    if retained is not None:
                        current = personal_inventory(repo_root, home)
                        current_owned = (set(state.get('managed_visible_packages', [])) == set(retained[1])
                                         and any(row['path'] == str(historical_path) and row['ok'] for row in current['adapters']))
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    issues.append(f'Invalid current ownership at historical adapter: {exc}')
            if platform_id in installed_platforms and in_scope and managed_exposure and not current_owned:
                display = "Codex" if platform_id == "codex" else "OpenClaw" if platform_id == "openclaw" else platform_id
                issues.append(
                    f"legacy {display} adapter still exposes LocalSetup-managed entries: {historical_path}"
                )
            rows.append(
                {
                    "id": transition["id"],
                    "path": str(historical_path),
                    "managed_exposure": managed_exposure,
                    "current_owner_exposure": current_owned,
                    "custom_entries": state.get("custom_entries", []),
                    "recorded": lock.get("adapter_transitions", []),
                }
            )
        historical_transitions[platform_id] = rows
    legacy_codex_transition = historical_transitions["codex"][0]

    registry_path = Path(str(lock.get("registry_path"))) if isinstance(lock, dict) and lock.get("registry_path") else expand_user_path(pack.global_registry, home)
    if not registry_path.exists():
        issues.append(f"missing global registry: {registry_path}")
    registry = load_registry(registry_path) if registry_path.exists() else {}
    provenance = provenance_report(repo_root, lock=lock, registry=registry, global_root=global_root, adapters=adapters)
    tmux_terminal_mode = terminal_mode_health(
        repo_root,
        home=home,
        global_root=global_root,
        lock=lock,
        adapters=adapters,
        target_root=attachment_root,
    )

    from .opencode_preflight import opencode_verification_blockers
    issues.extend(row["reason"] for row in opencode_verification_blockers(
        repo_root, home, attachment_root, scope, platform_ids))
    from .kilo_loading import kilo_loading_assessment
    native_loading = kilo_loading_assessment(attachment_root, adapters, personal)
    from .openclaw_loading import openclaw_loading_assessment
    native_loading.extend(openclaw_loading_assessment(attachment_root, adapters, personal))
    native_warnings = [row["reason"] for row in native_loading
                       if row["status"] == "unsupported-project-source"]

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": [*tmux_terminal_mode["warnings"], *native_warnings],
        "native_loading": native_loading,
        "provenance": provenance,
        "provenance_warnings": provenance["warnings"],
        "provenance_repair_hints": provenance["repair_hints"],
        "tmux_terminal_mode": tmux_terminal_mode,
        "tmux_terminal_mode_warnings": tmux_terminal_mode["warnings"],
        "tmux_terminal_mode_repair_hints": tmux_terminal_mode["repair_hints"],
        "adapters": adapters,
        "personal": personal,
        "level": level,
        "rules": rule_results,
        "legacy_codex_transition": legacy_codex_transition,
        "historical_adapter_transitions": historical_transitions,
    }
