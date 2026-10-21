"""Support helpers for the generic agent heartbeat runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from heartbeat_process import execute

try:
    import yaml
except ImportError:  # pragma: no cover - checked by the caller in normal use
    yaml = None

SCHEMA_VERSION = "1.0"
STATE_DIR_DEFAULT = ".localsetup/state/codex-heartbeat"
LOCK_NAME = "heartbeat.lock"
ACTIVE_NAME = "active.json"
LATEST_NAME = "latest.json"
RUNS_DIR_NAME = "runs"
MAX_TAIL_CHARS = 12000
DEFAULT_COMMAND_TIMEOUT = 300
MAX_COMMAND_TIMEOUT = 86400
PROMPT_PLACEHOLDER = "{heartbeat_prompt}"

BLOCKED_GIT_SUBCOMMANDS = {"commit", "push"}
GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--config-env",
    "--git-dir",
    "--namespace",
    "--super-prefix",
    "--work-tree",
}
GIT_GLOBAL_OPTIONS_WITH_EQUALS = (
    "--config-env=",
    "--exec-path=",
    "--git-dir=",
    "--namespace=",
    "--super-prefix=",
    "--work-tree=",
)
GIT_GLOBAL_FLAG_OPTIONS = {
    "--bare",
    "--glob-pathspecs",
    "--icase-pathspecs",
    "--literal-pathspecs",
    "--no-lazy-fetch",
    "--no-optional-locks",
    "--no-pager",
    "--no-replace-objects",
    "--noglob-pathspecs",
    "--paginate",
    "-p",
}
BLOCKED_EXECUTABLES = {
    "dd",
    "chmod",
    "chown",
    "docker",
    "mkfs",
    "mount",
    "mv",
    "podman",
    "umount",
    "kubectl",
    "rm",
    "rmdir",
    "shutdown",
    "reboot",
    "poweroff",
    "systemctl",
    "service",
}


class HeartbeatError(RuntimeError):
    """Raised for recoverable harness errors."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_json(payload: Any) -> str:
    return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise HeartbeatError("PyYAML is required to read codex_heartbeat.yaml")
    if not path.is_file():
        raise HeartbeatError(f"config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HeartbeatError("config root must be a mapping")
    return data


def config_path_for(target_root: Path) -> Path:
    return target_root / "config" / "codex_heartbeat.yaml"


def state_root_from_config(target_root: Path, config: dict[str, Any]) -> Path:
    heartbeat = config.get("heartbeat") if isinstance(config.get("heartbeat"), dict) else {}
    raw = str(heartbeat.get("state_dir") or STATE_DIR_DEFAULT)
    path = Path(raw).expanduser()
    if path.is_absolute():
        raise HeartbeatError("heartbeat.state_dir must be repo-relative")
    resolved = (target_root / path).resolve()
    try:
        resolved.relative_to(target_root.resolve())
    except ValueError as exc:
        raise HeartbeatError("heartbeat.state_dir must stay inside the target repository") from exc
    return resolved


def heartbeat_enabled(config: dict[str, Any]) -> bool:
    heartbeat = config.get("heartbeat") if isinstance(config.get("heartbeat"), dict) else {}
    return bool(heartbeat.get("enabled", False))


def command_policy(config: dict[str, Any]) -> dict[str, Any]:
    policy = config.get("direct_command_policy") if isinstance(config.get("direct_command_policy"), dict) else {}
    allowlist = policy.get("allowlist") if isinstance(policy.get("allowlist"), list) else []
    return {
        "allow_destructive": bool(policy.get("allow_destructive", False)),
        "allow_git_writes": bool(policy.get("allow_git_writes", False)),
        "allowlist": {str(item).strip() for item in allowlist if str(item).strip()},
    }


def _command_key(argv: list[str]) -> str:
    return " ".join(argv)


def _git_subcommand(argv: list[str]) -> str | None:
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None
        if not token.startswith("-"):
            return token
        if token in GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if index + 1 >= len(argv) or not argv[index + 1]:
                raise HeartbeatError(f"git global option requires a value: {token}")
            index += 2
            continue
        if token.startswith(GIT_GLOBAL_OPTIONS_WITH_EQUALS):
            if token.endswith("="):
                raise HeartbeatError(f"git global option requires a value: {token[:-1]}")
            index += 1
            continue
        if token.startswith("-C") and token != "-C":
            index += 1
            continue
        if token.startswith("-c") and token != "-c":
            index += 1
            continue
        if token in GIT_GLOBAL_FLAG_OPTIONS:
            index += 1
            continue
        raise HeartbeatError(f"unable to validate git global option: {token}")
    return None


def validate_direct_command(argv: list[str], config: dict[str, Any], *, allow_direct: bool = False) -> None:
    if not argv:
        raise HeartbeatError("command argv must not be empty")
    policy = command_policy(config)
    executable = Path(argv[0]).name
    if executable == "git" and not policy["allow_git_writes"]:
        subcommand = _git_subcommand(argv)
        if subcommand in BLOCKED_GIT_SUBCOMMANDS:
            raise HeartbeatError(f"direct command policy blocked git {subcommand}")
    if executable in BLOCKED_EXECUTABLES and not policy["allow_destructive"]:
        raise HeartbeatError(f"direct command policy blocked destructive executable: {executable}")
    if allow_direct or _command_key(argv) in policy["allowlist"]:
        return


def normalize_timeout(value: Any) -> int:
    if value is None:
        return DEFAULT_COMMAND_TIMEOUT
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise HeartbeatError("timeout_seconds must be an integer") from exc
    if timeout < 1 or timeout > MAX_COMMAND_TIMEOUT:
        raise HeartbeatError(f"timeout_seconds must be in range 1..{MAX_COMMAND_TIMEOUT}")
    return timeout


def _tail(text: str) -> str:
    return text[-MAX_TAIL_CHARS:]


def write_command_sidecar(sidecar_path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(sidecar_path, entry)
    entry["sidecar"] = sidecar_path.name
    entry["sidecar_sha256"] = sha256_file(sidecar_path)
    return entry


def run_command(
    command_id: str,
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    sidecar_path: Path,
    launcher_info: dict[str, Any] | None = None,
    stdin_text: str | None = None,
    protocol_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = utc_now()
    entry: dict[str, Any] = {
        "id": command_id,
        "argv": argv,
        "cwd": str(cwd),
        "started_at": started,
        "timeout_seconds": timeout_seconds,
    }
    if launcher_info:
        entry.update(launcher_info)
    try:
        options = {}
        if protocol_options is not None:
            from heartbeat_protocol import Receipt
            options = {**protocol_options, "receipt": Receipt()}
        entry.update(execute(argv, cwd=cwd, timeout=timeout_seconds, stdin_text=stdin_text, **options))
        entry["finished_at"] = utc_now()
    except Exception as exc:
        entry.update(
            {
                "returncode": 1,
                "timed_out": False,
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": utc_now(),
            }
        )
    return write_command_sidecar(sidecar_path, entry)


def load_hooks(config: dict[str, Any], name: str) -> list[dict[str, Any]]:
    hooks = config.get("hooks") if isinstance(config.get("hooks"), dict) else {}
    raw = hooks.get(name) if isinstance(hooks.get(name), list) else []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HeartbeatError(f"hooks.{name}[{index}] must be a mapping")
        command = item.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise HeartbeatError(f"hooks.{name}[{index}].command must be a non-empty argv list")
        normalized.append(
            {
                "id": str(item.get("id") or f"{name}-{index + 1}"),
                "command": command,
                "launcher_mode": "direct-argv",
                "timeout_seconds": normalize_timeout(item.get("timeout_seconds")),
                "allow_direct": bool(item.get("allow_direct", False)),
            }
        )
    return normalized


def _agent_profile(config: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profiles = config.get("agent_profiles") if isinstance(config.get("agent_profiles"), dict) else {}
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        raise HeartbeatError(f"agent profile not found: {profile_name}")
    return profile


def _path_env(profile: dict[str, Any]) -> str | None:
    raw_path = profile.get("path_env")
    if isinstance(raw_path, str) and raw_path.strip():
        return raw_path
    path_entries = profile.get("path")
    if isinstance(path_entries, list):
        entries = [str(item) for item in path_entries if str(item).strip()]
        if entries:
            return os.pathsep.join(entries)
    return None


def _profile_prompt(profile: dict[str, Any]) -> str:
    prompt = profile.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return "Read HEARTBEAT.md, inspect queued heartbeat work, and produce a concise status report."


def _profile_command_argv(profile: dict[str, Any]) -> list[str]:
    command = profile.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise HeartbeatError("agent profile command must be a non-empty argv list")
    return list(command)


def _prompt_input(profile: dict[str, Any], argv: list[str]) -> tuple[list[str], str | None, str]:
    transport = str(profile.get("prompt_transport") or "argv").strip()
    if transport not in {"argv", "stdin", "none"}:
        raise HeartbeatError("agent profile prompt_transport must be one of: argv, stdin, none")
    placeholder_count = argv.count(PROMPT_PLACEHOLDER)
    prompt = _profile_prompt(profile)
    if transport == "argv":
        if placeholder_count != 1:
            raise HeartbeatError(f"argv prompt transport requires exactly one {PROMPT_PLACEHOLDER} argument")
        return [prompt if item == PROMPT_PLACEHOLDER else item for item in argv], None, transport
    if placeholder_count:
        raise HeartbeatError(f"{PROMPT_PLACEHOLDER} is only valid with argv prompt transport")
    return argv, prompt if transport == "stdin" else None, transport


def _resolve_profile_executable(profile: dict[str, Any], argv: list[str]) -> tuple[list[str], str]:
    path_env = _path_env(profile)
    resolved = shutil.which(argv[0], path=path_env)
    if not resolved:
        raise HeartbeatError(f"unable to resolve agent profile executable: {argv[0]}")
    return [resolved, *argv[1:]], resolved


def _shell_login_command(profile: dict[str, Any], argv: list[str]) -> tuple[list[str], str]:
    shell_path = str(profile.get("shell") or "/bin/bash")
    if not Path(shell_path).is_absolute():
        resolved_shell = shutil.which(shell_path)
        if not resolved_shell:
            raise HeartbeatError(f"unable to resolve shell-login shell: {shell_path}")
        shell_path = resolved_shell
    rendered = shlex.join(argv)
    path_env = _path_env(profile)
    if path_env:
        rendered = f"PATH={shlex.quote(path_env)}; export PATH; {rendered}"
    return [shell_path, "-lc", rendered], rendered


def load_agent_command(config: dict[str, Any], *, target_root: Path | None = None) -> dict[str, Any] | None:
    if "codex" in config:
        raise HeartbeatError("codex configuration is obsolete; replace it with the agent configuration")
    agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    if not bool(agent.get("enabled", False)):
        return None
    profile_name = str(agent.get("profile") or "").strip()
    if not profile_name:
        raise HeartbeatError("agent.profile must name an agent_profiles entry when agent.enabled is true")
    profile = _agent_profile(config, profile_name)
    client = str(profile.get("client") or "").strip()
    if not client:
        raise HeartbeatError("agent profile client must be a non-empty label")
    launcher = str(profile.get("launcher") or "resolved-path").strip()
    if launcher == "lscli":
        from heartbeat_lscli import plan
        try:
            return plan(profile, agent, profile_name, target_root)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise HeartbeatError("LSCli heartbeat profile or registration is unavailable; verify explicit configuration and registration") from exc
    logical_argv, stdin_text, prompt_transport = _prompt_input(profile, _profile_command_argv(profile))
    command = logical_argv
    info: dict[str, Any] = {
        "client": client,
        "launcher_mode": launcher,
        "logical_argv": logical_argv,
        "profile": profile_name,
        "prompt_transport": prompt_transport,
    }
    if launcher == "direct-argv":
        info["resolved_executable"] = str(Path(command[0]).resolve()) if Path(command[0]).is_absolute() else None
    elif launcher == "resolved-path":
        command, resolved = _resolve_profile_executable(profile, logical_argv)
        info["resolved_executable"] = resolved
    elif launcher == "shell-login":
        if stdin_text is not None:
            raise HeartbeatError("shell-login profiles do not support stdin prompt transport")
        command, rendered = _shell_login_command(profile, logical_argv)
        info["rendered_command"] = rendered
        info["resolved_executable"] = None
    else:
        raise HeartbeatError("agent profile launcher must be one of: direct-argv, resolved-path, shell-login")
    return {
        "allow_direct": bool(agent.get("allow_direct", profile.get("allow_direct", False))),
        "command": command,
        "id": f"{profile_name}-agent",
        "launcher_info": info,
        "policy_command": logical_argv,
        "stdin_text": stdin_text,
        "timeout_seconds": normalize_timeout(agent.get("timeout_seconds", profile.get("timeout_seconds"))),
    }


def planned_commands(config: dict[str, Any], *, no_agent: bool = False, target_root: Path | None = None) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    commands.extend(load_hooks(config, "before"))
    if not no_agent:
        agent_command = load_agent_command(config, target_root=target_root)
        if agent_command:
            commands.append(agent_command)
    commands.extend(load_hooks(config, "after"))
    return commands


def plan_summary(*, target_root: Path, config_path: Path | None = None, no_agent: bool = False) -> dict[str, Any]:
    target_root = target_root.resolve()
    config_path = config_path or config_path_for(target_root)
    if not config_path.is_file():
        return {"ok": True, "commands": [], "config_exists": False}
    config = load_yaml(config_path)
    summaries: list[dict[str, Any]] = []
    try:
        planned = planned_commands(config, no_agent=no_agent, target_root=target_root)
    except HeartbeatError as exc:
        return {"ok": False, "commands": [], "config_exists": True, "error": str(exc)}
    for command in planned:
        info = command.get("launcher_info") if isinstance(command.get("launcher_info"), dict) else {}
        summaries.append(
            {
                "id": command["id"],
                "argv": command["command"],
                "policy_argv": command.get("policy_command", command["command"]),
                "client": info.get("client"),
                "launcher_mode": info.get("launcher_mode", command.get("launcher_mode", "direct-argv")),
                "prompt_transport": info.get("prompt_transport"),
                "resolved_executable": info.get("resolved_executable"),
                "rendered_command": info.get("rendered_command"),
                "timeout_seconds": command["timeout_seconds"],
            }
        )
    return {"ok": True, "commands": summaries, "config_exists": True}
