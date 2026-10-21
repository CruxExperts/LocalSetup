"""Private, single-use transitions for a managed interactive Bash pane."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterator, Sequence
import uuid

from .models import PaneError, PaneTransition


DISPATCH_KEY = "F13"
INVALIDATE_KEY = "F14"
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BINDING_KEY = re.compile(r"^[a-f0-9]{64}$")


def machine_generation() -> str:
    """Return a same-host, same-boot generation token without exposing machine IDs."""
    boot_id = ""
    for candidate in (Path("/proc/sys/kernel/random/boot_id"), Path("/var/run/host_boot_id")):
        try:
            boot_id = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if boot_id:
            break
    seed = "\0".join((platform.node(), str(os.getuid()) if hasattr(os, "getuid") else "unknown", boot_id or str(uuid.getnode())))
    return hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()


def _state_directory(state_dir: str | os.PathLike[str], binding_key: str, *, create: bool) -> Path:
    if not _BINDING_KEY.fullmatch(binding_key):
        raise PaneError("invalid_binding_key", "A valid managed-pane binding key is required.")
    state_root = Path(state_dir).expanduser()
    if not state_root.is_absolute() or "\x00" in str(state_root):
        raise PaneError("invalid_state_dir", "Private transition state must use an absolute path.")
    ops_root = state_root / "terminal_ops"
    root = ops_root / binding_key
    try:
        if create:
            ops_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        ops_info = ops_root.lstat()
        if (stat.S_ISLNK(ops_info.st_mode) or not stat.S_ISDIR(ops_info.st_mode)
                or (hasattr(os, "getuid") and ops_info.st_uid != os.getuid())):
            raise PaneError("unsafe_state_dir", "Private transition state must use an owned directory.")
        if create:
            # mkdir(..., parents=True) may have created this intermediary
            # directory with the ambient umask. Tighten it before creating or
            # using any operation bindings beneath it.
            ops_root.chmod(0o700)
        elif ops_info.st_mode & 0o022:
            raise PaneError("unsafe_state_dir", "Private transition state must not be writable by other users.")
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.lstat()
    except OSError as exc:
        raise PaneError("state_unavailable", "Private transition state is unavailable.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PaneError("unsafe_state_dir", "Private transition state must be a real directory.")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PaneError("unsafe_state_dir", "Private transition state must belong to the current user.")
    try:
        root.chmod(0o700)
    except OSError as exc:
        raise PaneError("state_unavailable", "Private transition state permissions could not be verified.") from exc
    return root


def _atomic_json(path: Path, payload: dict[str, Any], *, exclusive: bool = False) -> None:
    if exclusive and (path.exists() or path.is_symlink()):
        raise PaneError("operation_exists", "The operation ID already has a record; reconcile it before requesting another transition.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise PaneError("operation_exists", "The operation ID already has a record; reconcile it before requesting another transition.") from exc
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise PaneError("unsafe_state_file", "Transition state contains an unsafe symbolic link.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaneError("state_unavailable", "Transition state could not be read safely.") from exc
    if not isinstance(payload, dict):
        raise PaneError("invalid_state", "Transition state must contain a JSON object.")
    return payload


@contextmanager
def _lease(directory: Path, *, exclusive: bool) -> Iterator[Path | None]:
    path = directory / "writer-lease"
    if not exclusive:
        yield path if path.exists() and not path.is_symlink() else None
        return
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise PaneError("pane_busy", "A managed-pane transition already holds the writer lease.") from exc
    except OSError as exc:
        raise PaneError("state_unavailable", "The managed-pane writer lease could not be acquired.") from exc
    try:
        yield path
    finally:
        try:
            (path / "lease.json").unlink()
            path.rmdir()
        except OSError:
            # A retained lease is fail-closed state for read-only reconciliation.
            pass


@contextmanager
def _operation_guard(directory: Path) -> Iterator[None]:
    """Serialize receipt transitions and matching writer-lease cleanup."""
    path = directory / "operation.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise PaneError("state_unavailable", "The managed operation lock is unavailable.") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PaneError("unsafe_state_file", "The managed operation lock is not owner-private.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _cleanup_matching_lease(directory: Path, binding_key: str, operation_id: str,
                            token: object) -> bool:
    """Remove only a lease that exactly belongs to a completed receipt."""
    lease = directory / "writer-lease"
    if lease.is_symlink() or not lease.is_dir():
        return False
    try:
        lease_record = _read_json(lease / "lease.json")
    except PaneError:
        return False
    if (lease_record.get("binding_key") != binding_key
            or lease_record.get("operation_id") != operation_id
            or lease_record.get("token") != token):
        return False
    try:
        (lease / "lease.json").unlink()
        lease.rmdir()
    except OSError:
        return False
    return True


def stage_transition(
    state_dir: str | os.PathLike[str],
    binding: dict[str, Any],
    transition: PaneTransition,
    *,
    prompt_nonce: str,
) -> dict[str, Any]:
    """Stage one argv request before the native adapter sends the reserved key."""
    binding_key = binding.get("binding_key")
    if not isinstance(binding_key, str) or not isinstance(prompt_nonce, str) or not prompt_nonce:
        raise PaneError("invalid_binding", "A verified pane binding and primary-prompt nonce are required.")
    directory = _state_directory(state_dir, binding_key, create=True)
    with _operation_guard(directory):
        current = _read_json(directory / "shell.json")
        expected = {
            "binding_key": binding_key,
            "pane_id": binding.get("pane_id"),
            "pane_tty": binding.get("pane_tty"),
            "shell_pid": binding.get("shell_pid"),
            "shell_path": binding.get("shell_path"),
            "machine_generation": binding.get("machine_generation"),
        }
        if current.get("phase") != "primary" or current.get("keymap_ready") is not True or current.get("prompt_nonce") != prompt_nonce:
            raise PaneError("prompt_not_primary", "The pane is not at a verified primary prompt.")
        if any(current.get(key) != value for key, value in expected.items()):
            raise PaneError("binding_mismatch", "The primary prompt does not match the verified native pane binding.")
        operation_path = _operation_path(directory, transition.operation_id)
        if operation_path.exists() or operation_path.is_symlink():
            raise PaneError("operation_exists", "The operation ID already has a record; choose a fresh ID.")
        lease = directory / "writer-lease"
        try:
            lease.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise PaneError("pane_busy", "A managed-pane transition already holds the writer lease.") from exc
        except OSError as exc:
            raise PaneError("state_unavailable", "The managed-pane writer lease could not be acquired.") from exc
        token = uuid.uuid4().hex
        record = {
            "schema_version": 1,
            "binding_key": binding_key,
            "operation_id": transition.operation_id,
            "state": "staged",
            "transition": transition.to_dict(),
            "pane_id": binding.get("pane_id"),
            "pane_tty": binding.get("pane_tty"),
            "shell_pid": binding.get("shell_pid"),
            "shell_path": binding.get("shell_path"),
            "machine_generation": binding.get("machine_generation"),
            "target": binding.get("host_alias") or binding.get("repository"),
            "endpoint": binding.get("endpoint"),
            "controller_id": binding.get("controller_id"),
            "working_directory": transition.cwd or binding.get("cwd"),
            "prompt_nonce": prompt_nonce,
            "lease_token": token,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _atomic_json(lease / "lease.json", {"binding_key": binding_key,
                                                 "operation_id": transition.operation_id, "token": token})
            _atomic_json(operation_path, record, exclusive=True)
        except Exception:
            # Only unwind our new lease when no operation carrying its token was
            # staged. A partial/uncertain staging result remains fenced.
            try:
                existing = _read_json(operation_path)
            except PaneError:
                existing = None
            if not isinstance(existing, dict) or existing.get("lease_token") != token:
                if not _cleanup_matching_lease(directory, binding_key, transition.operation_id, token):
                    # We created this directory while holding the operation
                    # lock. Remove it only if the failed stage left it empty;
                    # any payload or unknown content keeps the fence in place.
                    try:
                        lease.rmdir()
                    except OSError:
                        pass
            raise
        return record


def _operation_path(directory: Path, operation_id: str) -> Path:
    if not _OPERATION_ID.fullmatch(operation_id or ""):
        raise PaneError("invalid_operation_id", "A valid operation ID is required.")
    return directory / f"{operation_id}.json"


def announce_prompt(
    state_dir: str | os.PathLike[str],
    *,
    binding_key: str,
    pane_id: str,
    shell_pid: int,
    pane_tty: str,
    shell_path: str,
    cwd: str,
    phase: str,
    prompt_nonce: str,
    generation: str,
    keymap_ready: bool,
) -> dict[str, Any]:
    """Publish the current primary-prompt generation for a safe staged transition."""
    if phase not in {"primary", "invalid"} or not isinstance(shell_pid, int) or shell_pid <= 0:
        raise PaneError("invalid_prompt", "The controlled shell prompt state is invalid.")
    if not all(isinstance(value, str) and value and "\x00" not in value for value in (pane_id, pane_tty, shell_path, cwd, prompt_nonce, generation)):
        raise PaneError("invalid_prompt", "The controlled shell identity is incomplete.")
    if not cwd.startswith("/") or "\n" in cwd or "\r" in cwd or "\t" in cwd:
        raise PaneError("invalid_prompt", "The controlled shell working directory is invalid.")
    if not isinstance(keymap_ready, bool):
        raise PaneError("invalid_prompt", "The controlled shell keymap state is invalid.")
    if phase == "primary" and not keymap_ready:
        phase = "invalid"
    directory = _state_directory(state_dir, binding_key, create=True)
    payload = {
        "schema_version": 1,
        "binding_key": binding_key,
        "pane_id": pane_id,
        "shell_pid": shell_pid,
        "pane_tty": pane_tty,
        "shell_path": shell_path,
        "cwd": cwd,
        "keymap_ready": keymap_ready,
        "phase": phase,
        "prompt_nonce": prompt_nonce,
        "machine_generation": generation,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _operation_guard(directory):
        _atomic_json(directory / "shell.json", payload)
    return payload


def read_prompt_state(state_dir: str | os.PathLike[str], binding_key: str) -> dict[str, Any]:
    directory = _state_directory(state_dir, binding_key, create=False)
    state = _read_json(directory / "shell.json")
    if state.get("binding_key") != binding_key:
        raise PaneError("binding_mismatch", "Prompt state belongs to a different managed pane.")
    return state


def claim_transition(
    state_dir: str | os.PathLike[str],
    *,
    binding_key: str,
    operation_id: str,
    pane_id: str,
    shell_pid: int,
    pane_tty: str,
    shell_path: str,
    prompt_nonce: str,
    generation: str,
) -> dict[str, Any]:
    """Consume a staged request once after the Bash handler has checked its live state."""
    directory = _state_directory(state_dir, binding_key, create=False)
    path = _operation_path(directory, operation_id)
    with _operation_guard(directory):
        with _lease(directory, exclusive=False) as lease:
            if lease is None:
                raise PaneError("writer_lease_missing", "The staged transition has no live writer lease.")
            lease_record = _read_json(lease / "lease.json")
            record = _read_json(path)
            current = _read_json(directory / "shell.json")
            if record.get("state") != "staged":
                raise PaneError("transition_not_staged", "The transition was already consumed or has an uncertain outcome.", effect="unknown")
            checks = {
                "binding_key": binding_key,
                "operation_id": operation_id,
                "pane_id": pane_id,
                "shell_pid": shell_pid,
                "pane_tty": pane_tty,
                "shell_path": shell_path,
                "prompt_nonce": prompt_nonce,
                "machine_generation": generation,
            }
            if any(record.get(name) != value for name, value in checks.items()):
                raise PaneError("transition_scope_mismatch", "The staged transition does not match the live pane, shell, or prompt.")
            if current.get("phase") != "primary" or any(current.get(name) != value for name, value in checks.items() if name != "operation_id"):
                raise PaneError("prompt_not_primary", "The pane is not at the staged primary prompt.")
            if current.get("keymap_ready") is not True:
                raise PaneError("unsupported_keymap", "The managed pane Readline keymap is no longer supported.")
            if lease_record.get("binding_key") != binding_key or lease_record.get("operation_id") != operation_id or lease_record.get("token") != record.get("lease_token"):
                raise PaneError("writer_lease_mismatch", "The staged transition does not hold the active writer lease.")
            record.update({"state": "running", "claimed_at": datetime.now(timezone.utc).isoformat()})
            _atomic_json(path, record)
            transition = record.get("transition")
            if not isinstance(transition, dict) or not isinstance(transition.get("argv"), list):
                record.update({"state": "unknown", "detail": "malformed staged argv"})
                _atomic_json(path, record)
                raise PaneError("invalid_transition", "The staged argv request is malformed.", effect="unknown")
            return record


def finish_transition(
    state_dir: str | os.PathLike[str],
    *,
    binding_key: str,
    operation_id: str,
    exit_code: int,
) -> dict[str, Any]:
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise PaneError("invalid_exit_code", "The transition exit code is invalid.")
    directory = _state_directory(state_dir, binding_key, create=False)
    path = _operation_path(directory, operation_id)
    with _operation_guard(directory):
        record = _read_json(path)
        if record.get("binding_key") != binding_key or record.get("operation_id") != operation_id:
            raise PaneError("transition_not_running", "The transition has no confirmed running receipt.", effect="unknown")
        if record.get("state") == "completed":
            _cleanup_matching_lease(directory, binding_key, operation_id, record.get("lease_token"))
            return record
        if record.get("state") != "running":
            raise PaneError("transition_not_running", "The transition has no confirmed running receipt.", effect="unknown")
        transition = record.get("transition")
        if isinstance(transition, dict) and transition.get("requires_sudo_receipt") is True:
            try:
                sudo = _read_json(directory / f"{operation_id}.sudo.json")
            except PaneError:
                sudo = None
            if (not isinstance(sudo, dict) or sudo.get("operation_id") != operation_id
                    or sudo.get("status") == "unknown"):
                record.update({"state": "unknown", "detail": "sudo outcome is uncertain; read-only reconciliation is required",
                               "reconciled_at": datetime.now(timezone.utc).isoformat()})
                _atomic_json(path, record)
                return record
        record.update({"state": "completed", "exit_code": exit_code, "completed_at": datetime.now(timezone.utc).isoformat()})
        _atomic_json(path, record)
        _cleanup_matching_lease(directory, binding_key, operation_id, record.get("lease_token"))
        return record


def reconcile_transition(
    state_dir: str | os.PathLike[str],
    *,
    binding_key: str,
    operation_id: str,
) -> dict[str, Any]:
    directory = _state_directory(state_dir, binding_key, create=False)
    with _operation_guard(directory):
        record = _read_json(_operation_path(directory, operation_id))
        if record.get("binding_key") != binding_key or record.get("operation_id") != operation_id:
            raise PaneError("binding_mismatch", "The transition receipt belongs to a different binding.")
        # A staged operation has not crossed the managed claim boundary yet.
        # If its original prompt generation is no longer live, fence it as
        # unknown so a delayed trigger cannot claim work against a later prompt.
        # Missing or unreadable prompt state is not evidence either way.
        if record.get("state") == "staged":
            try:
                current = _read_json(directory / "shell.json")
            except PaneError:
                current = None
            if isinstance(current, dict):
                expected = {
                    "binding_key": record.get("binding_key"),
                    "pane_id": record.get("pane_id"),
                    "shell_pid": record.get("shell_pid"),
                    "pane_tty": record.get("pane_tty"),
                    "shell_path": record.get("shell_path"),
                    "machine_generation": record.get("machine_generation"),
                    "prompt_nonce": record.get("prompt_nonce"),
                }
                if (current.get("phase") != "primary" or current.get("keymap_ready") is not True
                        or any(current.get(name) != value for name, value in expected.items())):
                    record.update({
                        "state": "unknown",
                        "detail": "the staged prompt generation is no longer live; reconcile without replay",
                        "reconciled_at": datetime.now(timezone.utc).isoformat(),
                    })
                    _atomic_json(_operation_path(directory, operation_id), record)
        # Observation is not proof that execution stopped. Preserve running
        # states so a slow workload can still write its one completion receipt.
        # Unknown operations remain fenced until independent evidence resolves.
        if record.get("state") == "completed":
            _cleanup_matching_lease(directory, binding_key, operation_id,
                                    record.get("lease_token"))
        return record


def render_bash_bootstrap(
    *,
    state_dir: str | os.PathLike[str],
    binding_key: str,
    pane_id: str | None = None,
    pane_tty: str | None = None,
    pane_id_env: str = "TMUX_PANE",
    python_executable: str = sys.executable,
) -> str:
    """Render the supported Bash gate; the native sender only needs to send F13."""
    if not _BINDING_KEY.fullmatch(binding_key):
        raise PaneError("invalid_binding", "The controlled shell requires an exact pane binding.")
    if pane_id is None and pane_id_env not in {"TMUX_PANE", "HERDR_PANE_ID"}:
        raise PaneError("invalid_binding", "The controlled shell requires a supported native pane identity source.")
    if pane_id is not None and (not pane_id or any(ord(char) < 32 for char in pane_id)):
        raise PaneError("invalid_binding", "The controlled shell requires an exact pane binding.")
    if pane_tty is not None and (not pane_tty or any(ord(char) < 32 for char in pane_tty)):
        raise PaneError("invalid_binding", "The controlled shell requires an exact TTY.")
    state = Path(state_dir).expanduser()
    if not state.is_absolute():
        raise PaneError("invalid_state_dir", "The controlled shell requires an absolute private state root.")
    source_root = Path(__file__).resolve().parents[3]
    runner = [shutil.which("env") or "/usr/bin/env", f"PYTHONPATH={source_root}", python_executable,
              "-m", "ls.core.terminal_ops.shell"]
    qrunner = " ".join(shlex.quote(part) for part in runner)
    return f'''# LocalSetup managed terminal shell gate, API v1.
[[ -n ${{BASH_VERSION:-}} && ${{-}} == *i* ]] || {{ printf '%s\\n' 'managed pane requires interactive Bash' >&2; return 1 2>/dev/null || exit 1; }}
__ls_terminal_binding={shlex.quote(binding_key)}
__ls_terminal_pane={shlex.quote(pane_id) if pane_id is not None else f'"${{{pane_id_env}:-}}"'}
__ls_terminal_tty={shlex.quote(pane_tty) if pane_tty is not None else '"$(tty 2>/dev/null)"'}
__ls_terminal_shell_pid=$BASHPID
while IFS= read -r __ls_terminal_secret_name; do
  [[ $__ls_terminal_secret_name == *_SUDO_KEY ]] && unset -v "$__ls_terminal_secret_name"
done < <(compgen -e)
unset __ls_terminal_secret_name
__ls_terminal_state={shlex.quote(str(state))}
__ls_terminal_runner=({qrunner})
__ls_terminal_generation="$({qrunner} generation)" || {{ printf '%s\\n' 'managed pane could not qualify this machine generation' >&2; return 1 2>/dev/null || exit 1; }}
__ls_terminal_prompt_phase=invalid
__ls_terminal_prompt_nonce=
__ls_terminal_expected_maps=
__ls_terminal_map_snapshot() {{
  local __ls_map
  bind -V 2>/dev/null | awk '$1 == "editing-mode" || $1 == "keymap" || $1 == "enable-bracketed-paste" {{ print }}' || return 1
  for __ls_map in emacs-standard emacs-meta emacs-ctlx; do
    printf '[%s]\\n' "$__ls_map"
    bind -m "$__ls_map" -p 2>/dev/null || return 1
    bind -m "$__ls_map" -X 2>/dev/null || return 1
  done
}}
__ls_terminal_is_primary_emacs() {{
  [[ ${{-}} == *i* ]] || return 1
  [[ "$(set -o | awk '$1 == "emacs" {{ print $2 }}')" == on ]] || return 1
  [[ -n $__ls_terminal_expected_maps ]] || return 1
  local __ls_dispatch_map
  __ls_dispatch_map="$(bind -m emacs-standard -X 2>/dev/null)" || return 1
  while IFS= read -r __ls_map; do
    if [[ $__ls_map == *'"\\e[25~":'* && $__ls_map == *__ls_terminal_managed_handler* ]]; then break; fi
  done <<< "$__ls_dispatch_map"
  [[ $__ls_map == *'"\\e[25~":'* && $__ls_map == *__ls_terminal_managed_handler* ]] || return 1
  [[ "$(__ls_terminal_map_snapshot)" == "$__ls_terminal_expected_maps" ]] || return 1
  return 0
}}
__ls_terminal_primary_prompt() {{
  __ls_terminal_prompt_phase=invalid
  __ls_terminal_prompt_nonce="$({qrunner} nonce)" || {{ __ls_terminal_prompt_phase=invalid; return 0; }}
  [[ -n $__ls_terminal_pane && -n $__ls_terminal_tty ]] || {{ __ls_terminal_report_invalid; return 0; }}
  __ls_terminal_is_primary_emacs || {{ __ls_terminal_report_invalid; return 0; }}
  local __ls_shell_path
  __ls_shell_path="$(readlink -f -- "/proc/$__ls_terminal_shell_pid/exe" 2>/dev/null)" || {{ __ls_terminal_report_invalid; return 0; }}
  "${{__ls_terminal_runner[@]}}" announce --state-dir "$__ls_terminal_state" --binding-key "$__ls_terminal_binding" --pane-id "$__ls_terminal_pane" --shell-pid "$__ls_terminal_shell_pid" --pane-tty "$__ls_terminal_tty" --shell-path "$__ls_shell_path" --cwd "$(pwd -P)" --phase primary --prompt-nonce "$__ls_terminal_prompt_nonce" --generation "$__ls_terminal_generation" --keymap-ready true >/dev/null 2>&1 || {{ __ls_terminal_report_invalid; return 0; }}
  __ls_terminal_prompt_phase=primary
}}
__ls_terminal_report_invalid() {{
  __ls_terminal_prompt_phase=invalid
  [[ -n $__ls_terminal_prompt_nonce && -n $__ls_terminal_pane && -n $__ls_terminal_tty && -n $__ls_terminal_shell_path ]] || return 0
  "${{__ls_terminal_runner[@]}}" announce --state-dir "$__ls_terminal_state" --binding-key "$__ls_terminal_binding" --pane-id "$__ls_terminal_pane" --shell-pid "$__ls_terminal_shell_pid" --pane-tty "$__ls_terminal_tty" --shell-path "$__ls_terminal_shell_path" --cwd "$(pwd -P)" --phase invalid --prompt-nonce "$__ls_terminal_prompt_nonce" --generation "$__ls_terminal_generation" --keymap-ready false >/dev/null 2>&1 || true
}}
__ls_terminal_invalidate_prompt() {{
  __ls_terminal_prompt_phase=invalid
  __ls_terminal_report_invalid
}}
__ls_terminal_managed_handler() {{
  if [[ -n ${{READLINE_LINE-}} ]]; then
    __ls_terminal_invalidate_prompt
    return 0
  fi
  [[ $__ls_terminal_prompt_phase == primary && -n $__ls_terminal_prompt_nonce ]] || return 0
  __ls_terminal_is_primary_emacs || {{ printf '%s\\n' 'managed pane keymap changed; transition not dispatched' >&2; return 0; }}
  [[ "$(tty 2>/dev/null)" == "$__ls_terminal_tty" ]] || {{ printf '%s\\n' 'managed pane TTY changed; transition not dispatched' >&2; return 0; }}
  local __ls_claim
  __ls_terminal_is_primary_emacs || {{ printf '%s\\n' 'managed pane keymap changed; transition not dispatched' >&2; return 0; }}
  [[ "$(readlink -f -- "/proc/$__ls_terminal_shell_pid/exe" 2>/dev/null)" == "$__ls_terminal_shell_path" ]] || return 0
  [[ "$BASHPID" == "$__ls_terminal_shell_pid" ]] || return 0
  __ls_claim="$({qrunner} claim --state-dir "$__ls_terminal_state" --binding-key "$__ls_terminal_binding" --operation-id-from-lease --pane-id "$__ls_terminal_pane" --shell-pid "$__ls_terminal_shell_pid" --pane-tty "$__ls_terminal_tty" --shell-path "$__ls_terminal_shell_path" --prompt-nonce "$__ls_terminal_prompt_nonce" --generation "$__ls_terminal_generation")" || return 0
  __ls_terminal_report_invalid
  local __ls_operation __ls_kind __ls_cwd __ls_encoded __ls_decoded
  IFS=$'\\t' read -r __ls_operation __ls_kind __ls_cwd <<< "$__ls_claim"
  local -a __ls_argv=()
  while IFS= read -r __ls_encoded; do
    [[ $__ls_encoded == x* ]] || return 0
    __ls_decoded="$(printf '%s' "${{__ls_encoded:1}}" | base64 --decode 2>/dev/null && printf '.')" || return 0
    [[ $__ls_decoded == *. ]] || return 0
    __ls_argv+=("${{__ls_decoded%.}}")
  done < <({qrunner} argv --state-dir "$__ls_terminal_state" --binding-key "$__ls_terminal_binding" --operation-id "$__ls_operation")
  local __ls_code=0
  local __ls_tty_mode
  __ls_tty_mode="$(stty -g < "$__ls_terminal_tty" 2>/dev/null)" || return 0
  stty sane < "$__ls_terminal_tty" 2>/dev/null || return 0
  if [[ $__ls_kind == cd ]]; then
    builtin cd -- "${{__ls_argv[0]}}" || __ls_code=$?
  elif [[ -n $__ls_cwd ]]; then
    if builtin cd -- "$__ls_cwd"; then
      "${{__ls_argv[@]}}" || __ls_code=$?
    else
      __ls_code=$?
    fi
  else
    "${{__ls_argv[@]}}" || __ls_code=$?
  fi
  stty "$__ls_tty_mode" < "$__ls_terminal_tty" 2>/dev/null || return 0
  "${{__ls_terminal_runner[@]}}" finish --state-dir "$__ls_terminal_state" --binding-key "$__ls_terminal_binding" --operation-id "$__ls_operation" --exit-code "$__ls_code" >/dev/null 2>&1 || return 0
  __ls_terminal_primary_prompt
}}
bind -m emacs-standard -x '"\\e[25~": __ls_terminal_managed_handler'
__ls_terminal_shell_path="$(readlink -f -- "/proc/$__ls_terminal_shell_pid/exe" 2>/dev/null)" || {{ printf '%s\\n' 'managed pane could not identify its Bash process' >&2; return 1 2>/dev/null || exit 1; }}
bind 'set enable-bracketed-paste off' || {{ printf '%s\\n' 'managed pane could not control bracketed paste' >&2; return 1 2>/dev/null || exit 1; }}
__ls_terminal_expected_maps="$(__ls_terminal_map_snapshot)" || {{ printf '%s\\n' 'managed pane could not verify its Readline keymaps' >&2; return 1 2>/dev/null || exit 1; }}
__ls_terminal_is_primary_emacs || {{ printf '%s\\n' 'managed pane Readline bindings are not supported' >&2; return 1 2>/dev/null || exit 1; }}
if declare -p PROMPT_COMMAND 2>/dev/null | grep -q '^declare \\-a'; then
  PROMPT_COMMAND=(__ls_terminal_primary_prompt)
else
  PROMPT_COMMAND=__ls_terminal_primary_prompt
fi
PS2='${{__ls_terminal_prompt_phase:0:$((__ls_terminal_prompt_phase=0))}}$(__ls_terminal_report_invalid)> '
'''


def write_bash_bootstrap(
    state_dir: str | os.PathLike[str],
    binding_key: str,
    *,
    pane_id: str | None = None,
    pane_id_env: str = "TMUX_PANE",
    pane_tty: str | None = None,
    python_executable: str = sys.executable,
) -> Path:
    """Atomically create the private Bash bootstrap used by one native pane."""
    directory = _state_directory(state_dir, binding_key, create=True)
    target = directory / "bashrc"
    if target.is_symlink():
        raise PaneError("unsafe_state_file", "The managed Bash bootstrap cannot be a symbolic link.")
    payload = render_bash_bootstrap(
        state_dir=state_dir,
        binding_key=binding_key,
        pane_id=pane_id,
        pane_id_env=pane_id_env,
        pane_tty=pane_tty,
        python_executable=python_executable,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".bashrc.", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink():
            raise PaneError("unsafe_state_file", "The managed Bash bootstrap cannot be a symbolic link.")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return target


def _read_operation(state_dir: str, binding_key: str, operation_id: str) -> tuple[Path, dict[str, Any]]:
    directory = _state_directory(state_dir, binding_key, create=False)
    path = _operation_path(directory, operation_id)
    record = _read_json(path)
    if record.get("binding_key") != binding_key or record.get("operation_id") != operation_id:
        raise PaneError("binding_mismatch", "The operation record belongs to a different pane binding.")
    return path, record


def _claim_cli(args: argparse.Namespace) -> int:
    directory = _state_directory(args.state_dir, args.binding_key, create=False)
    with _lease(directory, exclusive=False) as lease:
        if lease is None:
            return 2
        lease_record = _read_json(lease / "lease.json")
        operation_id = lease_record.get("operation_id")
        if not isinstance(operation_id, str):
            return 2
    record = claim_transition(
        args.state_dir,
        binding_key=args.binding_key,
        operation_id=operation_id,
        pane_id=args.pane_id,
        shell_pid=args.shell_pid,
        pane_tty=args.pane_tty,
        shell_path=args.shell_path,
        prompt_nonce=args.prompt_nonce,
        generation=args.generation,
    )
    transition = record["transition"]
    print("\t".join((operation_id, transition["kind"], transition.get("cwd") or "")))
    return 0


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-pane-shell")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("generation")
    subparsers.add_parser("nonce")
    announce = subparsers.add_parser("announce")
    announce.add_argument("--state-dir", required=True)
    announce.add_argument("--binding-key", required=True)
    announce.add_argument("--pane-id", required=True)
    announce.add_argument("--shell-pid", required=True, type=int)
    announce.add_argument("--pane-tty", required=True)
    announce.add_argument("--shell-path", required=True)
    announce.add_argument("--cwd", required=True)
    announce.add_argument("--phase", required=True, choices=("primary", "invalid"))
    announce.add_argument("--prompt-nonce", required=True)
    announce.add_argument("--generation", required=True)
    announce.add_argument("--keymap-ready", required=True, choices=("true", "false"))
    claim = subparsers.add_parser("claim")
    claim.add_argument("--state-dir", required=True)
    claim.add_argument("--binding-key", required=True)
    claim.add_argument("--operation-id-from-lease", action="store_true", required=True)
    claim.add_argument("--pane-id", required=True)
    claim.add_argument("--shell-pid", required=True, type=int)
    claim.add_argument("--pane-tty", required=True)
    claim.add_argument("--shell-path", required=True)
    claim.add_argument("--prompt-nonce", required=True)
    claim.add_argument("--generation", required=True)
    operation = subparsers.add_parser("argv")
    operation.add_argument("--state-dir", required=True)
    operation.add_argument("--binding-key", required=True)
    operation.add_argument("--operation-id", required=True)
    finish = subparsers.add_parser("finish")
    finish.add_argument("--state-dir", required=True)
    finish.add_argument("--binding-key", required=True)
    finish.add_argument("--operation-id", required=True)
    finish.add_argument("--exit-code", required=True, type=int)
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--state-dir", required=True)
    reconcile.add_argument("--binding-key", required=True)
    reconcile.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generation":
            print(machine_generation())
            return 0
        if args.command == "nonce":
            print(uuid.uuid4().hex)
            return 0
        if args.command == "announce":
            announce_prompt(
                args.state_dir,
                binding_key=args.binding_key,
                pane_id=args.pane_id,
                shell_pid=args.shell_pid,
                pane_tty=args.pane_tty,
                shell_path=args.shell_path,
                cwd=args.cwd,
                phase=args.phase,
                prompt_nonce=args.prompt_nonce,
                generation=args.generation,
                keymap_ready=args.keymap_ready == "true",
            )
            return 0
        if args.command == "claim":
            return _claim_cli(args)
        if args.command == "argv":
            _, record = _read_operation(args.state_dir, args.binding_key, args.operation_id)
            if record.get("state") != "running":
                return 2
            for value in record["transition"]["argv"]:
                if "\x00" in value:
                    return 2
                print("x" + base64.b64encode(value.encode("utf-8")).decode("ascii"))
            return 0
        if args.command == "finish":
            finish_transition(args.state_dir, binding_key=args.binding_key, operation_id=args.operation_id, exit_code=args.exit_code)
            return 0
        if args.command == "reconcile":
            print(json.dumps(reconcile_transition(args.state_dir, binding_key=args.binding_key, operation_id=args.operation_id), sort_keys=True))
            return 0
    except PaneError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
