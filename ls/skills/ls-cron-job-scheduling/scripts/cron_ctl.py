#!/usr/bin/env python3
# Purpose: Create, modify, remove, and install cron tasks from a manifest.
# Created: 2026-02-24
# Last updated: 2026-02-24

from __future__ import annotations

import argparse
import errno
import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

from _cron_manifest import (
    MAX_CMD_LEN,
    MAX_ID_LEN,
    MAX_TRIGGER_LEN,
    ManifestError,
    dump_manifest,
    load_manifest,
    normalize_command,
    normalize_sequence_order,
    validate_identifier,
    validate_manifest,
)

MANIFEST_DEFAULT = "cron/manifest.yaml"


def _load_manifest(path: Path, *, validate: bool = True) -> tuple[dict, dict, int]:
    try:
        data = load_manifest(path)
        normalized = validate_manifest(data) if validate else {}
    except ManifestError as exc:
        print(f"[cron_ctl] {exc}", file=sys.stderr)
        return {}, {}, 1
    return data, normalized, 0


def _save_manifest(path: Path, data: dict) -> int:
    try:
        dump_manifest(path, data)
    except ManifestError as exc:
        print(f"[cron_ctl] {exc}", file=sys.stderr)
        return 1
    return 0


def _command_preview(command: object) -> str:
    if isinstance(command, list):
        command_text = shlex.join(str(part) for part in command)
    else:
        command_text = str(command or "")
    return command_text[:60] + ("..." if len(command_text) > 60 else "")


def _trigger_tasks(tasks: list[dict], trigger: str) -> list[dict]:
    return [task for task in tasks if task["trigger"] == trigger]


def _runner_command(
    run_trigger: Path,
    manifest_abs: Path,
    repo_root: Path,
    trigger: str,
    delay_seconds: int = 0,
    log_dir: Path | None = None,
) -> str:
    argv = ["python3", str(run_trigger), "--manifest", str(manifest_abs), "--repo-root", str(repo_root)]
    if log_dir is not None:
        argv.extend(["--log-dir", str(log_dir)])
    if delay_seconds:
        argv.extend(["--delay-seconds", str(delay_seconds)])
    argv.append(trigger)
    return shlex.join(argv).replace("%", r"\%")


def cmd_validate(manifest_path: Path) -> int:
    _, _, code = _load_manifest(manifest_path)
    if code != 0:
        return code
    print("OK")
    return 0


def _readiness_item(label: str, status: str, detail: str, next_action: str | None = None) -> dict[str, str | None]:
    return {"label": label, "status": status, "detail": detail, "next_action": next_action}


def _path_inspection_failure(path: Path, exc: OSError | RuntimeError, action: str) -> tuple[str, str, str]:
    blocked_errors = {errno.EACCES, errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EPERM}
    blocked = isinstance(exc, RuntimeError) or isinstance(exc, PermissionError) or exc.errno in blocked_errors
    status = "blocked" if blocked else "unchecked"
    return status, f"could not inspect {path}: {type(exc).__name__}: {exc}", action


def _runner_path(repo_root: Path) -> str:
    """Resolve relative PATH entries as the runner will, from its working directory."""

    source_path = os.environ.get("PATH", os.defpath)
    directories: list[str] = []
    for entry in source_path.split(os.pathsep):
        search_dir = Path(entry or ".")
        if not search_dir.is_absolute():
            search_dir = repo_root / search_dir
        directories.append(os.path.normpath(str(search_dir)))
    return os.pathsep.join(directories)


def _task_executable_status(executable: str, repo_root: Path) -> tuple[str, str, str | None]:
    try:
        command_path = Path(executable)
        if command_path.is_absolute() or "/" in executable:
            candidate = command_path if command_path.is_absolute() else repo_root / command_path
            candidate = candidate.resolve()
            if not candidate.is_file():
                return "blocked", f"executable path does not name a file: {candidate}", f"Provide an existing executable path for {executable!r}."
            if not os.access(candidate, os.X_OK):
                return "blocked", f"not executable by the current user: {candidate}", f"Grant execute permission to {candidate} for the cron account."
            return "ready", f"executable file exists and is executable by the current user: {candidate}", None

        resolved = shutil.which(executable, path=_runner_path(repo_root))
        if resolved is None:
            return "blocked", f"not found in the runner's PATH from {repo_root}: {executable}", f"Install {executable!r} or configure a path available to cron."
        return "ready", f"found in the runner's PATH from {repo_root}: {resolved}; the cron account's PATH is not checked", None
    except (OSError, RuntimeError) as exc:
        status, detail, action = _path_inspection_failure(
            Path(executable), exc, f"Check the executable path for {executable!r} and rerun readiness."
        )
        return status, detail, action


def _log_directory_status(log_dir: Path, trigger: str) -> tuple[str, str, str | None]:
    log_dir = log_dir.expanduser().resolve()
    log_path = log_dir / f"{trigger}.log"
    if log_dir.exists():
        if not log_dir.is_dir():
            return "blocked", f"log path is not a directory: {log_dir}", f"Choose a directory for --log-dir."
        if os.name == "posix" and stat.S_IMODE(log_dir.stat().st_mode) & 0o077:
            return "blocked", f"existing log directory allows group/other access: {log_dir}", f"Restrict {log_dir} to the owner (mode 700) before running cron."
        if not os.access(log_dir, os.W_OK | os.X_OK):
            return "blocked", f"log directory is not writable/searchable by the current user: {log_dir}", f"Grant the cron account write and search access to {log_dir}."
        if log_path.exists():
            if not log_path.is_file():
                return "blocked", f"log target is not a regular file: {log_path}", f"Move {log_path} and let the runner create its log file."
            if os.name == "posix" and stat.S_IMODE(log_path.stat().st_mode) & 0o077:
                return "blocked", f"existing log file allows group/other access: {log_path}", f"Restrict {log_path} to the owner (mode 600) before running cron."
            if not os.access(log_path, os.W_OK):
                return "blocked", f"log file is not writable by the current user: {log_path}", f"Grant the cron account write access to {log_path}."
        return "ready", f"log directory and target permissions meet the runner's checks for the current user: {log_path}", None

    parent = log_dir.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        return "blocked", f"no writable parent found for log directory: {log_dir}", f"Choose a log directory the cron account can create."
    return "ready", f"directory can be created under the writable parent {parent}; permissions will be checked by the runner", None


def cmd_readiness(
    manifest_path: Path,
    repo_root: Path,
    trigger_filter: str | None,
    log_dir: Path | None,
    script_dir: Path,
) -> int:
    """Report observable pre-activation checks without changing state or running tasks."""

    checks: list[dict[str, str | None]] = []
    try:
        manifest_path = manifest_path.resolve()
        data = load_manifest(manifest_path)
        normalized = validate_manifest(data)
    except ManifestError as exc:
        checks.append(_readiness_item("Manifest", "blocked", str(exc), "Correct the manifest, then rerun readiness."))
        _print_readiness(checks)
        return 0
    except (OSError, RuntimeError) as exc:
        status, detail, action = _path_inspection_failure(
            manifest_path, exc, "Check the manifest path and permissions, then rerun readiness."
        )
        checks.append(_readiness_item("Manifest", status, detail, action))
        _print_readiness(checks)
        return 0
    checks.append(_readiness_item("Manifest", "ready", f"readable and valid: {manifest_path}"))

    try:
        repo_root = repo_root.expanduser().resolve()
        if not repo_root.is_dir():
            checks.append(_readiness_item("Working directory", "blocked", f"not a directory: {repo_root}", "Choose an existing --repo-root directory."))
        elif not os.access(repo_root, os.R_OK | os.X_OK):
            checks.append(_readiness_item("Working directory", "blocked", f"not readable/searchable by the current user: {repo_root}", "Grant the cron account read and search access to the working directory."))
        else:
            checks.append(_readiness_item("Working directory", "ready", f"readable and searchable by the current user: {repo_root}"))
    except (OSError, RuntimeError) as exc:
        status, detail, action = _path_inspection_failure(
            repo_root, exc, "Check the --repo-root path and permissions, then rerun readiness."
        )
        checks.append(_readiness_item("Working directory", status, detail, action))

    try:
        runner = (script_dir / "run_trigger.py").resolve()
        if runner.is_file() and os.access(runner, os.R_OK):
            checks.append(_readiness_item("Cron runner", "ready", f"readable: {runner}"))
        else:
            checks.append(_readiness_item("Cron runner", "blocked", f"missing or unreadable: {runner}", "Restore the cron skill runner file and its read permission."))
    except (OSError, RuntimeError) as exc:
        status, detail, action = _path_inspection_failure(
            script_dir / "run_trigger.py", exc, "Check the installed cron runner path and permissions."
        )
        checks.append(_readiness_item("Cron runner", status, detail, action))

    python3 = shutil.which("python3")
    if python3 is None:
        checks.append(_readiness_item("Cron interpreter", "blocked", "python3 is not found in the current PATH", "Install Python 3 or ensure python3 is on the cron account's PATH."))
    else:
        checks.append(_readiness_item("Cron interpreter", "ready", f"python3 resolves to {python3} in the current PATH; the cron account's PATH is not checked"))

    triggers = normalized["triggers"]
    if trigger_filter is not None:
        try:
            trigger_filter = validate_identifier(trigger_filter, "trigger", MAX_TRIGGER_LEN)
        except ManifestError as exc:
            checks.append(_readiness_item("Trigger selection", "blocked", str(exc), "Use a trigger name from the manifest."))
            _print_readiness(checks)
            return 0
        if trigger_filter not in triggers:
            checks.append(_readiness_item("Trigger selection", "blocked", f"unknown trigger: {trigger_filter}", "Choose a trigger name from the manifest."))
            _print_readiness(checks)
            return 0
        selected_triggers = {trigger_filter: triggers[trigger_filter]}
    else:
        selected_triggers = triggers

    for trigger, config in selected_triggers.items():
        schedule = config["schedule"] if "schedule" in config else f"@reboot after {config['on_boot_delay_minutes']} minutes"
        checks.append(_readiness_item(f"Trigger {trigger}", "ready", f"valid schedule: {schedule}"))
        if log_dir is not None:
            try:
                status, detail, action = _log_directory_status(log_dir, trigger)
            except (OSError, RuntimeError) as exc:
                status, detail, action = _path_inspection_failure(
                    log_dir, exc, "Check the --log-dir path and permissions, then rerun readiness."
                )
            checks.append(_readiness_item(f"Log permissions for {trigger}", status, detail, action))

    selected_tasks = [task for task in normalized["tasks"] if task["trigger"] in selected_triggers]
    if not selected_tasks:
        checks.append(_readiness_item("Tasks", "blocked", "no tasks are configured for the selected trigger(s)", "Add at least one task before preparing this automation."))
    else:
        for task in sorted(selected_tasks, key=lambda item: (item["trigger"], item["sequence_order"])):
            executable = task["argv"][0]
            status, detail, action = _task_executable_status(executable, repo_root)
            checks.append(_readiness_item(f"Task {task['id']} executable", status, detail, action))

    checks.extend(
        [
            _readiness_item(
                "Target cron account and service",
                "unchecked",
                "the intended crontab owner, its PATH, and cron service state are not queried",
                "Run this report as the intended cron account and verify cron is available on the activation host.",
            ),
            _readiness_item(
                "Task-specific requirements and recovery",
                "unchecked",
                "workload dependencies, settings, input/output paths, credentials, permissions, and recovery arrangements are not inferred or tested",
                "Review the task's requirements and recovery needs; verify any needed credentials through the approved channel.",
            ),
        ]
    )
    if log_dir is None:
        checks.append(
            _readiness_item(
                "Durable logs",
                "unchecked",
                "no --log-dir was supplied; cron output may not be retained",
                "If durable logs are needed, choose a protected path and rerun with --log-dir PATH.",
            )
        )

    _print_readiness(checks)
    return 0


def _print_readiness(checks: list[dict[str, str | None]]) -> None:
    statuses = {str(check["status"]) for check in checks}
    overall = "blocked" if "blocked" in statuses else "unchecked" if "unchecked" in statuses else "ready"
    print(f"Automation readiness: {overall}")
    for check in checks:
        print(f"  [{str(check['status']).upper()}] {check['label']}: {check['detail']}")
    actions = list(dict.fromkeys(str(check["next_action"]) for check in checks if check["next_action"]))
    if actions:
        print("Next actions:")
        for action in actions:
            print(f"  - {action}")
    print("Activation state and run history are separate and are not assessed here.")
    print("Read-only report: no task was run, enabled, or installed.")


def cmd_list(manifest_path: Path, trigger_filter: str | None) -> int:
    _, normalized, code = _load_manifest(manifest_path)
    if code != 0:
        return code
    tasks = normalized["tasks"]
    if trigger_filter:
        try:
            trigger_filter = validate_identifier(trigger_filter, "trigger", MAX_TRIGGER_LEN)
        except ManifestError as exc:
            print(f"[cron_ctl] {exc}", file=sys.stderr)
            return 1
        tasks = [task for task in tasks if task["trigger"] == trigger_filter]
    tasks.sort(key=lambda task: (task["trigger"], task["sequence_order"]))
    for task in tasks:
        print(
            f"  {task['id']}  trigger={task['trigger']}  order={task['sequence_order']}  "
            f"enabled={task['enabled']}  command={_command_preview(task['command'])}"
        )
    return 0


def cmd_add_task(manifest_path: Path, trigger: str, command: str, sequence_order: int | None, task_id: str | None) -> int:
    data, normalized, code = _load_manifest(manifest_path)
    if code != 0:
        return code
    try:
        tr = validate_identifier(trigger, "trigger", MAX_TRIGGER_LEN)
        normalize_command(command, "command")
        if task_id:
            tid = validate_identifier(task_id, "id", MAX_ID_LEN)
        else:
            tid = ""
    except ManifestError as exc:
        print(f"[cron_ctl] {exc}", file=sys.stderr)
        return 1
    if tr not in normalized["triggers"]:
        print(f"[cron_ctl] Unknown trigger: {tr}", file=sys.stderr)
        return 1
    tasks = data.get("tasks") or []
    existing_ids = {str(t.get("id", "")) for t in tasks if isinstance(t, dict)}
    if tid:
        if tid in existing_ids:
            print(f"[cron_ctl] Task id already exists: {tid}", file=sys.stderr)
            return 1
    else:
        base = "task"
        n = 1
        while f"{base}-{n}" in existing_ids:
            n += 1
        tid = f"{base}-{n}"
    if sequence_order is None:
        same_trigger = _trigger_tasks(normalized["tasks"], tr)
        sequence_order = max((int(task["sequence_order"]) for task in same_trigger), default=0) + 1
    try:
        sequence_order = normalize_sequence_order(sequence_order, "sequence_order")
    except ManifestError as exc:
        print(f"[cron_ctl] {exc}", file=sys.stderr)
        return 1
    command_text = command.strip()
    if len(command_text) > MAX_CMD_LEN:
        print(f"[cron_ctl] command exceeds {MAX_CMD_LEN} characters", file=sys.stderr)
        return 1
    tasks.append({"id": tid, "trigger": tr, "sequence_order": sequence_order, "command": command_text, "enabled": True})
    data["tasks"] = tasks
    return _save_manifest(manifest_path, data)


def cmd_remove_task(manifest_path: Path, task_id: str | None, trigger: str | None) -> int:
    if bool(task_id) == bool(trigger):
        print("[cron_ctl] Specify exactly one of --id ID or --trigger NAME", file=sys.stderr)
        return 1
    data, _, code = _load_manifest(manifest_path)
    if code != 0:
        return code
    tasks = [task for task in (data.get("tasks") or []) if isinstance(task, dict)]
    if task_id:
        try:
            task_id = validate_identifier(task_id, "id", MAX_ID_LEN)
        except ManifestError as exc:
            print(f"[cron_ctl] {exc}", file=sys.stderr)
            return 1
        tasks = [task for task in tasks if str(task.get("id", "")) != task_id]
        if len(tasks) == len(data.get("tasks") or []):
            print(f"[cron_ctl] No task with id: {task_id}", file=sys.stderr)
            return 1
    else:
        try:
            validated_trigger = validate_identifier(trigger, "trigger", MAX_TRIGGER_LEN)
        except ManifestError as exc:
            print(f"[cron_ctl] {exc}", file=sys.stderr)
            return 1
        tasks = [task for task in tasks if str(task.get("trigger", "")) != validated_trigger]
    data["tasks"] = tasks
    return _save_manifest(manifest_path, data)


def cmd_reorder(manifest_path: Path, trigger: str, order_ids: list[str]) -> int:
    data, normalized, code = _load_manifest(manifest_path)
    if code != 0:
        return code
    try:
        tr = validate_identifier(trigger, "trigger", MAX_TRIGGER_LEN)
        order_ids = [validate_identifier(task_id, "order id", MAX_ID_LEN) for task_id in order_ids if task_id]
    except ManifestError as exc:
        print(f"[cron_ctl] {exc}", file=sys.stderr)
        return 1
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    for task_id in order_ids:
        if task_id in seen_ids and task_id not in duplicate_ids:
            duplicate_ids.append(task_id)
        seen_ids.add(task_id)
    if duplicate_ids:
        print(
            f"[cron_ctl] Duplicate task id(s) in order for trigger {tr}: {', '.join(duplicate_ids)}",
            file=sys.stderr,
        )
        return 1
    if tr not in normalized["triggers"]:
        print(f"[cron_ctl] Unknown trigger: {tr}", file=sys.stderr)
        return 1
    tasks = list(data.get("tasks") or [])
    by_trigger = {t["id"]: t for t in tasks if isinstance(t, dict) and str(t.get("trigger", "")) == tr}
    unknown_ids = [task_id for task_id in order_ids if task_id not in by_trigger]
    if unknown_ids:
        print(f"[cron_ctl] Unknown task id(s) for trigger {tr}: {', '.join(unknown_ids)}", file=sys.stderr)
        return 1
    other = [t for t in tasks if isinstance(t, dict) and t.get("id") not in by_trigger]
    ordered = []
    for i, tid in enumerate(order_ids):
        if tid in by_trigger:
            by_trigger[tid]["sequence_order"] = i + 1
            ordered.append(by_trigger[tid])
    for t in by_trigger.values():
        if t not in ordered:
            ordered.append(t)
    ordered.sort(key=lambda t: int(t.get("sequence_order", 0)))
    data["tasks"] = other + ordered
    return _save_manifest(manifest_path, data)


def cmd_enable_disable(manifest_path: Path, task_id: str, enable: bool) -> int:
    data, _, code = _load_manifest(manifest_path)
    if code != 0:
        return code
    try:
        task_id = validate_identifier(task_id, "id", MAX_ID_LEN)
    except ManifestError as exc:
        print(f"[cron_ctl] {exc}", file=sys.stderr)
        return 1
    tasks = data.get("tasks") or []
    for t in tasks:
        if isinstance(t, dict) and str(t.get("id", "")) == task_id:
            t["enabled"] = enable
            return _save_manifest(manifest_path, data)
    print(f"[cron_ctl] No task with id: {task_id}", file=sys.stderr)
    return 1


def cmd_install(manifest_path: Path, repo_root: Path, output_path: Path | None, log_dir: Path | None, script_dir: Path) -> int:
    _, normalized, code = _load_manifest(manifest_path)
    if code != 0:
        return code
    repo_root = repo_root.resolve()
    if not repo_root.is_dir():
        print(f"[cron_ctl] Not a directory: {repo_root}", file=sys.stderr)
        return 1
    run_trigger = (script_dir / "run_trigger.py").resolve()
    if not run_trigger.is_file():
        print(f"[cron_ctl] Runner not found: {run_trigger}", file=sys.stderr)
        return 1
    manifest_abs = manifest_path.resolve()
    log_dir = log_dir.resolve() if log_dir is not None else None
    lines = [
        "# Generated user-crontab fragment; merge manually after inspecting the current crontab.",
        f"# Repo root: {repo_root}",
        f"# Manifest: {manifest_abs}",
        "",
    ]
    triggers = normalized["triggers"]
    for name, cfg in triggers.items():
        if "schedule" in cfg:
            lines.append(f"# Trigger: {name}")
            lines.append(f"{cfg['schedule']}\t{_runner_command(run_trigger, manifest_abs, repo_root, name, log_dir=log_dir)}")
            lines.append("")
        elif "on_boot_delay_minutes" in cfg:
            delay = cfg.get("on_boot_delay_minutes", 0)
            lines.append(f"# Trigger: {name} (after boot, delay {delay} min)")
            lines.append(f"@reboot\t{_runner_command(run_trigger, manifest_abs, repo_root, name, delay * 60, log_dir=log_dir)}")
            lines.append("")
    out = "\n".join(lines)
    if output_path:
        try:
            output_path = Path(output_path).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(out, encoding="utf-8")
        except OSError as exc:
            print(f"[cron_ctl] Failed to write output: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote {output_path}")
    else:
        print(out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage cron tasks from a manifest.")
    parser.add_argument("--manifest", default=MANIFEST_DEFAULT, help="Path to manifest.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="Validate manifest")
    p_validate.set_defaults(fn=lambda a: cmd_validate(Path(a.manifest)))

    p_readiness = sub.add_parser("readiness", help="Read-only automation readiness report")
    p_readiness.add_argument("--repo-root", default=".", help="Working directory used by scheduled tasks (default: cwd)")
    p_readiness.add_argument("--trigger", help="Assess one trigger (default: all triggers)")
    p_readiness.add_argument("--log-dir", help="Check existing or creatable protected log path without changing it")
    p_readiness.set_defaults(
        fn=lambda a: cmd_readiness(
            Path(a.manifest),
            Path(a.repo_root),
            getattr(a, "trigger", None),
            getattr(a, "log_dir", None) and Path(a.log_dir),
            Path(__file__).resolve().parent,
        )
    )

    p_list = sub.add_parser("list", help="List tasks (optionally for one trigger)")
    p_list.add_argument("--trigger", help="Filter by trigger name")
    p_list.set_defaults(fn=lambda a: cmd_list(Path(a.manifest), getattr(a, "trigger", None)))

    p_add = sub.add_parser("add-task", help="Add a task")
    p_add.add_argument("--trigger", required=True)
    p_add.add_argument("--command", required=True)
    p_add.add_argument("--sequence-order", type=int, default=None)
    p_add.add_argument("--id", dest="task_id", default=None)
    p_add.set_defaults(fn=lambda a: cmd_add_task(Path(a.manifest), a.trigger, a.command, getattr(a, "sequence_order", None), getattr(a, "task_id", None)))

    p_remove = sub.add_parser("remove-task", help="Remove task(s)")
    remove_selector = p_remove.add_mutually_exclusive_group(required=True)
    remove_selector.add_argument("--id", dest="task_id")
    remove_selector.add_argument("--trigger", help="Remove all tasks for this trigger")
    p_remove.set_defaults(fn=lambda a: cmd_remove_task(Path(a.manifest), getattr(a, "task_id", None), getattr(a, "trigger", None)))

    p_reorder = sub.add_parser("reorder", help="Reorder tasks for a trigger")
    p_reorder.add_argument("--trigger", required=True)
    p_reorder.add_argument("--order", required=True, help="Comma-separated task ids in desired order")
    p_reorder.set_defaults(fn=lambda a: cmd_reorder(Path(a.manifest), a.trigger, [x.strip() for x in a.order.split(",")]))

    p_enable = sub.add_parser("enable", help="Enable a task")
    p_enable.add_argument("--id", dest="task_id", required=True)
    p_enable.set_defaults(fn=lambda a: cmd_enable_disable(Path(a.manifest), a.task_id, True))

    p_disable = sub.add_parser("disable", help="Disable a task")
    p_disable.add_argument("--id", dest="task_id", required=True)
    p_disable.set_defaults(fn=lambda a: cmd_enable_disable(Path(a.manifest), a.task_id, False))

    p_install = sub.add_parser("install", help="Generate crontab fragment")
    p_install.add_argument("--repo-root", default=".", help="Repo root (default: cwd)")
    p_install.add_argument("--output", default=None, help="Write to file (default: stdout)")
    p_install.add_argument("--log-dir", default=None, help="Pass durable log directory to generated run_trigger.py commands")
    p_install.set_defaults(
        fn=lambda a: cmd_install(
            Path(a.manifest),
            Path(a.repo_root),
            getattr(a, "output", None) and Path(a.output),
            getattr(a, "log_dir", None) and Path(a.log_dir),
            Path(__file__).resolve().parent,
        )
    )

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
