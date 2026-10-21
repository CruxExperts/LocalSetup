"""Controller-owned tmux pane route over the shared terminal lifecycle."""

from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from typing import Any, Sequence
import uuid

from ls.core.terminal_ops import FilePaneStore, ManagedPaneService, PaneError, PaneRequest, PaneTransition, TmuxAdapter
from ls.core.terminal_ops.runtime import run_visible_sudo
from ls.core.terminal_ops.credentials import child_environment
from ls.core.terminal_ops.shell import _read_operation, machine_generation


_STATUS_HANDLE_PREFIX = "lsop1."


def _status_handle(binding_key: str, operation_id: str) -> str:
    return f"{_STATUS_HANDLE_PREFIX}{binding_key}.{operation_id}"


def _decode_status_handle(handle: str) -> tuple[str, str]:
    if not isinstance(handle, str) or not handle.startswith(_STATUS_HANDLE_PREFIX):
        raise PaneError("invalid_status_handle", "The durable operation status handle is invalid.")
    encoded = handle[len(_STATUS_HANDLE_PREFIX):]
    if encoded.count(".") != 1:
        raise PaneError("invalid_status_handle", "The durable operation status handle is invalid.")
    binding_key, operation_id = encoded.split(".", 1)
    if (not isinstance(binding_key, str) or len(binding_key) != 64
            or any(char not in "0123456789abcdef" for char in binding_key)
            or not isinstance(operation_id, str) or not operation_id
            or len(operation_id) > 128
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in operation_id)):
        raise PaneError("invalid_status_handle", "The durable operation status handle is invalid.")
    return binding_key, operation_id


def _existing_private_root(override: str | None = None) -> Path:
    root = Path(override or os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    if override is None:
        root = root / "localsetup" / "terminal-ops"
    root = root.expanduser().absolute()
    try:
        info = root.lstat()
    except OSError as exc:
        raise PaneError("state_unavailable", "The selected operation state root does not exist.") from exc
    if (root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise PaneError("unsafe_state_dir", "The selected operation state root must already be owner-private.")
    return root


def _read_handle_receipt(root: Path, binding_key: str, operation_id: str) -> dict[str, Any]:
    ops_root = root / "terminal_ops"
    binding_dir = ops_root / binding_key
    path = binding_dir / f"{operation_id}.json"
    for directory in (ops_root, binding_dir):
        try:
            info = directory.lstat()
        except OSError as exc:
            raise PaneError("operation_not_found", "The durable operation receipt is unavailable.") from exc
        # The shared terminal_ops parent may predate private binding-directory
        # creation and be searchable by other users. Its children and receipt
        # files stay private; reject unsafe writes and links without requiring
        # a mutating chmod during read-only status reconciliation.
        unsafe_mode = info.st_mode & (0o022 if directory == ops_root else 0o077)
        if (directory.is_symlink() or not directory.is_dir() or info.st_uid != os.getuid()
                or unsafe_mode):
            raise PaneError("unsafe_state_dir", "The durable operation receipt is not owner-private.")
    try:
        info = path.lstat()
        if (path.is_symlink() or not path.is_file() or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 1_100_000):
            raise PaneError("unsafe_state_file", "The durable operation receipt is not a bounded owner-private file.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except PaneError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise PaneError("state_unavailable", "The durable operation receipt could not be read safely.") from exc
    if (not isinstance(payload, dict) or payload.get("binding_key") != binding_key
            or payload.get("operation_id") != operation_id):
        raise PaneError("binding_mismatch", "The durable operation receipt does not match its status handle.")
    return payload


def _status_by_handle(handle: str, *, state_dir: str | None, wait: bool, timeout: float) -> dict[str, Any]:
    binding_key, operation_id = _decode_status_handle(handle)
    root = _existing_private_root(state_dir)
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout < 0):
        raise PaneError("invalid_timeout", "The status wait timeout must be zero or positive.")
    deadline = time.monotonic() + timeout
    while True:
        receipt = _read_handle_receipt(root, binding_key, operation_id)
        state = receipt.get("state")
        if not wait or state not in {"staged", "running", "claimed"} or time.monotonic() >= deadline:
            payload = {
                "api_version": 1,
                "binding_key": binding_key,
                "operation_id": operation_id,
                "run_id": operation_id,
                "status_handle": handle,
                "state": state if state in {"staged", "running", "claimed", "completed", "unknown"} else "unknown",
                "status": "running" if state in {"staged", "running", "claimed"} else state or "unknown",
                "target": receipt.get("pane_id"),
                "detail": receipt.get("detail"),
                "tail": "",
            }
            if state == "completed":
                payload["exit_code"] = receipt.get("exit_code")
            sudo_path = root / "terminal_ops" / binding_key / f"{operation_id}.sudo.json"
            try:
                info = sudo_path.lstat()
                if (sudo_path.is_symlink() or not sudo_path.is_file() or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > 65536):
                    raise PaneError("unsafe_state_file", "The sudo receipt is not a bounded owner-private file.")
                sudo = json.loads(sudo_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, PaneError):
                sudo = None
            if isinstance(sudo, dict):
                payload["sudo"] = sudo
            if wait and payload["status"] == "running":
                payload["timed_out"] = True
            return payload
        time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))


def _private_root(override: str | None = None) -> Path:
    root = Path(override or os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    if override is None:
        root = root / "localsetup" / "terminal-ops"
    root = root.expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise PaneError("unsafe_state_dir", "The selected pane state root must be owner-private.")
    root.chmod(0o700)
    return root


def _caller_identity(pane_pid: int, tty: str) -> str:
    """Prove the native pane contains this caller and bind its live foreground owner."""
    def process(pid: int) -> tuple[list[str], int]:
        info = Path(f"/proc/{pid}").stat()
        if info.st_uid != os.getuid():
            raise ValueError("different process owner")
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 1:].split()
        return fields, int(fields[1])
    try:
        ancestors: dict[int, list[str]] = {}
        pid = os.getpid()
        for _ in range(128):
            if pid <= 1 or pid in ancestors:
                break
            fields, parent = process(pid)
            ancestors[pid] = fields
            pid = parent
        if pane_pid not in ancestors:
            raise ValueError("caller is outside pane")
        native_tty = os.stat(tty)
        shell_tty = os.stat(f"/proc/{pane_pid}/fd/0")
        if not stat.S_ISCHR(native_tty.st_mode) or shell_tty.st_rdev != native_tty.st_rdev:
            raise ValueError("pane tty mismatch")
        leader = int(ancestors[pane_pid][5])
        if leader not in ancestors:
            raise ValueError("foreground leader is not caller ancestor")
        # The invoker survives a one-shot CLI process; an agent foreground
        # ancestor owns its pane across its separate command invocations.
        owner = os.getppid() if leader == os.getpid() else leader
        if owner not in ancestors or owner == pane_pid:
            raise ValueError("live invoker unavailable")
        return f"{owner}:{ancestors[owner][19]}"
    except (OSError, ValueError, IndexError) as exc:
        raise PaneError("caller_unverified", "The invoking controller is not in the exact native pane foreground ancestry.") from exc


def _tmux_context() -> dict[str, str]:
    parent_id = os.environ.get("TMUX_PANE", "")
    tmux_env = os.environ.get("TMUX", "")
    if not parent_id.startswith("%") or not parent_id[1:].isdigit() or not tmux_env:
        raise PaneError("caller_missing", "Run this command from the actual controller tmux pane with TMUX_PANE and TMUX set.")
    endpoint = Path(tmux_env.split(",", 1)[0])
    try:
        info = endpoint.stat()
    except OSError as exc:
        raise PaneError("endpoint_missing", "The controller tmux socket is unavailable.") from exc
    if not endpoint.is_absolute() or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PaneError("endpoint_unverified", "The controller tmux socket is not an exact owned endpoint.")
    binary = shutil.which("tmux")
    if binary is None:
        raise PaneError("tmux_missing", "The tmux executable is unavailable.")
    result = subprocess.run(
        [binary, "-S", str(endpoint), "display-message", "-t", parent_id, "-p", "-F",
         "#{pane_id}\t#{session_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_tty}"],
        capture_output=True, text=True, timeout=5, check=False,
        env=child_environment(os.environ),
    )
    parts = result.stdout.rstrip("\n").split("\t")
    if result.returncode != 0 or len(parts) != 5 or parts[0] != parent_id or not parts[1].startswith("$"):
        raise PaneError("parent_unverified", "The exact controller pane is unavailable at this tmux endpoint.")
    if not parts[2].isdigit() or not Path(parts[3]).is_absolute() or not parts[4].startswith("/dev/"):
        raise PaneError("parent_unverified", "The controller pane process, directory, or terminal is unverified.")
    try:
        process = Path(f"/proc/{parts[2]}/stat").read_text(encoding="ascii")
        start_time = process[process.rfind(")") + 1 :].split()[19]
    except (OSError, IndexError) as exc:
        raise PaneError("parent_unverified", "The controller pane process generation is unavailable.") from exc
    caller = _caller_identity(int(parts[2]), parts[4])
    identity = "\0".join((str(endpoint), parent_id, parts[2], start_time, caller))
    return {
        "endpoint": str(endpoint), "parent_id": parent_id, "session_id": parts[1],
        "parent_pid": parts[2], "cwd": parts[3], "tty": parts[4],
        "controller_id": hashlib.sha256(identity.encode()).hexdigest(),
    }


def _repository(cwd: str, explicit: str | None) -> tuple[str, Path]:
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, timeout=5, check=False,
        env=child_environment(os.environ),
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PaneError("repository_unverified", "The caller is outside a verified Git checkout.")
    common = Path(result.stdout.strip()).resolve()
    root = common.parent if common.name == ".git" else common.parent.parent
    slug = root.name.lower()
    if explicit is not None and explicit != slug:
        raise PaneError("repository_mismatch", "The requested repository does not match the canonical Git checkout.")
    return slug, root


def _runtime(root: Path) -> tuple[FilePaneStore, ManagedPaneService]:
    store = FilePaneStore(root)
    return store, ManagedPaneService(store, {"tmux": TmuxAdapter(state_dir=root)}, state_dir=root)


def _request(context: dict[str, str], repository: str, host_alias: str | None) -> PaneRequest:
    return PaneRequest(
        backend="tmux", endpoint=context["endpoint"], repository=repository,
        controller_id=context["controller_id"], parent_id=context["parent_id"],
        cwd=context["cwd"], host_alias=host_alias, session_id=context["session_id"],
        machine_id=machine_generation(),
    )


def pick(*, repository: str | None = None, host_alias: str | None = None,
         cwd: str | None = None, state_dir: str | None = None) -> dict[str, Any]:
    context = _tmux_context()
    if cwd is not None and not os.path.samefile(cwd, context["cwd"]):
        raise PaneError("cwd_mismatch", "The requested directory is not the controller pane's current directory.")
    slug, git_root = _repository(context["cwd"], repository)
    if slug == "fleetops":
        try:
            relative = Path(context["cwd"]).resolve().relative_to(git_root)
        except ValueError as exc:
            raise PaneError("repository_mismatch", "The controller directory is outside the canonical repository.") from exc
        if len(relative.parts) >= 2 and relative.parts[0] == "hosts":
            if host_alias != relative.parts[1]:
                raise PaneError("host_alias_required", "Supply the exact admitted host alias from the FleetOps caller binding.")
        elif host_alias is not None:
            raise PaneError("host_alias_mismatch", "A host alias requires the matching host checkout.")
    root = _private_root(state_dir)
    _, service = _runtime(root)
    request = _request(context, slug, host_alias)
    status = service.ensure(request)
    payload = status.to_dict()
    if status.binding is not None:
        payload.update({
            "target": status.binding.pane_id,
            "pane_id": status.binding.pane_id,
            "session": request.session_id,
            "parent_pane_id": request.parent_id,
            "state_dir": str(root),
            "reason": "created" if status.changed else "idle" if status.state == "ready" else status.state,
        })
    return payload


def _selected(target: str, state_dir: str | None) -> tuple[Path, ManagedPaneService, PaneRequest]:
    if not target.startswith("%") or not target[1:].isdigit():
        raise PaneError("target_required", "Use the exact native %pane target returned by pick.")
    context = _tmux_context()
    root = _private_root(state_dir)
    store, service = _runtime(root)
    matches = []
    names = {field.name for field in fields(PaneRequest)}
    for record in store.records():
        status = record.get("status")
        binding = record.get("last_binding")
        if not isinstance(binding, dict):
            binding = status.get("binding") if isinstance(status, dict) else None
        values = record.get("request")
        if not isinstance(binding, dict) or binding.get("pane_id") != target or not isinstance(values, dict):
            continue
        request = PaneRequest(**{key: value for key, value in values.items() if key in names})
        if record.get("binding_key") != request.binding_key:
            raise PaneError("binding_mismatch", "The pane record does not match its exact owner scope.")
        if (request.endpoint == context["endpoint"] and request.parent_id == context["parent_id"]
                and request.session_id == context["session_id"] and request.controller_id == context["controller_id"]
                and request.cwd == context["cwd"]):
            matches.append(request)
    if len(matches) != 1:
        raise PaneError("target_unverified", "The target has no unique current binding to this controller pane.")
    return root, service, matches[0]


def _sudo_argv(argv: Sequence[str]) -> tuple[list[str], str | None, str | None] | None:
    if not argv or argv[0] != "sudo":
        return None
    index = 1
    user = group = None
    while index < len(argv):
        item = argv[index]
        if item == "--":
            index += 1
            break
        if item in {"-u", "-g"} and index + 1 < len(argv):
            if item == "-u":
                user = argv[index + 1]
            else:
                group = argv[index + 1]
            index += 2
            continue
        if item.startswith("-"):
            raise PaneError("sudo_option_unsupported", "Use direct sudo argv with optional -u/-g and --; other sudo options need the owning workflow.")
        break
    if index >= len(argv):
        raise PaneError("invalid_argv", "The sudo command has no workload argv.")
    return list(argv[index:]), user, group


def run(target: str, argv: Sequence[str], *, timeout: float,
        authentication_timeout: float = 120.0,
        state_dir: str | None = None) -> dict[str, Any]:
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise PaneError("invalid_timeout", "The operation timeout must be positive.")
    root, service, request = _selected(target, state_dir)
    current = service.inspect(request)
    if current.state != "ready" or current.binding is None or current.binding.pane_id != target:
        return current.to_dict()
    sudo = _sudo_argv(argv)
    if sudo is not None:
        workload, user, group = sudo
        return run_visible_sudo(service, request, workload, timeout=timeout,
                                authentication_timeout=authentication_timeout,
                                runas_user=user, runas_group=group,
                                validate_current=lambda: _owner_matches(request))
    operation_id = uuid.uuid4().hex
    dispatched = service.transition(request, PaneTransition(operation_id, tuple(argv)))
    if dispatched.state != "busy":
        return dispatched.to_dict()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, receipt = _read_operation(str(root), request.binding_key, operation_id)
        except PaneError:
            break
        if receipt.get("state") == "completed":
            prompt_deadline = min(deadline, time.monotonic() + 1.0)
            while time.monotonic() < prompt_deadline and service.inspect(request).state != "ready":
                time.sleep(0.02)
            settled = service.reconcile(request, operation_id).to_dict()
            settled.update({"status": "completed", "exit_code": receipt.get("exit_code"),
                            "run_id": operation_id, "status_handle": _status_handle(request.binding_key, operation_id),
                            "target": target, "tail": ""})
            return settled
        time.sleep(0.05)
    running = service.reconcile(request, operation_id).to_dict()
    running.update({"status": "running" if running.get("state") == "busy" else "unknown",
                    "run_id": operation_id, "status_handle": _status_handle(request.binding_key, operation_id), "target": target,
                    "detail": "Execution is still running; use the durable status handle to observe it without replay." if running.get("state") == "busy" else "The operation outcome remains uncertain; reconcile without replay.",
                    "tail": ""})
    running["status_handle"] = _status_handle(request.binding_key, operation_id)
    return running


def _owner_matches(request: PaneRequest) -> bool:
    try:
        context = _tmux_context()
    except PaneError:
        return False
    return (context["endpoint"] == request.endpoint and context["parent_id"] == request.parent_id
            and context["session_id"] == request.session_id and context["controller_id"] == request.controller_id)


def status(target: str | None, operation_id: str | None = None, *, state_dir: str | None = None,
           wait: bool = False, timeout: float = 30.0, status_handle: str | None = None) -> dict[str, Any]:
    if status_handle is not None:
        if target is not None or operation_id is not None:
            raise PaneError("invalid_status_handle", "Use a durable status handle by itself.")
        return _status_by_handle(status_handle, state_dir=state_dir, wait=wait, timeout=timeout)
    if isinstance(target, str) and target.startswith(_STATUS_HANDLE_PREFIX):
        if operation_id is not None:
            raise PaneError("invalid_status_handle", "A durable status handle already identifies its operation.")
        return _status_by_handle(target, state_dir=state_dir, wait=wait, timeout=timeout)
    if target is None:
        raise PaneError("target_required", "Supply a managed pane target or a durable status handle.")
    root, service, request = _selected(target, state_dir)
    if operation_id is None:
        payload = service.inspect(request).to_dict()
        payload.update({"target": target, "session": request.session_id,
                        "status": "idle" if payload["state"] == "ready" else payload["state"], "tail": ""})
        return payload
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout < 0):
        raise PaneError("invalid_timeout", "The status wait timeout must be zero or positive.")
    deadline = time.monotonic() + timeout
    while True:
        try:
            _, receipt = _read_operation(str(root), request.binding_key, operation_id)
        except PaneError as exc:
            raise PaneError(exc.code, exc.message) from exc
        if not wait or receipt.get("state") not in {"staged", "running", "claimed"} or time.monotonic() >= deadline:
            break
        time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
    if receipt.get("state") == "completed":
        payload = service.reconcile(request, operation_id).to_dict()
        payload.update({"exit_code": receipt.get("exit_code")})
    else:
        payload = service.inspect(request).to_dict()
        payload.update({"state": "unknown" if receipt.get("state") == "unknown" else "busy",
                        "detail": receipt.get("detail")})
    payload.update({"target": target, "run_id": operation_id,
                    "status": "running" if receipt.get("state") in {"staged", "running", "claimed"} else receipt.get("state", "unknown"),
                    "status_handle": _status_handle(request.binding_key, operation_id), "tail": ""})
    if wait and payload["status"] == "running":
        payload["timed_out"] = True
    return payload
