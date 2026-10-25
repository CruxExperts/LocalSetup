from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
import importlib.util

from .dependencies import ACCEPTED_DEPENDENCY_MODES, normalize_dependency_mode
from .paths import PathValidationError


DEPENDENCY_MODES = ACCEPTED_DEPENDENCY_MODES
MIGRATION_MODES = {"conservative-auto", "report-only"}
ATTACH_MODES = {"symlink", "portable"}
BACKUP_POLICIES = {"timestamped"}


@dataclass(frozen=True)
class OutputPreferences:
    json: bool = True
    report: str | None = None
    markdown: bool = False


@dataclass(frozen=True)
class FeedbackPreferences:
    repository: str | None = None
    labels: list[str] = field(default_factory=list)
    capture_enabled: bool = True
    auto_draft: bool = True


@dataclass(frozen=True)
class InstallConfig:
    platforms: list[str] | None = None
    packs: list[str] | None = None
    preset: str | None = None
    skills: list[str] | None = None
    skill_classes: list[str] | None = None
    skill_tags: list[str] | None = None
    exclude_skills: list[str] | None = None
    workflows: list[str] | None = None
    global_packs: list[str] | None = None
    global_preset: str | None = None
    global_skills: list[str] | None = None
    global_skill_classes: list[str] | None = None
    global_skill_tags: list[str] | None = None
    global_exclude_skills: list[str] | None = None
    global_workflows: list[str] | None = None
    repo_packs: list[str] | None = None
    repo_preset: str | None = None
    repo_skills: list[str] | None = None
    repo_skill_classes: list[str] | None = None
    repo_skill_tags: list[str] | None = None
    repo_exclude_skills: list[str] | None = None
    repo_workflows: list[str] | None = None
    skill_scope: str | None = None
    attach_mode: str = "symlink"
    home: str | None = None
    target_directory: str | None = None
    data_root: str | None = None
    dependency_mode: str = "prompt-only"
    migration_mode: str = "conservative-auto"
    backup_dir: str | None = None
    backup_policy: str = "timestamped"
    output: OutputPreferences = field(default_factory=OutputPreferences)
    feedback: FeedbackPreferences = field(default_factory=FeedbackPreferences)


def _as_list(value: Any, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    return value


def _as_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def load_install_config(path: Path | None) -> InstallConfig:
    if path is None:
        return InstallConfig()
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid install config JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("install config must be a JSON object")
    _validate_against_schema(path, data)
    return parse_install_config(data)


def parse_install_config(data: dict[str, Any]) -> InstallConfig:
    """Construct installation preferences without resolving files or authority."""
    _validate_against_schema(Path(__file__), data)

    output_data = data.get("output", {})
    if output_data is None:
        output_data = {}
    if not isinstance(output_data, dict):
        raise ValueError("output must be a JSON object")
    feedback_data = data.get("feedback", {})
    if feedback_data is None:
        feedback_data = {}
    if not isinstance(feedback_data, dict):
        raise ValueError("feedback must be a JSON object")
    if set(feedback_data) - {"repository", "labels", "capture_enabled", "auto_draft"}:
        raise ValueError("feedback contains unsupported fields; credentials belong in the process environment")

    config = InstallConfig(
        platforms=_as_list(data.get("platforms"), "platforms"),
        packs=_as_list(data.get("packs"), "packs"),
        preset=_as_str(data.get("preset"), "preset"),
        skills=_as_list(data.get("skills"), "skills"),
        skill_classes=_as_list(data.get("skill_classes"), "skill_classes"),
        skill_tags=_as_list(data.get("skill_tags"), "skill_tags"),
        exclude_skills=_as_list(data.get("exclude_skills"), "exclude_skills"),
        workflows=_as_list(data.get("workflows"), "workflows"),
        global_packs=_as_list(data.get("global_packs"), "global_packs"),
        global_preset=_as_str(data.get("global_preset"), "global_preset"),
        global_skills=_as_list(data.get("global_skills"), "global_skills"),
        global_skill_classes=_as_list(data.get("global_skill_classes"), "global_skill_classes"),
        global_skill_tags=_as_list(data.get("global_skill_tags"), "global_skill_tags"),
        global_exclude_skills=_as_list(data.get("global_exclude_skills"), "global_exclude_skills"),
        global_workflows=_as_list(data.get("global_workflows"), "global_workflows"),
        repo_packs=_as_list(data.get("repo_packs"), "repo_packs"),
        repo_preset=_as_str(data.get("repo_preset"), "repo_preset"),
        repo_skills=_as_list(data.get("repo_skills"), "repo_skills"),
        repo_skill_classes=_as_list(data.get("repo_skill_classes"), "repo_skill_classes"),
        repo_skill_tags=_as_list(data.get("repo_skill_tags"), "repo_skill_tags"),
        repo_exclude_skills=_as_list(data.get("repo_exclude_skills"), "repo_exclude_skills"),
        repo_workflows=_as_list(data.get("repo_workflows"), "repo_workflows"),
        skill_scope=_as_str(data.get("skill_scope"), "skill_scope"),
        attach_mode=str(data.get("attach_mode", "symlink")),
        home=_as_str(data.get("home"), "home"),
        target_directory=_as_str(data.get("target_directory"), "target_directory"),
        data_root=_as_str(data.get("data_root"), "data_root"),
        dependency_mode=normalize_dependency_mode(str(data.get("dependency_mode", "prompt-only"))),
        migration_mode=str(data.get("migration_mode", "conservative-auto")),
        backup_dir=_as_str(data.get("backup_dir"), "backup_dir"),
        backup_policy=str(data.get("backup_policy", "timestamped")),
        output=OutputPreferences(
            json=bool(output_data.get("json", True)),
            report=_as_str(output_data.get("report"), "output.report"),
            markdown=bool(output_data.get("markdown", False)),
        ),
        feedback=FeedbackPreferences(
            repository=_as_str(feedback_data.get("repository"), "feedback.repository"),
            labels=_as_list(feedback_data.get("labels", []), "feedback.labels") or [],
            capture_enabled=feedback_data.get("capture_enabled", True),
            auto_draft=feedback_data.get("auto_draft", True),
        ),
    )
    validate_install_config(config)
    return config


def resolve_install_preferences(project: Path, home: Path, explicit: Path | None = None) -> InstallConfig:
    """Shared installation and privacy preferences; invalid input fails closed."""
    from .framework_configuration.service import merge, section
    common = section("installation", project, home) or {}
    if explicit is not None:
        load_install_config(explicit)
        common = merge(common, json.loads(explicit.read_text(encoding="utf-8")))
    return parse_install_config(common)


def _validate_against_schema(config_path: Path, data: dict[str, Any]) -> None:
    if importlib.util.find_spec("jsonschema") is None:
        return
    from jsonschema import Draft202012Validator

    schema_path = config_path.parents[1] / "ls" / "config" / "install.schema.json"
    if not schema_path.exists():
        schema_path = Path(__file__).resolve().parents[1] / "config" / "install.schema.json"
    if not schema_path.exists():
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        dotted = ".".join(str(part) for part in first.path) or "<root>"
        raise ValueError(f"install config schema validation failed at {dotted}: {first.message}")


def validate_install_config(config: InstallConfig) -> None:
    if config.skill_scope is not None and (
        not isinstance(config.skill_scope, str) or config.skill_scope not in {"repo", "personal", "both"}
    ):
        raise ValueError(f"unsupported skill scope: {config.skill_scope}")
    if config.attach_mode not in ATTACH_MODES:
        raise ValueError(f"unsupported attach mode: {config.attach_mode}")
    if config.dependency_mode not in DEPENDENCY_MODES:
        raise ValueError(f"unsupported dependency mode: {config.dependency_mode}")
    if config.migration_mode not in MIGRATION_MODES:
        raise ValueError(f"unsupported migration mode: {config.migration_mode}")
    if config.backup_policy not in BACKUP_POLICIES:
        raise ValueError(f"unsupported backup policy: {config.backup_policy}")
    if type(config.feedback.capture_enabled) is not bool or type(config.feedback.auto_draft) is not bool:
        raise ValueError("feedback capture_enabled and auto_draft must be booleans")
    if config.feedback.repository is not None:
        from .github_repo.policy import parse_target
        parse_target("github.com", config.feedback.repository)
    if any(not isinstance(label, str) or not label or len(label) > 50 for label in config.feedback.labels):
        raise ValueError("feedback labels must be non-empty strings of at most 50 characters")
    if len(config.feedback.labels) != len(set(config.feedback.labels)):
        raise ValueError("feedback labels must be unique")
    for field_name, path_value in {
        "home": config.home,
        "target_directory": config.target_directory,
        "data_root": config.data_root,
        "backup_dir": config.backup_dir,
        "output.report": config.output.report,
    }.items():
        if path_value and "\x00" in path_value:
            raise PathValidationError(f"{field_name} contains a NUL byte")


def merge_cli_config(
    base: InstallConfig,
    *,
    platforms: list[str] | None = None,
    packs: list[str] | None = None,
    preset: str | None = None,
    skills: list[str] | None = None,
    skill_classes: list[str] | None = None,
    skill_tags: list[str] | None = None,
    exclude_skills: list[str] | None = None,
    workflows: list[str] | None = None,
    global_packs: list[str] | None = None,
    global_preset: str | None = None,
    global_skills: list[str] | None = None,
    global_skill_classes: list[str] | None = None,
    global_skill_tags: list[str] | None = None,
    global_exclude_skills: list[str] | None = None,
    global_workflows: list[str] | None = None,
    repo_packs: list[str] | None = None,
    repo_preset: str | None = None,
    repo_skills: list[str] | None = None,
    repo_skill_classes: list[str] | None = None,
    repo_skill_tags: list[str] | None = None,
    repo_exclude_skills: list[str] | None = None,
    repo_workflows: list[str] | None = None,
    skill_scope: str | None = None,
    attach_mode: str | None = None,
    home: str | None = None,
    target_directory: str | None = None,
    dependency_mode: str | None = None,
    migration_mode: str | None = None,
    backup_dir: str | None = None,
    report: str | None = None,
    json_output: bool | None = None,
    markdown: bool | None = None,
) -> InstallConfig:
    merged = InstallConfig(
        platforms=platforms if platforms is not None else base.platforms,
        packs=packs if packs is not None else base.packs,
        preset=preset if preset is not None else base.preset,
        skills=skills if skills is not None else base.skills,
        skill_classes=skill_classes if skill_classes is not None else base.skill_classes,
        skill_tags=skill_tags if skill_tags is not None else base.skill_tags,
        exclude_skills=exclude_skills if exclude_skills is not None else base.exclude_skills,
        workflows=workflows if workflows is not None else base.workflows,
        global_packs=global_packs if global_packs is not None else base.global_packs,
        global_preset=global_preset if global_preset is not None else base.global_preset,
        global_skills=global_skills if global_skills is not None else base.global_skills,
        global_skill_classes=global_skill_classes if global_skill_classes is not None else base.global_skill_classes,
        global_skill_tags=global_skill_tags if global_skill_tags is not None else base.global_skill_tags,
        global_exclude_skills=global_exclude_skills if global_exclude_skills is not None else base.global_exclude_skills,
        global_workflows=global_workflows if global_workflows is not None else base.global_workflows,
        repo_packs=repo_packs if repo_packs is not None else base.repo_packs,
        repo_preset=repo_preset if repo_preset is not None else base.repo_preset,
        repo_skills=repo_skills if repo_skills is not None else base.repo_skills,
        repo_skill_classes=repo_skill_classes if repo_skill_classes is not None else base.repo_skill_classes,
        repo_skill_tags=repo_skill_tags if repo_skill_tags is not None else base.repo_skill_tags,
        repo_exclude_skills=repo_exclude_skills if repo_exclude_skills is not None else base.repo_exclude_skills,
        repo_workflows=repo_workflows if repo_workflows is not None else base.repo_workflows,
        skill_scope=skill_scope if skill_scope is not None else base.skill_scope,
        attach_mode=attach_mode or base.attach_mode,
        home=home or base.home,
        target_directory=target_directory or base.target_directory,
        data_root=base.data_root,
        dependency_mode=normalize_dependency_mode(dependency_mode or base.dependency_mode),
        migration_mode=migration_mode or base.migration_mode,
        backup_dir=backup_dir or base.backup_dir,
        backup_policy=base.backup_policy,
        output=OutputPreferences(
            json=base.output.json if json_output is None else json_output,
            report=report or base.output.report,
            markdown=base.output.markdown if markdown is None else markdown,
        ),
        feedback=base.feedback,
    )
    validate_install_config(merged)
    return merged


def config_to_dict(config: InstallConfig) -> dict[str, Any]:
    return {
        "platforms": config.platforms,
        "packs": config.packs,
        "preset": config.preset,
        "skills": config.skills,
        "skill_classes": config.skill_classes,
        "skill_tags": config.skill_tags,
        "exclude_skills": config.exclude_skills,
        "workflows": config.workflows,
        "global_packs": config.global_packs,
        "global_preset": config.global_preset,
        "global_skills": config.global_skills,
        "global_skill_classes": config.global_skill_classes,
        "global_skill_tags": config.global_skill_tags,
        "global_exclude_skills": config.global_exclude_skills,
        "global_workflows": config.global_workflows,
        "repo_packs": config.repo_packs,
        "repo_preset": config.repo_preset,
        "repo_skills": config.repo_skills,
        "repo_skill_classes": config.repo_skill_classes,
        "repo_skill_tags": config.repo_skill_tags,
        "repo_exclude_skills": config.repo_exclude_skills,
        "repo_workflows": config.repo_workflows,
        "skill_scope": config.skill_scope,
        "attach_mode": config.attach_mode,
        "home": config.home,
        "target_directory": config.target_directory,
        "data_root": config.data_root,
        "dependency_mode": config.dependency_mode,
        "migration_mode": config.migration_mode,
        "backup_dir": config.backup_dir,
        "backup_policy": config.backup_policy,
        "output": {
            "json": config.output.json,
            "report": config.output.report,
            "markdown": config.output.markdown,
        },
        "feedback": {
            "repository": config.feedback.repository,
            "labels": config.feedback.labels,
            "capture_enabled": config.feedback.capture_enabled,
            "auto_draft": config.feedback.auto_draft,
        },
    }
