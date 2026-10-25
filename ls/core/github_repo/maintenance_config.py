from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from ..paths import validate_repo_relative_path
from ..schema import validate_json_schema
from .model import canonical_json, digest_json
from .policy import PolicyError, normalize_hostname


CONFIG_RELATIVE_PATH = ".github/localsetup-maintenance.json"
WORKFLOW_RELATIVE_PATH = ".github/workflows/shared-maintenance.yml"
CONFIG_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config" / "github-maintenance.schema.json"
SUPPORTED_ADAPTERS = ("uv", "python-stdlib", "bun", "cargo")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_FULL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$")
_EXECUTABLES = {
    "uv": {"uv"},
    "python-stdlib": {"python", "python3"} | {f"python3.{minor}" for minor in range(8, 20)},
    "bun": {"bun"},
    "cargo": {"cargo"},
}
_ECOSYSTEMS = {
    "rubygems", "npm", "pip", "maven", "nuget", "composer", "go", "rust",
    "erlang", "actions", "pub", "swift", "other",
}
_PACKAGE = re.compile(r"^[A-Za-z0-9@._/+:-]{1,200}$")
_VERSION = re.compile(r"^[A-Za-z0-9.+_~-]{1,100}$")
_DAILY_SCHEDULE = re.compile(r"^(?:[0-9]|[1-5][0-9]) (?:[0-9]|1[0-9]|2[0-3]) \* \* \*$")
_MISSING = object()
_SHELLS = {"sh", "bash", "dash", "zsh", "fish", "pwsh", "powershell", "cmd", "cmd.exe"}
_CREDENTIAL_ARGUMENT = re.compile(r"(?i)(?:^|[-_])(?:token|secret|password|passwd|credential|api[-_]?key|private[-_]?key|auth)(?:=|$)")
_KNOWN_TOKEN = re.compile(r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
_PRIVATE_INPUT_LIMIT = 1024 * 1024


class MaintenanceError(ValueError):
    """A safe, user-facing GitHub maintenance validation error."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _safe_config_bytes(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MaintenanceError("maintenance config is missing or unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _PRIVATE_INPUT_LIMIT:
        raise MaintenanceError("maintenance config must be a regular file under 1 MiB")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MaintenanceError("maintenance config is missing or unreadable") from exc


def _read_json_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) > _PRIVATE_INPUT_LIMIT:
        raise MaintenanceError("maintenance config exceeds the 1 MiB size limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MaintenanceError("maintenance config must be valid UTF-8 JSON without duplicate keys") from exc
    if not isinstance(value, dict):
        raise MaintenanceError("maintenance config must be a JSON object")
    if CONFIG_SCHEMA_PATH.is_symlink() or not CONFIG_SCHEMA_PATH.is_file():
        raise MaintenanceError("maintenance configuration schema is missing or unsafe")
    issues = validate_json_schema(value, CONFIG_SCHEMA_PATH, label="GitHub maintenance config", required=True)
    if issues:
        first = issues[0].split(": ", 1)[0]
        raise MaintenanceError(first)
    _validate_semantics(value)
    return value


def _validate_semantics(value: dict[str, Any]) -> None:
    try:
        hostname = normalize_hostname(value["hostname"])
    except (KeyError, TypeError, PolicyError) as exc:
        raise MaintenanceError("maintenance hostname must be a normalized bare DNS hostname") from exc
    if hostname != value["hostname"]:
        raise MaintenanceError("maintenance hostname must be lowercase and normalized")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise MaintenanceError("unsupported maintenance config schema")
    repository_id = value.get("repository_id")
    if type(repository_id) is not int or repository_id <= 0:
        raise MaintenanceError("repository_id must be a positive immutable GitHub ID")
    full_name = value.get("full_name")
    if not isinstance(full_name, str) or not _FULL_NAME.fullmatch(full_name):
        raise MaintenanceError("full_name must use OWNER/REPO format")
    workflow_commit = value.get("workflow_commit")
    if not isinstance(workflow_commit, str) or not _HEX40.fullmatch(workflow_commit):
        raise MaintenanceError("workflow_commit must be a full lowercase 40-character commit SHA")
    owners = value.get("validation_owners")
    if (
        not isinstance(owners, list)
        or not owners
        or any(not isinstance(owner, str) or not _WORKFLOW_FILE.fullmatch(owner) for owner in owners)
        or len(set(owners)) != len(owners)
    ):
        raise MaintenanceError("validation_owners must be unique GitHub workflow filenames")
    schedule = value.get("advisory_schedule", _MISSING)
    if schedule is not _MISSING and schedule is not None:
        if not isinstance(schedule, str) or not _valid_daily_schedule(schedule):
            raise MaintenanceError("advisory_schedule must be null or a fixed daily cron expression")
    external_tools = value.get("external_tool_packages", [])
    if not isinstance(external_tools, list) or len(external_tools) > 500:
        raise MaintenanceError("external_tool_packages must be a bounded array of package records")
    seen_tools: set[tuple[str, str, str]] = set()
    for record in external_tools:
        if not isinstance(record, dict) or set(record) != {"ecosystem", "name", "version"}:
            raise MaintenanceError("external_tool_packages entries must contain ecosystem, name, and version")
        ecosystem, name, version = record["ecosystem"], record["name"], record["version"]
        if (
            not isinstance(ecosystem, str) or ecosystem not in _ECOSYSTEMS
            or not isinstance(name, str) or not _PACKAGE.fullmatch(name) or name in {".", ".."} or "//" in name
            or not isinstance(version, str) or not _VERSION.fullmatch(version)
        ):
            raise MaintenanceError("external_tool_packages contains an invalid package record")
        key = (ecosystem, name, version)
        if key in seen_tools:
            raise MaintenanceError("external_tool_packages entries must be unique")
        seen_tools.add(key)
    adapters = value.get("adapters")
    if not isinstance(adapters, dict) or not adapters or set(adapters) - set(SUPPORTED_ADAPTERS):
        raise MaintenanceError("adapters must contain only supported adapter names")
    for adapter_name, commands in adapters.items():
        if not isinstance(commands, list) or not commands:
            raise MaintenanceError(f"{adapter_name} must contain at least one command")
        names: set[str] = set()
        for command in commands:
            if not isinstance(command, dict):
                raise MaintenanceError(f"{adapter_name} command must be an object")
            name = command.get("name")
            if not isinstance(name, str) or name in names:
                raise MaintenanceError(f"{adapter_name} command names must be unique")
            names.add(name)
            _validate_command(adapter_name, command)


def _valid_daily_schedule(value: str) -> bool:
    return bool(_DAILY_SCHEDULE.fullmatch(value))


def _validate_command(adapter_name: str, command: dict[str, Any]) -> None:
    cwd = command.get("cwd")
    if not isinstance(cwd, str) or not cwd or "\\" in cwd or "\x00" in cwd or cwd.startswith("~"):
        raise MaintenanceError(f"{adapter_name} command cwd must be a safe repository-relative path")
    if cwd != ".":
        try:
            validate_repo_relative_path(cwd, "maintenance command cwd")
        except ValueError as exc:
            raise MaintenanceError(f"{adapter_name} command cwd must be a safe repository-relative path") from exc
        if any(part in {"", ".", ".."} for part in PurePosixPath(cwd).parts):
            raise MaintenanceError(f"{adapter_name} command cwd must be a safe repository-relative path")

    argv = command.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 64
        or any(not isinstance(part, str) or not part or any(ord(char) < 32 or ord(char) == 127 for char in part) for part in argv)
        or argv[0] not in _EXECUTABLES[adapter_name]
    ):
        raise MaintenanceError(f"{adapter_name} command argv must begin with its fixed supported executable")
    for index, part in enumerate(argv):
        if part.casefold() in _SHELLS or (index > 0 and part in {"-c", "--command", "/c", "-Command"}):
            raise MaintenanceError("shell commands and shell-evaluation flags are not supported")
        if _CREDENTIAL_ARGUMENT.search(part) or _KNOWN_TOKEN.search(part):
            raise MaintenanceError("credential arguments and literal tokens are not allowed in tracked config")
        if index > 0 and _CREDENTIAL_ARGUMENT.search(argv[index - 1]) and "=" not in argv[index - 1]:
            raise MaintenanceError("credential arguments and literal tokens are not allowed in tracked config")
    if adapter_name == "python-stdlib" and any(part in {"-c", "--command", "-mexec"} for part in argv[1:]):
        raise MaintenanceError("inline Python execution is not supported")
    if adapter_name == "uv" and "--script" in argv:
        raise MaintenanceError("uv inline script execution is not supported")
    timeout = command.get("timeout_seconds")
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise MaintenanceError(f"{adapter_name} command timeout_seconds must be between 1 and 600")


def parse_config_bytes(raw: bytes) -> dict[str, Any]:
    return _read_json_bytes(raw)


def read_config(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _safe_config_bytes(path)
    return _read_json_bytes(raw), raw


def canonical_config_bytes(value: dict[str, Any]) -> bytes:
    _validate_semantics(value)
    return (canonical_json(value) + "\n").encode("utf-8")


def config_digest(value: dict[str, Any]) -> str:
    return digest_json(value)


def render_shared_workflow(config: dict[str, Any]) -> bytes:
    _validate_semantics(config)
    external_tools = json.dumps(
        config.get("external_tool_packages", []), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    # Values are constrained to a package-data grammar without apostrophes, so
    # the compact JSON array is safe as a single-quoted YAML scalar.
    schedule = config.get("advisory_schedule", _default_advisory_schedule(config["repository_id"]))
    lines = [
        "name: Shared maintenance",
        "",
        "on:",
        "  push:",
        "  pull_request:",
        "  merge_group:",
        "  workflow_dispatch:",
    ]
    if schedule is not None:
        lines.extend(("  schedule:", f"    - cron: '{schedule}'"))
    lines.extend((
        "",
        "permissions:",
        "  contents: read",
        "  actions: read",
        "  security-events: read",
        "",
        "jobs:",
        "  maintenance:",
        "    uses: CruxExperts/LocalSetup/.github/workflows/github-maintenance.yml@" + config["workflow_commit"],
        "    with:",
        f'      expected-repository-id: "{config["repository_id"]}"',
        "      report-issues: false",
        f"      external-tool-packages-json: '{external_tools}'",
        "",
    ))
    return ("\n".join(lines)).encode("utf-8")


def _default_advisory_schedule(repository_id: int) -> str:
    return f"{repository_id % 60} {(repository_id // 60) % 24} * * *"
