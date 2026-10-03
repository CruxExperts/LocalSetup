from __future__ import annotations

import os
import platform
from pathlib import Path
import sys

from .adapters import adapter_status, adapter_targets, recorded_adapter_status
from .dependencies import dependency_status, tool_status
from .inventory import install_inventory
from .manifests import load_pack_config, load_platforms
from .migration import detect_legacy_artifacts, scan_legacy_references
from .paths import expand_user_path
from .path_contract import paths_manifest_issues
from .provenance import provenance_report
from .lockfile import load_json
from .registry import load_registry
from .skills import validate_skill_catalog
from .terminal_mode_health import terminal_mode_health
from .workflows import validate_workflow_catalog


def _is_wsl() -> bool:
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "microsoft" in version or "wsl" in version


def _writable_status(path: Path) -> dict:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return {
        "path": str(path),
        "nearest_existing": str(probe),
        "ok": probe.exists() and os.access(probe, os.W_OK),
    }


def run_doctor(
    repo_root: Path,
    *,
    home: Path,
    packs: list[str] | None = None,
    platform_ids: list[str] | None = None,
    dependency_mode: str = "prompt-only",
    data_root: Path | None = None,
    target_root: Path | None = None,
    envman: bool = False,
) -> dict:
    blockers: list[str] = []
    warnings: list[str] = []
    attachment_root = target_root or repo_root
    dependency_root = data_root or (home / ".local" / "share" / "localsetup")

    environment = {
        "os": platform.system(),
        "platform": platform.platform(),
        "is_wsl": _is_wsl(),
        "python": sys.executable,
        "python_version": platform.python_version(),
        "home": str(home),
        "repo_root": str(repo_root),
        "target_root": str(attachment_root),
    }
    if platform.system().lower().startswith("windows"):
        blockers.append("native Windows is unsupported; run LocalSetup from WSL2")
    resolver_issues = paths_manifest_issues(repo_root, home)
    warnings.extend(f"resolver: {issue}" for issue in resolver_issues)

    try:
        pack = load_pack_config(repo_root)
        platforms = load_platforms(repo_root)
        manifest = {"ok": True, "pack": pack.pack_id, "platforms": [p.platform_id for p in platforms]}
    except Exception as exc:
        blockers.append(f"manifest validation failed: {exc}")
        payload = {
            "ok": False,
            "environment": environment,
            "manifest": {"ok": False, "error": str(exc)},
            "tools": [],
            "dependencies": {},
            "adapter_collisions": [],
            "legacy": {},
            "writable_paths": [],
            "blockers": blockers,
            "warnings": warnings,
        }
        if envman:
            payload["envman"] = _envman_status()
        return payload

    catalog_issues = validate_skill_catalog(repo_root, require_jsonschema=False)
    if catalog_issues:
        blockers.extend(f"skill catalog: {issue}" for issue in catalog_issues)
    workflow_issues = validate_workflow_catalog(repo_root, require_jsonschema=False)
    if workflow_issues:
        blockers.extend(f"workflow catalog: {issue}" for issue in workflow_issues)

    tools = [tool_status("git"), tool_status("rg")]
    if not tools[0]["ok"]:
        blockers.append("missing required tool: git")
    if not tools[1]["ok"]:
        warnings.append("missing recommended tool: rg")

    dep_status = dependency_status(repo_root, mode=dependency_mode, data_root=dependency_root, target_root=attachment_root).to_dict()
    if dep_status["warnings"]:
        warnings.extend(dep_status["warnings"])
    if dep_status["mode"] != "prompt-only" and dep_status["blockers"]:
        blockers.extend(dep_status["blockers"])

    global_root = expand_user_path(pack.global_root, home)
    lock_path = attachment_root / ".localsetup" / "lock.json"
    lock = load_json(lock_path)
    recorded_adapters = recorded_adapter_status(lock, global_root)
    recorded_paths = {adapter["repo_path"] for adapter in recorded_adapters}
    adapters = (
        adapter_status(repo_root, home, global_root, platform_ids=platform_ids, target_root=attachment_root)
        if platform_ids is not None
        else recorded_adapters
    )
    if target_root is not None and not adapters:
        warnings.append("target directory was provided but no platforms were selected; install will be global-only with no repo adapters")
    collisions: list[dict] = []
    for adapter in adapters:
        if adapter["repo_path"] in recorded_paths and not adapter["exists"]:
            blockers.append(f"recorded adapter is missing: {adapter['repo_path']}; run verify and review a repair plan")
        if adapter["collision_reason"]:
            collision = {
                "platform": adapter["platform"],
                "path": adapter["repo_path"],
                "reason": adapter["collision_reason"],
            }
            collisions.append(collision)
            blockers.append(f"adapter collision ({adapter['collision_reason']}): {adapter['repo_path']}")
        for failure in adapter.get("package_integrity_failures", []):
            subject = failure.get("package") or "adapter marker"
            blockers.append(
                "adapter package target mismatch "
                f"({subject}): {adapter['repo_path']}"
            )

    writable_paths = [_writable_status(global_root), _writable_status(home), _writable_status(attachment_root)]
    for target in adapter_targets(repo_root, home, platform_ids=platform_ids, target_root=attachment_root):
        writable_paths.append(_writable_status(target["repo_path"].parent))
    for item in writable_paths:
        if not item["ok"]:
            blockers.append(f"path is not writable: {item['path']}")

    legacy = {
        "artifacts": detect_legacy_artifacts(repo_root, home=home, platform_ids=platform_ids, target_root=attachment_root),
        "references": scan_legacy_references(repo_root),
    }
    if legacy["artifacts"]:
        warnings.append("legacy artifacts detected; run migrate for a conservative report and backup")
    registry_path = expand_user_path(pack.global_registry, home)
    provenance = provenance_report(
        repo_root,
        lock=lock,
        registry=load_registry(registry_path) if registry_path.exists() else {},
        global_root=global_root,
        adapters=adapters,
    )
    tmux_terminal_mode = terminal_mode_health(
        repo_root,
        home=home,
        global_root=global_root,
        lock=lock,
        adapters=adapters,
        target_root=attachment_root,
    )
    warnings.extend(tmux_terminal_mode["warnings"])

    payload = {
        "ok": not blockers,
        "environment": environment,
        "manifest": manifest,
        "tools": tools,
        "dependencies": dep_status,
        "adapter_collisions": collisions,
        "legacy": legacy,
        "inventory": install_inventory(repo_root, home=home, target_root=target_root, platform_ids=platform_ids),
        "resolver": {
            "ok": not resolver_issues,
            "issues": resolver_issues,
        },
        "provenance": provenance,
        "provenance_warnings": provenance["warnings"],
        "provenance_repair_hints": provenance["repair_hints"],
        "tmux_terminal_mode": tmux_terminal_mode,
        "tmux_terminal_mode_warnings": tmux_terminal_mode["warnings"],
        "tmux_terminal_mode_repair_hints": tmux_terminal_mode["repair_hints"],
        "writable_paths": writable_paths,
        "blockers": blockers,
        "warnings": warnings,
    }
    if envman:
        payload["envman"] = _envman_status()
    return payload


def _envman_status() -> dict:
    try:
        from .envman import probe_status

        return probe_status()
    except Exception:
        # The optional external probe must never change doctor health or expose diagnostics.
        return {"tool": "envman", "status": "present-unverified", "code": "probe-failed"}
