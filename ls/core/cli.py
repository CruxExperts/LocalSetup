from __future__ import annotations

import argparse
import os
import json
from importlib.resources import files
from pathlib import Path
import subprocess
import sys
import time

from .adapters import adapter_path_state, adapter_status, recorded_adapter_status
from .apply import apply_plan
from .config import DEPENDENCY_MODES, InstallConfig, config_to_dict, load_install_config, merge_cli_config
from .context import build_agent_context, render_markdown_report
from .conversion import convert_repo
from .dependencies import ensure_dependencies
from .diffing import diff_plan_current
from .doctor import run_doctor
from .docs import generate_alias_outputs
from .global_first_audit import audit_global_first
from .git_state import git_status_snapshot, status_delta
from .harness import disable as harness_disable
from .harness import enable as harness_enable
from .harness import budget as harness_budget
from .harness import init as harness_init
from .harness import payload_to_text as harness_payload_to_text
from .harness import plan as harness_plan
from .harness import run as harness_run
from .harness import status as harness_status
from .repo_finalizer import payload_to_text as repo_finalizer_payload_to_text
from .repo_finalizer import plan as repo_finalizer_plan
from .repo_finalizer import run as repo_finalizer_run
from .repo_finalizer import status as repo_finalizer_status
from .hooks import run_maintainer_gate
from .inventory import install_inventory
from .manifests import load_pack_config
from .manifests import load_platforms
from .manifests import validate_manifest_schemas
from .lockfile import load_json, save_json, save_text
from .handoff import agent_prompt_payload
from .health import read_health_status, repair_queue, write_health_event, write_repair_queue_prompts
from .locking import PackageRootLockTimeout
from .migration import conservative_migrate, scan_legacy_references
from .package import build_public_artifact, verify_release_artifact, write_installed_sbom, write_source_sbom
from .package_surface import validate_package_surfaces
from .path_contract import (
    build_paths_manifest,
    resolve_doc_path,
    resolve_named_path,
    resolve_package_path,
    resolve_tool_path,
    write_paths_manifest,
)
from .path_reprocessor import reprocess_localsetup_paths
from .paths import expand_user_path
from .plugin_packs import build_codex_plugins, load_plugin_pack_configs, plan_plugin_packs, validate_codex_plugin_path, validate_plugin_pack_manifest
from .plan import build_install_plan
from .provenance import provenance_report
from .query import adopt_recommendations, graph_payload, pack_reasoning, skill_payload, workflow_payload
from .repair import run_repair
from .registry import load_registry
from .repo_profiles import REPO_PROFILES, render_repo_profile
from .rollback import rollback
from .selection import PRESETS
from .shell import (
    SHIM_ENV,
    SHIM_HOME_ENV,
    SHIM_SOURCE_ROOT_ENV,
    detect_invocation_target,
    register_shell_command,
    shell_registration_status,
)
from .skills import candidate_skill_path_blockers, candidate_skill_proposal, candidate_skill_proposal_markdown, load_skill_catalog, validate_candidate_skill, validate_skill_catalog
from .skills import parse_skill_frontmatter
from .test_workers import test_workers_payload
from .workflows import load_workflow_catalog, validate_workflow_catalog
from .verify import verify_install
from .trace import write_trace
from .wizard import run_wizard
from .versioning import (
    VERSION_SYNC_PREFIX,
    check_version_files,
    commit_version_sync,
    plan_version,
    print_json,
    publish_preflight,
    push_lines_to_plans,
    stage_version_files,
    sync_version_files,
)
from .cli_parser import build_parser
from . import cli_client_state_commands, cli_install_commands, cli_misc_commands, cli_state_commands
from . import cli_install_support
from .domain_shapes import cli as cli_domain_shapes


def _repo_root() -> Path:
    return Path(str(files("ls"))).resolve().parent


def ls_home(home: Path) -> Path:
    return home / ".local" / "share" / "localsetup"


def _config_data_root(config: InstallConfig, home: Path) -> Path:
    if config.data_root:
        return Path(config.data_root).expanduser().resolve()
    return ls_home(home)


def _split_csv(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    expanded: list[str] = []
    for value in values:
        parts = [part.strip() for part in value.split(",")]
        if any(not part for part in parts):
            raise ValueError(f"empty value in comma-separated list: {value!r}")
        expanded.extend(parts)
    if not expanded:
        raise ValueError("at least one value is required")
    return expanded


def _add_config_flags(parser: argparse.ArgumentParser, *, include_apply: bool = False) -> None:
    parser.add_argument("--config")
    parser.add_argument("--target-directory", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=None, help="Emit JSON output explicitly; JSON is the default for non-Markdown commands.")
    parser.add_argument("--report")
    parser.add_argument("--backup-dir")
    parser.add_argument("--trace-json")
    parser.add_argument("--policy-mode", choices=["permissive", "standard", "strict", "ci"], default="standard")
    parser.add_argument("--dependency-mode", choices=sorted(DEPENDENCY_MODES))
    if include_apply:
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--yes", action="store_true", dest="apply", help=argparse.SUPPRESS)


def _add_selector_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", choices=sorted(PRESETS))
    parser.add_argument("--packs", nargs="*")
    parser.add_argument("--skills", nargs="*")
    parser.add_argument("--skill-classes", nargs="*", dest="skill_classes")
    parser.add_argument("--skill-tags", nargs="*", dest="skill_tags")
    parser.add_argument("--exclude-skills", nargs="*", dest="exclude_skills")
    parser.add_argument("--workflows", nargs="*")
    parser.add_argument("--global-preset", choices=sorted(PRESETS), dest="global_preset")
    parser.add_argument("--global-packs", nargs="*", dest="global_packs")
    parser.add_argument("--global-skills", nargs="*", dest="global_skills")
    parser.add_argument("--global-skill-classes", nargs="*", dest="global_skill_classes")
    parser.add_argument("--global-skill-tags", nargs="*", dest="global_skill_tags")
    parser.add_argument("--global-exclude-skills", nargs="*", dest="global_exclude_skills")
    parser.add_argument("--global-workflows", nargs="*", dest="global_workflows")
    parser.add_argument("--repo-preset", choices=sorted(PRESETS), dest="repo_preset")
    parser.add_argument("--repo-packs", nargs="*", dest="repo_packs")
    parser.add_argument("--repo-skills", nargs="*", dest="repo_skills")
    parser.add_argument("--repo-skill-classes", nargs="*", dest="repo_skill_classes")
    parser.add_argument("--repo-skill-tags", nargs="*", dest="repo_skill_tags")
    parser.add_argument("--repo-exclude-skills", nargs="*", dest="repo_exclude_skills")
    parser.add_argument("--repo-workflows", nargs="*", dest="repo_workflows")
    parser.add_argument("--mode", choices=["symlink", "portable"])
    parser.add_argument("--platforms", "--tools", nargs="*", dest="platforms")


def _add_visual_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--no-color", action="store_const", const="never", dest="color")
    parser.add_argument("--glyphs", choices=["auto", "ascii", "unicode"], default="auto")


def _is_global_shim_invocation() -> bool:
    return os.environ.get(SHIM_ENV) == "1"


def _has_target_directory(args: argparse.Namespace) -> bool:
    return hasattr(args, "target_directory")


def _target_directory_value(args: argparse.Namespace) -> str | None:
    return getattr(args, "target_directory", None) if _has_target_directory(args) else None


def _inject_global_target(args: argparse.Namespace) -> None:
    from .installed_source import wheel_module
    shim = _is_global_shim_invocation()
    if not shim and not wheel_module(Path(__file__)):
        return
    if args.cmd not in {"plan", "install", "update", "verify", "rollback", "adapters", "doctor", "migrate", "context", "convert", "harness", "context-index", "provenance", "health"}:
        return
    if _target_directory_value(args):
        return
    if not shim and getattr(args, "config", None):
        configured = load_install_config(Path(args.config).resolve())
        if configured.target_directory:
            return
    setattr(args, "target_directory", str(detect_invocation_target()))
    setattr(args, "detected_target_directory", True)


def _resolved_config(args: argparse.Namespace, default_home: Path) -> InstallConfig:
    base = load_install_config(Path(args.config).resolve() if getattr(args, "config", None) else None)
    cli_home = str(default_home)
    if getattr(args, "home_override", None):
        cli_home = args.home_override
    elif getattr(args, "home", None) is None and base.home:
        cli_home = base.home
    return merge_cli_config(
        base,
        platforms=_split_csv(getattr(args, "platforms", None)) if hasattr(args, "platforms") else None,
        packs=_split_csv(getattr(args, "packs", None)) if hasattr(args, "packs") else None,
        preset=getattr(args, "preset", None),
        skills=_split_csv(getattr(args, "skills", None)) if hasattr(args, "skills") else None,
        skill_classes=_split_csv(getattr(args, "skill_classes", None)) if hasattr(args, "skill_classes") else None,
        skill_tags=_split_csv(getattr(args, "skill_tags", None)) if hasattr(args, "skill_tags") else None,
        exclude_skills=_split_csv(getattr(args, "exclude_skills", None)) if hasattr(args, "exclude_skills") else None,
        workflows=_split_csv(getattr(args, "workflows", None)) if hasattr(args, "workflows") else None,
        global_packs=_split_csv(getattr(args, "global_packs", None)) if hasattr(args, "global_packs") else None,
        global_preset=getattr(args, "global_preset", None),
        global_skills=_split_csv(getattr(args, "global_skills", None)) if hasattr(args, "global_skills") else None,
        global_skill_classes=_split_csv(getattr(args, "global_skill_classes", None)) if hasattr(args, "global_skill_classes") else None,
        global_skill_tags=_split_csv(getattr(args, "global_skill_tags", None)) if hasattr(args, "global_skill_tags") else None,
        global_exclude_skills=_split_csv(getattr(args, "global_exclude_skills", None)) if hasattr(args, "global_exclude_skills") else None,
        global_workflows=_split_csv(getattr(args, "global_workflows", None)) if hasattr(args, "global_workflows") else None,
        repo_packs=_split_csv(getattr(args, "repo_packs", None)) if hasattr(args, "repo_packs") else None,
        repo_preset=getattr(args, "repo_preset", None),
        repo_skills=_split_csv(getattr(args, "repo_skills", None)) if hasattr(args, "repo_skills") else None,
        repo_skill_classes=_split_csv(getattr(args, "repo_skill_classes", None)) if hasattr(args, "repo_skill_classes") else None,
        repo_skill_tags=_split_csv(getattr(args, "repo_skill_tags", None)) if hasattr(args, "repo_skill_tags") else None,
        repo_exclude_skills=_split_csv(getattr(args, "repo_exclude_skills", None)) if hasattr(args, "repo_exclude_skills") else None,
        repo_workflows=_split_csv(getattr(args, "repo_workflows", None)) if hasattr(args, "repo_workflows") else None,
        skill_scope=getattr(args, "skill_scope", None),
        attach_mode=getattr(args, "mode", None),
        home=cli_home,
        target_directory=getattr(args, "target_directory", None),
        dependency_mode=getattr(args, "dependency_mode", None),
        backup_dir=getattr(args, "backup_dir", None),
        report=getattr(args, "report", None),
        json_output=getattr(args, "json", None),
        markdown=getattr(args, "markdown", None),
    )


def _write_report(path: str | None, payload: dict, *, markdown: bool = False) -> None:
    if not path:
        return
    report_path = Path(path).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if markdown:
        save_text(report_path, render_markdown_report(payload))
    else:
        save_json(report_path, payload)


def _print_payload(payload: dict, *, markdown: bool = False) -> None:
    if markdown:
        print(render_markdown_report(payload), end="")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _print_no_command_help() -> None:
    print("localsetup: command required", file=sys.stderr)
    print("", file=sys.stderr)
    print("Examples:", file=sys.stderr)
    print("  localsetup doctor", file=sys.stderr)
    print("  localsetup verify --level filesystem", file=sys.stderr)
    print("  localsetup self-refresh --dependency-mode uv-sync", file=sys.stderr)
    print("  localsetup --help", file=sys.stderr)


def _health_status_from_payload(payload: dict) -> str:
    if payload.get("ok") is False or payload.get("blockers") or payload.get("decisions"):
        return "blocked"
    return "ok"


def _record_health_for_payload(
    *,
    root: Path,
    home: Path,
    target_root: Path,
    operation: str,
    mode: str,
    payload: dict,
    git_pre: dict | None = None,
    git_post: dict | None = None,
    planned_paths: list[str] | None = None,
) -> dict:
    blockers = [str(item) for item in payload.get("blockers", [])]
    warnings = [str(item) for item in payload.get("warnings", [])]
    delta = status_delta(git_pre, git_post, planned_paths or []) if git_pre and git_post else None
    install_payload = payload.get("install") if isinstance(payload.get("install"), dict) else {}
    event = write_health_event(
        repo_root=root,
        home=home,
        target_root=target_root,
        operation=operation,
        mode=mode,
        status=_health_status_from_payload(payload),
        payload=payload,
        blockers=blockers,
        warnings=warnings,
        decisions=payload.get("decisions", []),
        backups=payload.get("backups", []),
        journal_path=payload.get("journal") or install_payload.get("journal"),
        report_path=payload.get("report"),
        git_pre=git_pre,
        git_post=git_post,
        localsetup_created_delta=delta,
    )
    summary = {
        "event_id": event["event_id"],
        "status": event["status"],
        "latest": str(home / ".local" / "share" / "localsetup" / "state" / "health" / "latest.json"),
        "repo_summary": str(target_root / ".localsetup" / "health.json"),
        "agent_status": str(target_root / ".localsetup" / "AGENT_STATUS.md"),
        "next_actions": event.get("next_actions", []),
    }
    payload["health"] = summary
    return summary


_SUPPORT_ORIGINALS = {name: getattr(cli_install_support, name) for name in (
    "_all_configured_packs",
    "_policy_findings",
    "_existing_target_platforms",
    "_selector_free",
    "_repair_detected_existing_state",
    "_global_selector_kwargs_from_lock",
    "_build_auto_inferred_plan",
    "_build_auto_new_repo_plan",
    "_auto_plan_payload",
    "_apply_install_plan",
    "_auto_default_context",
    "_run_self_refresh",
)}
_FACADE_SUPPORT_WRAPPERS: dict[str, object] = {}

def _sync_install_support() -> None:
    cli_install_support.sync_from_facade(sys.modules[__name__])
    for name, original in _SUPPORT_ORIGINALS.items():
        facade_value = globals()[name]
        cli_install_support.__dict__[name] = facade_value if facade_value is not _FACADE_SUPPORT_WRAPPERS[name] else original

def _all_configured_packs(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._all_configured_packs(*args, **kwargs)

def _policy_findings(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._policy_findings(*args, **kwargs)

def _existing_target_platforms(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._existing_target_platforms(*args, **kwargs)

def _selector_free(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._selector_free(*args, **kwargs)

def _repair_detected_existing_state(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._repair_detected_existing_state(*args, **kwargs)

def _global_selector_kwargs_from_lock(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._global_selector_kwargs_from_lock(*args, **kwargs)

def _build_auto_inferred_plan(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._build_auto_inferred_plan(*args, **kwargs)

def _build_auto_new_repo_plan(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._build_auto_new_repo_plan(*args, **kwargs)

def _auto_plan_payload(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._auto_plan_payload(*args, **kwargs)

def _apply_install_plan(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._apply_install_plan(*args, **kwargs)

def _auto_default_context(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._auto_default_context(*args, **kwargs)

def _run_self_refresh(*args, **kwargs):
    _sync_install_support()
    return cli_install_support._run_self_refresh(*args, **kwargs)

_FACADE_SUPPORT_WRAPPERS.update({
    "_all_configured_packs": _all_configured_packs,
    "_policy_findings": _policy_findings,
    "_existing_target_platforms": _existing_target_platforms,
    "_selector_free": _selector_free,
    "_repair_detected_existing_state": _repair_detected_existing_state,
    "_global_selector_kwargs_from_lock": _global_selector_kwargs_from_lock,
    "_build_auto_inferred_plan": _build_auto_inferred_plan,
    "_build_auto_new_repo_plan": _build_auto_new_repo_plan,
    "_auto_plan_payload": _auto_plan_payload,
    "_apply_install_plan": _apply_install_plan,
    "_auto_default_context": _auto_default_context,
    "_run_self_refresh": _run_self_refresh,
})


def _add_harness_target_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-directory", default=argparse.SUPPRESS)


def _harness_target(args: argparse.Namespace) -> Path | None:
    value = getattr(args, "target_directory", None)
    return Path(value).expanduser().resolve() if value else None




def _main(argv: list[str] | None = None) -> int:
    parser = build_parser(_add_config_flags, _add_selector_flags, _add_visual_flags, _add_harness_target_flags)
    args = parser.parse_args(argv)
    if args.cmd is None:
        _print_no_command_help()
        return 2
    if args.cmd == "agent":
        parser.error("place agent immediately after localsetup; use agent options for workspace, state and runtime selection")
    _inject_global_target(args)
    global_shim = _is_global_shim_invocation()
    shim_source_root = os.environ.get(SHIM_SOURCE_ROOT_ENV) if global_shim else None
    shim_home = os.environ.get(SHIM_HOME_ENV) if global_shim else None
    root = Path(args.source_root or args.repo or shim_source_root or str(_repo_root())).resolve()
    home = Path(args.home or shim_home or Path.home()).expanduser().resolve()
    client_state_result = cli_client_state_commands.handle(args, root, home)
    if client_state_result is not None:
        return client_state_result
    facade = sys.modules[__name__]
    for handler in (cli_install_commands, cli_state_commands, cli_misc_commands, cli_domain_shapes):
        result = handler.handle(facade, args, root, home)
        if result is not None:
            return result
    return 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["llm"]:
        from .agent.completion_cli import main as completion_main
        return completion_main(arguments[1:])
    if arguments[:1] == ["agent"]:
        from .agent.cli import main as agent_main
        return agent_main(arguments[1:])
    try:
        return _main(arguments)
    except Exception as exc:
        print(f"localsetup: {exc}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
