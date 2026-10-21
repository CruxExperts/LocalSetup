"""Thin native Herdr and tmux adapters for managed terminal panes."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import time
from typing import Any, Protocol

from ..herdr_ops import layout as herdr_layout
from ..herdr_ops import transport as herdr_transport
from .credentials import child_environment
from .models import PaneBinding, PaneError, PaneRequest, PaneStatus
from .shell import DISPATCH_KEY, machine_generation, read_prompt_state, write_bash_bootstrap


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _credential_names_in_process(pid: int) -> set[str] | None:
    """Read only names from a same-user process's initial environment."""
    try:
        if Path(f"/proc/{pid}").stat().st_uid != os.getuid():
            return None
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    names: set[str] = set()
    for item in raw.split(b"\0"):
        name, separator, value = item.partition(b"=")
        if separator and name.endswith(b"_SUDO_KEY") and value:
            try:
                decoded = name.decode("ascii")
            except UnicodeDecodeError:
                return None
            if not _ENV_NAME.fullmatch(decoded):
                return None
            names.add(decoded)
    return names


class PaneAdapter(Protocol):
    """Native endpoint operations needed by the common lifecycle service."""

    def inspect(self, request: PaneRequest) -> PaneStatus: ...
    def create(self, request: PaneRequest) -> PaneBinding: ...
    def send_trigger(self, request: PaneRequest, binding: PaneBinding) -> None: ...


def _result_payload(value: Any, key: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PaneError("native_unavailable", "The native pane endpoint returned an invalid response.")
    result = value.get("result", value)
    if key and isinstance(result, dict) and isinstance(result.get(key), dict):
        return result[key]
    if isinstance(result, dict):
        return result
    raise PaneError("native_unavailable", "The native pane endpoint returned an invalid response.")


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _process_cwd(pid: int) -> str | None:
    try:
        path = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
    return path if Path(path).is_absolute() and Path(path).is_dir() else None


def _process_tty_matches(pid: int, tty: str) -> bool:
    try:
        shell_fd = os.stat(f"/proc/{pid}/fd/0")
        pane_tty = os.stat(tty)
        return stat.S_ISCHR(shell_fd.st_mode) and stat.S_ISCHR(pane_tty.st_mode) and shell_fd.st_rdev == pane_tty.st_rdev
    except OSError:
        return False


def _shell_owns_foreground(pid: int, tty: str) -> bool:
    """An old primary-prompt receipt is insufficient while a child owns the TTY."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 1 :].split()
        # Linux fields after comm: state, ppid, pgrp, session, tty_nr, tpgid.
        return int(fields[4]) != 0 and int(fields[2]) == int(fields[5]) and _process_tty_matches(pid, tty)
    except (OSError, ValueError, IndexError):
        return False


def _prompt_status(state_dir: str | os.PathLike[str], request: PaneRequest, pane_id: str,
                   *, terminal_id: str | None, tab_id: str | None, native_pid: int | None,
                   native_tty: str | None, label: str, created: bool = False) -> PaneStatus:
    try:
        prompt = read_prompt_state(state_dir, request.binding_key)
    except PaneError as exc:
        if exc.code in {"state_unavailable", "unsafe_state_dir", "unsafe_state_file"}:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The controlled Bash prompt handshake is unavailable.", changed=created)
        raise
    pid = prompt.get("shell_pid")
    tty = prompt.get("pane_tty")
    shell_path = prompt.get("shell_path")
    cwd = prompt.get("cwd")
    generation = prompt.get("machine_generation")
    exact_identity = (
        prompt.get("pane_id") == pane_id
        and isinstance(pid, int) and pid > 0
        and isinstance(tty, str) and bool(tty)
        and isinstance(shell_path, str) and Path(shell_path).name == "bash"
        and isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).is_dir()
        and isinstance(generation, str) and generation == machine_generation()
        and (native_pid is None or native_pid == pid)
        and (native_tty is None or _same_path(native_tty, tty))
        and _process_cwd(pid) is not None
        and _same_path(cwd, _process_cwd(pid) or "")
        and _process_tty_matches(pid, tty)
    )
    if not exact_identity:
        return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                          detail="The native pane and controlled Bash identity do not match.", changed=created)
    credentials = _credential_names_in_process(pid)
    if credentials is None or credentials:
        return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                          detail="The managed Bash initial environment cannot be proven credential-free.", changed=created)
    state = "ready" if (prompt.get("phase") == "primary"
                        and prompt.get("keymap_ready") is True
                        and _shell_owns_foreground(pid, tty)) else "busy"
    binding = PaneBinding(
        name=label,
        binding_key=request.binding_key,
        backend=request.backend,
        endpoint=request.endpoint,
        parent_id=request.parent_id,
        pane_id=pane_id,
        repository=request.repository,
        controller_id=request.controller_id,
        host_alias=request.host_alias,
        workspace_id=request.workspace_id,
        session_id=request.session_id,
        tab_id=tab_id,
        terminal_id=terminal_id,
        pane_tty=tty,
        shell_pid=pid,
        shell_path=shell_path,
        machine_generation=generation,
        cwd=cwd,
        state=state,
        metadata={"prompt_nonce": prompt.get("prompt_nonce"), "phase": prompt.get("phase")},
    )
    return PaneStatus(state, request.backend, request.binding_key, request.name, binding=binding,
                      detail=None if state == "ready" else "The controlled shell is not at its primary prompt.",
                      changed=created)


class HerdrAdapter:
    """Herdr adapter using its existing owned-pane allocator and exact native IDs."""

    def __init__(self, client: Any | None = None, *, state_dir: str | os.PathLike[str] | None = None):
        self.client = client
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else None

    def _state(self) -> Path:
        return herdr_transport.state_directory(self.state_dir)

    def _request(self, request: PaneRequest, method: str, params: dict[str, Any], *, mutation: bool = False) -> dict[str, Any]:
        if self.client is None:
            return herdr_transport.request(request.endpoint, method, params, mutation=mutation)
        response = self.client.request(method, params)
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, dict):
            return result
        return _result_payload(response)

    def _endpoint(self, request: PaneRequest) -> str:
        if self.client is not None:
            socket_path = getattr(self.client, "socket_path", None)
            if socket_path is None or str(Path(socket_path).expanduser().absolute()) != str(Path(request.endpoint).expanduser().absolute()):
                raise PaneError("endpoint_mismatch", "The injected Herdr client is not bound to the requested native endpoint.")
            identity = getattr(self.client, "endpoint_identity", None)
            if not callable(identity):
                raise PaneError("endpoint_unverified", "The injected Herdr client cannot qualify its native endpoint.")
            identity()
        endpoint = str(Path(request.endpoint).expanduser().absolute())
        herdr_transport.verify_local_herdr_endpoint(endpoint)
        return endpoint

    def _server_credential_names(self, request: PaneRequest) -> set[str]:
        if self.client is not None:
            identity = self.client.endpoint_identity()
            pid = identity.get("pid") if isinstance(identity, dict) else None
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(request.endpoint)
                pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise PaneError("endpoint_unverified", "The Herdr endpoint owner changed before pane creation.")
        if not isinstance(pid, int) or pid <= 0:
            raise PaneError("server_environment_unknown", "The Herdr server environment cannot be qualified.")
        names = _credential_names_in_process(pid)
        if names is None:
            raise PaneError("server_environment_unknown", "The Herdr server environment cannot be qualified.")
        return names

    def _inventory(self, request: PaneRequest) -> list[dict[str, Any]]:
        if self.client is not None and callable(getattr(self.client, "_pane_inventory", None)):
            return self.client._pane_inventory(request.workspace_id)
        result = self._request(request, "pane.list", {"workspace_id": request.workspace_id})
        panes = result.get("panes")
        if not isinstance(panes, list) or any(not isinstance(pane, dict) for pane in panes):
            raise PaneError("native_unavailable", "Herdr did not return an exact workspace pane inventory.")
        return panes

    def _owned(self, request: PaneRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        owner_id = request.binding_key[:32]
        owner, pane = herdr_layout.owned_pane(
            request.endpoint, owner_id, state_dir=self._state(), allow_managed_ops=True
        )
        expected = {
            "api_version": 1,
            "binding_key": request.binding_key,
            "repository": request.repository,
            "controller_id": request.controller_id,
            "host_alias": request.host_alias,
            "parent_id": request.parent_id,
            "workspace_id": request.workspace_id,
            "machine_id": request.machine_id,
        }
        if owner.get("purpose") != "managed_ops" or owner.get("owner_metadata") != expected:
            raise PaneError("owner_scope_mismatch", "The stored Herdr pane belongs to a different managed scope.")
        if not isinstance(pane, dict):
            raise PaneError("pane_missing", "The owned Herdr pane is no longer present.", effect="unknown")
        return owner, pane

    def inspect(self, request: PaneRequest) -> PaneStatus:
        if request.backend != "herdr":
            raise PaneError("backend_mismatch", "The Herdr adapter received a different backend request.")
        self._endpoint(request)
        try:
            owner, pane = self._owned(request)
        except herdr_layout.HerdrOpsError as exc:
            if exc.code in {"not_found", "state_dir_unavailable"}:
                # Look for matching labels before permitting a new allocation.
                inventory = self._inventory(request)
                parent_rows = [row for row in inventory if row.get("pane_id") == request.parent_id]
                parent_tab = parent_rows[0].get("tab_id") if len(parent_rows) == 1 else None
                matches = []
                for row in inventory:
                    if row.get("label") != request.name or parent_tab is None or row.get("tab_id") != parent_tab:
                        continue
                    other = herdr_layout._managed_ops_owner(request.endpoint, str(row.get("pane_id")), self._state())
                    metadata = other.get("owner_metadata") if isinstance(other, dict) else None
                    if (isinstance(metadata, dict) and other.get("state") == "owned"
                            and metadata.get("parent_id") != request.parent_id
                            and metadata.get("controller_id") != request.controller_id
                            and any(parent.get("pane_id") == metadata.get("parent_id")
                                    and parent.get("tab_id") == parent_tab for parent in inventory)):
                        continue
                    matches.append(row)
                if matches:
                    return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                      detail="A matching Herdr label has no exact managed owner record.")
                return PaneStatus("missing", request.backend, request.binding_key, request.name)
            if exc.effect == "unknown" or exc.code in {"managed_pane_missing", "pane_not_found"}:
                return PaneStatus("unknown", request.backend, request.binding_key, request.name, detail=exc.message)
            raise PaneError(exc.code, exc.message, effect=exc.effect) from exc
        except PaneError as exc:
            if exc.code == "not_found":
                return PaneStatus("missing", request.backend, request.binding_key, request.name)
            raise
        pane_id = pane.get("pane_id")
        if not isinstance(pane_id, str) or pane.get("workspace_id") != request.workspace_id:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The owned Herdr pane identity or workspace changed.")
        inventory = self._inventory(request)
        parents = [row for row in inventory if row.get("pane_id") == request.parent_id]
        if len(parents) != 1 or not isinstance(parents[0].get("tab_id"), str):
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The exact Herdr parent is no longer in the explicit workspace.")
        if (pane.get("pane_id") != pane_id or pane.get("workspace_id") != request.workspace_id
                or pane.get("tab_id") != parents[0].get("tab_id")
                or owner.get("tab_id") != parents[0].get("tab_id")):
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The managed Herdr pane is no longer beside its exact parent.")
        if pane.get("label") != request.name:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The managed Herdr pane label changed.")
        if owner.get("caller_pane_id") != request.parent_id or owner.get("workspace_id") != request.workspace_id:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The managed Herdr parent binding changed.")
        if owner.get("cwd") != request.cwd:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The managed Herdr pane's original working-directory binding changed.")
        if self.client is not None and callable(getattr(self.client, "process_info", None)):
            process_result = self.client.process_info(pane_id)
        else:
            process_result = self._request(request, "pane.process_info", {"pane_id": pane_id})
        process = _result_payload(process_result, "process_info")
        shell_pid = process.get("shell_pid")
        foreground = process.get("foreground_processes")
        if not isinstance(shell_pid, int) or shell_pid <= 0 or not isinstance(foreground, list):
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="Herdr cannot verify the managed pane's shell occupant.")
        if any(not isinstance(item, dict) or item.get("pid") != shell_pid for item in foreground):
            return PaneStatus("busy", request.backend, request.binding_key, request.name,
                              detail="A foreground process occupies the managed Herdr pane.")
        try:
            shell_tty = os.readlink(f"/proc/{shell_pid}/fd/0")
        except OSError:
            shell_tty = ""
        if not shell_tty.startswith("/dev/"):
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The managed Herdr shell TTY cannot be verified from the local process.")
        tab_id = pane.get("tab_id")
        # Herdr does not expose a portable parent-pane field. The allocator receipt
        # binds the exact parent and tab, and the actual Bash state binds the child.
        state = _prompt_status(self._state(), request, pane_id,
                               terminal_id=pane.get("terminal_id"), tab_id=tab_id,
                               native_pid=shell_pid, native_tty=shell_tty, label=request.name)
        return state

    def create(self, request: PaneRequest) -> PaneBinding:
        if request.backend != "herdr":
            raise PaneError("backend_mismatch", "The Herdr adapter received a different backend request.")
        endpoint = self._endpoint(request)
        parent = self._request(request, "pane.get", {"pane_id": request.parent_id}).get("pane")
        if not isinstance(parent, dict) or parent.get("workspace_id") != request.workspace_id:
            raise PaneError("parent_mismatch", "The explicit Herdr parent is not in the requested workspace.")
        inventory = self._inventory(request)
        parent_rows = [row for row in inventory if row.get("pane_id") == request.parent_id]
        if len(parent_rows) != 1 or parent_rows[0].get("workspace_id") != request.workspace_id:
            raise PaneError("parent_mismatch", "The exact Herdr parent is not in the requested workspace.")
        root = self._state()
        bootstrap = write_bash_bootstrap(root, request.binding_key, pane_id_env="HERDR_PANE_ID")
        metadata = {
            "api_version": 1, "binding_key": request.binding_key,
            "repository": request.repository, "controller_id": request.controller_id,
            "host_alias": request.host_alias, "parent_id": request.parent_id,
            "workspace_id": request.workspace_id, "machine_id": request.machine_id,
        }
        environment = {
            "INPUTRC": "/dev/null",
            "PROMPT_COMMAND": "source " + shlex.quote(str(bootstrap)),
        }
        for name in self._server_credential_names(request):
            environment[name] = ""
        try:
            created = herdr_layout.ensure_owned_pane(
                endpoint, request.workspace_id or "", request.parent_id, request.cwd,
                request.binding_key[:32], state_dir=root, ratio=1 / 3,
                purpose="managed_ops", owner_metadata=metadata, environment=environment,
            )
        except herdr_layout.HerdrOpsError as exc:
            raise PaneError(exc.code, exc.message, effect=exc.effect) from exc
        pane = created.get("pane")
        if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str):
            raise PaneError("split_unconfirmed", "Herdr created no verifiable pane identity; reconcile before retrying.", effect="unknown")
        pane_id = pane["pane_id"]
        try:
            if self.client is not None and callable(getattr(self.client, "_rename_pane", None)):
                renamed = self.client._rename_pane(pane_id, request.name)
            else:
                renamed = self._request(request, "pane.rename", {"pane_id": pane_id, "label": request.name}, mutation=True)
        except Exception as exc:
            raise PaneError("rename_unknown", "The pane was created but its native label result is uncertain; reconcile before retrying.", effect="unknown") from exc
        del renamed
        # Creation is not ready until the native shell announces a primary prompt.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            current = self.inspect(request)
            if current.state == "ready" and current.binding is not None:
                return current.binding
            time.sleep(0.05)
        raise PaneError("shell_not_qualified", "The created Herdr pane has not completed its controlled Bash handshake.", effect="unknown")

    def send_trigger(self, request: PaneRequest, binding: PaneBinding) -> None:
        # Reverify the native exact binding immediately before the reserved key.
        current = self.inspect(request)
        if current.state != "ready" or current.binding is None or current.binding.pane_id != binding.pane_id:
            raise PaneError("pane_not_ready", "The exact managed Herdr pane is no longer at a verified primary prompt.")
        if self.client is not None and callable(getattr(self.client, "send_keys", None)):
            self.client.send_keys(binding.pane_id, [DISPATCH_KEY])
        else:
            self._request(request, "pane.send_keys", {"pane_id": binding.pane_id, "keys": [DISPATCH_KEY]}, mutation=True)


class TmuxAdapter:
    """Exact-pane tmux adapter; it never resolves a session's first pane."""

    def __init__(self, tmux_binary: str = "tmux", *, state_dir: str | os.PathLike[str] | None = None):
        self.tmux_binary = tmux_binary
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else None

    def _state(self) -> Path:
        state = self.state_dir or (Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "localsetup" / "terminal-ops")
        if not state.is_absolute():
            raise PaneError("invalid_state_dir", "Managed tmux state must use an absolute private path.")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state.is_symlink() or not state.is_dir():
            raise PaneError("unsafe_state_dir", "Managed tmux state must be a real private directory.")
        state.chmod(0o700)
        return state

    def _run(self, request: PaneRequest, args: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        endpoint = Path(request.endpoint).expanduser()
        if not endpoint.is_absolute() or "\x00" in request.endpoint:
            raise PaneError("invalid_endpoint", "tmux requires an explicit absolute server socket path.")
        try:
            info = endpoint.lstat()
        except OSError as exc:
            raise PaneError("endpoint_unavailable", "The exact tmux server socket is unavailable.") from exc
        if endpoint.is_symlink() or not stat.S_ISSOCK(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise PaneError("endpoint_unverified", "The exact tmux server socket is not owned by this controller.")
        try:
            return subprocess.run([self.tmux_binary, "-S", str(endpoint), *args], capture_output=True,
                                  text=True, errors="replace", timeout=timeout, check=False, shell=False,
                                  env=child_environment(os.environ))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PaneError("native_unavailable", "The explicit tmux server did not return a reliable response.", effect="unknown") from exc

    def _pane(self, request: PaneRequest, pane_id: str) -> dict[str, str] | None:
        result = self._run(request, ["display-message", "-t", pane_id, "-p", "-F",
                                     "#{pane_id}\t#{pane_current_path}\t#{pane_tty}\t#{pane_pid}\t#{session_id}\t#{session_name}\t#{pane_title}\t#{pane_height}\t#{pane_width}\t#{window_id}"])
        if result.returncode != 0:
            return None
        parts = result.stdout.rstrip("\n").split("\t")
        if len(parts) != 10 or parts[0] != pane_id:
            raise PaneError("native_unavailable", "tmux did not return the exact requested pane fields.")
        return dict(zip(("pane_id", "cwd", "tty", "pid", "session", "session_name", "title", "height", "width", "window_id"), parts))

    def _list(self, request: PaneRequest) -> list[str]:
        session = request.session_id or ""
        target = session if session.startswith("$") else "=" + session
        result = self._run(request, ["list-panes", "-t", target, "-F", "#{pane_id}"])
        if result.returncode != 0:
            if "can't find session" in (result.stderr + result.stdout).lower():
                return []
            raise PaneError("native_unavailable", "The explicit tmux session pane inventory is unavailable.")
        return [line for line in result.stdout.splitlines() if line]

    def _option(self, request: PaneRequest, pane_id: str, name: str) -> str | None:
        result = self._run(request, ["show-options", "-p", "-t", pane_id, "-v", name])
        return result.stdout.strip() if result.returncode == 0 else None

    def _server_credential_names(self, request: PaneRequest) -> set[str]:
        names: set[str] = set()
        for args in (["show-environment", "-g"], ["show-environment", "-t", request.session_id or ""]):
            result = self._run(request, args)
            if result.returncode != 0:
                raise PaneError("server_environment_unknown", "The tmux environment cannot be qualified before pane creation.")
            for row in result.stdout.splitlines():
                name, separator, value = row.partition("=")
                if separator and name.endswith("_SUDO_KEY") and value:
                    if not _ENV_NAME.fullmatch(name):
                        raise PaneError("server_environment_unknown", "A tmux credential variable has an invalid name.")
                    names.add(name)
        return names

    @staticmethod
    def _in_session(pane: dict[str, str], request: PaneRequest) -> bool:
        session = request.session_id or ""
        return pane.get("session") == session if session.startswith("$") else pane.get("session_name") == session

    def inspect(self, request: PaneRequest) -> PaneStatus:
        if request.backend != "tmux":
            raise PaneError("backend_mismatch", "The tmux adapter received a different backend request.")
        parent = self._pane(request, request.parent_id)
        if parent is None or not self._in_session(parent, request):
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The exact tmux parent pane is unavailable in its bound session.")
        matches: list[dict[str, str]] = []
        unsafe_label = False
        for pane_id in self._list(request):
            pane = self._pane(request, pane_id)
            if pane is None:
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  detail="The native tmux pane inventory changed during exact lookup.")
            key = self._option(request, pane_id, "@localsetup_binding_key")
            if key == request.binding_key:
                matches.append(pane)
                if pane["title"] != request.name:
                    unsafe_label = True
            elif pane["title"] == request.name and pane["window_id"] == parent["window_id"]:
                other_parent_id = self._option(request, pane_id, "@localsetup_parent_id")
                other_parent = self._pane(request, other_parent_id) if other_parent_id else None
                other_controller = self._option(request, pane_id, "@localsetup_controller")
                if not (self._option(request, pane_id, "@localsetup_managed_ops") == "1"
                        and key and other_parent_id != request.parent_id
                        and other_controller and other_controller != request.controller_id
                        and other_parent is not None and other_parent["window_id"] == parent["window_id"]):
                    unsafe_label = True
        if not matches:
            if unsafe_label:
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  detail="A matching tmux pane title has no exact managed scope metadata.")
            return PaneStatus("missing", request.backend, request.binding_key, request.name)
        if len(matches) != 1 or unsafe_label:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="More than one exact managed tmux pane matches this binding.")
        pane = matches[0]
        if (self._option(request, pane["pane_id"], "@localsetup_parent_id") != request.parent_id
                or self._option(request, pane["pane_id"], "@localsetup_repository") != request.repository
                or self._option(request, pane["pane_id"], "@localsetup_controller") != request.controller_id
                or self._option(request, pane["pane_id"], "@localsetup_host_alias") != (request.host_alias or "")
                or self._option(request, pane["pane_id"], "@localsetup_machine_id") != (request.machine_id or "")
                or self._option(request, pane["pane_id"], "@localsetup_initial_cwd") != request.cwd
                or self._option(request, pane["pane_id"], "@localsetup_session") != request.session_id
                or self._option(request, pane["pane_id"], "@localsetup_state_dir") != str(self._state())
                or not self._in_session(pane, request)):
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The exact tmux pane no longer matches its original owner scope.")
        if parent is None or parent["window_id"] != pane["window_id"]:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The managed tmux pane is no longer beside its exact parent.")
        pid = int(pane["pid"]) if pane["pid"].isdigit() else None
        tty = pane["tty"] or None
        try:
            proc = subprocess.run(["readlink", "-f", f"/proc/{pid}/exe"], capture_output=True,
                                  text=True, timeout=1, check=False,
                                  env=child_environment(os.environ)) if pid else None
            native_shell = proc.stdout.strip() if proc and proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            native_shell = None
        if native_shell is None or Path(native_shell).name != "bash":
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The tmux pane occupant is not a verifiable Bash shell.")
        if not Path(pane["cwd"]).is_dir():
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The tmux pane working directory cannot be verified.")
        state = _prompt_status(self._state(), request, pane["pane_id"], terminal_id=None,
                               tab_id=pane["window_id"], native_pid=pid, native_tty=tty,
                               label=pane["title"])
        if state.binding is not None:
            metadata = dict(state.binding.metadata)
            metadata["actual_tmux_cwd"] = pane["cwd"]
            binding = PaneBinding(**(state.binding.to_dict() | {"cwd": pane["cwd"], "metadata": metadata}))
            state = PaneStatus(state.state, state.backend, state.binding_key, state.name, binding,
                               state.operation_id, state.detail, state.changed)
        return state

    def create(self, request: PaneRequest) -> PaneBinding:
        if request.backend != "tmux":
            raise PaneError("backend_mismatch", "The tmux adapter received a different backend request.")
        if not Path(request.cwd).is_dir():
            raise PaneError("invalid_cwd", "The tmux pane working directory must exist locally.")
        parent = self._pane(request, request.parent_id)
        if parent is None or not self._in_session(parent, request):
            raise PaneError("parent_mismatch", "The exact tmux parent is not in the requested session.")
        if self._list(request) == []:
            raise PaneError("session_missing", "Managed tmux panes may only be created inside an existing session.")
        if int(parent["height"]) < 3:
            raise PaneError("pane_too_small", "The exact tmux parent is too short for a one-third-height pane.")
        state = self._state()
        bootstrap = write_bash_bootstrap(state, request.binding_key, pane_id_env="TMUX_PANE")
        bash = shutil.which("bash")
        if bash is None:
            raise PaneError("bash_missing", "A local Bash executable is required for a managed tmux pane.")
        ratio_height = max(1, int(parent["height"]) // 3)
        args = ["split-window", "-v", "-d", "-P", "-F", "#{pane_id}", "-t", request.parent_id,
                "-c", request.cwd, "-l", str(ratio_height), "-e", "INPUTRC=/dev/null",
                *[part for name in sorted(self._server_credential_names(request)) for part in ("-e", f"{name}=")],
                bash, "--noprofile", "--rcfile", str(bootstrap), "-i"]
        result = self._run(request, args, timeout=10)
        pane_id = result.stdout.strip()
        if result.returncode != 0 or not pane_id.startswith("%"):
            raise PaneError("split_unknown", "tmux did not confirm the exact pane ID; reconcile before retrying.", effect="unknown")
        metadata = {
            "@localsetup_managed_ops": "1",
            "@localsetup_binding_key": request.binding_key,
            "@localsetup_parent_id": request.parent_id,
            "@localsetup_repository": request.repository,
            "@localsetup_controller": request.controller_id,
            "@localsetup_host_alias": request.host_alias or "",
            "@localsetup_machine_id": request.machine_id or "",
            "@localsetup_initial_cwd": request.cwd,
        }
        for key, value in metadata.items():
            option = self._run(request, ["set-option", "-p", "-t", pane_id, key, value])
            if option.returncode != 0:
                raise PaneError("owner_metadata_unknown", "tmux created the pane but could not confirm its owner metadata.", effect="unknown")
        for key, value in (("@localsetup_state_dir", str(state)), ("@localsetup_session", request.session_id or "")):
            option = self._run(request, ["set-option", "-p", "-t", pane_id, key, value])
            if option.returncode != 0:
                raise PaneError("owner_metadata_unknown", "tmux created the pane but could not confirm its state binding.", effect="unknown")
        title = self._run(request, ["select-pane", "-t", pane_id, "-T", request.name])
        if title.returncode != 0:
            raise PaneError("pane_label_unknown", "tmux created the pane but its title could not be verified.", effect="unknown")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            current = self.inspect(request)
            if current.state == "ready" and current.binding is not None:
                return current.binding
            time.sleep(0.05)
        raise PaneError("shell_not_qualified", "The created tmux pane has not completed its controlled Bash handshake.", effect="unknown")

    def send_trigger(self, request: PaneRequest, binding: PaneBinding) -> None:
        current = self.inspect(request)
        if current.state != "ready" or current.binding is None or current.binding.pane_id != binding.pane_id:
            raise PaneError("pane_not_ready", "The exact managed tmux pane is no longer at a verified primary prompt.")
        # tmux on supported hosts does not consistently name F13; send its
        # documented xterm sequence as exact key bytes, never shell text.
        sent = self._run(request, ["send-keys", "-H", "-t", binding.pane_id,
                                   "1b", "5b", "32", "35", "7e"])
        if sent.returncode != 0:
            raise PaneError("dispatch_unknown", "The reserved tmux trigger result is uncertain; reconcile without replay.", effect="unknown")

