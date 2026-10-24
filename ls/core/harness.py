from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml


HEARTBEAT_SKILL = "ls-codex-heartbeat-checks"
HEARTBEAT_TRIGGER_ID = "codex-heartbeat"
HEARTBEAT_TASK_ID = "codex-heartbeat-run"
HEARTBEAT_DOC = "HEARTBEAT.md"
HEARTBEAT_CONFIG = "config/codex_heartbeat.yaml"
CRON_MANIFEST = "cron/manifest.yaml"
CRON_OUTPUT = "cron/codex-heartbeat.crontab"


def _heartbeat_script(repo_root: Path) -> Path:
    return repo_root / "ls" / "skills" / HEARTBEAT_SKILL / "scripts" / "codex_heartbeat.py"


def _cron_ctl(repo_root: Path) -> Path:
    return repo_root / "ls" / "skills" / "ls-cron-job-scheduling" / "scripts" / "cron_ctl.py"


def _template(repo_root: Path, name: str) -> Path:
    return repo_root / "ls" / "skills" / HEARTBEAT_SKILL / "templates" / name


def _load_runtime(repo_root: Path) -> ModuleType:
    script = _heartbeat_script(repo_root)
    spec = importlib.util.spec_from_file_location("localsetup_codex_heartbeat_runtime", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load heartbeat runtime: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def _write_text_if_missing(path: Path, text: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _target(target_root: Path | None, repo_root: Path) -> Path:
    return (target_root or repo_root).expanduser().resolve()


def _interval_schedule(minutes: int) -> str:
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ValueError("heartbeat.interval_minutes must be an integer in range 1..1440")
    if minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
    if minutes % 60 == 0 and 24 % (minutes // 60) == 0:
        hours = minutes // 60
        return "0 0 * * *" if hours == 24 else f"0 */{hours} * * *"
    raise ValueError("Heartbeat cron requires a minute divisor of 60 or a whole-hour divisor of 24")


def _heartbeat_command(repo_root: Path, target_root: Path) -> list[str]:
    return [
        sys.executable,
        "-I", "-B",
        str((repo_root / "ls" / "tools" / "localsetup.py").resolve()),
        "--source-root",
        str(repo_root.resolve()),
        "--target-directory",
        str(target_root.resolve()),
        "harness",
        "codex-heartbeat",
        "run",
        "--scheduled",
    ]


def _cron_task(repo_root: Path, target_root: Path, enabled: bool, timeout_seconds: int = 1800) -> dict[str, Any]:
    return {
        "id": HEARTBEAT_TASK_ID,
        "trigger": HEARTBEAT_TRIGGER_ID,
        "sequence_order": 100,
        "command": _heartbeat_command(repo_root, target_root),
        "enabled": enabled,
        "timeout_seconds": timeout_seconds,
    }


def _cron_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    tasks = manifest.get("tasks") if isinstance(manifest.get("tasks"), list) else []
    task = next((item for item in tasks if isinstance(item, dict) and item.get("id") == HEARTBEAT_TASK_ID), None)
    triggers = manifest.get("triggers") if isinstance(manifest.get("triggers"), dict) else {}
    return {
        "manifest_exists": bool(manifest),
        "trigger": triggers.get(HEARTBEAT_TRIGGER_ID),
        "task": task,
    }


def _upsert_cron_manifest(repo_root: Path, target_root: Path, config: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    manifest_path = target_root / CRON_MANIFEST
    manifest = _read_yaml(manifest_path)
    triggers = manifest.get("triggers")
    if not isinstance(triggers, dict):
        triggers = {}
    heartbeat = config.get("heartbeat") if isinstance(config.get("heartbeat"), dict) else {}
    if enabled or HEARTBEAT_TRIGGER_ID not in triggers:
        interval = heartbeat.get("interval_minutes", 15)
        triggers[HEARTBEAT_TRIGGER_ID] = {"schedule": _interval_schedule(interval)}
    manifest["triggers"] = triggers

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    new_task = _cron_task(repo_root, target_root, enabled=enabled)
    replaced = False
    for index, task in enumerate(tasks):
        if isinstance(task, dict) and task.get("id") == HEARTBEAT_TASK_ID:
            tasks[index] = {**task, **new_task}
            replaced = True
            break
    if not replaced:
        tasks.append(new_task)
    manifest["tasks"] = tasks
    _write_yaml(manifest_path, manifest)
    validate_cron_manifest(repo_root, manifest_path)
    return {"path": str(manifest_path), "summary": _cron_summary(manifest)}


def validate_cron_manifest(repo_root: Path, manifest_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(_cron_ctl(repo_root)), "--manifest", str(manifest_path), "validate"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())


def _install_live_crontab(repo_root: Path, target_root: Path, *, yes: bool) -> dict[str, Any]:
    if not yes:
        raise RuntimeError("live crontab install requires --install-crontab and --yes")
    manifest = target_root / CRON_MANIFEST
    output = target_root / CRON_OUTPUT
    generate = subprocess.run(
        [
            sys.executable,
            "-I", "-B",
            str(_cron_ctl(repo_root)),
            "--manifest",
            str(manifest),
            "install",
            "--repo-root",
            str(target_root),
            "--output",
            str(output),
            "--trigger",
            HEARTBEAT_TRIGGER_ID,
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if generate.returncode != 0:
        raise RuntimeError((generate.stderr or generate.stdout).strip())
    from .harness_schedule import install_fragment
    enabled = _read_existing_target_config(target_root).get("heartbeat", {}).get("enabled") is True
    fragment = output.read_text(encoding="utf-8") if enabled else ""
    return install_fragment(target_root, fragment) | {"output": str(output)}


def _load_target_config(repo_root: Path, target_root: Path) -> dict[str, Any]:
    config_path = target_root / HEARTBEAT_CONFIG
    if not config_path.is_file():
        init(repo_root, target_root)
    return _read_yaml(config_path)


def _read_existing_target_config(target_root: Path) -> dict[str, Any]:
    config_path = target_root / HEARTBEAT_CONFIG
    return _read_yaml(config_path) if config_path.is_file() else {}


def _int_or_default(value: Any, default: int) -> int:
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return default
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return default


def _positive_int_or_default(value: Any, default: int) -> int:
    parsed = _int_or_default(value, default)
    return parsed if parsed > 0 else default


def _float_or_default(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _heartbeat_budget_policy(config: dict[str, Any], queue_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    heartbeat = config.get("heartbeat") if isinstance(config.get("heartbeat"), dict) else {}
    agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    profiles = config.get("agent_profiles") if isinstance(config.get("agent_profiles"), dict) else {}
    profile_name = str(agent.get("profile") or "heartbeat")
    profile = profiles.get(profile_name) if isinstance(profiles.get(profile_name), dict) else {}
    direct_policy = config.get("direct_command_policy") if isinstance(config.get("direct_command_policy"), dict) else {}
    queue_policy = queue_policy or {}

    interval_minutes = max(1, _int_or_default(heartbeat.get("interval_minutes"), 15))
    occupancy_ratio = _float_or_default(queue_policy.get("max_subagent_occupancy_ratio"), 0.8)
    if occupancy_ratio < 0:
        occupancy_ratio = 0.0
    if occupancy_ratio > 1:
        occupancy_ratio = 1.0
    default_timeout = _positive_int_or_default(agent.get("timeout_seconds"), 0)
    if default_timeout <= 0:
        default_timeout = _positive_int_or_default(profile.get("timeout_seconds"), 1800)
    return {
        "interval_minutes": interval_minutes,
        "max_subagent_occupancy_ratio": occupancy_ratio,
        "effective_runtime_seconds": math.floor(interval_minutes * 60 * occupancy_ratio),
        "max_parallel": _positive_int_or_default(queue_policy.get("max_parallel"), 1),
        "max_total": _positive_int_or_default(queue_policy.get("max_total"), 1),
        "max_depth": _positive_int_or_default(queue_policy.get("max_depth"), 1),
        "execution_order": str(queue_policy.get("execution_order") or "serial"),
        "default_timeout_seconds": default_timeout,
        "allow_git_writes": bool(direct_policy.get("allow_git_writes", False)),
        "allow_destructive": bool(direct_policy.get("allow_destructive", False)),
    }


def _task_consumption(task: dict[str, Any], default_timeout: int) -> dict[str, Any]:
    status = str(task.get("status") or "unknown")
    allocated = _positive_int_or_default(task.get("allocated_runtime_seconds"), 0)
    if allocated <= 0:
        allocated = _positive_int_or_default(task.get("timeout_seconds"), default_timeout)
    actual_raw = task.get("actual_runtime_seconds")
    actual = _int_or_default(actual_raw, -1)
    if status == "completed" and actual >= 0:
        consumed = actual
        rule = "actual"
    else:
        consumed = allocated
        rule = "allocated"
    return {
        "id": str(task.get("id") or ""),
        "status": status,
        "allocated_runtime_seconds": allocated,
        "actual_runtime_seconds": actual if actual >= 0 else None,
        "reserved_runtime_seconds": consumed,
        "reservation_rule": rule,
    }


def budget(repo_root: Path, target_root: Path | None = None) -> dict[str, Any]:
    target = _target(target_root, repo_root)
    config = _read_existing_target_config(target)
    heartbeat = config.get("heartbeat") if isinstance(config.get("heartbeat"), dict) else {}
    queue_path_value = heartbeat.get("task_queue_path")
    queue_path: Path | None = None
    queue: dict[str, Any] = {}
    queue_exists = False
    queue_error: str | None = None
    if isinstance(queue_path_value, str) and queue_path_value.strip():
        candidate = Path(queue_path_value).expanduser()
        if candidate.is_absolute():
            queue_error = "heartbeat.task_queue_path must be repo-local, not absolute"
            queue_path = candidate
        else:
            queue_path = (target / candidate).resolve()
        if queue_error is None:
            try:
                queue_path.relative_to(target)
            except ValueError:
                queue_error = "heartbeat.task_queue_path must stay under target_root"
        if queue_error is None:
            try:
                queue_exists = queue_path.is_file()
                queue = _read_yaml(queue_path) if queue_exists else {}
                if not queue_exists:
                    queue = {}
            except ValueError as exc:
                queue_error = str(exc)
                queue = {}
        else:
            queue = {}
    queue_policy = queue.get("policy") if isinstance(queue.get("policy"), dict) else {}
    policy = _heartbeat_budget_policy(config, queue_policy)
    raw_tasks = queue.get("tasks") if isinstance(queue.get("tasks"), list) else []
    tasks = [_task_consumption(task, policy["default_timeout_seconds"]) for task in raw_tasks if isinstance(task, dict)]
    reserved = sum(task["reserved_runtime_seconds"] for task in tasks)
    return {
        "ok": queue_error is None,
        "target_root": str(target),
        "config_path": str(target / HEARTBEAT_CONFIG),
        "task_queue_path": str(queue_path) if queue_path else None,
        "policy": policy,
        "summary": {
            "config_exists": bool(config),
            "task_queue_configured": queue_path is not None,
            "task_queue_exists": queue_exists,
            "task_count": len(tasks),
            "reserved_runtime_seconds": reserved,
            "remaining_runtime_seconds": max(policy["effective_runtime_seconds"] - reserved, 0),
            "queue_error": queue_error,
        },
        "tasks": tasks,
    }


def plan(repo_root: Path, target_root: Path | None = None) -> dict[str, Any]:
    target = _target(target_root, repo_root)
    config_path = target / HEARTBEAT_CONFIG
    manifest_path = target / CRON_MANIFEST
    manifest = _read_yaml(manifest_path) if manifest_path.is_file() else {}
    runtime = _load_runtime(repo_root)
    status_payload = runtime.status(target_root=target, config_path=config_path) if config_path.exists() else {
        "ok": True,
        "config_exists": False,
        "enabled": False,
    }
    command_plan = runtime.plan_summary(target_root=target, config_path=config_path,
        no_agent=status_payload.get("scheduled_agent") is not True) if config_path.exists() else {
        "ok": True,
        "commands": [],
        "config_exists": False,
    }
    return {
        "ok": True,
        "target_root": str(target),
        "heartbeat_doc": str(target / HEARTBEAT_DOC),
        "config_path": str(config_path),
        "cron_manifest": str(manifest_path),
        "state_dir": str(target / ".localsetup" / "state" / "codex-heartbeat"),
        "launcher_command": _heartbeat_command(repo_root, target),
        "command_plan": command_plan,
        "status": status_payload,
        "cron": _cron_summary(manifest),
    }


def init(repo_root: Path, target_root: Path | None = None) -> dict[str, Any]:
    target = _target(target_root, repo_root)
    doc_created = _write_text_if_missing(target / HEARTBEAT_DOC, _template(repo_root, "HEARTBEAT.md").read_text(encoding="utf-8"))
    config_created = _write_text_if_missing(
        target / HEARTBEAT_CONFIG,
        _template(repo_root, "codex_heartbeat.yaml").read_text(encoding="utf-8"),
    )
    return {
        "ok": True,
        "target_root": str(target),
        "created": {
            HEARTBEAT_DOC: doc_created,
            HEARTBEAT_CONFIG: config_created,
        },
        "status": plan(repo_root, target)["status"],
    }


def enable(
    repo_root: Path,
    target_root: Path | None = None,
    *,
    install_crontab: bool = False,
    yes: bool = False,
    scheduled_agent: bool | None = None,
) -> dict[str, Any]:
    if install_crontab and not yes:
        raise RuntimeError("live crontab install requires --install-crontab and --yes")
    target = _target(target_root, repo_root)
    config = _load_target_config(repo_root, target)
    heartbeat = config.setdefault("heartbeat", {})
    if not isinstance(heartbeat, dict):
        raise ValueError("heartbeat config must be a mapping")
    if scheduled_agent is not None:
        if type(scheduled_agent) is not bool:
            raise ValueError("Scheduled agent selection must be a boolean")
        heartbeat["scheduled_agent"] = scheduled_agent
    mode = heartbeat.get("scheduled_agent", False)
    if type(mode) is not bool:
        raise ValueError("heartbeat.scheduled_agent must be a boolean")
    if mode:
        agent = config.get("agent")
        if not isinstance(agent, dict) or agent.get("enabled") is not True:
            raise ValueError("Configure and enable an agent profile before selecting scheduled reviews")
        _load_runtime(repo_root).planned_commands(config, target_root=target)
    _interval_schedule(heartbeat.get("interval_minutes", 15))
    heartbeat["enabled"] = True
    _write_yaml(target / HEARTBEAT_CONFIG, config)
    cron = _upsert_cron_manifest(repo_root, target, config, enabled=True)
    crontab = _install_live_crontab(repo_root, target, yes=yes) if install_crontab else {"installed": False}
    return {"ok": True, "target_root": str(target), "enabled": True, "cron": cron, "crontab": crontab}


def disable(
    repo_root: Path,
    target_root: Path | None = None,
    *,
    install_crontab: bool = False,
    yes: bool = False,
) -> dict[str, Any]:
    if install_crontab and not yes:
        raise RuntimeError("live crontab install requires --install-crontab and --yes")
    target = _target(target_root, repo_root)
    config = _load_target_config(repo_root, target)
    heartbeat = config.setdefault("heartbeat", {})
    if not isinstance(heartbeat, dict):
        raise ValueError("heartbeat config must be a mapping")
    heartbeat["enabled"] = False
    _write_yaml(target / HEARTBEAT_CONFIG, config)
    cron = _upsert_cron_manifest(repo_root, target, config, enabled=False)
    crontab = _install_live_crontab(repo_root, target, yes=yes) if install_crontab else {"installed": False}
    return {"ok": True, "target_root": str(target), "enabled": False, "cron": cron, "crontab": crontab}


def status(repo_root: Path, target_root: Path | None = None) -> dict[str, Any]:
    target = _target(target_root, repo_root)
    runtime = _load_runtime(repo_root)
    payload = runtime.status(target_root=target, config_path=target / HEARTBEAT_CONFIG)
    manifest_path = target / CRON_MANIFEST
    payload["cron"] = _cron_summary(_read_yaml(manifest_path) if manifest_path.is_file() else {})
    return payload


def run(
    repo_root: Path,
    target_root: Path | None = None,
    *,
    no_agent: bool = False,
    force: bool = False,
    scheduled: bool = False,
) -> dict[str, Any]:
    target = _target(target_root, repo_root)
    runtime = _load_runtime(repo_root)
    return runtime.run_once(target_root=target, config_path=target / HEARTBEAT_CONFIG,
                            no_agent=no_agent, force=force, scheduled=scheduled)


def payload_to_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
