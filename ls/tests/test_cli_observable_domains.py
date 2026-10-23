"""Domain delegation preserves shell argv and explicit source/home selection."""
from pathlib import Path
import sys
import types

import pytest

from ls.core import cli


@pytest.mark.parametrize(
    ("domain", "module_name", "arguments"),
    [
        ("shell", "ls.core.terminal_ops.command_cli", ["run", "--", "bash", "-c", "printf '%s' '$literal'"]),
        ("app-profile", "ls.core.application_profiles.cli", ["show", "--application", "tmux"]),
    ],
)
def test_domain_forwarding_preserves_arguments(monkeypatch, tmp_path, domain, module_name, arguments):
    received = []
    module = types.ModuleType(module_name)

    def main(argv, *, source_root, home):
        received.append((argv, source_root, home))
        return 17

    module.main = main
    monkeypatch.setitem(sys.modules, module_name, module)
    root = Path(__file__).resolve().parents[2]
    code = cli.main(["--source-root", str(root), "--home", str(tmp_path), domain, *arguments])
    assert code == 17
    assert received == [(arguments, root, tmp_path)]


def test_domain_help_reaches_owning_parser(monkeypatch, tmp_path):
    module = types.ModuleType("ls.core.terminal_ops.command_cli")
    module.main = lambda argv, **kwargs: 0 if argv == ["--help"] else 1
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert cli.main(["--home", str(tmp_path), "shell", "--help"]) == 0


def test_schedule_cli_binds_enrollment_owner_and_preserves_state_root(monkeypatch, tmp_path):
    from ls.core.terminal_ops import command_cli, command_scheduler

    received = []
    owner = {"pid": 42, "starttime": 123, "agent_id": "controller", "uid": 1000}
    monkeypatch.setattr(command_cli, "_owner", lambda args: owner)
    monkeypatch.setattr(command_scheduler, "install_schedule", lambda **kwargs: received.append(kwargs) or {"ok": True})
    root = Path(__file__).resolve().parents[2]
    state = str(tmp_path / "shell-state")
    assert command_cli.main(
        ["--state-root", state, "schedule", "install", "--grant-id", "user-cleanup"],
        source_root=root, home=tmp_path,
    ) == 0
    assert received == [{
        "source_root": root, "state_root": state, "grant_id": "user-cleanup",
        "authority_owner": {key: owner[key] for key in ("pid", "starttime", "agent_id")},
    }]


def test_schedule_install_requires_qualified_owner_before_enrollment(monkeypatch, tmp_path, capsys):
    from ls.core.terminal_ops import command_cli, command_scheduler

    monkeypatch.delenv("LOCALSETUP_SHELL_OWNER_PID", raising=False)
    monkeypatch.delenv("LOCALSETUP_SHELL_OWNER_STARTTIME", raising=False)
    monkeypatch.setattr(command_scheduler, "install_schedule", lambda **kwargs: pytest.fail("must not enroll without an owner"))
    root = Path(__file__).resolve().parents[2]
    assert command_cli.main(["schedule", "install", "--grant-id", "user-cleanup"], source_root=root, home=tmp_path) == 2
    assert "owner_identity_required" in capsys.readouterr().out


def test_stream_rechecks_output_published_with_terminal_status(capsys):
    from ls.core.terminal_ops.command_cli import _stream_operation

    class FinishingOperation:
        ready = False

        def logs(self, handle, *, cursor, owner):
            data = "abcdef"[cursor:] if self.ready else ""
            return {"output": data, "cursor": cursor + len(data), "end_cursor": 6 if self.ready else 0}

        def status(self, handle, *, owner):
            self.ready = True
            return {"state": "completed", "exit_code": 0, "output_end_cursor": 6}

    result = _stream_operation(FinishingOperation(), {"operation_handle": "operation"}, timeout=1, owner=None)
    assert capsys.readouterr().out == "abcdef"
    assert result["stream_cursor"] == 6
    assert result["stream_gap"] is False


def test_stream_timeout_also_bounds_terminal_backlog(capsys):
    from ls.core.terminal_ops.command_cli import _stream_operation

    class TerminalBacklog:
        def logs(self, handle, *, cursor, owner):
            return {"output": "a", "cursor": cursor + 1, "end_cursor": 100}

        def status(self, handle, *, owner):
            return {"state": "completed", "exit_code": 0, "output_end_cursor": 100}

    result = _stream_operation(TerminalBacklog(), {"operation_handle": "operation"}, timeout=0, owner=None)
    assert result["stream_timed_out"] is True
    assert result["stream_cursor"] == 1


def test_stream_reconciles_busy_status_without_repeating_output(capsys):
    from ls.core.terminal_ops.command_cli import _stream_operation
    from ls.core.terminal_ops.command_state import ShellCommandError

    class BusyThenComplete:
        busy = True

        def logs(self, handle, *, cursor, owner):
            return {"output": "one"[cursor:], "cursor": 3, "end_cursor": 3}

        def status(self, handle, *, owner):
            if self.busy:
                self.busy = False
                raise ShellCommandError("busy", "runner finalization is in progress")
            return {"state": "completed", "exit_code": 0, "output_end_cursor": 3}

    result = _stream_operation(BusyThenComplete(), {"operation_handle": "operation"}, timeout=1, owner=None)
    assert capsys.readouterr().out == "one"
    assert result["stream_cursor"] == 3
    assert result["stream_timed_out"] is False
