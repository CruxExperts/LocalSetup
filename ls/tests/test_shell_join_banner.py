"""Focused formatting and CLI checks for shell join advertisements."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import unicodedata

import pytest

from ls.core.terminal_ops import command_cli
from ls.core.terminal_ops.command_state import ShellCommandError
from ls.core.terminal_ops.join_banner import JoinBannerError, render_join_banner


SOURCE_ROOT = Path(__file__).resolve().parents[2]
SESSION = "lsshell1." + "a" * 24 + "." + "b" * 32
OPERATION = "lsopcmd1." + "a" * 24 + "." + "b" * 32 + ".job_1"
OWNER = {"pid": 11, "starttime": 22, "agent_id": "native-agent",
         "machine_generation": "machine-generation", "host": "worker-host"}


def _entry(host: str = "box04", handle: str = SESSION) -> dict[str, str]:
    return {"host": host, "join_command": f"localsetup shell attach --handle {handle}"}


def _status(host: str, command: str, *, state: str = "ready") -> dict[str, object]:
    return {"api_version": 1, "ok": True, "state": state,
            "native_state": "ready", "session_state": "ready",
            "machine": {"host": host, "generation": "machine-generation"},
            "join_command": command}


def _display_width(text: str) -> int:
    return sum(0 if unicodedata.category(char).startswith("M") else
               2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in text)


def test_single_banner_has_exact_spacing_and_keeps_service_command() -> None:
    entry = {"host": "box04", "join_command": "localsetup shell attach --session ls-12345678-abcdef12"}
    command = entry["join_command"]
    width = max(len(command), len(entry["host"]) + 4)
    hashes = width - len(entry["host"]) - 2
    top = "#" * (hashes // 2) + f" {entry['host']} " + "#" * (hashes - hashes // 2)

    assert render_join_banner([entry]) == f"{top}\n\n{command}\n\n{'#' * width}"
    assert len(top) == width


@pytest.mark.parametrize("target", ["--session ls-12345678-abcdef12", f"--handle {OPERATION}"])
def test_scoped_banner_retains_quoted_state_root(target: str) -> None:
    command = shlex.join(["localsetup", "shell", "--state-root", "/tmp/state with 'quotes'",
                          "attach", *shlex.split(target)])
    assert f"\n{command}\n" in render_join_banner([{"host": "fixture", "join_command": command}])


@pytest.mark.parametrize("command", [
    "localsetup shell --state-root relative attach --session ls-12345678-abcdef12",
    "localsetup shell --state-root /tmp/$(id) attach --session ls-12345678-abcdef12",
    "localsetup shell --state-root /tmp/`id` attach --session ls-12345678-abcdef12",
    "localsetup shell --state-root /tmp/a;b attach --session ls-12345678-abcdef12",
    "localsetup shell --state-root /tmp/a attach --session ls-12345678-abcdef12;id",
    "localsetup shell --state-root /tmp/a --state-root /tmp/b attach --session ls-12345678-abcdef12",
    "localsetup shell --config /tmp/config attach --session ls-12345678-abcdef12",
])
def test_scoped_banner_rejects_unsafe_or_unexpected_command_text(command: str) -> None:
    with pytest.raises(JoinBannerError):
        render_join_banner([{"host": "fixture", "join_command": command}])


def test_multiple_entries_share_width_and_use_the_next_header_as_separator() -> None:
    entries = [
        {"host": "a", "join_command": "localsetup shell attach --session ls-12345678-abcdef12"},
        {"host": "rack-東京", "join_command": f"localsetup shell attach --handle {OPERATION}"},
    ]
    width = max(_display_width(item["join_command"]) for item in entries)
    rendered = render_join_banner(entries)
    rows = rendered.split("\n")

    assert rows == [
        "#" * ((width - 3) // 2) + " a " + "#" * (width - 3 - (width - 3) // 2),
        "", entries[0]["join_command"], "",
        "#" * ((width - _display_width(" rack-東京 ")) // 2)
        + " rack-東京 "
        + "#" * (width - _display_width(" rack-東京 ")
               - (width - _display_width(" rack-東京 ")) // 2),
        "", entries[1]["join_command"], "", "#" * width,
    ]
    assert _display_width(rows[0]) == width
    assert _display_width(rows[4]) == width


@pytest.mark.parametrize("entry", [
    {"host": "box04\nnot-a-host", "join_command": f"localsetup shell attach --handle {SESSION}"},
    {"host": "box04", "join_command": f"localsetup shell attach --handle {SESSION}\x1b[31m"},
    {"host": "box04", "join_command": f"localsetup shell join --handle {SESSION} --invite secret"},
    {"host": "box04", "join_command": "localsetup shell admit --handle anything"},
])
def test_banner_rejects_controls_and_non_attach_instructions(entry: dict[str, str]) -> None:
    with pytest.raises(JoinBannerError):
        render_join_banner([entry])


def test_banner_color_detects_terminal_and_honors_explicit_override() -> None:
    class Terminal:
        def __init__(self, tty: bool):
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    assert command_cli._color_capable(Terminal(True), {"TERM": "xterm-256color"})
    assert not command_cli._color_capable(Terminal(False), {"TERM": "xterm"})
    assert not command_cli._color_capable(Terminal(True), {"TERM": "xterm", "NO_COLOR": ""})
    assert not command_cli._color_capable(Terminal(True), {"TERM": "dumb"})
    assert command_cli._color_capable(Terminal(False), {"FORCE_COLOR": "1"})
    assert not command_cli._color_capable(Terminal(False), {"FORCE_COLOR": "0"})
    assert not command_cli._color_capable(Terminal(False), {"FORCE_COLOR": "1", "NO_COLOR": ""})
    assert "\x1b[" not in render_join_banner([_entry()])
    assert "\x1b[36m" in render_join_banner([_entry()], color=True)


def test_ensure_prints_banner_without_changing_json_result(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    original = {"api_version": 1, "ok": True, "state": "ready", "handle": SESSION,
                "machine": {"host": "box04", "generation": "machine-generation"},
                "join_command": f"localsetup shell attach --handle {SESSION}"}

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        def ensure(self, **_kwargs):
            return dict(original)

        def status(self, handle, *, owner):
            assert handle == SESSION and owner == OWNER
            return _status("box04", original["join_command"])

    monkeypatch.setattr(command_cli, "ShellCommandService", FakeService)
    monkeypatch.setattr(command_cli, "_owner", lambda _args: OWNER)
    monkeypatch.setattr(command_cli, "resolve_tmux_runtime_profile", lambda **_kwargs: {
        "profile": {"settings": {}, "digest": "profile-digest", "name": "agent-shell",
                    "base_profile": None},
        "settings": {}, "warnings": [],
    })

    code = command_cli.main(["ensure", "--owner-pid", "11", "--owner-starttime", "22"],
                            source_root=SOURCE_ROOT, home=Path("/tmp/home"))
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert code == 0
    assert output == original | {"owner": {key: OWNER[key] for key in
                                              ("pid", "starttime", "agent_id", "machine_generation")}}
    assert "box04" in captured.err and original["join_command"] in captured.err
    assert captured.err.startswith("\n") and captured.err.endswith("\n\n")
    assert "\x1b[" not in captured.err


def test_run_banner_is_emitted_before_stream_wait_and_json_stays_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    initial = {"api_version": 1, "ok": True, "state": "running", "operation_handle": OPERATION,
               "machine": {"host": "worker-host", "generation": "machine-generation"},
               "join_command": f"localsetup shell attach --handle {OPERATION}"}

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            return dict(initial)

        def status(self, handle, *, owner):
            assert handle == OPERATION and owner == OWNER
            return _status("worker-host", initial["join_command"], state="running")

    def stream(_service, result, *, timeout, owner):
        assert timeout == 1 and owner == OWNER
        before_wait = capsys.readouterr()
        assert before_wait.out == ""
        assert "worker-host" in before_wait.err
        assert initial["join_command"] in before_wait.err
        return result | {"state": "completed", "exit_code": 0}

    monkeypatch.setattr(command_cli, "ShellCommandService", FakeService)
    monkeypatch.setattr(command_cli, "_owner", lambda _args: OWNER)
    monkeypatch.setattr(command_cli, "_stream_operation", stream)
    code = command_cli.main(["run", "--handle", SESSION, "--id", "job", "--stream", "--timeout", "1",
                             "--owner-pid", "11", "--owner-starttime", "22", "--", "true"],
                            source_root=SOURCE_ROOT, home=Path("/tmp/home"))
    captured = capsys.readouterr()
    assert code == 0 and captured.err == ""
    assert json.loads(captured.out) == initial | {"state": "completed", "exit_code": 0}


def test_join_uses_verified_machine_and_service_attach_command(
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    original = {"api_version": 1, "ok": True, "state": "joined", "role": "member",
                "handle": SESSION,
                "join_command": f"localsetup shell attach --handle {SESSION}"}
    observer_command = "localsetup shell attach --session ls-12345678-abcdef12"

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        def join_family(self, *_args, **_kwargs):
            return dict(original)

        def status(self, handle, *, owner):
            assert handle == SESSION and owner == OWNER
            return _status("family-host", observer_command)

    monkeypatch.setattr(command_cli, "ShellCommandService", FakeService)
    monkeypatch.setattr(command_cli, "_owner", lambda _args: OWNER)
    code = command_cli.main(["join", "--handle", SESSION, "--invite", "private-invite",
                             "--owner-pid", "11", "--owner-starttime", "22"],
                            source_root=SOURCE_ROOT, home=Path("/tmp/home"))
    captured = capsys.readouterr()

    assert code == 0 and json.loads(captured.out) == original
    assert "family-host" in captured.err
    assert observer_command in captured.err
    assert "private-invite" not in captured.err
    assert "shell join" not in captured.err


def test_stream_periodic_reminder_prints_a_ready_banner_and_keeps_json_event(
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    ticks = iter((0.0, 0.0, 301.0, 301.0, 301.0, 301.0))
    monkeypatch.setattr(command_cli.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(command_cli.time, "sleep", lambda _seconds: None)
    alias_command = "localsetup shell attach --session ls-12345678-abcdef12"

    class QuietThenDone:
        calls = 0

        def status(self, _handle, *, owner):
            self.calls += 1
            return _status("stream-host", alias_command,
                           state="running" if self.calls == 1 else "completed")

    result = command_cli._stream_operation(
        QuietThenDone(), {"operation_handle": OPERATION}, timeout=600, owner=None,
    )
    captured = capsys.readouterr()
    assert result["state"] == "completed" and captured.out == ""
    assert "stream-host" in captured.err and alias_command in captured.err
    assert '"event":"join_reminder"' in captured.err


@pytest.mark.parametrize("native_state,expect_banner", [("ready", True), ("unknown", False)])
def test_status_wait_reminder_rechecks_readiness_before_banner(
    monkeypatch: pytest.MonkeyPatch, capsys, native_state: str, expect_banner: bool,
) -> None:
    alias_command = "localsetup shell attach --session ls-12345678-abcdef12"

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        def status(self, _handle, *, owner):
            return {"api_version": 1, "ok": True, "state": "running",
                    "session_state": "ready", "native_state": native_state,
                    "machine": {"host": "wait-host", "generation": "machine-generation"},
                    "join_command": alias_command}

        def wait(self, handle, *, timeout, owner, progress):
            assert handle == OPERATION
            progress({"api_version": 1, "event": "join_reminder",
                      "machine": {"host": "stale-host"}, "join_command": alias_command})
            return {"api_version": 1, "ok": True, "state": "completed", "exit_code": 0}

    monkeypatch.setattr(command_cli, "ShellCommandService", FakeService)
    code = command_cli.main(["status", "--handle", OPERATION, "--wait"],
                            source_root=SOURCE_ROOT, home=Path("/tmp/home"))
    captured = capsys.readouterr()

    assert code == 0 and json.loads(captured.out)["state"] == "completed"
    assert '"event":"join_reminder"' in captured.err
    assert ("wait-host" in captured.err and alias_command in captured.err) is expect_banner
    banner_lines = [line for line in captured.err.splitlines() if line.startswith("#")]
    assert bool(banner_lines) is expect_banner


def test_batch_advertise_keeps_valid_targets_and_reports_each_error_in_order(
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    alias_one = "ls-12345678-abcdef12"
    alias_two = "ls-87654321-fedcba98"
    calls: list[tuple[str, object]] = []

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        def status(self, handle, *, owner):
            calls.append((handle, owner))
            if handle == "invalid":
                raise ShellCommandError("session_handle_invalid", "The shell handle is malformed.")
            alias = alias_one if handle == SESSION else alias_two
            host = "host-one" if handle == SESSION else "host-two"
            return _status(host, f"localsetup shell attach --session {alias}")

    monkeypatch.setattr(command_cli, "ShellCommandService", FakeService)
    monkeypatch.setattr(command_cli, "_owner", lambda _args: OWNER)
    code = command_cli.main(["advertise", "--handle", SESSION, "--handle", "invalid",
                             "--handle", OPERATION, "--owner-pid", "11", "--owner-starttime", "22"],
                            source_root=SOURCE_ROOT, home=Path("/tmp/home"))
    captured = capsys.readouterr()
    response = json.loads(captured.out)

    assert code == 1 and response["ok"] is False and response["state"] == "partial"
    assert [item["handle"] for item in response["entries"]] == [SESSION, "invalid", OPERATION]
    assert [item["ok"] for item in response["entries"]] == [True, False, True]
    assert [handle for handle, owner in calls] == [SESSION, "invalid", OPERATION]
    assert all(owner == OWNER for _, owner in calls)
    assert captured.err.index("host-one") < captured.err.index("host-two")
    assert "localsetup shell attach --session ls-12345678-abcdef12" in captured.err
    assert "localsetup shell attach --session ls-87654321-fedcba98" in captured.err
    assert "invalid" not in captured.err


def test_invalid_target_has_no_advertisement(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        def status(self, _handle, *, owner):
            return {"api_version": 1, "ok": True, "state": "unknown", "native_state": "missing"}

    monkeypatch.setattr(command_cli, "ShellCommandService", FakeService)
    code = command_cli.main(["advertise", "--handle", SESSION],
                            source_root=SOURCE_ROOT, home=Path("/tmp/home"))
    captured = capsys.readouterr()
    response = json.loads(captured.out)

    assert code == 1 and response["ok"] is False
    assert response["entries"][0]["code"] == "session_not_ready"
    assert captured.err == ""
