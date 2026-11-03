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

    from ls.core.terminal_ops.command_native_backend import NativeCanvasV3
    from ls.core.terminal_ops.command_state import ShellCommandError

    diagnostics: dict[str, object] = {}
    samples: list[dict[str, object]] = []
    tmux_path = Path(shutil.which("tmux") or "tmux").resolve()
    original_show = NativeCanvasV3.show_dialog
    original_command = NativeCanvasV3._process_command

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
        if diagnostics.get("show_entered"):
            if len(samples) == 4:
                samples.pop(0)
            samples.append({
                "pid": pid, "command_available": command is not None,
                "argv0_is_tmux": bool(command and Path(os.fsdecode(command[0])).resolve() == tmux_path),
                "popup_argument_present": bool(command and b"display-popup" in command),
            })
        return command

    monkeypatch.setattr(NativeCanvasV3, "show_dialog", observed_show)
    monkeypatch.setattr(NativeCanvasV3, "_process_command", staticmethod(observed_command))
    return diagnostics
