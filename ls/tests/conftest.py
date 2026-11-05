import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins = ["ls.core.test_plugin"]


@pytest.fixture
def synthetic_runtime_interpreter(tmp_path, monkeypatch):
    """Supply owned bytes for inventory unit tests; never execute this file."""
    from ls.core.agent import runtime_integrity
    interpreter = tmp_path / 'synthetic-python'
    interpreter.write_bytes(b'synthetic interpreter inventory fixture')
    interpreter.chmod(0o700)
    monkeypatch.setattr(runtime_integrity, 'sys', SimpleNamespace(
        executable=str(interpreter), version_info=sys.version_info))
    return interpreter


@pytest.fixture
def default_opencode_environment(monkeypatch):
    """Select default discovery roots only for tests that request this fixture."""
    for name in ('OPENCODE_TEST_HOME', 'OPENCODE_CONFIG_DIR',
                 'OPENCODE_DISABLE_EXTERNAL_SKILLS', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def native_popup_diagnostics(monkeypatch):
    """Observe private popup failures without retaining arguments or input."""
    import shutil
    import subprocess
    import threading

    from ls.core.terminal_ops.command_native_backend import NativeCanvasV3
    from ls.core.terminal_ops.command_state import ShellCommandError, process_info, process_state

    diagnostics: dict[str, object] = {}
    samples: list[dict[str, object]] = []
    tmux_path = Path(shutil.which("tmux") or "tmux").resolve()
    original_show = NativeCanvasV3.show_dialog
    original_command = NativeCanvasV3._process_command
    original_launch = NativeCanvasV3._run_overlay_client
    expected: dict[str, bytes] = {}
    local = threading.local()
    original_popen = subprocess.Popen

    def observed_popen(*args, **kwargs):
        scope = getattr(local, "scope", None)
        argv = args[0] if args else kwargs.get("args")
        if (scope is None or scope.get("attempted") or argv is None or argv is not scope["argv"]
                or len(argv) < 8 or argv[1] != "-f" or argv[3] != "-S"
                or os.fsencode(argv[4]) != scope["endpoint"] or argv[5] != "display-popup"
                or argv[6] != "-c" or os.fsencode(argv[7]) != scope["client"]
                or kwargs.get("stderr") != subprocess.DEVNULL):
            return original_popen(*args, **kwargs)
        scope["attempted"] = True
        try:
            scope["fd"] = os.memfd_create("popup-stderr", getattr(os, "MFD_CLOEXEC", 0))
        except (AttributeError, OSError) as exc:
            scope["capture_error"] = type(exc).__name__
        else:
            kwargs = {**kwargs, "stderr": scope["fd"]}
        process = original_popen(*args, **kwargs)
        scope["process"] = process
        return process

    def failure_evidence(scope):
        result = {"capture_error": scope.get("capture_error")}
        process = scope.get("process")
        if process is not None:
            pid = process.pid
            info = process_info(pid)
            result.update(pid=pid, process=(
                {key: info.get(key) for key in ("pid", "ppid", "starttime")} if info else None),
                state=process_state(pid))
            if all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")):
                try:
                    status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    result["exit"] = ({"exited": False} if status is None else
                                      {"exited": True, "si_code": status.si_code, "si_status": status.si_status})
                except (OSError, ChildProcessError) as exc:
                    result["exit"] = {"error": type(exc).__name__}
            else:
                result["exit"] = {"error": "unsupported"}
        fd = scope.get("fd")
        if fd is not None:
            length = os.fstat(fd).st_size
            data = os.pread(fd, min(length, 1048576), 0).lower()
            phrases = {
                "client_missing": (b"no such client", b"can't find client", b"client not found"),
                "terminal_unsupported": (b"terminal does not support", b"terminal lacks", b"not a terminal", b"unknown terminal"),
                "server_missing": (b"no server running", b"failed to connect to server", b"server not found", b"no such socket"),
                "invalid_argument": (b"invalid argument", b"unknown option", b"usage:"),
                "invalid_size": (b"invalid size", b"size too small", b"width too small", b"height too small", b"popup too large"),
                "permission_denied": (b"permission denied", b"operation not permitted"),
                "session_missing": (b"no such session", b"can't find session"),
                "pane_missing": (b"no such pane", b"can't find pane"),
                "nested_session": (b"sessions should be nested",),
                "server_terminated": (b"server exited unexpectedly", b"lost server"),
                "popup_unsupported": (b"unknown command", b"unknown flag", b"popup already exists"),
            }
            result["stderr"] = {"bytes": length, "classified_bytes": len(data), "flags": {
                name: (True if any(phrase in data for phrase in choices) else False if len(data) == length else None)
                for name, choices in phrases.items()
            }}
        return result


    def observed_launch(self, record, client, argv, *, on_spawn, deadline=None):
        previous = getattr(local, "scope", None)
        scope = {"argv": argv, "endpoint": os.fsencode(record["endpoint"]),
                 "client": os.fsencode(client["name"])}
        local.scope = scope
        expected.update(endpoint=os.fsencode(record["endpoint"]), client=os.fsencode(client["name"]))
        try:
            return original_launch(self, record, client, argv, on_spawn=on_spawn, deadline=deadline)
        except ShellCommandError as exc:
            if exc.code == "overlay_identity_unknown":
                try:
                    diagnostics["failure"] = failure_evidence(scope)
                except Exception as error:
                    diagnostics["failure"] = {"diagnostic_error": type(error).__name__}
            raise
        finally:
            local.scope = previous
            expected.clear()
            if scope.get("fd") is not None:
                os.close(scope["fd"])

    def observed_show(self, record, client_pid, channel, *, on_spawn):
        samples.clear()
        diagnostics.clear()
        diagnostics.update(show_entered=True, process_samples=samples)
        try:
            return original_show(self, record, client_pid, channel, on_spawn=on_spawn)
        except ShellCommandError as exc:
            diagnostics.update(error_code=exc.code, effect=exc.effect)
            raise

    def observed_command(pid: int) -> list[bytes] | None:
        command = original_command(pid)
        if diagnostics.get("show_entered") and expected:
            if len(samples) == 4:
                samples.pop(0)
            samples.append({
                "pid": pid, "command_available": command is not None,
                "command_nonempty": bool(command),
                "process_present_after_read": process_info(pid) is not None,
                "process_zombie_after_read": process_state(pid) == "Z",
                "argv0_is_tmux": bool(command and Path(os.fsdecode(command[0])).resolve() == tmux_path),
                "popup_argument_present": bool(command and b"display-popup" in command),
                "endpoint_matches": bool(command and expected.get("endpoint") in command),
                "client_matches": bool(command and expected.get("client") in command),
            })
        return command

    monkeypatch.setattr(NativeCanvasV3, "show_dialog", observed_show)
    monkeypatch.setattr(subprocess, "Popen", observed_popen)
    monkeypatch.setattr(NativeCanvasV3, "_run_overlay_client", observed_launch)
    monkeypatch.setattr(NativeCanvasV3, "_process_command", staticmethod(observed_command))
    return diagnostics
