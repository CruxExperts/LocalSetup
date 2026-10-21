from __future__ import annotations

from pathlib import Path
import re

from .manifests import load_pack_config
from .paths import expand_user_path, target_lockfile_path


RETIRED_POWERSHELL_SURFACES = [
    "install.ps1",
    "ls/discovery/core/os_detector.ps1",
    "ls/lib/data_paths.ps1",
    "ls/tests/automated_test.ps1",
    "ls/tools/skill_importer_scan.ps1",
    "ls/tools/verify_context.ps1",
    "ls/tools/verify_rules.ps1",
]

SHELL_WRAPPER_SURFACES = [
    "install",
    "ls/tests/automated_test.sh",
    "ls/tools/skill_importer_scan",
    "ls/tools/verify_context",
    "ls/tools/verify_rules",
    "ls/tools/tmux_ops",
    "ls/tools/tmux_terminal_mode",
]

LEGACY_DEPLOY_SURFACES = [
    "ls/tools/deploy",
    "ls/tools/deploy.sh",
    "ls/tools/deploy.ps1",
]

DOC_CLAIM_PATTERNS = {
    "root_lockfile": re.compile(r"localsetup\.lock\.json"),
    "target_local_framework_command": re.compile(
        r"python3\s+ls/tools/localsetup\.py\s+(install --apply|verify|rollback|doctor|plan)\b"
    ),
    "target_local_tool_command": re.compile(r"\./ls/(tools|tests)/"),
    "target_venv": re.compile(r"\.localsetup/venv"),
    "retired_powershell_surface": re.compile(
        r"(install|verify_context|verify_rules|skill_importer_scan|automated_test|data_paths|os_detector)\.ps1"
    ),
    "framework_sync_claim": re.compile(r"syncs current framework", re.IGNORECASE),
}

DOC_SCAN_ROOTS = [
    "README.md",
    "ls/docs/HARNESS_AUTOMATION.md",
    "ls/docs/MULTI_PLATFORM_INSTALL.md",
    "ls/docs/PLATFORM_REGISTRY.md",
    "ls/docs/QUICKSTART.md",
    "ls/docs/REPO_AND_DATA_SEPARATION.md",
    "ls/docs/REPO_CONVERSION.md",
    "ls/skills/ls-localsetup-project-context",
    "ls/skills/ls-context-index-search",
    "ls/templates",
]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _scan_doc_claims(source_root: Path) -> list[dict]:
    findings: list[dict] = []
    files: list[Path] = []
    for rel in DOC_SCAN_ROOTS:
        path = source_root / rel
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file() and p.suffix in {".md", ".yaml", ".yml", ".json"})
    for path in sorted(files):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(lines, start=1):
            for claim, pattern in DOC_CLAIM_PATTERNS.items():
                if not pattern.search(line):
                    continue
                allowed_source_contributor_note = (
                    claim == "target_local_framework_command"
                    and "source-checkout" in line
                    and "--target-directory" in line
                )
                allowed_source_check = "--source-root ." in line
                allowed_source_test = claim == "target_local_tool_command" and (
                    "Tests" in line or "source checks" in line or "source checkout" in line
                )
                allowed_legacy_target_venv_note = claim == "target_venv" and (
                    "legacy target" in line.lower()
                    or "target-local" in line.lower()
                    or "pre-uv installs" in line.lower()
                    or "target project's own `.venv`" in line
                )
                if (
                    allowed_source_contributor_note
                    or allowed_source_check
                    or allowed_source_test
                    or allowed_legacy_target_venv_note
                ):
                    continue
                findings.append(
                    {
                        "kind": "docs_claim",
                        "claim": claim,
                        "path": _relative(path, source_root),
                        "line": line_no,
                    }
                )
    return findings


def audit_global_first(source_root: Path, *, home: Path, target_root: Path | None = None) -> dict:
    source = source_root.resolve(strict=False)
    target = (target_root or source).resolve(strict=False)
    pack = load_pack_config(source)
    localsetup_home = expand_user_path(pack.global_home, home)
    package_root = expand_user_path(pack.package_root, home)
    registry_path = expand_user_path(pack.registry_path, home)
    old_package_root = home / ".local" / "share" / "agents" / "skills" / "localsetup"

    observations: list[dict] = []
    blockers: list[dict] = []
    warnings: list[dict] = []

    target_framework = target / "ls"
    if target != source and target_framework.exists():
        blockers.append({"kind": "stale_framework_source", "path": str(target_framework)})
    observations.append(
        {
            "kind": "target_framework",
            "path": str(target_framework),
            "exists": target_framework.exists(),
            "allowed": target == source,
        }
    )

    legacy_lock = target / "localsetup.lock.json"
    lock = target_lockfile_path(target)
    if legacy_lock.exists() and legacy_lock != lock:
        blockers.append({"kind": "legacy_root_lockfile", "path": str(legacy_lock)})
    observations.append({"kind": "target_lockfile", "path": str(lock), "exists": lock.exists()})
    observations.append({"kind": "legacy_root_lockfile", "path": str(legacy_lock), "exists": legacy_lock.exists()})

    observations.append(
        {
            "kind": "global_layout",
            "localsetup_home": str(localsetup_home),
            "package_root": str(package_root),
            "registry_path": str(registry_path),
            "legacy_package_root": str(old_package_root),
            "legacy_package_root_exists": old_package_root.exists(),
        }
    )
    if old_package_root.exists() and old_package_root.resolve(strict=False) != package_root.resolve(strict=False):
        warnings.append({"kind": "legacy_package_root", "path": str(old_package_root)})

    active_ps1 = [rel for rel in RETIRED_POWERSHELL_SURFACES if (source / rel).exists()]
    if active_ps1:
        blockers.extend({"kind": "retired_powershell_surface", "path": rel} for rel in active_ps1)
    observations.append({"kind": "retired_powershell_surfaces", "present": active_ps1})
    observations.append({"kind": "shell_wrappers", "present": [rel for rel in SHELL_WRAPPER_SURFACES if (source / rel).exists()]})

    legacy_deploy = [rel for rel in LEGACY_DEPLOY_SURFACES if (source / rel).exists()]
    legacy_package_dirs = []
    if package_root.exists():
        legacy_package_dirs = sorted(path.name for path in package_root.glob("localsetup-*"))
    if legacy_deploy:
        blockers.extend({"kind": "legacy_deploy_surface", "path": rel} for rel in legacy_deploy)
    observations.append({"kind": "legacy_deploy_surfaces", "present": legacy_deploy})
    observations.append({"kind": "legacy_package_dirs", "present": legacy_package_dirs})

    docs_claims = _scan_doc_claims(source)
    if docs_claims:
        blockers.extend(docs_claims)

    return {
        "ok": not blockers,
        "source_root": str(source),
        "target_root": str(target),
        "localsetup_home": str(localsetup_home),
        "package_root": str(package_root),
        "registry_path": str(registry_path),
        "blockers": blockers,
        "warnings": warnings,
        "observations": observations,
    }
