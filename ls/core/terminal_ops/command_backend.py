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
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from ..application_profiles.service import load_tmux_runtime_defaults

from .credentials import child_environment
from .command_state import ShellCommandError, process_info, process_state
from .command_native_backend import NativeCanvasV3, native_control_bindings


_SESSION_FORMAT = "#{session_id}\t#{session_name}\t#{session_created}\t#{session_attached}"
_WINDOW_FORMAT = "#{window_id}\t#{window_name}\t#{window_active}\t#{window_panes}"
_PANE_FORMAT = ("#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{pane_dead}\t"
                "#{pane_dead_status}\t#{pane_current_command}\t#{pane_active}\t"
                "#{@localsetup_shell_session}\t#{@localsetup_shell_operation}\t"
                "#{@localsetup_shell_request}\t#{@localsetup_shell_label}")


def _runtime_settings(record: Mapping[str, Any]) -> Mapping[str, Any]:
    stored = record.get("runtime_settings")
    if isinstance(stored, dict):
        return stored
    return load_tmux_runtime_defaults(
        source_root=Path(__file__).resolve().parents[3], name="agent-shell")["settings"]


def _family_width(record: Mapping[str, Any]) -> int:
    width = (record.get("canvas_width") if record.get("layout_mode") in {"canvas-v2", "canvas-v3"} else
             _runtime_settings(record)["layout"]["canvas_columns"])
    maximum = 500 if record.get("layout_mode") in {"canvas-v2", "canvas-v3"} else 160
    if type(width) is not int or not 80 <= width <= maximum:
        raise ShellCommandError("family_text_contract_invalid", "The native family canvas width is unqualified.")
    return width


def _join_status_row(record: Mapping[str, Any], theme: Mapping[str, str],
                     runtime: Mapping[str, Any]) -> str:
    if runtime["baseline"]["status_rows"] == 1:
        return ""
    return f" #[fg={theme['accent']}]localsetup shell attach --session {record['native_name']}"


def _status_rows_option(runtime: Mapping[str, Any], enabled: bool) -> str:
    if not enabled:
        return "off"
    rows = runtime["baseline"]["status_rows"]
    return "on" if rows == 1 else str(rows)


def _canvas_page_command(record: Mapping[str, Any], step: int) -> str:
    return shlex.join(["env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
                       sys.executable, "-m", "ls.core.terminal_ops.command_canvas",
                       "--session-record", str(record["session_record_path"]),
                       "--page-step", str(step), "--client-pid", "#{client_pid}"])


def _canvas_mouse_command(record: Mapping[str, Any]) -> str:
    return _canvas_page_command(record, 0) + " --mouse-x #{mouse_x} --mouse-y #{mouse_y}"


def _native_control_serve_command(record: Mapping[str, Any]) -> str:
    return shlex.join(["env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
                       sys.executable, "-m", "ls.core.terminal_ops.command_native_control",
                       "--session-record", str(record["session_record_path"]), "--serve"])


@dataclass(frozen=True, slots=True)
class NativeResult:
    stdout: str
    stderr: str
    returncode: int


class TmuxCommandBackend:
    """Use one tmux socket per agent session; never reuse arbitrary user panes."""

    _OBSERVER_COPY_KEYS = {
        "Up": "cursor-up", "Down": "cursor-down", "PPage": "page-up",
        "NPage": "page-down", "Home": "history-top", "End": "history-bottom",
        "q": "cancel", "Escape": "cancel",
    }

    def __init__(
        self, tmux_binary: str | None = None, *, config_file: str | None = None,
        socket_root: str | os.PathLike[str] | None = None,
    ):
        self.tmux_binary = tmux_binary or shutil.which("tmux")
        self.config_file = config_file
        self._socket_root = Path(socket_root) if socket_root is not None else None
        self._native_canvas_v3 = NativeCanvasV3(self)

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
        argv.extend(["-f", self.config_file or "/dev/null"])
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

    def inspect_canvas_v3(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return self._native_canvas_v3.inspect(record)

    def apply_native_layout(
        self, record: Mapping[str, Any], plan: Mapping[str, Any],
        expected_inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._native_canvas_v3.apply(record, plan, expected_inventory)

    def create_native_operation(
        self, record: Mapping[str, Any], operation: Mapping[str, Any], *,
        command: Sequence[str], pane_rows: int | None = None,
    ) -> dict[str, Any]:
        return self._native_canvas_v3.create_operation(
            record, operation, command=command, pane_rows=pane_rows)

    def remove_native_operation(
        self, record: Mapping[str, Any], operation: Mapping[str, Any], *,
        expected_inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._native_canvas_v3.remove_operation(
            record, operation, expected_inventory=expected_inventory)

    def show_native_menu(
        self, record: Mapping[str, Any], client_pid: int, mode_label: str,
        entries: Sequence[Mapping[str, Any]], *, on_spawn: Callable[[int, int], None],
    ) -> dict[str, Any]:
        return self._native_canvas_v3.show_menu(
            record, client_pid, mode_label, entries, on_spawn=on_spawn)

    def show_native_overlay(
        self, record: Mapping[str, Any], client_pid: int, title: str,
        lines: Sequence[str], *, on_spawn: Callable[[int, int], None],
    ) -> dict[str, Any]:
        return self._native_canvas_v3.show_overlay(
            record, client_pid, title, lines, on_spawn=on_spawn)

    def dismiss_native_overlay(
        self, record: Mapping[str, Any], client_pid: int,
        expected_overlay_pid: int | None, expected_overlay_starttime: int | None,
    ) -> dict[str, Any]:
        return self._native_canvas_v3.dismiss_overlay(
            record, client_pid, expected_overlay_pid, expected_overlay_starttime)

    def show_native_dialog(
        self, record: Mapping[str, Any], client_pid: int, channel: Any, *,
        on_spawn: Callable[[int, int], None],
    ) -> dict[str, Any]:
        return self._native_canvas_v3.show_dialog(record, client_pid, channel, on_spawn=on_spawn)

    def create_session(self, record: Mapping[str, Any], *, cwd: str, settings: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = str(record["endpoint"])
        native_name = str(record["native_name"])
        runtime = _runtime_settings(record)
        theme = runtime["theme"]
        sleep = shutil.which("sleep")
        if not sleep:
            raise ShellCommandError("sleep_unavailable", "The non-executing tmux status process is unavailable.")
        command = ["start-server", ";", "set-option", "-g", "history-limit",
                   str(settings["history_limit"]), ";"]
        command.extend(["new-session", "-d", "-P", "-F", "#{session_id}\t#{window_id}\t#{pane_id}",
                        "-s", native_name, "-n", "status", "-c", cwd])
        canvas_mode = record.get("layout_mode") == "canvas-v2"
        native_canvas_mode = record.get("layout_mode") == "canvas-v3"
        if record.get("layout_mode") in {"family-v1", "canvas-v2", "canvas-v3"}:
            command.extend(["-x", str(_family_width(record)), "-y", str(record["primary_viewport_height"])])
        if canvas_mode:
            command.append(shlex.join(["env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
                                       sys.executable, "-m", "ls.core.terminal_ops.command_canvas",
                                       "--session-record", str(record["session_record_path"])]))
        elif native_canvas_mode:
            command.append(_native_control_serve_command(record))
        else:
            command.extend([sleep, "86400"])
        output = self._checked(endpoint, command, mutation=True, allow_create=True)
        fields = output.rstrip("\n").split("\t")
        if len(fields) != 3 or not fields[0].startswith("$") or not fields[1].startswith("@") or not fields[2].startswith("%"):
            raise ShellCommandError("native_identity_unknown", "tmux created no verifiable session identity.", effect="unknown")
        native_session_id, status_window_id, status_pane_id = fields
        target = self._private_target(native_session_id)
        if record.get("layout_mode") in {"family-v1", "canvas-v2", "canvas-v3"}:
            self._checked(endpoint, ["set-option", "-t", target, "window-size", "manual"], mutation=True)
        # Keep the pane after its short bootstrap exits. The session's steady
        # state is a dead, readable status pane rather than an extra process.
        self._checked(endpoint, ["set-window-option", "-t", status_window_id,
                                 "remain-on-exit", "on"], mutation=True)
        self._checked(endpoint, ["set-option", "-p", "-t", status_pane_id,
                                 "remain-on-exit-format", runtime["recovery"]["idle_label"]], mutation=True)
        pane_text = self._checked(endpoint, ["display-message", "-t", status_pane_id,
                                             "-p", "-F", _PANE_FORMAT])
        pane_parts = pane_text.rstrip("\n").split("\t")
        if (len(pane_parts) != 11 or pane_parts[0] != status_pane_id
                or pane_parts[3] != "0" or not pane_parts[1].isdigit()
                or (not canvas_mode and not native_canvas_mode and pane_parts[5] != "sleep")):
            raise ShellCommandError("status_pane_identity_unknown", "The temporary status pane process cannot be qualified.", effect="unknown")
        status_pane_pid = int(pane_parts[1])
        bootstrap = process_info(status_pane_pid)
        try:
            cmdline = Path(f"/proc/{status_pane_pid}/cmdline").read_bytes().split(b"\0")
        except OSError as exc:
            raise ShellCommandError("status_pane_identity_unknown", "The temporary status process disappeared before qualification.", effect="unknown") from exc
        if (bootstrap is None or len(cmdline) < 3 or
                (canvas_mode and (Path(os.fsdecode(cmdline[0])).resolve() != Path(sys.executable).resolve()
                                  or b"ls.core.terminal_ops.command_canvas" not in cmdline)) or
                (native_canvas_mode and (Path(os.fsdecode(cmdline[0])).resolve() != Path(sys.executable).resolve()
                                        or b"ls.core.terminal_ops.command_native_control" not in cmdline
                                        or b"--serve" not in cmdline)) or
                (not canvas_mode and not native_canvas_mode and (Path(os.fsdecode(cmdline[0])).resolve() != Path(sleep).resolve()
                                      or cmdline[1:] != [b"86400", b""]))):
            raise ShellCommandError("status_pane_identity_unknown", "The temporary status command does not match the expected process.", effect="unknown")
        if not canvas_mode and not native_canvas_mode:
            self._stop_status_bootstrap(status_pane_pid, bootstrap["starttime"])
        session_options = {
            "@localsetup_shell_api": "1",
            "@localsetup_shell_handle": str(record["handle"]),
            "@localsetup_shell_key": str(record["session_key"]),
            "@localsetup_shell_machine": str(record["machine_generation"]),
            "@localsetup_shell_owner": str(record["owner_fingerprint"]),
            "@localsetup_shell_profile": str(record["settings_digest"]),
            "@localsetup_shell_status_window": status_window_id,
            "@localsetup_shell_layout": str(record.get("layout_mode", "legacy-v1")),
        }
        # Every mutable setting is scoped to our exact private session/window.
        mouse = "on" if settings["mouse"] else "off"
        self._checked(endpoint, ["set-option", "-t", target, "mouse", mouse], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status",
                                 _status_rows_option(runtime, settings["status"])], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status-position", str(settings["status_position"])], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status-interval",
                                 str(runtime["baseline"]["status_interval_seconds"])], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status-style",
                                 f"bg={theme['background']},fg={theme['accent']}"], mutation=True)
        for key, value in (("@localsetup_shell_active", "0"), ("@localsetup_shell_done", "0"),
                           ("@localsetup_shell_output_bytes", "0"), ("@localsetup_shell_elapsed", "0"),
                           ("@localsetup_shell_input_state", "RO")):
            self._checked(endpoint, ["set-option", "-t", target, key, value], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status-format[0]",
                                 " #[fg=" + theme["accent"] + "][LS] #[fg=" + theme["text"] + "]"
                                 "#{@localsetup_shell_input_state} | active #{@localsetup_shell_active}"
                                 " | done #{@localsetup_shell_done} | out #{@localsetup_shell_output_bytes}B"
                                 " | elapsed #{@localsetup_shell_elapsed}s"], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "status-format[1]",
                                 _join_status_row(record, theme, runtime)], mutation=True)
        self._checked(endpoint, ["set-option", "-t", target, "destroy-unattached", "off"], mutation=True)
        self._checked(endpoint, ["set-option", "-s", "exit-empty", "on"], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", status_window_id, "history-limit", str(settings["history_limit"])], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", status_window_id, "@localsetup_shell_status", "1"], mutation=True)
        self._checked(endpoint, ["set-window-option", "-t", status_window_id, "@localsetup_shell_session", str(record["handle"])], mutation=True)
        if record.get("layout_mode") in {"family-v1", "canvas-v2", "canvas-v3"}:
            self._checked(endpoint, ["set-option", "-p", "-t", status_pane_id,
                                     "@localsetup_shell_label", "root · Idle"], mutation=True)
            self._checked(endpoint, ["set-option", "-t", target, "prefix",
                                     runtime["baseline"]["prefix"]], mutation=True)
            self._checked(endpoint, ["set-option", "-t", target, "prefix2",
                                     runtime["baseline"]["prefix2"]], mutation=True)
            self._checked(endpoint, ["set-option", "-t", target, "status-left-length",
                                     str(_family_width(record))], mutation=True)
            self._checked(endpoint, ["set-option", "-t", target, "status-right-length", "0"], mutation=True)
            self._checked(endpoint, ["set-option", "-t", target, "status-right", ""], mutation=True)
            self._checked(endpoint, ["set-option", "-t", target, "status-left", ""], mutation=True)
            self._checked(endpoint, ["set-window-option", "-t", status_window_id,
                                     "pane-border-style", f"fg={theme['border']},bg={theme['background']}"], mutation=True)
            self._checked(endpoint, ["set-window-option", "-t", status_window_id,
                                     "pane-border-status", "off" if canvas_mode else runtime["layout"]["pane_border_status"]], mutation=True)
            self._checked(endpoint, ["set-window-option", "-t", status_window_id,
                                     "pane-border-format", runtime["layout"]["pane_border_format"]], mutation=True)
            if self._run(endpoint, ["list-keys", "-T", "prefix"]).stdout.strip():
                self._checked(endpoint, ["unbind-key", "-a", "-T", "prefix"], mutation=True)
            self._checked(endpoint, ["unbind-key", "-a", "-T", "root"], mutation=True)
            for table in ("copy-mode", "copy-mode-vi"):
                self._checked(endpoint, ["unbind-key", "-a", "-T", table], mutation=True)
                for key, action in self._OBSERVER_COPY_KEYS.items():
                    self._checked(endpoint, ["bind-key", "-T", table, key,
                                             "send-keys", "-X", action], mutation=True)
            actions = {"ignore": ["display-message", "Observer input is disabled"],
                       "copy_mode_up": ["copy-mode", "-u"],
                       "copy_mode_mouse": ["copy-mode", "-e"],
                       "detach": ["detach-client"]}
            for key, action_id in runtime["keys"].items():
                action = actions[action_id]
                self._checked(endpoint, ["bind-key", "-T", "root", key, *action], mutation=True)
            if canvas_mode:
                for key, step in (("Left", -1), ("Right", 1)):
                    self._checked(endpoint, ["bind-key", "-T", "root", key,
                                             "run-shell", "-b",
                                             _canvas_page_command(record, step)], mutation=True)
                self._checked(endpoint, ["bind-key", "-T", "root", "MouseDown1Pane",
                                         "run-shell", "-b", _canvas_mouse_command(record)], mutation=True)
            if native_canvas_mode:
                native_bindings = native_control_bindings(record)
                if set(native_bindings).intersection(runtime["keys"]):
                    raise ShellCommandError("native_control_key_collision", "A native canvas key overlaps the registered runtime bindings.")
                for key, action in native_bindings.items():
                    self._checked(endpoint, ["bind-key", "-T", "root", key, *action], mutation=True)
            self._checked(endpoint, ["select-pane", "-d", "-t", status_pane_id], mutation=True)
        if record.get("layout_mode") == "family-v1":
            # Only family-v1 uses pane-died reconciliation. Canvas runners
            # finalize their own receipts; the family hook rejects those
            # modes and tmux exposes its failing run-shell in view-mode.
            script = shlex.join([
                "env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
                sys.executable, "-m", "ls.core.terminal_ops.command_finalize",
                "--session-record", str(record["session_record_path"]),
                "--pane", "#{hook_pane}",
            ])
            self._checked(endpoint, ["set-hook", "-t", target, "pane-died",
                                     "run-shell -b " + shlex.quote(script)], mutation=True)
        detached_script = shlex.join([
            "env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
            sys.executable, "-m", "ls.core.terminal_ops.command_finalize",
            "--session-record", str(record["session_record_path"]), "--detached",
        ])
        self._checked(endpoint, ["set-hook", "-t", target, "client-detached",
                                 "run-shell -b " + shlex.quote(detached_script)], mutation=True)
        for name, value in session_options.items():
            self._checked(endpoint, ["set-option", "-t", target, name, value], mutation=True)
        if native_canvas_mode:
            native_generation = int((record.get("native_layout") or {}).get("desired_generation", 0))
            self._checked(endpoint, ["set-option", "-t", target,
                                     "@localsetup_shell_layout_generation", str(native_generation)], mutation=True)
            for scope, target_id in (("window", status_window_id), ("pane", status_pane_id)):
                if scope == "window":
                    self._checked(endpoint, ["set-window-option", "-t", target_id,
                                             "@localsetup_shell_window_role", "status"], mutation=True)
                    self._checked(endpoint, ["set-window-option", "-t", target_id,
                                             "@localsetup_shell_generation", str(native_generation)], mutation=True)
                    self._checked(endpoint, ["set-window-option", "-t", target_id,
                                             "@localsetup_shell_session", str(record["handle"])], mutation=True)
                else:
                    for key, value in (("@localsetup_shell_role", "status"),
                                       ("@localsetup_shell_generation", str(native_generation)),
                                       ("@localsetup_shell_session", str(record["handle"]))):
                        self._checked(endpoint, ["set-option", "-p", "-t", target_id,
                                                 key, value], mutation=True)
        session_text = self._checked(endpoint, ["display-message", "-t", target, "-p", "-F", _SESSION_FORMAT])
        window_text = self._checked(endpoint, ["display-message", "-t", status_window_id, "-p", "-F", _WINDOW_FORMAT])
        pane_text = self._checked(endpoint, ["display-message", "-t", status_pane_id, "-p", "-F", _PANE_FORMAT])
        session_parts, window_parts, pane_parts = (line.split("\t") for line in (session_text.rstrip("\n"), window_text.rstrip("\n"), pane_text.rstrip("\n")))
        if (len(session_parts) != 4 or session_parts[0] != native_session_id
                or len(window_parts) != 4 or window_parts[0] != status_window_id
                or len(pane_parts) != 11 or pane_parts[0] != status_pane_id):
            raise ShellCommandError("native_identity_unknown", "The new tmux session failed exact identity verification.", effect="unknown")
        pane_limit = self._checked(endpoint, ["display-message", "-p", "-t", status_pane_id,
                                              "-F", "#{history_limit}"]).strip()
        if pane_limit != str(settings["history_limit"]):
            raise ShellCommandError("profile_readback_mismatch", "The new pane did not adopt the requested history capacity.", effect="unknown")
        if record.get("layout_mode") in {"family-v1", "canvas-v2", "canvas-v3"}:
            native_layout = {
                name: self._checked(endpoint, ["show-options", "-t", target, "-qv", name]).strip()
                for name in ("window-size",)
            }
            dimensions = self._checked(endpoint, ["display-message", "-p", "-t", status_window_id,
                                                 "-F", "#{window_width}\t#{window_height}"]).strip()
            if (native_layout != {"window-size": "manual"}
                    or dimensions != f"{_family_width(record)}\t{record['primary_viewport_height']}"):
                raise ShellCommandError("family_viewport_mismatch", "The new family session did not retain its fixed canvas.", effect="unknown")
        sessions = self._checked(endpoint, ["list-sessions", "-F", "#{session_id}\t#{session_name}"]).splitlines()
        if len(sessions) != 1 or sessions[0].split("\t", 1)[0] != native_session_id:
            raise ShellCommandError("unexpected_native_resources", "The private tmux server contains an unexpected session.", effect="unknown")
        created = {
            "native_session_id": native_session_id,
            "server_session_name": session_parts[1],
            "status_window_id": status_window_id,
            "status_pane_id": status_pane_id,
            "status_pane_pid": int(pane_parts[1]) if pane_parts[1].isdigit() else None,
            "status_pane_starttime": bootstrap["starttime"] if canvas_mode or native_canvas_mode else None,
            "session_created": session_parts[2],
            "endpoint": endpoint,
        }
        if native_canvas_mode and (record.get("native_layout") or {}).get("compact_idle"):
            created.update(self._native_canvas_v3.create_summary_pane(dict(record) | created))
        return created

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
        for name in ("api", "handle", "key", "machine", "owner", "profile", "status_window", "layout"):
            option = f"@localsetup_shell_{name}"
            result = self._run(endpoint, ["show-options", "-t", target, "-qv", option])
            value = result.stdout.strip() if result.returncode == 0 else ""
            options[name] = value or None
        for name in ("window-size", "window-width", "window-height"):
            result = self._run(endpoint, ["show-options", "-t", target, "-qv", name])
            options[name.replace("-", "_")] = result.stdout.strip() if result.returncode == 0 else None
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
                if len(parts) != 11:
                    return {"state": "unknown", "windows": []}
                panes.append({
                    "pane_id": parts[0], "pane_pid": int(parts[1]) if parts[1].isdigit() else None,
                    "pane_tty": parts[2], "dead": parts[3] == "1",
                    "dead_status": parts[4], "current_command": parts[5], "active": parts[6] == "1",
                    "metadata": {"session": parts[7] or None, "operation": parts[8] or None,
                                 "request": parts[9] or None, "label": parts[10] or None},
                })
                guard = self._run(endpoint, ["display-message", "-p", "-t", parts[0],
                                             "-F", "#{pane_input_off}\t#{pane_width}\t#{pane_height}\t#{history_size}"])
                guard_parts = guard.stdout.strip().split("\t")
                if guard.returncode == 0 and len(guard_parts) == 4:
                    panes[-1].update(input_off=guard_parts[0] == "1",
                                     width=int(guard_parts[1]) if guard_parts[1].isdigit() else None,
                                     height=int(guard_parts[2]) if guard_parts[2].isdigit() else None,
                                     history_size=int(guard_parts[3]) if guard_parts[3].isdigit() else None)
            windows.append({
                "window_id": window_id, "name": name, "active": active == "1",
                "pane_count": int(pane_count) if pane_count.isdigit() else -1,
                "metadata": metadata, "panes": panes,
            })
            dimensions = self._run(endpoint, ["display-message", "-p", "-t", window_id,
                                             "-F", "#{window_width}\t#{window_height}"])
            dimension_parts = dimensions.stdout.strip().split("\t")
            if dimensions.returncode == 0 and len(dimension_parts) == 2:
                windows[-1]["width"] = int(dimension_parts[0]) if dimension_parts[0].isdigit() else None
                windows[-1]["height"] = int(dimension_parts[1]) if dimension_parts[1].isdigit() else None
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
                "status": status.stdout.strip() in {"on", "2", "3", "4", "5"} if status.returncode == 0 else None,
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
        runtime = _runtime_settings(record)
        self._checked(endpoint, ["set-option", "-t", target, "status",
                                 _status_rows_option(runtime, settings["status"])], mutation=True)
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
        if record.get("layout_mode") == "canvas-v3":
            return self.create_native_operation(record, operation, command=command)
        if record.get("layout_mode") == "family-v1":
            window_id = str(record["status_window_id"])
            split_target = f"{target}:{window_id}"
            replacement_pane = operation.get("replaces_pane_id")
            if replacement_pane is not None:
                observed = self.inspect_session(record)
                matching = [pane for window in observed.get("windows", [])
                            if window.get("window_id") == window_id
                            for pane in window.get("panes", [])
                            if (pane.get("pane_id") == replacement_pane
                                and pane.get("dead") is True
                                and pane.get("metadata", {}).get("session") == record.get("handle")
                                and pane.get("metadata", {}).get("operation") == operation.get("replaces_operation_id")
                                and pane.get("metadata", {}).get("request") == operation.get("replaces_request_digest")
                                and pane.get("metadata", {}).get("label") == operation.get("replaces_pane_label"))]
                if observed.get("state") != "ready" or len(matching) != 1:
                    raise ShellCommandError("native_inventory_unknown", "The exact retained pane changed before replacement.", effect="unknown")
                split_target = str(replacement_pane)
            output = self._checked(
                endpoint,
                ["split-window", "-d", "-v", "-P", "-F", "#{window_id}\t#{pane_id}",
                 "-t", split_target, "-c", str(operation["cwd"]),
                 shlex.join(list(command))],
                mutation=True,
            )
            fields = output.rstrip("\n").split("\t")
            if len(fields) != 2 or fields[0] != window_id or not fields[1].startswith("%"):
                raise ShellCommandError("native_identity_unknown", "tmux created no verifiable family pane.", effect="unknown")
            self._checked(endpoint, ["select-pane", "-d", "-t", fields[1]], mutation=True)
            return {"window_id": window_id, "pane_id": fields[1]}
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
        if record.get("layout_mode") == "canvas-v3":
            observed = self.inspect_canvas_v3(record)
            matches = [pane for window in observed.get("windows", []) for pane in window.get("panes", [])
                       if pane.get("pane_id") == native.get("pane_id")
                       and pane.get("role") == "operation"
                       and pane.get("operation_id") == operation.get("operation_id")]
            placeholders = [pane for window in observed.get("windows", []) for pane in window.get("panes", [])
                            if pane.get("pane_id") == native.get("parking_pane_id")
                            and pane.get("role") == "placeholder"
                            and pane.get("operation_id") == operation.get("operation_id")]
            if observed.get("state") != "ready" or len(matches) != 1 or len(placeholders) != 1:
                raise ShellCommandError("native_identity_unknown", "The native workload and parking placeholder tags could not be proven.", effect="unknown")
            return
        if record.get("layout_mode") == "family-v1":
            values = {
                "@localsetup_shell_session": str(record["handle"]),
                "@localsetup_shell_operation": str(operation["operation_id"]),
                "@localsetup_shell_request": str(operation["request_digest"]),
                "@localsetup_shell_label": str(operation["pane_label"]),
            }
            for name, value in values.items():
                self._checked(endpoint, ["set-option", "-p", "-t", str(native["pane_id"]), name, value], mutation=True)
            self._checked(endpoint, ["set-window-option", "-t", window_id,
                                     "history-limit", str(record["settings"]["history_limit"])], mutation=True)
            self._checked(endpoint, ["set-window-option", "-t", window_id, "remain-on-exit", "on"], mutation=True)
            actual = self._checked(endpoint, ["display-message", "-p", "-t", str(native["pane_id"]),
                                              "-F", _PANE_FORMAT]).rstrip("\n").split("\t")
            if len(actual) != 11 or actual[0] != str(native["pane_id"]) or actual[7:] != [
                    str(record["handle"]), str(operation["operation_id"]),
                    str(operation["request_digest"]), str(operation["pane_label"])]:
                raise ShellCommandError("pane_metadata_mismatch", "The new family pane did not retain its exact identity and label.", effect="unknown")
            return
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
        if record.get("layout_mode") == "canvas-v3":
            # Visibility and selection are committed only through the persisted
            # native layout plan; selecting a parked window would bypass it.
            observed = self.inspect_canvas_v3(record)
            matches = [pane for window in observed.get("windows", []) for pane in window.get("panes", [])
                       if pane.get("pane_id") == operation.get("pane_id")
                       and pane.get("role") == "operation"]
            if observed.get("state") != "ready" or len(matches) != 1:
                raise ShellCommandError("native_inventory_unknown", "The exact native operation pane could not be selected.", effect="unknown")
            if matches[0].get("window_id") == record.get("status_window_id"):
                self._checked(str(record["endpoint"]), ["select-pane", "-d", "-t", str(operation["pane_id"])], mutation=True)
            return
        if record.get("layout_mode") == "family-v1":
            self._checked(str(record["endpoint"]), ["select-pane", "-t", str(operation["pane_id"])], mutation=True)
        else:
            self._checked(str(record["endpoint"]), ["select-window", "-t", str(operation["window_id"])], mutation=True)

    def arrange_family_window(
        self, record: Mapping[str, Any], *, expected_window: Mapping[str, Any],
    ) -> None:
        """Equalize only the verified family window after inventory validation."""
        endpoint = str(record["endpoint"])
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        windows = [window for window in observed.get("windows", [])
                   if window.get("window_id") == record.get("status_window_id")]
        if (record.get("layout_mode") != "family-v1" or observed.get("state") != "ready"
                or options.get("handle") != record.get("handle")
                or options.get("key") != record.get("session_key")
                or options.get("machine") != record.get("machine_generation")
                or options.get("owner") != record.get("owner_fingerprint")
                or len(windows) != 1
                or self.window_identity(windows[0]) != expected_window
                or windows[0].get("width") != _family_width(record)
                or windows[0].get("height") != record.get("primary_viewport_height", 24)):
            raise ShellCommandError("native_inventory_unknown", "The family layout changed before equalization.", effect="unknown")
        self._checked(endpoint, ["select-layout", "-t", str(record["status_window_id"]),
                                 "even-vertical"], mutation=True)
        after = self.inspect_session(record)
        windows = [window for window in after.get("windows", [])
                   if window.get("window_id") == record.get("status_window_id")]
        if (after.get("state") != "ready" or len(windows) != 1
                or windows[0].get("width") != _family_width(record)
                or windows[0].get("height") != record.get("primary_viewport_height", 24)
                or len(windows[0].get("panes", [])) > 1
                and any(pane.get("height") is None or pane["height"] < (
                    1 if pane.get("pane_id") == record.get("status_pane_id") else 3)
                        for pane in windows[0]["panes"])):
            raise ShellCommandError("family_layout_readback_mismatch", "The family layout could not be equalized within its fixed viewport.", effect="unknown")

    def resize_family_window(
        self, record: Mapping[str, Any], *, expected_window: Mapping[str, Any], height: int,
        width: int | None = None,
    ) -> dict[str, Any]:
        if type(height) is not int or not 1 <= height <= 512:
            raise ShellCommandError("invalid_viewport", "Family height must be from 1 through 512 rows.")
        canvas = record.get("layout_mode") in {"canvas-v2", "canvas-v3"}
        target_width = _family_width(record) if width is None else width
        if canvas and (type(target_width) is not int or not 80 <= target_width <= 500):
            raise ShellCommandError("invalid_viewport", "Canvas width must be from 80 through 500 columns.")
        observed = self.inspect_session(record)
        windows = [item for item in observed.get("windows", [])
                   if item.get("window_id") == record.get("status_window_id")]
        if (record.get("layout_mode") not in {"family-v1", "canvas-v2", "canvas-v3"} or observed.get("state") != "ready"
                or len(windows) != 1 or self.window_identity(windows[0]) != expected_window
                or windows[0].get("width") != _family_width(record)
                or windows[0].get("height") != record.get("primary_viewport_height")
                or any(pane.get("input_off") is not True for pane in windows[0].get("panes", []))):
            raise ShellCommandError("native_inventory_unknown", "The exact family window changed before resize.", effect="unknown")
        self._checked(str(record["endpoint"]), ["resize-window", "-t", str(record["status_window_id"]),
                                                "-x", str(target_width), "-y", str(height)], mutation=True)
        after = self.inspect_session(record)
        matching = [item for item in after.get("windows", [])
                    if item.get("window_id") == record.get("status_window_id")]
        if (after.get("state") != "ready" or len(matching) != 1
                or matching[0].get("width") != target_width or matching[0].get("height") != height):
            raise ShellCommandError("family_viewport_readback_unknown", "The resized family window could not be qualified.", effect="unknown")
        return matching[0]

    def repair_idle_family_view(self, record: Mapping[str, Any]) -> None:
        """Restore only an exact, empty overview pane after native readback."""
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        windows = observed.get("windows", [])
        if (record.get("layout_mode") != "family-v1" or observed.get("state") != "ready"
                or any(options.get(key) != record.get(value) for key, value in
                       (("handle", "handle"), ("key", "session_key"),
                        ("machine", "machine_generation"), ("owner", "owner_fingerprint")))
                or type(observed.get("session", {}).get("attached")) is not int
                or observed["session"]["attached"] != 0
                or len(windows) != 1 or windows[0].get("window_id") != record.get("status_window_id")
                or len(windows[0].get("panes", [])) != 1
                or windows[0]["panes"][0].get("pane_id") != record.get("status_pane_id")
                or windows[0]["panes"][0].get("dead") is not True
                or observed.get("applied_settings", {}).get("pane_history_limit", {}).get(
                    record.get("status_pane_id")) != record.get("settings", {}).get("history_limit")):
            raise ShellCommandError("idle_repair_unqualified", "The exact empty family overview is not proven.", effect="unknown")
        endpoint = str(record["endpoint"])
        self._checked(endpoint, ["select-pane", "-d", "-t", str(record["status_pane_id"])], mutation=True)
        self._checked(endpoint, ["set-option", "-t", str(record["native_session_id"]),
                                 "window-size", "manual"], mutation=True)
        self._checked(endpoint, ["resize-window", "-t", str(record["status_window_id"]),
                                 "-x", str(_family_width(record)), "-y", str(record["primary_viewport_height"])], mutation=True)

    def repair_idle_overview_label(self, record: Mapping[str, Any], *, expected_inventory: Mapping[str, Any]) -> bool:
        """Label only the exact, exited overview pane; never relabel command exits."""
        observed = self.inspect_session(record)
        windows = [window for window in observed.get("windows", [])
                   if window.get("window_id") == record.get("status_window_id")]
        options = observed.get("options", {})
        if (observed.get("state") != "ready"
                or any(options.get(key) != record.get(value) for key, value in
                       (("handle", "handle"), ("key", "session_key"),
                        ("machine", "machine_generation"), ("owner", "owner_fingerprint")))
                or self.inventory_identity(observed) != expected_inventory
                or len(windows) != 1 or len(windows[0].get("panes", [])) != 1
                or windows[0]["panes"][0].get("pane_id") != record.get("status_pane_id")
                or windows[0]["panes"][0].get("dead") is not True):
            raise ShellCommandError("idle_repair_unqualified", "The exact exited overview is not proven.", effect="unknown")
        endpoint = str(record["endpoint"])
        pane = str(record["status_pane_id"])
        before = self._checked(endpoint, ["show-options", "-p", "-v", "-t", pane,
                                          "remain-on-exit-format"]).strip()
        if before == "Idle":
            return False
        self._checked(endpoint, ["set-option", "-p", "-t", pane,
                                 "remain-on-exit-format", "Idle"], mutation=True)
        after = self._checked(endpoint, ["show-options", "-p", "-v", "-t", pane,
                                         "remain-on-exit-format"]).strip()
        if after != "Idle":
            raise ShellCommandError("idle_label_unverified", "The overview label did not read back.", effect="unknown")
        return True

    def label_completed_pane(self, record: Mapping[str, Any], operation: Mapping[str, Any],
                             *, expected_window: Mapping[str, Any]) -> bool:
        """Show a verified command result on its own dead pane only."""
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        windows = [window for window in observed.get("windows", [])
                   if window.get("window_id") == operation.get("window_id")]
        if (observed.get("state") != "ready"
                or any(options.get(key) != record.get(value) for key, value in
                       (("handle", "handle"), ("key", "session_key"),
                        ("machine", "machine_generation"), ("owner", "owner_fingerprint")))
                or len(windows) != 1 or self.window_identity(windows[0]) != expected_window):
            raise ShellCommandError("completed_label_unqualified", "The exact command window changed.", effect="unknown")
        panes = [pane for pane in windows[0].get("panes", [])
                 if pane.get("pane_id") == operation.get("pane_id")]
        tags = (panes[0].get("metadata", {}) if record.get("layout_mode") == "family-v1" else
                windows[0].get("metadata", {})) if len(panes) == 1 else {}
        if (len(panes) != 1 or panes[0].get("dead") is not True
                or panes[0].get("pane_pid") != operation.get("runner_pid")
                or tags.get("operation") != operation.get("operation_id")
                or tags.get("request") != operation.get("request_digest")
                or operation.get("state") != "completed" or operation.get("process_scope") != "collected"
                or operation.get("output_drain") not in {"available", "unavailable"}):
            raise ShellCommandError("completed_label_unqualified", "The dead command pane is not fully proven.", effect="unknown")
        code = operation.get("exit_code")
        if type(code) is not int:
            raise ShellCommandError("completed_label_unqualified", "The command result has no exact exit code.")
        label = "Completed · Idle" if code == 0 else f"Failed · exit {code}"
        endpoint, pane = str(record["endpoint"]), str(operation["pane_id"])
        before = self._checked(endpoint, ["show-options", "-p", "-v", "-t", pane,
                                          "remain-on-exit-format"]).strip()
        if before == label:
            return False
        self._checked(endpoint, ["set-option", "-p", "-t", pane,
                                 "remain-on-exit-format", label], mutation=True)
        after = self._checked(endpoint, ["show-options", "-p", "-v", "-t", pane,
                                         "remain-on-exit-format"]).strip()
        if after != label:
            raise ShellCommandError("completed_label_unverified", "The command label did not read back.", effect="unknown")
        return True

    def capture_screen(self, record: Mapping[str, Any], operation: Mapping[str, Any] | None, *, lines: int) -> str:
        endpoint = str(record["endpoint"])
        target = (str(operation["pane_id"]) if operation and operation.get("pane_id")
                  else str(record["status_pane_id"]))
        return self._checked(endpoint, ["capture-pane", "-p", "-S", f"-{lines}", "-t", target])

    def send_input(self, record: Mapping[str, Any], operation: Mapping[str, Any], *, buffer_name: str,
                   input_path: str, eof: bool) -> None:
        endpoint = str(record["endpoint"])
        pane_id = str(operation["pane_id"])
        guarded = record.get("layout_mode") in {"family-v1", "canvas-v3"}
        promoted: list[tuple[int, str]] = []
        if guarded:
            clients = self._clients(record)
            promoted = [(client["pid"], client["name"]) for client in clients
                        if "read-only" not in client["flags"]]
            for _pid, name in promoted:
                self._checked(endpoint, ["refresh-client", "-t", name, "-f", "read-only"], mutation=True)
            if any("read-only" not in client["flags"] for client in self._clients(record)):
                raise ShellCommandError("observer_guard_unknown", "A family observer could not be made read-only.", effect="unknown")
            # No application bytes or pane-enable mutation are allowed while a
            # native observer can reach an altered tmux command binding.
            self.verify_observer_keys(record)
        disabled = False
        try:
            if guarded:
                self._checked(endpoint, ["select-pane", "-e", "-t", pane_id], mutation=True)
            self._checked(endpoint, ["load-buffer", "-b", buffer_name, input_path], mutation=True)
            self._checked(endpoint, ["paste-buffer", "-d", "-b", buffer_name, "-t", pane_id], mutation=True)
            if eof:
                self._checked(endpoint, ["send-keys", "-t", pane_id, "C-d"], mutation=True)
        finally:
            if guarded:
                self._checked(endpoint, ["select-pane", "-d", "-t", pane_id], mutation=True)
                disabled = self._checked(endpoint, ["display-message", "-p", "-t", pane_id,
                                                    "-F", "#{pane_input_off}"]).strip() == "1"
                if not disabled:
                    raise ShellCommandError("observer_guard_unknown", "The family pane input guard did not read back.", effect="unknown")
                self.verify_observer_keys(record)
                for pid, name in promoted:
                    current = [client for client in self._clients(record)
                               if client["pid"] == pid and client["name"] == name]
                    if len(current) == 1 and "read-only" in current[0]["flags"]:
                        self._checked(endpoint, ["switch-client", "-r", "-t", name, ";",
                                                 "refresh-client", "-t", name, "-f", "ignore-size"], mutation=True)

    def _clients(self, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        endpoint = str(record["endpoint"])
        target = self._private_target(str(record["native_session_id"]))
        output = self._checked(endpoint, ["list-clients", "-t", target,
                                          "-F", "#{client_pid}\t#{client_name}\t#{client_flags}\t#{client_session}"])
        clients: list[dict[str, Any]] = []
        for row in output.splitlines():
            parts = row.split("\t")
            if (len(parts) != 4 or not parts[0].isdigit()
                    or not parts[1].startswith("/dev/pts/")
                    or parts[3] != record.get("server_session_name", record.get("native_name"))):
                raise ShellCommandError("observer_identity_unknown", "A native observer cannot be qualified.", effect="unknown")
            clients.append({"pid": int(parts[0]), "name": parts[1], "flags": set(parts[2].split(","))})
        return clients

    def promote_observer(self, record: Mapping[str, Any], pid: int) -> None:
        self.verify_observer_keys(record)
        clients = [client for client in self._clients(record) if client["pid"] == pid]
        if (len(clients) != 1 or "read-only" not in clients[0]["flags"]
                or "ignore-size" not in clients[0]["flags"]):
            raise ShellCommandError("observer_identity_unknown", "The new read-only viewer could not be qualified.", effect="unknown")
        name = clients[0]["name"]
        endpoint = str(record["endpoint"])
        self._checked(endpoint, ["switch-client", "-r", "-t", name, ";",
                                 "refresh-client", "-t", name, "-f", "ignore-size"], mutation=True)
        after = [client for client in self._clients(record) if client["pid"] == pid and client["name"] == name]
        if (len(after) != 1 or "read-only" in after[0]["flags"]
                or "ignore-size" not in after[0]["flags"]):
            raise ShellCommandError("observer_guard_unknown", "The scroll-capable viewer state did not read back.", effect="unknown")

    def verify_observer_keys(self, record: Mapping[str, Any]) -> None:
        """Fail closed if a command-capable prefix or viewer key escaped the baseline."""
        endpoint, target = str(record["endpoint"]), self._private_target(str(record["native_session_id"]))
        runtime = _runtime_settings(record)
        for name in ("prefix", "prefix2"):
            actual = self._checked(endpoint, ["show-options", "-t", target, "-qv", name]).strip()
            if actual != runtime["baseline"][name]:
                raise ShellCommandError("observer_key_guard_unknown", f"The private {name} key changed.", effect="unknown")
        prefix_keys = self._run(endpoint, ["list-keys", "-T", "prefix"])
        if prefix_keys.returncode == 0 and prefix_keys.stdout.strip():
            raise ShellCommandError("observer_key_guard_unknown", "A prefix command key remains bound.", effect="unknown")
        if prefix_keys.returncode not in {0, 1}:
            raise ShellCommandError("observer_key_guard_unknown", "The prefix table could not be qualified.", effect="unknown")
        actions = {"ignore": ("display-message", "Observer input is disabled"),
                   "copy_mode_up": ("copy-mode", "-u"),
                   "copy_mode_mouse": ("copy-mode", "-e"),
                   "detach": ("detach-client",)}
        expected_root = {key: actions[action] for key, action in runtime["keys"].items()}
        if record.get("layout_mode") == "canvas-v2":
            expected_root.update({key: ("run-shell", "-b", _canvas_page_command(record, step))
                                  for key, step in (("Left", -1), ("Right", 1))})
            expected_root["MouseDown1Pane"] = ("run-shell", "-b", _canvas_mouse_command(record))
        elif record.get("layout_mode") == "canvas-v3":
            expected_root.update(native_control_bindings(record))
        if self._observer_bindings(endpoint, "root") != expected_root:
            raise ShellCommandError("observer_key_guard_unknown", "The observer key table changed.", effect="unknown")
        for table in ("copy-mode", "copy-mode-vi"):
            expected = {key: ("send-keys", "-X", action)
                        for key, action in self._OBSERVER_COPY_KEYS.items()}
            if self._observer_bindings(endpoint, table) != expected:
                raise ShellCommandError("observer_key_guard_unknown", "A copy-mode key table changed.", effect="unknown")

    def _observer_bindings(self, endpoint: str, table: str) -> dict[str, tuple[str, ...]]:
        """Parse the entire native binding, rejecting options and chained commands."""
        bindings: dict[str, tuple[str, ...]] = {}
        for line in self._checked(endpoint, ["list-keys", "-T", table]).splitlines():
            try:
                tokens = shlex.split(line, posix=True)
            except ValueError as exc:
                raise ShellCommandError("observer_key_guard_unknown", "A viewer binding could not be parsed.", effect="unknown") from exc
            if (len(tokens) < 5 or tokens[:3] != ["bind-key", "-T", table]
                    or tokens[3] in bindings):
                raise ShellCommandError("observer_key_guard_unknown", "A viewer binding is ambiguous.", effect="unknown")
            bindings[tokens[3]] = tuple(tokens[4:])
        return bindings

    def kill_window(
        self, record: Mapping[str, Any], operation: Mapping[str, Any],
        *, expected_window: Mapping[str, Any],
    ) -> None:
        if record.get("layout_mode") == "canvas-v3":
            raise ShellCommandError(
                "native_inventory_required",
                "Native operation cleanup requires the complete tagged canvas inventory.",
                effect="unknown",
            )
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
                or (record.get("layout_mode") != "family-v1" and observed["session"]["attached"] != 0)
                or len(windows) != 1
                or self.window_identity(windows[0]) != expected_window):
            raise ShellCommandError("native_inventory_unknown", "The exact command window changed before cleanup.", effect="unknown")
        if record.get("layout_mode") == "family-v1":
            panes = windows[0].get("panes", [])
            matches = [pane for pane in panes if pane.get("pane_id") == operation.get("pane_id")
                       and pane.get("metadata", {}).get("session") == record.get("handle")
                       and pane.get("metadata", {}).get("operation") == operation.get("operation_id")
                       and pane.get("metadata", {}).get("request") == operation.get("request_digest")
                       and pane.get("metadata", {}).get("label") == operation.get("pane_label")]
            if len(matches) != 1 or len(panes) <= 1:
                raise ShellCommandError("native_inventory_unknown", "The exact family pane cannot be removed safely.", effect="unknown")
            self._checked(str(record["endpoint"]), ["kill-pane", "-t", str(operation["pane_id"])], mutation=True)
            return
        self._checked(str(record["endpoint"]), ["kill-window", "-t", str(operation["window_id"])], mutation=True)

    @staticmethod
    def window_identity(window: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "window_id": window.get("window_id"),
            "metadata": window.get("metadata"),
            "pane_count": window.get("pane_count"),
            "width": window.get("width"), "height": window.get("height"),
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
        if record.get("layout_mode") == "canvas-v3":
            observed = self.inspect_canvas_v3(record)
            if (observed.get("state") != "ready"
                    or observed.get("inventory") != expected_inventory
                    or type(observed.get("identity", {}).get("session", {}).get("attached")) is not int
                    or observed["identity"]["session"]["attached"] != 0):
                raise ShellCommandError("session_identity_unknown", "The private native canvas changed before close.", effect="unknown")
            self._checked(endpoint, ["kill-server"], mutation=True)
            deadline = time.monotonic() + 1.0
            while self._endpoint_state(endpoint) == "live" and time.monotonic() < deadline:
                time.sleep(0.02)
            if not self.remove_stale_endpoint(record):
                raise ShellCommandError("native_cleanup_pending", "The exact private tmux server did not remove its socket yet.", effect="unknown")
            return
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

    def retire_session(self, record: Mapping[str, Any], *, expected_inventory: Mapping[str, Any]) -> None:
        """Let tmux destroy the private session only after its last viewer detaches."""
        if record.get("layout_mode") == "canvas-v3":
            observed = self.inspect_canvas_v3(record)
            attached = observed.get("identity", {}).get("session", {}).get("attached")
            if (observed.get("state") != "ready" or observed.get("inventory") != expected_inventory
                    or type(attached) is not int or attached < 1):
                raise ShellCommandError("retirement_identity_unknown", "The attached native canvas changed before retirement.", effect="unknown")
            options = observed.get("inventory", {}).get("options", {})
            if any(options.get(key) != record.get(value) for key, value in
                   (("handle", "handle"), ("key", "session_key"),
                    ("machine", "machine_generation"), ("owner", "owner_fingerprint"))):
                raise ShellCommandError("retirement_identity_unknown", "The attached native canvas owner identity changed before retirement.", effect="unknown")
            target = self._private_target(str(record["native_session_id"]))
            endpoint = str(record["endpoint"])
            self._checked(endpoint, ["set-option", "-t", target, "destroy-unattached", "on"], mutation=True)
            if self._endpoint_state(endpoint) == "live":
                actual = self._checked(endpoint, ["show-options", "-t", target, "-qv",
                                                  "destroy-unattached"]).strip()
                if actual != "on":
                    raise ShellCommandError("retirement_unverified", "Native last-detach cleanup did not read back.", effect="unknown")
            return
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        if (observed.get("state") != "ready" or self.inventory_identity(observed) != expected_inventory
                or any(options.get(key) != record.get(value) for key, value in
                       (("handle", "handle"), ("key", "session_key"),
                        ("machine", "machine_generation"), ("owner", "owner_fingerprint")))
                or type(observed.get("session", {}).get("attached")) is not int
                or observed["session"]["attached"] < 1):
            raise ShellCommandError("retirement_identity_unknown", "The attached private session changed before retirement.", effect="unknown")
        target = self._private_target(str(record["native_session_id"]))
        endpoint = str(record["endpoint"])
        self._checked(endpoint, ["set-option", "-t", target, "destroy-unattached", "on"], mutation=True)
        if self._endpoint_state(endpoint) == "live":
            actual = self._checked(endpoint, ["show-options", "-t", target, "-qv",
                                              "destroy-unattached"]).strip()
            if actual != "on":
                raise ShellCommandError("retirement_unverified", "Native last-detach cleanup did not read back.", effect="unknown")

    def update_telemetry(self, record: Mapping[str, Any], values: Mapping[str, str]) -> None:
        """Publish counters from exact operation receipts, never a synthetic heartbeat."""
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        if (observed.get("state") != "ready"
                or any(options.get(key) != record.get(value) for key, value in
                       (("handle", "handle"), ("key", "session_key"),
                        ("machine", "machine_generation"), ("owner", "owner_fingerprint")))):
            raise ShellCommandError("telemetry_identity_unknown", "The private session changed before telemetry update.", effect="unknown")
        endpoint, target = str(record["endpoint"]), self._private_target(str(record["native_session_id"]))
        for key, value in values.items():
            if key not in {"active", "done", "output_bytes", "elapsed", "input_state"}:
                raise ShellCommandError("invalid_telemetry", "An unknown status field was requested.")
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}|RO|INPUT|REQUESTED", value):
                raise ShellCommandError("invalid_telemetry", "A status field has an invalid value.")
            name = f"@localsetup_shell_{key}"
            current = self._checked(endpoint, ["show-options", "-t", target, "-qv", name]).strip()
            if current != value:
                self._checked(endpoint, ["set-option", "-t", target, name, value], mutation=True)
                if self._checked(endpoint, ["show-options", "-t", target, "-qv", name]).strip() != value:
                    raise ShellCommandError("telemetry_readback_unknown", "The status field did not read back.", effect="unknown")

    def apply_runtime_baseline(self, record: Mapping[str, Any]) -> None:
        """Reapply schema-checked private appearance and viewer keys after exact identity checks."""
        observed = self.inspect_session(record)
        options = observed.get("options", {})
        if (observed.get("state") != "ready"
                or any(options.get(key) != record.get(value) for key, value in
                       (("handle", "handle"), ("key", "session_key"),
                        ("machine", "machine_generation"), ("owner", "owner_fingerprint"),
                        ("profile", "settings_digest")))):
            raise ShellCommandError("runtime_baseline_identity_unknown", "The private session is not proven for baseline reset.", effect="unknown")
        runtime = _runtime_settings(record)
        theme = runtime["theme"]
        endpoint, target = str(record["endpoint"]), self._private_target(str(record["native_session_id"]))
        fields = {
            "status": _status_rows_option(runtime, record["settings"]["status"]),
            "status-position": str(record["settings"]["status_position"]),
            "status-interval": str(runtime["baseline"]["status_interval_seconds"]),
            "status-style": f"bg={theme['background']},fg={theme['accent']}",
            "status-format[0]": (" #[fg=" + theme["accent"] + "][LS] #[fg=" + theme["text"] + "]"
                                 "#{@localsetup_shell_input_state} | active #{@localsetup_shell_active}"
                                 " | done #{@localsetup_shell_done} | out #{@localsetup_shell_output_bytes}B"
                                 " | elapsed #{@localsetup_shell_elapsed}s"),
            "status-format[1]": _join_status_row(record, theme, runtime),
        }
        if record.get("layout_mode") in {"family-v1", "canvas-v2", "canvas-v3"}:
            windows = [window for window in observed.get("windows", [])
                       if window.get("window_id") == record.get("status_window_id")]
            if (len(windows) != 1 or windows[0].get("width") != _family_width(record)
                    or any(pane.get("input_off") is not True for pane in windows[0].get("panes", []))):
                raise ShellCommandError("runtime_baseline_geometry_unknown", "The family canvas or pane input guard is not proven.", effect="unknown")
            fields.update(prefix=runtime["baseline"]["prefix"],
                          prefix2=runtime["baseline"]["prefix2"],
                          **{"window-size": runtime["baseline"]["window_size"]})
        for name, value in fields.items():
            actual = self._checked(endpoint, ["show-options", "-t", target, "-qv", name]).strip()
            if actual != value.strip():
                self._checked(endpoint, ["set-option", "-t", target, name, value], mutation=True)
                readback = self._checked(endpoint, ["show-options", "-t", target, "-qv", name]).strip()
                if readback != value.strip():
                    raise ShellCommandError("runtime_baseline_readback_unknown", f"The private {name} option did not read back.", effect="unknown")
        if record.get("layout_mode") in {"family-v1", "canvas-v2", "canvas-v3"}:
            if self._run(endpoint, ["list-keys", "-T", "prefix"]).stdout.strip():
                self._checked(endpoint, ["unbind-key", "-a", "-T", "prefix"], mutation=True)
            self._checked(endpoint, ["unbind-key", "-a", "-T", "root"], mutation=True)
            for table in ("copy-mode", "copy-mode-vi"):
                self._checked(endpoint, ["unbind-key", "-a", "-T", table], mutation=True)
                for key, action in self._OBSERVER_COPY_KEYS.items():
                    self._checked(endpoint, ["bind-key", "-T", table, key,
                                             "send-keys", "-X", action], mutation=True)
            actions = {"ignore": ["display-message", "Observer input is disabled"],
                       "copy_mode_up": ["copy-mode", "-u"], "copy_mode_mouse": ["copy-mode", "-e"],
                       "detach": ["detach-client"]}
            for key, action_id in runtime["keys"].items():
                self._checked(endpoint, ["bind-key", "-T", "root", key, *actions[action_id]], mutation=True)
            if record.get("layout_mode") == "canvas-v2":
                for key, step in (("Left", -1), ("Right", 1)):
                    self._checked(endpoint, ["bind-key", "-T", "root", key,
                                             "run-shell", "-b",
                                             _canvas_page_command(record, step)], mutation=True)
                self._checked(endpoint, ["bind-key", "-T", "root", "MouseDown1Pane",
                                         "run-shell", "-b", _canvas_mouse_command(record)], mutation=True)
            if record.get("layout_mode") == "canvas-v3":
                native_bindings = native_control_bindings(record)
                if set(native_bindings).intersection(runtime["keys"]):
                    raise ShellCommandError("native_control_key_collision", "A native canvas key overlaps the registered runtime bindings.")
                for key, action in native_bindings.items():
                    self._checked(endpoint, ["bind-key", "-T", "root", key, *action], mutation=True)
            keys = self._checked(endpoint, ["list-keys", "-T", "root"]).splitlines()
            extra_keys = (3 if record.get("layout_mode") == "canvas-v2" else
                          len(native_control_bindings(record)) if record.get("layout_mode") == "canvas-v3" else 0)
            if (len(keys) != len(runtime["keys"]) + extra_keys
                    or any(not any(re.search(r"\s-T root\s+" + re.escape(key) + r"\s", line)
                                   for line in keys) for key in runtime["keys"])):
                raise ShellCommandError("runtime_key_readback_unknown", "The private viewer key table did not read back.", effect="unknown")
            status_window = str(record["status_window_id"])
            border = f"fg={theme['border']},bg={theme['background']}"
            self._checked(endpoint, ["set-window-option", "-t", status_window,
                                     "pane-border-style", border], mutation=True)
            for name in ("pane-border-status", "pane-border-format"):
                value = ("off" if record.get("layout_mode") == "canvas-v2" and name == "pane-border-status"
                         else runtime["layout"][name.replace("-", "_")])
                self._checked(endpoint, ["set-window-option", "-t", status_window,
                                         name, value], mutation=True)
                actual = self._checked(endpoint, ["show-options", "-w", "-t", status_window,
                                                 "-qv", name]).strip()
                if actual != value:
                    raise ShellCommandError("runtime_baseline_readback_unknown", f"The private {name} option did not read back.", effect="unknown")
            self.verify_observer_keys(record)

    def attach(self, record: Mapping[str, Any], operation: Mapping[str, Any] | None = None,
               *, on_resize: Callable[[int], None] | None = None,
               on_ready: Callable[[int], None] | None = None) -> int:
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
        attach_flags = ["-r"]
        command = [self.tmux_binary or "tmux", "-S", endpoint, "attach-session", *attach_flags, "-t", target]
        if on_resize is None and on_ready is None:
            return subprocess.run(command, env=env, check=False, shell=False).returncode
        pending = True
        resize_at = time.monotonic()
        debounce = (_runtime_settings(record)["layout"].get("resize_debounce_ms", 250) / 1000
                    if record.get("layout_mode") == "canvas-v2" else 0)
        previous_handler = signal.getsignal(signal.SIGWINCH)
        def mark_resize(_signum: int, _frame: Any) -> None:
            nonlocal pending, resize_at
            pending = True
            resize_at = time.monotonic()
        signal.signal(signal.SIGWINCH, mark_resize)
        try:
            with subprocess.Popen(command, env=env, shell=False) as child:
                if on_ready is not None:
                    try:
                        deadline = time.monotonic() + 2.0
                        while True:
                            try:
                                on_ready(child.pid)
                                break
                            except ShellCommandError as exc:
                                if exc.code != "observer_identity_unknown" or time.monotonic() >= deadline:
                                    raise
                                time.sleep(0.05)
                    except ShellCommandError:
                        child.terminate()
                        child.wait(timeout=2)
                        raise
                while True:
                    if pending and on_resize is not None and time.monotonic() - resize_at >= debounce:
                        pending = False
                        try:
                            on_resize(min(512, max(1, os.get_terminal_size(0).lines)))
                        except (OSError, ShellCommandError):
                            pass
                    try:
                        return child.wait(timeout=(0.02 if record.get("layout_mode") == "canvas-v3"
                                                   else 0.05 if record.get("layout_mode") == "canvas-v2"
                                                   else 0.5))
                    except subprocess.TimeoutExpired:
                        continue
        finally:
            signal.signal(signal.SIGWINCH, previous_handler)
