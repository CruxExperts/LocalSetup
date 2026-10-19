"""Request-owned pane tasks with durable local or explicitly relayed receipts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import time
from typing import Any, Mapping, Sequence
import uuid

from .errors import HerdrOpsError
from .layout import cleanup_owned_pane, create_owned_pane, owned_pane
from .transport import (
    atomic_write_json,
    atomic_write_bytes,
    bounded_wait,
    owner_id as validate_owner_id,
    read_json_state,
    request,
    state_directory,
    verify_local_herdr_endpoint,
    writer_lock,
)


MAX_COMMAND_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_READ_LINES = 200
POLL_INTERVAL = 0.2
SHELL_READY_TIMEOUT = 10.0
TASK_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_REMOTE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}$")


def _task_path(root: Path, task_id: str) -> Path:
    return root / "tasks" / f"{validate_owner_id(task_id)}.json"


def _load_task(root: Path, task_id: str) -> dict[str, Any]:
    record = read_json_state(_task_path(root, task_id))
    if record.get("task_id") != task_id:
        raise HerdrOpsError("ownership_mismatch", "Task receipt does not match the requested operation.")
    return record


def _save_task(root: Path, record: dict[str, Any]) -> None:
    atomic_write_json(_task_path(root, str(record["task_id"])), record)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise HerdrOpsError("command_required", "Provide a non-empty argv command after `--`.")
    if any(not isinstance(arg, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in arg) for arg in argv):
        raise HerdrOpsError("invalid_command", "Command arguments must be single-line strings without control characters.")
    command = tuple(argv)
    if not command[0].strip():
        raise HerdrOpsError("invalid_command", "The command name cannot be empty.")
    if len(shlex.join(command).encode("utf-8")) > MAX_COMMAND_BYTES:
        raise HerdrOpsError("command_too_large", "The encoded command exceeds the 64 KiB limit.")
    return command


def _remote_receipt_store(ssh_target: str | None, remote_state_dir: str | None) -> dict[str, str] | None:
    if ssh_target is None and remote_state_dir is None:
        return None
    if not isinstance(ssh_target, str) or not _REMOTE_TARGET.fullmatch(ssh_target):
        raise HerdrOpsError("invalid_ssh_target", "Remote task receipts require one explicit safe SSH target alias.")
    if not isinstance(remote_state_dir, str) or any(ord(ch) < 32 for ch in remote_state_dir):
        raise HerdrOpsError("remote_state_dir_required", "Provide an absolute remote state directory with the SSH target.")
    path = PurePosixPath(remote_state_dir)
    if not path.is_absolute() or ".." in path.parts or str(path) == "/":
        raise HerdrOpsError("invalid_remote_state_dir", "Remote receipt state must be a non-root absolute POSIX path without parent traversal.")
    return {"kind": "ssh", "target": ssh_target, "state_dir": str(path)}


def _runner_script(argv: Sequence[str], cwd: str, task_id: str, nonce: str, receipt_path: str, marker: str) -> bytes:
    command = " ".join(shlex.quote(part) for part in argv)
    cd = shlex.quote(cwd)
    receipt = shlex.quote(receipt_path)
    marker_q = shlex.quote(marker)
    # The command runs in a subshell so shell builtins such as `exit` cannot
    # terminate the receipt writer. The parent waits for its real status.
    script = f"""#!/bin/bash
set +e
umask 077
(
  cd -- {cd} || exit $?
  command {command}
) &
__lsherdr_child=$!
wait "$__lsherdr_child"
__lsherdr_rc=$?
__lsherdr_tmp={receipt}.tmp.${{BASHPID}}
printf '{{"schema_version":1,"task_id":"{task_id}","nonce":"{nonce}","exit_code":%d}}\\n' "$__lsherdr_rc" > "$__lsherdr_tmp" || exit 125
chmod 600 "$__lsherdr_tmp" || exit 125
mv -f "$__lsherdr_tmp" {receipt} || exit 125
printf '\\n%s:%d\\n' {marker_q} "$__lsherdr_rc"
exit "$__lsherdr_rc"
"""
    return script.encode("utf-8")


def _store_local_runner(root: Path, task_id: str, script: bytes) -> tuple[str, str]:
    directory = root / "receipts"
    if directory.is_symlink():
        raise HerdrOpsError("state_dir_unavailable", "The task receipt directory must not be a symlink.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    runner = directory / f"{task_id}.sh"
    receipt = directory / f"{task_id}.json"
    if runner.exists() or receipt.exists() or runner.is_symlink() or receipt.is_symlink():
        raise HerdrOpsError("operation_state_conflict", "Request-specific task files already exist; refusing to overwrite them.")
    atomic_write_bytes(runner, script)
    return str(runner), str(receipt)


def _ssh_run(target: str, argv: Sequence[str], *, stdin: bytes | None = None, timeout: float = 30.0) -> subprocess.CompletedProcess[bytes]:
    try:
        module = __import__("ls.core.herdr_ops.remote", fromlist=["ssh_run"])
        operation = getattr(module, "ssh_run")
    except (ImportError, AttributeError):
        raise HerdrOpsError("remote_receipt_transport_unavailable", "The qualified SSH receipt transport is unavailable.") from None
    try:
        return operation(target, argv, stdin=stdin, timeout=timeout)
    except HerdrOpsError:
        raise
    except (OSError, subprocess.SubprocessError, TimeoutError):
        raise HerdrOpsError("remote_receipt_transport_unknown", "Remote receipt transport failed; reconcile before retrying.", "unknown") from None


def _store_remote_runner(store: Mapping[str, str], task_id: str, script: bytes) -> tuple[str, str]:
    directory = PurePosixPath(store["state_dir"]) / task_id
    runner = str(directory / "runner.sh")
    receipt = str(directory / "receipt.json")
    temporary = str(directory / ".runner.sh.tmp")
    shell = " ".join(
        (
            "umask 077;",
            "mkdir -p -m 700", shlex.quote(str(directory)), "&&",
            "tmp=" + shlex.quote(temporary) + ";",
            "cat > \"$tmp\" && chmod 600 \"$tmp\" && mv -f \"$tmp\"", shlex.quote(runner),
        )
    )
    completed = _ssh_run(store["target"], ["bash", "-c", shell], stdin=script)
    if completed.returncode != 0:
        raise HerdrOpsError("remote_runner_stage_failed", "Could not stage the request-specific runner through the selected SSH route.")
    return runner, receipt


def _read_receipt(record: Mapping[str, Any]) -> dict[str, Any] | None:
    task_id = str(record["task_id"])
    transport = record.get("receipt_transport")
    if not isinstance(transport, dict):
        raise HerdrOpsError("state_invalid", "Task state has no receipt transport record.")
    if transport.get("kind") == "local":
        path = Path(str(transport.get("receipt_path", "")))
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise HerdrOpsError("receipt_read_failed", "Could not read the task's private exit receipt.") from None
    elif transport.get("kind") == "ssh":
        target = transport.get("target")
        path = transport.get("receipt_path")
        if not isinstance(target, str) or not isinstance(path, str):
            raise HerdrOpsError("state_invalid", "Task state has an incomplete remote receipt transport record.")
        from .remote import _qualify_ssh_target
        if not isinstance(transport.get("remote_socket"), str) or _qualify_ssh_target(target) != transport.get("ssh_settings"):
            raise HerdrOpsError("receipt_route_mismatch", "The remote receipt route no longer matches the pane relay.", "unknown")
        try:
            completed = _ssh_run(target, ["cat", path], timeout=15)
        except HerdrOpsError as exc:
            if exc.effect == "unknown":
                return None
            raise
        if completed.returncode != 0:
            return None
        raw = completed.stdout
    else:
        raise HerdrOpsError("state_invalid", "Task receipt transport kind is unsupported.")
    if len(raw) > MAX_RECEIPT_BYTES:
        raise HerdrOpsError("receipt_invalid", "Task exit receipt exceeded the size limit.")
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise HerdrOpsError("receipt_invalid", "Task exit receipt is malformed.") from None
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("task_id") != task_id
        or receipt.get("nonce") != record.get("nonce")
        or isinstance(receipt.get("exit_code"), bool)
        or not isinstance(receipt.get("exit_code"), int)
        or not 0 <= receipt["exit_code"] <= 255
    ):
        raise HerdrOpsError("receipt_invalid", "Task exit receipt does not match this request.")
    return receipt


def _send(endpoint: str, pane_id: str, text: str) -> dict[str, Any]:
    return request(endpoint, "pane.send_input", {"pane_id": pane_id, "text": text, "keys": ["enter"]}, mutation=True)


def _wait_for_controlled_shell(endpoint: str, pane_id: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        result = request(endpoint, "pane.process_info", {"pane_id": pane_id})
        info = result.get("process_info")
        foreground = info.get("foreground_processes") if isinstance(info, dict) else None
        shell_pid = info.get("shell_pid") if isinstance(info, dict) else None
        if isinstance(shell_pid, int) and isinstance(foreground, list):
            bash = any(
                isinstance(item, dict)
                and item.get("pid") == shell_pid
                and (Path(str(item.get("name", ""))).name == "bash" or Path(str(item.get("argv0", ""))).name == "bash")
                for item in foreground
            )
            if bash:
                return
        time.sleep(POLL_INTERVAL)
    raise HerdrOpsError("controlled_shell_timeout", "The new pane did not reach the controlled Bash shell in time.", "unknown")


def _has_exact_line(text: str, marker: str) -> bool:
    return any(line.strip() == marker for line in text.splitlines())


def _ready_handshake(endpoint: str, pane_id: str, task_id: str) -> None:
    marker = f"__LSHERDR_READY_{task_id}__"
    _send(endpoint, pane_id, f"printf '%s\\n' {shlex.quote(marker)}")
    deadline = time.monotonic() + SHELL_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            result = request(endpoint, "pane.read", {"pane_id": pane_id, "source": "recent", "lines": MAX_READ_LINES})
        except HerdrOpsError:
            time.sleep(POLL_INTERVAL)
            continue
        read = result.get("read")
        text = read.get("text") if isinstance(read, dict) else None
        if isinstance(text, str) and _has_exact_line(text, marker):
            return
        time.sleep(POLL_INTERVAL)
    raise HerdrOpsError("controlled_shell_timeout", "The controlled Bash readiness marker was not observed.", "unknown")


def run_task(
    endpoint: str,
    workspace_id: str,
    caller_pane_id: str,
    cwd: str | os.PathLike[str],
    argv: Sequence[str],
    *,
    state_dir: str | os.PathLike[str] | None = None,
    wait_seconds: float = 0,
    ssh_target: str | None = None,
    remote_state_dir: str | None = None,
) -> dict[str, Any]:
    """Create a dedicated lower pane and submit one argv command exactly once."""
    command = _validate_argv(argv)
    wait_for = bounded_wait(wait_seconds)
    root = state_directory(state_dir)
    remote_store = _remote_receipt_store(ssh_target, remote_state_dir)
    task_id = uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    marker = f"__LSHERDR_EXIT_{nonce}__"
    workdir = Path(cwd).expanduser()
    if not workdir.is_absolute() or any(ord(ch) < 32 or ord(ch) == 127 for ch in str(workdir)):
        raise HerdrOpsError("invalid_cwd", "Task execution requires an explicit absolute working directory.")
    command_hash = hashlib.sha256(shlex.join(command).encode("utf-8")).hexdigest()
    task: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "owner_id": task_id,
        "endpoint": str(Path(endpoint).expanduser().absolute()),
        "workspace_id": workspace_id,
        "caller_pane_id": caller_pane_id,
        "cwd": str(workdir),
        "argv_sha256": command_hash,
        "nonce": nonce,
        "marker": marker,
        "state": "preparing",
        "created_at": _now(),
    }
    with writer_lock(root):
        _save_task(root, task)
        try:
            if remote_store is None:
                verify_local_herdr_endpoint(endpoint)
                receipt_dir = root / "receipts"
                runner_path = str(receipt_dir / f"{task_id}.sh")
                receipt_path = str(receipt_dir / f"{task_id}.json")
                script = _runner_script(command, str(workdir), task_id, nonce, receipt_path, marker)
                runner_path, receipt_path = _store_local_runner(root, task_id, script)
                task["receipt_transport"] = {"kind": "local", "receipt_path": receipt_path}
            else:
                from .remote import verify_receipt_tunnel
                route = verify_receipt_tunnel(endpoint, remote_store["target"], state_dir=root)
                receipt_path = str(PurePosixPath(remote_store["state_dir"]) / task_id / "receipt.json")
                runner_path = str(PurePosixPath(remote_store["state_dir"]) / task_id / "runner.sh")
                script = _runner_script(command, str(workdir), task_id, nonce, receipt_path, marker)
                task["receipt_transport"] = {"kind": "ssh", "target": remote_store["target"],
                                             "state_dir": remote_store["state_dir"], "receipt_path": receipt_path,
                                             "remote_socket": route["remote_socket"],
                                             "tunnel_owner": route["tunnel_owner"],
                                             "ssh_settings": route["ssh_settings"]}
                task["runner_path"] = runner_path
                task["state"] = "staging"
                _save_task(root, task)
                _store_remote_runner(remote_store, task_id, script)
        except HerdrOpsError as exc:
            task.update({"state": "unknown" if exc.effect == "unknown" else "failed", "error": exc.as_dict(), "updated_at": _now()})
            _save_task(root, task)
            return {"task_id": task_id, "owner_id": task_id, "state": task["state"], "error": exc.as_dict(), "effect": exc.effect}
        task["runner_path"] = runner_path
        task["state"] = "prepared"
        task["updated_at"] = _now()
        _save_task(root, task)
        try:
            created = create_owned_pane(
                endpoint,
                workspace_id,
                caller_pane_id,
                workdir,
                state_dir=root,
                owner_id=task_id,
                purpose="task",
            )
        except HerdrOpsError as exc:
            task.update({"state": "unknown" if exc.effect == "unknown" else "failed", "error": exc.as_dict(), "updated_at": _now()})
            _save_task(root, task)
            return {"task_id": task_id, "owner_id": task_id, "state": task["state"], "error": exc.as_dict(), "effect": exc.effect}
        pane = created["pane"]
        task.update({"pane_id": pane["pane_id"], "terminal_id": pane["terminal_id"], "tab_id": pane.get("tab_id"), "state": "prepared", "updated_at": _now()})
        _save_task(root, task)
        try:
            _send(endpoint, pane["pane_id"], "exec /bin/bash --noprofile --norc")
            _wait_for_controlled_shell(endpoint, pane["pane_id"], time.monotonic() + SHELL_READY_TIMEOUT)
            _ready_handshake(endpoint, pane["pane_id"], task_id)
        except HerdrOpsError as exc:
            task.update({"state": "unknown" if exc.effect == "unknown" else "failed", "error": exc.as_dict(), "updated_at": _now()})
            _save_task(root, task)
            if exc.effect == "none":
                cleanup_owned_pane(endpoint, task_id, state_dir=root, allow_task_cancel=True)
            return {"task_id": task_id, "owner_id": task_id, "state": task["state"], "error": exc.as_dict(), "effect": exc.effect}
        try:
            admission = _send(endpoint, pane["pane_id"], f"/bin/bash --noprofile --norc {shlex.quote(runner_path)}")
        except HerdrOpsError as exc:
            task.update({"state": "unknown" if exc.effect == "unknown" else "failed", "error": exc.as_dict(), "updated_at": _now()})
            _save_task(root, task)
            if exc.effect != "unknown":
                cleanup_owned_pane(endpoint, task_id, state_dir=root, allow_task_cancel=True)
            return {"task_id": task_id, "owner_id": task_id, "state": task["state"], "error": exc.as_dict(), "effect": exc.effect}
        task.update({"state": "running", "admission": admission, "dispatched_at": _now(), "updated_at": _now()})
        try:
            _save_task(root, task)
        except HerdrOpsError:
            raise HerdrOpsError("receipt_write_uncertain", "Input was admitted, but the task receipt could not be updated. Reconcile before retrying.", "unknown") from None
        result = {"task_id": task_id, "owner_id": task_id, "state": "running", "admission": admission, "completion": "pending"}
    if wait_for:
        return wait_task(endpoint, task_id, timeout=wait_for, state_dir=root)
    return result


def _refresh_task(endpoint: str, root: Path, task: dict[str, Any]) -> dict[str, Any]:
    if task.get("state") in TASK_TERMINAL_STATES:
        return task
    if task.get("state") not in {"running", "prepared", "unknown"}:
        return task
    try:
        receipt = _read_receipt(task)
    except HerdrOpsError as exc:
        task.update({"state": "unknown", "error": exc.as_dict(), "updated_at": _now()})
        _save_task(root, task)
        return task
    if receipt is not None:
        code = receipt["exit_code"]
        public_receipt = {"kind": "atomic-exit", "task_id": task["task_id"], "exit_code": code}
        public_receipt["nonce"] = task["nonce"]
        task.update({"state": "completed" if code == 0 else "failed", "exit_code": code, "receipt": public_receipt, "completed_at": _now(), "updated_at": _now()})
        _save_task(root, task)
        return task
    try:
        _owner, pane = owned_pane(endpoint, str(task["task_id"]), state_dir=root)
    except HerdrOpsError:
        task.update({"state": "unknown", "updated_at": _now()})
        _save_task(root, task)
        return task
    if pane is None:
        task.update({"state": "unknown", "updated_at": _now()})
        _save_task(root, task)
        return task
    task.update({"pane_id": pane["pane_id"], "workspace_id": pane["workspace_id"], "tab_id": pane.get("tab_id")})
    task["state"] = "unknown" if task.get("state") == "unknown" else "running"
    task["updated_at"] = _now()
    _save_task(root, task)
    return task


def task_status(endpoint: str, task_id: str, *, state_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    identity = validate_owner_id(task_id)
    root = state_directory(state_dir)
    with writer_lock(root):
        task = _load_task(root, identity)
        if task.get("endpoint") != str(Path(endpoint).expanduser().absolute()):
            raise HerdrOpsError("endpoint_mismatch", "The task belongs to a different Herdr endpoint.")
        task = _refresh_task(endpoint, root, task)
        return _public_task(task)


def wait_task(endpoint: str, task_id: str, *, timeout: float = 60, state_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    duration = bounded_wait(timeout)
    deadline = time.monotonic() + duration
    root = state_directory(state_dir)
    identity = validate_owner_id(task_id)
    with writer_lock(root):
        task = _load_task(root, identity)
        if task.get("endpoint") != str(Path(endpoint).expanduser().absolute()):
            raise HerdrOpsError("endpoint_mismatch", "The task belongs to a different Herdr endpoint.")
        remote_receipt = isinstance(task.get("receipt_transport"), dict) and task["receipt_transport"].get("kind") == "ssh"
        task = _refresh_task(endpoint, root, task)
        public = _public_task(task)
    if remote_receipt:
        if task.get("state") in TASK_TERMINAL_STATES or time.monotonic() >= deadline:
            return public
        # A remote receipt must not trigger an SSH read every short poll. Wait
        # on the unique pane marker as a wake-up signal, then verify the actual
        # atomic receipt once through the same SSH route.
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait_ms = min(20_000, max(1, int(remaining * 1000)))
            result = request(
                endpoint,
                "pane.wait_for_output",
                {
                    "pane_id": task["pane_id"],
                    "source": "recent",
                    "match": {"type": "substring", "value": str(task["marker"])},
                    "lines": MAX_READ_LINES,
                    "timeout_ms": wait_ms,
                },
                timeout=min(30.0, wait_ms / 1000 + 2.0),
            )
            matched_line = result.get("matched_line")
            if isinstance(matched_line, str) and str(task["marker"]) in matched_line:
                with writer_lock(root):
                    task = _load_task(root, identity)
                    task = _refresh_task(endpoint, root, task)
                    return _public_task(task)
        return public
    while True:
        with writer_lock(root):
            task = _load_task(root, identity)
            if task.get("endpoint") != str(Path(endpoint).expanduser().absolute()):
                raise HerdrOpsError("endpoint_mismatch", "The task belongs to a different Herdr endpoint.")
            task = _refresh_task(endpoint, root, task)
            public = _public_task(task)
        if task.get("state") in TASK_TERMINAL_STATES or time.monotonic() >= deadline:
            return public
        time.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.monotonic())))


def cancel_task(endpoint: str, task_id: str, *, state_dir: str | os.PathLike[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    identity = validate_owner_id(task_id)
    root = state_directory(state_dir, create=not dry_run)
    with writer_lock(root) if not dry_run else _read_lock(root):
        task = _load_task(root, identity)
        if task.get("endpoint") != str(Path(endpoint).expanduser().absolute()):
            raise HerdrOpsError("endpoint_mismatch", "The task belongs to a different Herdr endpoint.")
        if task.get("state") in TASK_TERMINAL_STATES:
            return _public_task(task)
        if dry_run:
            return {"task_id": identity, "state": task.get("state"), "pane_id": task.get("pane_id"), "planned": "close-exact-owned-task-pane", "changed": False}
        task = _refresh_task(endpoint, root, task)
        if task.get("state") in TASK_TERMINAL_STATES:
            return _public_task(task)
        try:
            closed = cleanup_owned_pane(endpoint, identity, state_dir=root, allow_task_cancel=True)
        except HerdrOpsError as exc:
            task.update({"state": "unknown" if exc.effect == "unknown" else task.get("state"), "error": exc.as_dict(), "updated_at": _now()})
            _save_task(root, task)
            return {"task_id": identity, "state": task["state"], "error": exc.as_dict(), "effect": exc.effect}
        if closed.get("state") == "closed":
            task.update({"state": "unknown", "cancellation": "owned-pane-close-confirmed; exit receipt pending", "updated_at": _now()})
        else:
            task.update({"state": "unknown", "updated_at": _now()})
        _save_task(root, task)
        return _public_task(task)


def cleanup_task(endpoint: str, task_id: str, *, state_dir: str | os.PathLike[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Close a completed task pane while preserving its real exit outcome."""
    identity = validate_owner_id(task_id)
    root = state_directory(state_dir, create=not dry_run)
    context = _read_lock(root) if dry_run else writer_lock(root)
    with context:
        task = _load_task(root, identity)
        if task.get("endpoint") != str(Path(endpoint).expanduser().absolute()):
            raise HerdrOpsError("endpoint_mismatch", "The task belongs to a different Herdr endpoint.")
        if dry_run:
            preview = task
            if task.get("state") not in {"completed", "failed"} or not isinstance(task.get("receipt"), dict):
                receipt = _read_receipt(task)
                if receipt is None:
                    raise HerdrOpsError("task_active", "The task has no verified terminal exit receipt; cleanup is not planned.")
                code = receipt["exit_code"]
                preview = dict(task)
                preview.update({
                    "state": "completed" if code == 0 else "failed",
                    "exit_code": code,
                    "receipt": {"kind": "atomic-exit", "task_id": identity, "nonce": task["nonce"], "exit_code": code},
                })
            return {**_public_task(preview), "planned": "close-completed-task-pane", "changed": False}
        task = _refresh_task(endpoint, root, task)
        if task.get("state") not in {"completed", "failed"} or not isinstance(task.get("receipt"), dict):
            raise HerdrOpsError("task_active", "The task has no verified terminal exit receipt; use its explicit cancel operation or reconcile it first.")
        closed = cleanup_owned_pane(endpoint, identity, state_dir=root)
        return {**_public_task(task), "pane_cleanup": closed}


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    fields = ("task_id", "owner_id", "state", "workspace_id", "pane_id", "exit_code", "receipt", "error", "created_at", "updated_at", "cancellation")
    result = {key: task[key] for key in fields if key in task}
    if task.get("state") in {"running", "prepared", "unknown"}:
        result["completion"] = "pending" if task.get("state") != "unknown" else "unknown"
    elif task.get("state") in {"completed", "failed"}:
        result["completion"] = "confirmed" if isinstance(task.get("receipt"), dict) else "not_started"
    return result


class _read_lock:
    """No-op context for dry-run reads that must not create lock/state files."""

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self) -> Path:
        return self.root

    def __exit__(self, *_: object) -> None:
        return None
