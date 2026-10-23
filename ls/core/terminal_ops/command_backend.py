"""Dedicated tmux server adapter for observable agent-owned shell sessions."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence

from .credentials import child_environment
from .command_state import ShellCommandError, process_info, process_state


_SESSION_FORMAT = "#{session_id}\t#{session_name}\t#{session_created}\t#{session_attached}"
_WINDOW_FORMAT = "#{window_id}\t#{window_name}\t#{window_active}\t#{window_panes}"
_PANE_FORMAT = "#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_current_command}\t#{pane_active}"


@dataclass(frozen=True, slots=True)
class NativeResult:
    stdout: str
    stderr: str
    returncode: int


class TmuxCommandBackend:
    """Use one tmux socket per agent session; never reuse arbitrary user panes."""

    def __init__(
        self, tmux_binary: str | None = None, *, config_file: str | None = None,
        socket_root: str | os.PathLike[str] | None = None,
    ):
        self.tmux_binary = tmux_binary or shutil.which("tmux")
        self.config_file = config_file
        self._socket_root = Path(socket_root) if socket_root is not None else None

    def capability(self) -> dict[str, Any]:
        if os.name != "posix" or not Path("/proc/self/stat").exists():
            return {"state": "unavailable", "backend": "tmux", "reason": "Linux /proc process identity is required."}
        bash = shutil.which("bash")
        if bash is None:
            return {"state": "unavailable", "backend": "tmux", "reason": "Bash is required for explicit shell scripts."}
        if not self.tmux_binary:
            return {
                "state": "unavailable", "backend": "tmux",
                "reason": "tmux is not installed.",
                "install_suggestion": "Install tmux with the operating system package manager, then retry.",
            }
        try:
            result = subprocess.run(
                [self.tmux_binary, "-V"], capture_output=True, text=True,
                errors="replace", timeout=3.0, check=False, shell=False,
                env=self._environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"state": "unavailable", "backend": "tmux", "reason": "tmux version could not be qualified."}
        version_text = result.stdout.strip()
        match = re.fullmatch(r"tmux\s+(\d+)\.(\d+)(?:[A-Za-z][A-Za-z0-9.-]*|[.-][A-Za-z0-9.-]+)?", version_text)
        if result.returncode != 0 or match is None:
            return {"state": "unavailable", "backend": "tmux", "reason": "tmux returned an unrecognized version."}
        version = (int(match.group(1)), int(match.group(2)))
        if version < (2, 6):
            return {"state": "unavailable", "backend": "tmux", "reason": "tmux 2.6 or later is required.", "version": version_text}
        return {"state": "available", "backend": "tmux", "tmux": self.tmux_binary,
                "bash": bash, "version": version_text}

    def socket_path(self, session_id: str) -> Path:
        root = self._socket_root or (Path(tempfile.gettempdir()) / f"localsetup-shell-{os.getuid()}")
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = root.lstat()
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ShellCommandError("unsafe_socket_root", "The private tmux socket directory is unsafe.")
            root.chmod(0o700)
        except OSError as exc:
            raise ShellCommandError("native_unavailable", "The private tmux socket directory is unavailable.") from exc
        return root / f"s-{session_id[:32]}.sock"

    def _environment(self) -> dict[str, str]:
        environment = child_environment(os.environ)
        # Every call targets this adapter's explicit private socket. Inheriting
        # a caller's TMUX markers could redirect command interpretation.
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        return environment

    def _run(
        self,
        endpoint: str | os.PathLike[str],
        args: Sequence[str],
        *,
        timeout: float = 8.0,
        mutation: bool = False,
        allow_create: bool = False,
    ) -> NativeResult:
        binary = self.tmux_binary
        if not binary:
            raise ShellCommandError("tmux_unavailable", "tmux is not installed.")
        socket = Path(endpoint)
        if not socket.is_absolute() or "\x00" in str(socket):
            raise ShellCommandError("invalid_endpoint", "The private tmux socket path is invalid.")
        try:
            info = socket.lstat()
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.getuid()):
                raise ShellCommandError("unsafe_endpoint", "The private tmux endpoint is not an owned socket.")
        except FileNotFoundError:
            if not allow_create:
                raise ShellCommandError("session_missing", "The exact private tmux server is not running.")
        argv = [binary]
        if self.config_file:
            argv.extend(["-f", self.config_file])
        argv.extend(["-S", str(socket), *args])
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ShellCommandError(
                "native_ack_unknown", "tmux did not return a reliable acknowledgement.",
                effect="unknown" if mutation else "none",
            ) from exc
        except OSError as exc:
            raise ShellCommandError("native_unavailable", "The exact private tmux endpoint is unavailable.") from exc
        if allow_create:
            try:
                info = socket.lstat()
            except OSError as exc:
                if result.returncode == 0:
                    raise ShellCommandError(
                        "native_identity_unknown", "tmux acknowledged creation without a verifiable owned socket.",
                        effect="unknown",
                    ) from exc
            else:
                if (stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode)
                        or info.st_uid != os.getuid()):
                    raise ShellCommandError(
                        "unsafe_endpoint", "tmux created an endpoint that cannot be qualified.", effect="unknown",
                    )
        return NativeResult(result.stdout, result.stderr, result.returncode)

    @staticmethod
    def _endpoint_state(endpoint: str | os.PathLike[str]) -> str:
        path = Path(endpoint)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unknown"
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ShellCommandError("unsafe_endpoint", "The private tmux endpoint is not an owner-private socket.")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.2)
        try:
            client.connect(str(path))
            return "live"
        except OSError as exc:
            if exc.errno in {errno.ECONNREFUSED, errno.ENOENT}:
                return "stale" if exc.errno == errno.ECONNREFUSED else "missing"
            return "unknown"
        finally:
            client.close()

    def remove_stale_endpoint(self, record: Mapping[str, Any]) -> bool:
        """Remove this session's exact owned socket only after proving no listener."""
        endpoint = str(record.get("endpoint", ""))
        path = Path(endpoint)
        state = self._endpoint_state(path)
        if state == "missing":
            return True
        if state != "stale":
            return False
        before = path.lstat()
        try:
            parent = path.parent.lstat()
        except OSError:
            return False
        if (stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
            raise ShellCommandError("unsafe_socket_root", "The private tmux socket directory is unsafe.")
        if self._endpoint_state(path) != "stale":
            return False
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ShellCommandError("endpoint_identity_changed", "The exact private endpoint changed during cleanup.", effect="unknown")
        path.unlink()
        return not path.exists()

    def _checked(
        self,
        endpoint: str | os.PathLike[str],
        args: Sequence[str],
        *,
        mutation: bool = False,
        allow_create: bool = False,
    ) -> str:
        result = self._run(endpoint, args, mutation=mutation, allow_create=allow_create)
        if result.returncode != 0:
            raise ShellCommandError(
                "native_command_failed", "tmux rejected a verified shell lifecycle operation.",
                effect="unknown" if mutation else "none",
            )
        return result.stdout

    @staticmethod
    def _private_target(native_session_id: str) -> str:
        if not isinstance(native_session_id, str) or not native_session_id.startswith("$") or not native_session_id[1:].isdigit():
            raise ShellCommandError("invalid_native_session", "The exact tmux session identity is invalid.")
        return native_session_id

    def create_session(self, record: Mapping[str, Any], *, cwd: str, settings: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = str(record["endpoint"])
        native_name = str(record["native_name"])
        sleep = shutil.which("sleep")
        if not sleep:
            raise ShellCommandError("sleep_unavailable", "The non-executing tmux status process is unavailable.")
        output = self._checked(
            endpoint,
            ["start-server", ";", "set-option", "-g", "history-limit", str(settings["history_limit"]), ";",
             "new-session", "-d", "-P", "-F", "#{session_id}\t#{window_id}\t#{pane_id}",
             "-s", native_name, "-n", "status", "-c", cwd, sleep, "86400"],
            mutation=True,
            allow_create=True,
        )
        fields = output.rstrip("\n").split("\t")
        if len(fields) != 3 or not fields[0].startswith("$") or not fields[1].startswith("@") or not fields[2].startswith("%"):
            raise ShellCommandError("native_identity_unknown", "tmux created no verifiable session identity.", effect="unknown")
        native_session_id, status_window_id, status_pane_id = fields
        target = self._private_target(native_session_id)
        # Keep the pane after its short bootstrap exits. The session's steady
        # state is a dead, readable status pane rather than an extra process.
        self._checked(endpoint, ["set-window-option", "-t", status_window_id,
                                 "remain-on-exit", "on"], mutation=True)
        pane_text = self._checked(endpoint, ["display-message", "-t", status_pane_id,
                                             "-p", "-F", _PANE_FORMAT])
        pane_parts = pane_text.rstrip("\n").split("\t")
        if (len(pane_parts) != 7 or pane_parts[0] != status_pane_id
                or pane_parts[3] != "0" or not pane_parts[1].isdigit()
                or pane_parts[5] != "sleep"):
            raise ShellCommandError("status_pane_identity_unknown", "The temporary status pane process cannot be qualified.", effect="unknown")
        status_pane_pid = int(pane_parts[1])
        bootstrap = process_info(status_pane_pid)
        try:
            cmdline = Path(f"/proc/{status_pane_pid}/cmdline").read_bytes().split(b"\0")
        except OSError as exc:
            raise ShellCommandError("status_pane_identity_unknown", "The temporary status process disappeared before qualification.", effect="unknown") from exc
        if (bootstrap is None or len(cmdline) < 3
                or Path(os.fsdecode(cmdline[0])).resolve() != Path(sleep).resolve()
                or cmdline[1:] != [b"86400", b""]):
            raise ShellCommandError("status_pane_identity_unknown", "The temporary status command does not match the expected process.", effect="unknown")
        self._stop_status_bootstrap(status_pane_pid, bootstrap["starttime"])
        session_options = {
            "@localsetup_shell_api": "1",
            "@localsetup_shell_handle": str(record["handle"]),
            "@localsetup_shell_key": str(record["session_key"]),
            "@localsetup_shell_machine": str(record["machine_generation"]),
            "@localsetup_shell_owner": str(record["owner_fingerprint"]),
            "@localsetup_shell_profile": str(record["settings_digest"]),
            "@localsetup_shell_status_window": status_window_id,
        }
        # Every mutable setting is scoped to our exact private session/window.
        mouse = "on" if settings["mouse"] else "off"
        self._checked(endpoint, ["set-option", "-t", target, "mouse", mouse], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status", "on" if settings["status"] else "off"], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status-position", str(settings["status_position"])], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "destroy-unattached", "off"], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", status_window_id, "history-limit", str(settings["history_limit"])], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", status_window_id, "@localsetup_shell_status", "1"], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", status_window_id, "@localsetup_shell_session", str(record["handle"])], mutation=True)
        for name, value in session_options.items():
            self._checked(endpoint, ["set-option", "-t", target, name, value], mutation=True)
        session_text = self._checked(endpoint, ["display-message", "-t", target, "-p", "-F", _SESSION_FORMAT])
        window_text = self._checked(endpoint, ["display-message", "-t", status_window_id, "-p", "-F", _WINDOW_FORMAT])
        pane_text = self._checked(endpoint, ["display-message", "-t", status_pane_id, "-p", "-F", _PANE_FORMAT])
        session_parts, window_parts, pane_parts = (line.split("\t") for line in (session_text.rstrip("\n"), window_text.rstrip("\n"), pane_text.rstrip("\n")))
        if (len(session_parts) != 4 or session_parts[0] != native_session_id
                or len(window_parts) != 4 or window_parts[0] != status_window_id
                or len(pane_parts) != 7 or pane_parts[0] != status_pane_id):
            raise ShellCommandError("native_identity_unknown", "The new tmux session failed exact identity verification.", effect="unknown")
        pane_limit = self._checked(endpoint, ["display-message", "-p", "-t", status_pane_id,
                                              "-F", "#{history_limit}"]).strip()
        if pane_limit != str(settings["history_limit"]):
            raise ShellCommandError("profile_readback_mismatch", "The new pane did not adopt the requested history capacity.", effect="unknown")
        sessions = self._checked(endpoint, ["list-sessions", "-F", "#{session_id}\t#{session_name}"]).splitlines()
        if len(sessions) != 1 or sessions[0].split("\t", 1)[0] != native_session_id:
            raise ShellCommandError("unexpected_native_resources", "The private tmux server contains an unexpected session.", effect="unknown")
        return {
            "native_session_id": native_session_id,
            "server_session_name": session_parts[1],
            "status_window_id": status_window_id,
            "status_pane_id": status_pane_id,
            "status_pane_pid": int(pane_parts[1]) if pane_parts[1].isdigit() else None,
            "session_created": session_parts[2],
            "endpoint": endpoint,
        }

    @staticmethod
    def _stop_status_bootstrap(pid: int, starttime: int) -> None:
        """Terminate only the exact, qualified temporary pane process."""
        opener = getattr(os, "pidfd_open", None)
        sender = getattr(signal, "pidfd_send_signal", None)
        if opener is None or sender is None:
            raise ShellCommandError("status_pane_cleanup_unavailable", "Exact temporary pane cleanup requires pidfd support.", effect="unknown")
        current = process_info(pid)
        if current is None or current["starttime"] != starttime:
            raise ShellCommandError("status_pane_identity_unknown", "The temporary status process identity changed.", effect="unknown")
        try:
            descriptor = opener(pid, 0)
        except OSError as exc:
            raise ShellCommandError("status_pane_cleanup_pending", "The exact temporary process could not be opened for cleanup.", effect="unknown") from exc
        try:
            current = process_info(pid)
            if current is None or current["starttime"] != starttime:
                raise ShellCommandError("status_pane_identity_unknown", "The temporary status process identity changed.", effect="unknown")
            sender(descriptor, signal.SIGTERM, None, 0)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                current = process_info(pid)
                if (current is None or current["starttime"] != starttime
                        or process_state(pid) in {"Z", "X"}):
                    return
                time.sleep(0.02)
            current = process_info(pid)
            if (current is not None and current["starttime"] == starttime
                    and process_state(pid) not in {"Z", "X"}):
                sender(descriptor, signal.SIGKILL, None, 0)
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    current = process_info(pid)
                    if (current is None or current["starttime"] != starttime
                            or process_state(pid) in {"Z", "X"}):
                        return
                    time.sleep(0.02)
                raise ShellCommandError("status_pane_cleanup_pending", "The temporary process remains after termination.", effect="unknown")
        except OSError as exc:
            raise ShellCommandError("status_pane_cleanup_pending", "The exact temporary process could not be terminated.", effect="unknown") from exc
        finally:
            os.close(descriptor)

    def inspect_session(self, record: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = str(record["endpoint"])
        endpoint_state = self._endpoint_state(endpoint)
        if endpoint_state in {"missing", "stale"}:
            return {"state": "missing", "windows": []}
        if endpoint_state != "live":
            return {"state": "unknown", "windows": []}
        native_session_id = record.get("native_session_id")
        target = self._private_target(native_session_id)
        session_text = self._checked(endpoint, ["display-message", "-t", target, "-p", "-F", _SESSION_FORMAT])
        session_parts = session_text.rstrip("\n").split("\t")
        if len(session_parts) != 4 or session_parts[0] != native_session_id:
            return {"state": "unknown", "windows": []}
        sessions = self._checked(endpoint, ["list-sessions", "-F", "#{session_id}\t#{session_name}"]).splitlines()
        if len(sessions) != 1 or sessions[0].split("\t", 1)[0] != native_session_id:
            return {"state": "unknown", "windows": []}
        options: dict[str, str | None] = {}
        for name in ("api", "handle", "key", "machine", "owner", "profile", "status_window"):
            option = f"@localsetup_shell_{name}"
            result = self._run(endpoint, ["show-options", "-t", target, "-qv", option])
            options[name] = result.stdout.strip() if result.returncode == 0 else None
        windows_text = self._checked(endpoint, ["list-windows", "-t", target, "-F", _WINDOW_FORMAT])
        windows: list[dict[str, Any]] = []
        for row in windows_text.splitlines():
            fields = row.split("\t")
            if len(fields) != 4:
                return {"state": "unknown", "windows": []}
            window_id, name, active, pane_count = fields
            metadata: dict[str, str | None] = {}
            for key in ("status", "session", "operation", "request"):
                option = f"@localsetup_shell_{key}"
                result = self._run(endpoint, ["show-options", "-w", "-t", window_id, "-qv", option])
                metadata[key] = result.stdout.strip() if result.returncode == 0 else None
            pane_text = self._checked(endpoint, ["list-panes", "-t", window_id, "-F", _PANE_FORMAT])
            panes: list[dict[str, Any]] = []
            for pane_row in pane_text.splitlines():
                parts = pane_row.split("\t")
                if len(parts) != 7:
                    return {"state": "unknown", "windows": []}
                panes.append({
                    "pane_id": parts[0], "pane_pid": int(parts[1]) if parts[1].isdigit() else None,
                    "pane_tty": parts[2], "dead": parts[3] == "1",
                    "dead_status": parts[4], "current_command": parts[5], "active": parts[6] == "1",
                })
            windows.append({
                "window_id": window_id, "name": name, "active": active == "1",
                "pane_count": int(pane_count) if pane_count.isdigit() else -1,
                "metadata": metadata, "panes": panes,
            })
        status = self._run(endpoint, ["show-options", "-t", target, "-qv", "status"])
        position = self._run(endpoint, ["show-options", "-t", target, "-qv", "status-position"])
        mouse = self._run(endpoint, ["show-options", "-t", target, "-qv", "mouse"])
        history: dict[str, int | None] = {}
        pane_history: dict[str, int | None] = {}
        for window in windows:
            result = self._run(endpoint, ["show-options", "-w", "-t", window["window_id"], "-qv", "history-limit"])
            value = result.stdout.strip()
            history[window["window_id"]] = int(value) if result.returncode == 0 and value.isdigit() else None
            for pane in window["panes"]:
                actual = self._run(endpoint, ["display-message", "-p", "-t", pane["pane_id"],
                                              "-F", "#{history_limit}"])
                actual_value = actual.stdout.strip()
                pane_history[pane["pane_id"]] = (
                    int(actual_value) if actual.returncode == 0 and actual_value.isdigit() else None
                )
        session_fields = dict(zip(("id", "name", "created", "attached"), session_parts))
        if session_fields["attached"].isdigit():
            session_fields["attached"] = int(session_fields["attached"])
        return {
            "state": "ready", "session": session_fields,
            "options": options, "windows": windows,
            "applied_settings": {
                "mouse": mouse.stdout.strip() == "on" if mouse.returncode == 0 else None,
                "status": status.stdout.strip() == "on" if status.returncode == 0 else None,
                "status_position": position.stdout.strip() if position.returncode == 0 else None,
                "history_limit": history,
                "pane_history_limit": pane_history,
            },
        }

    def apply_session_settings(
        self, record: Mapping[str, Any], settings: Mapping[str, Any], profile_digest: str,
    ) -> dict[str, Any]:
        """Change only this dedicated server/session and verify native readback."""
        endpoint = str(record["endpoint"])
        target = self._private_target(str(record["native_session_id"]))
        before = self.inspect_session(record)
        if before.get("state") != "ready":
            raise ShellCommandError("session_identity_unknown", "The private tmux session failed identity verification.", effect="unknown")
        opts = before.get("options", {})
        if (opts.get("handle") != record.get("handle") or opts.get("key") != record.get("session_key")
                or opts.get("machine") != record.get("machine_generation")):
            raise ShellCommandError("session_identity_unknown", "The private tmux session identity changed.", effect="unknown")
        if settings["history_limit"] != record["settings"]["history_limit"]:
            raise ShellCommandError("profile_history_requires_new_session", "Existing tmux panes cannot change history capacity; create a new session with this profile.")
        self._checked(endpoint, ["set-option", "-t", target, "mouse", "on" if settings["mouse"] else "off"], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status", "on" if settings["status"] else "off"], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status-position", str(settings["status_position"])], mutation=True)
        for window in before.get("windows", []):
            self._checked(endpoint, ["set-window-option", "-t", window["window_id"],
                                     "history-limit", str(settings["history_limit"])], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "@localsetup_shell_profile", profile_digest], mutation=True)
        after = self.inspect_session(record)
        if after.get("state") != "ready":
            raise ShellCommandError("profile_readback_unknown", "tmux profile changes could not be read back.", effect="unknown")
        native = after.get("applied_settings", {})
        if (native.get("mouse") is not settings["mouse"] or native.get("status") is not settings["status"]
                or native.get("status_position") != settings["status_position"]
                or any(value != settings["history_limit"] for value in native.get("history_limit", {}).values())
                or any(value != settings["history_limit"] for value in native.get("pane_history_limit", {}).values())):
            raise ShellCommandError("profile_readback_mismatch", "tmux did not read back all requested profile settings.", effect="unknown")
        return {"state": "applied", "settings": dict(settings), "native_settings": native}

    def set_owner_identity(
        self, record: Mapping[str, Any], *, allowed_previous_owners: set[str],
    ) -> None:
        """Update the exact private session owner marker after explicit recovery."""
        endpoint = str(record["endpoint"])
        target = self._private_target(str(record["native_session_id"]))
        observed = self.inspect_session(record)
        opts = observed.get("options", {})
        if (observed.get("state") != "ready" or opts.get("handle") != record.get("handle")
                or opts.get("key") != record.get("session_key")
                or opts.get("machine") != record.get("machine_generation")
                or opts.get("owner") not in allowed_previous_owners
                or type(observed.get("session", {}).get("attached")) is not int
                or observed["session"]["attached"] != 0):
            raise ShellCommandError("session_identity_unknown", "Recovery cannot verify the exact tmux session.", effect="unknown")
        self._checked(endpoint, ["set-option", "-t", target, "@localsetup_shell_owner",
                                 str(record["owner_fingerprint"])], mutation=True)
        result = self._run(endpoint, ["show-options", "-t", target, "-qv", "@localsetup_shell_owner"])
        if result.returncode != 0 or result.stdout.strip() != record["owner_fingerprint"]:
            raise ShellCommandError("owner_readback_mismatch", "The adopted owner identity was not read back.", effect="unknown")

    def create_operation_window(
        self,
        record: Mapping[str, Any],
        operation: Mapping[str, Any],
        *,
        command: Sequence[str],
    ) -> dict[str, Any]:
        endpoint = str(record["endpoint"])
        target = self._private_target(str(record["native_session_id"]))
        name = str(operation["native_window_name"])
        output = self._checked(
            endpoint,
            ["new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}", "-t", target,
             "-n", name, "-c", str(operation["cwd"]), shlex.join(list(command))],
            mutation=True,
        )
        fields = output.rstrip("\n").split("\t")
        if len(fields) != 2 or not fields[0].startswith("@") or not fields[1].startswith("%"):
            raise ShellCommandError("native_identity_unknown", "tmux created no verifiable command window.", effect="unknown")
        return {"window_id": fields[0], "pane_id": fields[1]}

    def tag_operation_window(self, record: Mapping[str, Any], operation: Mapping[str, Any], native: Mapping[str, Any]) -> None:
        endpoint = str(record["endpoint"])
        window_id = str(native["window_id"])
        values = {
            "@localsetup_shell_session": str(record["handle"]),
            "@localsetup_shell_operation": str(operation["operation_id"]),
            "@localsetup_shell_request": str(operation["request_digest"]),
        }
        for name, value in values.items():
            self._checked(endpoint, ["set-window-option", "-t", window_id, name, value], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", window_id, "history-limit", str(record["settings"]["history_limit"])], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", window_id, "remain-on-exit", "on"], mutation=True)
        actual = self._checked(endpoint, ["display-message", "-p", "-t", str(native["pane_id"]),
                                          "-F", "#{history_limit}"]).strip()
        if actual != str(record["settings"]["history_limit"]):
            raise ShellCommandError("profile_readback_mismatch", "The command pane has a different history capacity.", effect="unknown")

    def start_capture(
        self,
        record: Mapping[str, Any],
        operation: Mapping[str, Any],
        *,
        command: Sequence[str],
    ) -> None:
        endpoint = str(record["endpoint"])
        pane_id = str(operation["pane_id"])
        self._checked(endpoint, ["pipe-pane", "-o", "-t", pane_id, shlex.join(list(command))], mutation=True)

    def stop_capture(self, record: Mapping[str, Any], operation: Mapping[str, Any]) -> None:
        """Close the exact pane output pipe so its collector can flush and exit."""
        self._checked(str(record["endpoint"]),
                      ["pipe-pane", "-t", str(operation["pane_id"])], mutation=True)

    def select_operation(self, record: Mapping[str, Any], operation: Mapping[str, Any]) -> None:
        self._checked(str(record["endpoint"]), ["select-window", "-t", str(operation["window_id"])], mutation=True)

    def capture_screen(self, record: Mapping[str, Any], operation: Mapping[str, Any] | None, *, lines: int) -> str:
        endpoint = str(record["endpoint"])
        target = (str(operation["pane_id"]) if operation and operation.get("pane_id")
                  else str(record["status_pane_id"]))
        return self._checked(endpoint, ["capture-pane", "-p", "-S", f"-{lines}", "-t", target])

    def send_input(self, record: Mapping[str, Any], operation: Mapping[str, Any], *, buffer_name: str,
                   input_path: str, eof: bool) -> None:
        endpoint = str(record["endpoint"])
        pane_id = str(operation["pane_id"])
        self._checked(endpoint, ["load-buffer", "-b", buffer_name, input_path], mutation=True)
        self._checked(endpoint, ["paste-buffer", "-d", "-b", buffer_name, "-t", pane_id], mutation=True)
        if eof:
            self._checked(endpoint, ["send-keys", "-t", pane_id, "C-d"], mutation=True)

    def kill_window(
        self, record: Mapping[str, Any], operation: Mapping[str, Any],
        *, expected_window: Mapping[str, Any],
    ) -> None:
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        windows = [window for window in observed.get("windows", [])
                   if window.get("window_id") == operation.get("window_id")]
        if (observed.get("state") != "ready"
                or options.get("handle") != record.get("handle")
                or options.get("key") != record.get("session_key")
                or options.get("machine") != record.get("machine_generation")
                or options.get("owner") != record.get("owner_fingerprint")
                or type(observed.get("session", {}).get("attached")) is not int
                or observed["session"]["attached"] != 0
                or len(windows) != 1
                or self.window_identity(windows[0]) != expected_window):
            raise ShellCommandError("native_inventory_unknown", "The exact command window changed before cleanup.", effect="unknown")
        self._checked(str(record["endpoint"]), ["kill-window", "-t", str(operation["window_id"])], mutation=True)

    @staticmethod
    def window_identity(window: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "window_id": window.get("window_id"),
            "metadata": window.get("metadata"),
            "pane_count": window.get("pane_count"),
            "panes": sorted(({
                "pane_id": pane.get("pane_id"), "pane_pid": pane.get("pane_pid"),
                "dead": pane.get("dead"),
            } for pane in window.get("panes", [])), key=lambda pane: str(pane["pane_id"])),
        }

    @staticmethod
    def inventory_identity(observed: Mapping[str, Any]) -> dict[str, Any]:
        """Identity-bearing tmux readback used for a final pre-delete comparison."""
        return {
            "session": observed.get("session"),
            "options": observed.get("options"),
            "windows": sorted((TmuxCommandBackend.window_identity(window)
                               for window in observed.get("windows", [])),
                              key=lambda window: str(window["window_id"])),
        }

    def close_session(self, record: Mapping[str, Any], *, expected_inventory: Mapping[str, Any]) -> None:
        endpoint = str(record["endpoint"])
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        if (observed.get("state") != "ready"
                or options.get("handle") != record.get("handle")
                or options.get("key") != record.get("session_key")
                or options.get("machine") != record.get("machine_generation")
                or type(observed.get("session", {}).get("attached")) is not int
                or observed["session"]["attached"] != 0
                or self.inventory_identity(observed) != expected_inventory):
            raise ShellCommandError("session_identity_unknown", "The private tmux server failed exact identity checks before close.", effect="unknown")
        # This socket was created exclusively for this one session. Kill the
        # dedicated server as well as the session so normal close removes the
        # native endpoint rather than leaving an empty server behind.
        self._checked(endpoint, ["kill-server"], mutation=True)
        deadline = time.monotonic() + 1.0
        while self._endpoint_state(endpoint) == "live" and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.remove_stale_endpoint(record):
            raise ShellCommandError("native_cleanup_pending", "The exact private tmux server did not remove its socket yet.", effect="unknown")

    def attach(self, record: Mapping[str, Any], operation: Mapping[str, Any] | None = None) -> int:
        endpoint = str(record["endpoint"])
        target = self._private_target(str(record["native_session_id"]))
        if operation is not None:
            window_id = operation.get("window_id")
            pane_id = operation.get("pane_id")
            if (not isinstance(window_id, str) or not re.fullmatch(r"@[0-9]+", window_id)
                    or not isinstance(pane_id, str) or not re.fullmatch(r"%[0-9]+", pane_id)):
                raise ShellCommandError("invalid_native_pane", "The exact operation pane target is invalid.")
            target = f"{target}:{window_id}"
        env = self._environment()
        if not os.isatty(0) or not os.isatty(1):
            raise ShellCommandError("observer_tty_required", "Attach requires an interactive terminal.")
        return subprocess.run(
            [self.tmux_binary or "tmux", "-S", endpoint, "attach-session", "-r", "-t", target],
            env=env, check=False, shell=False,
        ).returncode
