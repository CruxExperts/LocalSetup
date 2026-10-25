"""Focused lifecycle checks for owned, observable command sessions."""

from __future__ import annotations

import multiprocessing
import json
import os
from pathlib import Path
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService, _digest, _profile_digest
from ls.core.terminal_ops.command_cli import _bounded_file, _exit_code, _print, _stream_operation
from ls.core.terminal_ops import command_state
from ls.core.terminal_ops.command_state import (
    ShellCommandError,
    atomic_json,
    log_lock,
    make_owner_binding,
    owner_fingerprint,
    process_info,
    read_json,
)


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def test_runner_reaps_orphans_while_preserving_workload_status() -> None:
    # Isolate subreaper ownership from pytest and its worker processes.
    script = r'''
import os, subprocess, sys, time
from pathlib import Path
from ls.core.terminal_ops.command_runner import _become_subreaper, _reap_adopted_children
assert _become_subreaper()
workload = subprocess.Popen([sys.executable, "-c", "input(); raise SystemExit(37)"], stdin=subprocess.PIPE)
try:
    spawner = "import os,time,sys; from pathlib import Path; pid=os.fork(); print(pid, flush=True) if pid else None\nif not pid:\n while not Path(sys.argv[1]).exists(): time.sleep(.02)\nos._exit(0)"
    # Do not capture the orphan's inherited stdout through a waiting pipe.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        release = Path(directory) / "release"
        with tempfile.TemporaryFile() as output:
            subprocess.run([sys.executable, "-c", spawner, str(release)], stdout=output, check=True)
            output.seek(0)
            orphan = int(output.read())
        _reap_adopted_children(workload)
        remained_alive = Path(f"/proc/{orphan}").exists()
        release.touch()
        deadline = time.monotonic() + 5
        while Path(f"/proc/{orphan}").exists() and time.monotonic() < deadline:
            _reap_adopted_children(workload)
            time.sleep(.02)
    assert not Path(f"/proc/{orphan}").exists(), "exited orphan was not reaped"
    assert remained_alive, "live adopted child was killed"
    assert workload.poll() is None
    workload.stdin.write(b"\n")
    workload.stdin.flush()
    deadline = time.monotonic() + 5
    stat = Path(f"/proc/{workload.pid}/stat")
    while stat.read_text().rsplit(")", 1)[1].split()[0] != "Z" and time.monotonic() < deadline:
        time.sleep(.02)
    assert stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    _reap_adopted_children(workload)
    assert workload.wait(timeout=5) == 37
finally:
    if workload.poll() is None:
        workload.kill()
    workload.wait()
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


class VirtualBackend:
    """Small deterministic adapter for lifecycle and authority tests."""

    def __init__(self, *, available: bool = True):
        self.available = available
        self.closed = False
        self.created_operations = 0
        self.attached = False
        self.applied: dict[str, object] | None = None

    def capability(self):
        return {"state": "available" if self.available else "unavailable", "backend": "fake"}

    def socket_path(self, session_id: str) -> Path:
        return Path("/tmp/localsetup-fake") / f"{session_id}.sock"

    def create_session(self, record, *, cwd, settings):
        self.closed = False
        self.applied = dict(settings)
        self.owner_marker = record["owner_fingerprint"]
        return {
            "native_session_id": "$fake1",
            "server_session_name": record["native_name"],
            "status_window_id": "@status",
            "status_pane_id": "%status",
            "status_pane_pid": None,
            "session_created": "fake",
        }

    def inspect_session(self, record):
        if self.closed:
            return {"state": "missing", "windows": []}
        options = {
            "api": "1", "handle": record["handle"], "key": record["session_key"],
            "machine": record["machine_generation"],
            "owner": getattr(self, "owner_marker", record["owner_fingerprint"]),
            "profile": record["settings_digest"], "status_window": record["status_window_id"],
            "layout": record.get("layout_mode"),
        }
        windows = [{"window_id": "@status", "pane_count": 1,
                    "metadata": {"status": "1", "session": record["handle"]},
                    "panes": [{"pane_id": "%status", "pane_pid": None,
                               "dead": getattr(self, "status_dead", True)}]}]
        operation = getattr(self, "operation", None)
        if operation:
            panes = [{"pane_id": operation["pane_id"],
                      "pane_pid": operation.get("pane_pid"), "dead": operation.get("dead", False)}]
            if operation.get("extra_pane"):
                panes.append({"pane_id": "%unrecorded", "pane_pid": None, "dead": False})
            windows.append({
                "window_id": operation["window_id"],
                "pane_count": len(panes),
                "metadata": {"session": record["handle"], "operation": operation["operation_id"],
                             "request": operation["request_digest"]},
                "panes": panes,
            })
        settings = self.applied or record["settings"]
        readback = {
            "mouse": settings["mouse"], "status": settings["status"],
            "status_position": settings["status_position"],
            "history_limit": {"@status": settings["history_limit"]},
        }
        return {"state": "ready", "options": options, "windows": windows,
                "session": {"attached": getattr(self, "client_count", 0)},
                "applied_settings": readback}

    def create_operation_window(self, record, operation, *, command):
        self.created_operations += 1
        self.operation = {"window_id": "@command", "pane_id": "%command",
                          "operation_id": operation["operation_id"],
                          "request_digest": operation["request_digest"]}
        return dict(self.operation)

    def tag_operation_window(self, record, operation, native):
        self.operation["operation_id"] = operation["operation_id"]

    def start_capture(self, record, operation, *, command):
        return None

    def select_operation(self, record, operation):
        return None

    def kill_window(self, record, operation, *, expected_window):
        observed = self.inspect_session(record)
        assert observed["session"]["attached"] == 0
        windows = [window for window in observed["windows"]
                   if window["window_id"] == operation["window_id"]]
        assert len(windows) == 1
        assert TmuxCommandBackend.window_identity(windows[0]) == expected_window
        self.operation = None

    def apply_session_settings(self, record, settings, profile_digest):
        self.applied = dict(settings)
        return {"state": "applied", "settings": dict(settings)}

    def set_owner_identity(self, record, *, allowed_previous_owners):
        assert self.owner_marker in allowed_previous_owners
        self.owner_marker = record["owner_fingerprint"]
        return None

    def close_session(self, record, *, expected_inventory):
        assert TmuxCommandBackend.inventory_identity(self.inspect_session(record)) == expected_inventory
        self.closed = True

    def remove_stale_endpoint(self, record):
        return True

    def attach(self, record, operation=None):
        self.attached = True
        self.attach_operation = operation
        return 0


def _profile(home: Path) -> dict[str, object]:
    return resolve_profile("tmux", "agent-shell", source_root=SOURCE_ROOT, home=home)


def _ensure(service: ShellCommandService, home: Path, cwd: Path) -> tuple[dict[str, object], dict[str, object]]:
    profile = _profile(home)
    owner = make_owner_binding(owner_pid=os.getpid())
    result = service.ensure(cwd=cwd, settings=profile["settings"],
                            settings_digest=profile["digest"], owner=owner,
                            profile_name=profile["name"], profile_base=profile["base_profile"],
                            layout_mode="legacy-v1")
    return result, owner


def _stable_owner(monkeypatch, agent_id: str = "recovery-fixture") -> dict[str, object]:
    monkeypatch.setattr(command_state, "discover_agent_id", lambda _pid: None)
    return make_owner_binding(owner_pid=os.getpid(), agent_id=agent_id)


def _replace_recorded_owner(
    service: ShellCommandService, handle: str, owner: dict[str, object], process: subprocess.Popen,
) -> tuple[Path, dict[str, object]]:
    info = process_info(process.pid)
    assert info is not None
    previous = {**owner, "pid": process.pid, "starttime": info["starttime"]}
    directory, session, _ = service._load(handle)
    session.update(owner=previous, owner_fingerprint=owner_fingerprint(previous))
    atomic_json(directory / "session.json", session)
    owner_dir = service._owner_dir(session["scope_key"])
    atomic_json(owner_dir / "owner.json", {
        "schema_version": 1, "scope_key": session["scope_key"],
        "owner": previous, "owner_fingerprint": session["owner_fingerprint"],
        "active_handle": handle, "updated_at": service.clock(),
    })
    return directory, session


def _complete_virtual_operation(
    service: ShellCommandService, backend: VirtualBackend, handle: str,
    owner: dict[str, object], operation_id: str,
) -> tuple[str, Path]:
    running = service.run(handle, operation_id, script="true", owner=owner)
    op_handle = str(running["operation_handle"])
    directory, _, _ = service._load(op_handle)
    paths = service._paths(directory, operation_id)
    operation = read_json(paths["record"])
    process = process_info(os.getpid())
    assert process is not None
    operation.update(state="completed", process_scope="collected", output_drain="available",
                     runner_pid=os.getpid(), runner_starttime=process["starttime"], exit_code=0)
    atomic_json(paths["record"], operation)
    atomic_json(paths["drained"], {"state": "drained", "storage": "available", "bytes_seen": 0})
    backend.operation["pane_pid"] = os.getpid()
    return op_handle, paths["record"]


def _observer_process(state_root: str, handle: str, output) -> None:
    service = ShellCommandService(state_root, backend=VirtualBackend(), source_root=SOURCE_ROOT)
    observed = service.status(handle)
    attached = service.attach(handle)
    try:
        service.run(handle, "observer-mutation", script="true")
    except ShellCommandError as exc:
        mutation_code = exc.code
    else:  # pragma: no cover - an authorization failure is required here
        mutation_code = "unexpectedly_allowed"
    output.send({"status": observed["state"], "attached": attached["attached"],
                 "mutation_code": mutation_code})
    output.close()


def test_capability_probe_does_not_create_private_state(tmp_path: Path) -> None:
    state_root = tmp_path / "not-created"
    service = ShellCommandService(state_root, backend=VirtualBackend(available=False), source_root=SOURCE_ROOT)

    assert service.capabilities()["state"] == "unavailable"
    profile = _profile(tmp_path / "profile-home")
    result = service.ensure(cwd=tmp_path, settings=profile["settings"],
                            settings_digest=profile["digest"])

    assert result["state"] == "unavailable"
    assert not state_root.exists()


@pytest.mark.parametrize("root_name", [None, "state", "state with spaces and 'quotes'"])
def test_observer_attach_is_read_only_and_handle_bound(tmp_path: Path, monkeypatch, root_name: str | None) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    override = tmp_path / root_name if root_name is not None else None
    state_root = command_state.private_root_path(override)
    state_root.mkdir(parents=True, mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(override, backend=backend, source_root=SOURCE_ROOT)
    ensured, _ = _ensure(service, tmp_path / "home", tmp_path)
    assert ensured["created"] is True
    assert ensured["joined"] is False
    assert ensured["native_session_name"].startswith("ls-")
    from ls.core.terminal_ops.command_cli import build_parser
    from ls.core.terminal_ops.join_banner import validate_join_entry

    command = shlex.split(ensured["join_command"])
    prefix = ["localsetup", "shell"] + (["--state-root", str(state_root)] if override is not None else [])
    assert command == [*prefix,
                       "attach", "--session", ensured["session_alias"]]
    parsed = build_parser().parse_args(command[2:])
    observer = ShellCommandService(parsed.state_root, backend=backend, source_root=SOURCE_ROOT)
    assert observer.resolve_alias(parsed.session) == ensured["handle"]
    validate_join_entry({"host": ensured["machine"]["host"], "join_command": ensured["join_command"]})

    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_observer_process, args=(str(state_root), str(ensured["handle"]), send),
    )
    process.start()
    send.close()
    assert receive.poll(5)
    observed = receive.recv()
    process.join(5)
    assert process.exitcode == 0
    assert observed == {"status": "ready", "attached": True, "mutation_code": "owner_identity_mismatch"}
    assert backend.attached is False  # attachment occurred only in the forked observer copy


def test_native_attach_requires_tty_and_read_only_mode(monkeypatch) -> None:
    backend = TmuxCommandBackend("tmux")
    record = {"endpoint": "/tmp/only-fixture-socket", "native_session_id": "$1"}
    calls = []
    monkeypatch.setattr(os, "isatty", lambda _fd: False)
    with pytest.raises(ShellCommandError) as missing_tty:
        backend.attach(record)
    assert missing_tty.value.code == "observer_tty_required"
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0))
    assert backend.attach(record) == 0
    assert calls[0][-4:] == ["attach-session", "-r", "-t", "$1"]


@pytest.mark.parametrize("version", ["tmux 3.3a", "tmux 3.5a", "tmux 3.4"])
def test_native_capability_accepts_tmux_patch_letters(monkeypatch, version: str) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=version, stderr=""))
    assert TmuxCommandBackend("tmux").capability()["state"] == "available"


def test_operation_idempotency_and_status_include_bounded_recent_output(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    handle = str(ensured["handle"])

    first = service.run(handle, "stable-op", script="printf 'hello\\n'", owner=owner)
    assert first["machine"] == ensured["machine"]
    assert shlex.split(first["join_command"]) == [
        "localsetup", "shell", "--state-root", str(state_root),
        "attach", "--handle", first["operation_handle"],
    ]
    assert first["session_join_command"] == ensured["join_command"]
    assert first["join_target"] == "operation_pane"
    repeated = service.run(handle, "stable-op", script="printf 'hello\\n'", owner=owner)
    assert repeated["operation_handle"] == first["operation_handle"]
    assert backend.created_operations == 1
    with pytest.raises(ShellCommandError, match="different command data") as conflict:
        service.run(handle, "stable-op", script="printf 'different\\n'", owner=owner)
    assert conflict.value.code == "operation_id_conflict"

    directory, session, op_id = service._load(first["operation_handle"])
    paths = service._paths(directory, str(op_id))
    output = "".join(f"line-{number:03d}\n" for number in range(205)).encode()
    paths["log"].write_bytes(output)
    paths["log"].chmod(0o600)
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": len(output), "available": True})

    status = service.status(first["operation_handle"])
    assert status["state"] == "running"
    assert status["machine"] == ensured["machine"]
    assert status["join_command"] == first["join_command"]
    assert status["output"].splitlines()[0] == "line-005"
    assert status["output"].splitlines()[-1] == "line-204"
    assert len(status["output"].splitlines()) == session["settings"]["default_lines"] == 200
    assert status["output_truncated"] is True
    assert status["output_end_cursor"] == len(output)

    backend.operation["request_digest"] = "wrong-request"
    with pytest.raises(ShellCommandError) as mismatched_pane:
        service.attach(str(first["operation_handle"]))
    assert mismatched_pane.value.code == "operation_identity_unknown"
    assert backend.attached is False
    backend.operation["request_digest"] = first["request_digest"]
    attached = service.attach(str(first["operation_handle"]))
    assert attached["attached"] is True
    assert backend.attach_operation["operation_id"] == "stable-op"
    assert attached["join_command"] == first["join_command"]

    operation = read_json(paths["record"])
    operation.update(state="completed", process_scope="collected", exit_code=0)
    atomic_json(paths["record"], operation)
    backend.attached = False
    completed = service.attach(str(first["operation_handle"]))
    assert completed["attached"] is False
    assert completed["join_target"] == "session"
    assert completed["join_command"] == ensured["join_command"]
    assert shlex.split(completed["logs_command"]) == [
        "localsetup", "shell", "--state-root", str(state_root),
        "logs", "--handle", first["operation_handle"],
    ]
    assert backend.attached is False


def test_log_decoding_caps_utf8_without_skipping_cursor_bytes(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    service = ShellCommandService(state_root, backend=VirtualBackend(), source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    running = service.run(str(ensured["handle"]), "invalid-output", script="true", owner=owner)
    handle = str(running["operation_handle"])
    directory, _, op_id = service._load(handle)
    paths = service._paths(directory, str(op_id))

    invalid = b"\xff" * 300_000
    paths["log"].write_bytes(invalid)
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": len(invalid), "available": True})
    invalid_status = service.status(handle)
    invalid_view = service.view(handle)
    assert len(invalid_status["output"].encode("utf-8")) <= 32_768
    assert len("\n".join(invalid_view["lines"]).encode("utf-8")) <= 32_768
    assert invalid_status["output_truncated"] is True
    assert invalid_view["truncated"] is True
    cursor = 0
    returned = []
    while cursor < len(invalid):
        chunk = service.logs(handle, cursor=cursor)
        assert len(chunk["output"].encode("utf-8")) <= 32_768
        assert chunk["cursor"] > cursor
        returned.append(chunk["output"])
        cursor = chunk["cursor"]
    assert "".join(returned) == "\ufffd" * len(invalid)
    assert cursor == len(invalid)

    boundary = b"A" * 1022 + b"\xffB"
    paths["log"].write_bytes(boundary)
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": len(boundary), "available": True})
    first = service.logs(handle, cursor=0, max_bytes=1024)
    assert first["output"] == "A" * 1022
    assert first["cursor"] == 1022
    assert first["truncated"] is True
    second = service.logs(handle, cursor=first["cursor"], max_bytes=1024)
    assert second["output"] == "\ufffdB"
    assert second["cursor"] == len(boundary)

    partial = b"\xe2\x82"
    paths["log"].write_bytes(partial)
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": 2, "available": True})
    pending = service.logs(handle, cursor=0, max_bytes=1024)
    assert pending["output"] == ""
    assert pending["cursor"] == 0
    paths["log"].write_bytes(partial + b"\xac")
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": 3, "available": True})
    completed_character = service.logs(handle, cursor=pending["cursor"], max_bytes=1024)
    assert completed_character["output"] == "€"
    assert completed_character["cursor"] == 3


def test_previous_log_pages_expose_exact_cursors_and_rollover_gap(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    service = ShellCommandService(state_root, backend=VirtualBackend(), source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    running = service.run(str(ensured["handle"]), "paged-output", script="true", owner=owner)
    handle = str(running["operation_handle"])
    directory, _, op_id = service._load(handle)
    paths = service._paths(directory, str(op_id))
    raw = b"".join(f"line-{i:04d}\n".encode() for i in range(600))
    paths["log"].write_bytes(raw)
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": len(raw), "available": True})
    newest = service.logs(handle, lines=20)
    older = service.logs(handle, before_cursor=newest["page_start_cursor"], lines=20)
    assert older["output"].splitlines() == [f"line-{i:04d}" for i in range(560, 580)]
    assert older["next_cursor"] == newest["page_start_cursor"]
    assert older["previous_cursor"] == older["page_start_cursor"]
    paths["log"].write_bytes(raw[100:])
    atomic_json(paths["cursor"], {"base_offset": 100, "end_offset": len(raw), "available": True})
    gap = service.logs(handle, before_cursor=50, lines=20)
    assert gap["gap"] is True and gap["page_start_cursor"] == 100


def test_giant_line_recent_output_bounds_logs_status_and_view(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    service = ShellCommandService(state_root, backend=VirtualBackend(), source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    running = service.run(str(ensured["handle"]), "giant-line", script="true", owner=owner)
    handle = str(running["operation_handle"])
    directory, _, op_id = service._load(handle)
    paths = service._paths(directory, str(op_id))
    raw = b"A" * 300_000
    paths["log"].write_bytes(raw)
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": len(raw), "available": True})
    tail = service.logs(handle)
    status = service.status(handle)
    view = service.view(handle)
    assert tail["truncated"] is True
    assert tail["cursor"] == len(raw)
    assert len(tail["output"].encode("utf-8")) <= 262_144
    assert len(status["output"].encode("utf-8")) <= 262_144
    assert len("\n".join(view["lines"]).encode("utf-8")) <= 262_144
    assert view["truncated"] is True


def test_status_exposes_profile_state_and_rejects_active_conflict(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    profile = _profile(tmp_path / "home")
    status = service.status(str(ensured["handle"]), owner=owner)
    assert status["profile_settings"] == profile["settings"]
    assert status["profile_readback"]["history_limit"]["@status"] == profile["settings"]["history_limit"]

    operation = service.run(str(ensured["handle"]), "busy-op", script="sleep 30", owner=owner)
    changed = dict(profile["settings"], mouse=not profile["settings"]["mouse"])
    changed_digest = _profile_digest(changed, "agent-shell")
    with pytest.raises(ShellCommandError) as busy:
        service.apply_profile(str(ensured["handle"]), changed, changed_digest, owner=owner)
    assert busy.value.code == "profile_session_busy"
    assert backend.created_operations == 1
    assert operation["state"] == "running"


def test_dotted_operation_ids_have_distinct_private_paths(tmp_path: Path) -> None:
    service = ShellCommandService(tmp_path / "state", backend=VirtualBackend(), source_root=SOURCE_ROOT)
    first = service._paths(tmp_path, "build.one")
    second = service._paths(tmp_path, "build.two")
    assert first["record"].name.endswith(".json")
    assert second["record"].name.endswith(".json")
    assert set(first.values()).isdisjoint(second.values())
    assert set(service._paths(tmp_path, "foo").values()).isdisjoint(
        service._paths(tmp_path, "foo.payload").values())


def test_session_status_bounds_operation_metadata(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    service = ShellCommandService(state_root, backend=VirtualBackend(), source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    handle = str(ensured["handle"])
    directory, session, _ = service._load(handle)
    for index in range(25):
        op_id = f"finished-{index:02d}"
        atomic_json(service._paths(directory, op_id)["record"], {
            "operation_id": op_id, "operation_handle": service._op_handle(session["scope_key"],
                                                                       session["session_id"], op_id),
            "session_handle": handle, "request_digest": "a" * 64,
            "state": "collected", "effect_certainty": "known", "created_at": index,
        })
    status = service.status(handle, owner=owner)
    assert status["operation_count"] == 25
    assert status["operations_truncated"] is True
    assert len(status["operations"]) == 20
    assert all("output" not in operation for operation in status["operations"])


def test_large_native_status_preserves_receipts_with_bounded_history(capsys):
    ids = [f'operation-{i:04d}' for i in range(250)]
    history = {'state': 'unknown', 'lost_rows': None, 'rows_before': 20000,
        'rows_after': 10000, 'reason': 'Exact retention cannot be established.',
        'operation_ids': ids, 'items': {key: {'state': 'unknown', 'rows_before': 100,
            'rows_after': 50, 'reason': 'Exact retention cannot be established.'} for key in ids}}
    layout = {'mode': 'follow', 'desired_generation': 400, 'applied_generation': 399,
        'layout_state': 'unknown', 'pending': True, 'selected_operation_id': ids[-1],
        'operation_order': ids, 'activity_snapshot': dict.fromkeys(ids, 1),
        'weights': dict.fromkeys(ids, 10), 'visible_operation_ids': ids,
        'requested_plan': {'page': 2, 'page_count': 40, 'candidate_operation_ids': ids,
            'assignments': [{'operation_id': key, 'visible': False, 'height': 1} for key in ids],
            'collapsed_sections': [{'operation_ids': ids}]}}
    value = {'api_version': 1, 'ok': True, 'state': 'ready', 'handle': 'exact-session',
        'machine': {'host': 'fixture'}, 'join_command': 'localsetup shell attach --session fixture',
        'native_layout': layout, 'native_history_retention': history,
        'operation_count': len(ids), 'operations_truncated': True,
        'operations': [{'operation_id': key, 'operation_handle': 'exact-'+key,
            'state': 'completed', 'exit_code': 0, 'native_history_retention': history}
            for key in ids[-20:]]}
    before = json.dumps(value, sort_keys=True)
    _print(value)
    encoded = capsys.readouterr().out
    reply = json.loads(encoded)
    assert len(encoded.encode()) <= 65_537 and reply['ok'] and reply['response_truncated']
    assert reply['handle'] == value['handle'] and reply['join_command'] == value['join_command']
    assert len(reply['operations']) == 20 and reply['operation_count'] == 250
    for operation in reply['operations']:
        retained = operation['native_history_retention']
        assert retained['items_count'] == 250 and retained['items_truncated']
        assert list(retained['items']) == [operation['operation_id']]
        assert retained['state'] == 'unknown' and retained['lost_rows'] is None
        assert operation['state'] == 'completed' and operation['exit_code'] == 0
    assert len(reply['native_layout']['operation_order']) == 20
    assert reply['native_layout']['operation_order_count'] == 250
    assert reply['native_layout']['requested_plan']['page_count'] == 40
    assert reply['native_layout']['layout_state'] == 'unknown'
    assert reply['native_layout']['pending'] is True
    assert reply['native_layout']['applied_generation'] == 399
    assert json.dumps(value, sort_keys=True) == before


def test_stream_returns_one_bounded_terminal_receipt(capsys) -> None:
    class CompleteWithBacklog:
        def logs(self, _handle, *, cursor, owner):
            data = "abcdef"[cursor:cursor + 2]
            return {"output": data, "cursor": cursor + len(data), "end_cursor": 6, "gap": False}

        def status(self, _handle, *, owner):
            return {"state": "completed", "exit_code": 0, "output": "ef",
                    "output_cursor": 6, "output_gap": True}

    result = _stream_operation(CompleteWithBacklog(), {"operation_handle": "op"}, timeout=1, owner=None)
    assert capsys.readouterr().out == ""
    assert result["output"] == "ef"
    assert result["stream_cursor"] == 6
    assert result["stream_gap"] is True
    assert result["stream_timed_out"] is False


def test_cli_json_packet_caps_control_characters_and_metadata(capsys) -> None:
    _print({"api_version": 1, "ok": True, "output": "\0" * 32_768,
            "cursor": 32_768, "gap": True})
    raw = capsys.readouterr().out.encode("utf-8")
    assert len(raw) <= 65_536
    receipt = json.loads(raw)
    assert receipt["cursor"] == 32_768 and receipt["gap"] is True
    assert receipt.get("response_truncated") is True


def test_cli_json_packet_uses_one_budget_for_all_text_fields(capsys) -> None:
    _print({"api_version": 1, "output": "α" * 20_000,
            "lines": ["line\t" * 20 for _ in range(250)],
            "tail": "\0" * 20_000, "cursor": 421, "previous_cursor": 17,
            "gap": True})
    raw = capsys.readouterr().out.encode("utf-8")
    packet = json.loads(raw)
    texts = [packet["output"], "\n".join(packet["lines"]), packet["tail"]]
    assert sum(len(item.encode("utf-8")) for item in texts) <= 32_768
    assert sum(len(item.splitlines()) for item in texts) <= 200
    assert len(raw) <= 65_536
    assert packet["cursor"] == 421 and packet["previous_cursor"] == 17
    assert packet["gap"] is True and packet["response_truncated"] is True


def test_stream_emits_join_reminder_after_quiet_monotonic_interval(monkeypatch, capsys) -> None:
    from ls.core.terminal_ops import command_cli

    ticks = iter((0.0, 0.0, 301.0, 301.0, 301.0, 301.0))
    monkeypatch.setattr(command_cli.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(command_cli.time, "sleep", lambda _seconds: None)

    class QuietThenDone:
        calls = 0

        def status(self, _handle, *, owner):
            self.calls += 1
            return {"state": "running" if self.calls == 1 else "completed",
                    "operation_handle": "op", "output": "", "output_cursor": 0,
                    "join_command": "localsetup shell attach --session ls-12345678-12345678",
                    "machine": {"host": "fixture"}}

    done = _stream_operation(QuietThenDone(), {"operation_handle": "op"}, timeout=600, owner=None)
    captured = capsys.readouterr()
    assert done["state"] == "completed" and captured.out == ""
    assert '"event":"join_reminder"' in captured.err


def test_command_wait_exit_codes_preserve_result_and_timeout() -> None:
    assert _exit_code({"state": "completed", "exit_code": 42}, command_wait=True) == 42
    assert _exit_code({"state": "running", "stream_timed_out": True}, command_wait=True) == 124
    assert _exit_code({"state": "running", "wait_timed_out": True}, command_wait=True) == 124
    assert _exit_code({"state": "unknown"}, command_wait=True) == 125
    assert _exit_code({"state": "running"}, command_wait=False) == 0


def test_complete_payload_and_cli_file_bounds_precede_dispatch(tmp_path: Path) -> None:
    input_file = tmp_path / "oversized-input"
    with input_file.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ShellCommandError) as file_error:
        _bounded_file(str(input_file), 8 * 1024 * 1024, "stdin")
    assert file_error.value.code == "invalid_stdin"

    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    with pytest.raises(ShellCommandError) as payload_error:
        service.run(str(ensured["handle"]), "large", script="true",
                    stdin=b"x" * (12 * 1024 * 1024), owner=owner)
    assert payload_error.value.code == "invalid_payload"
    assert backend.created_operations == 0


def test_log_cursor_and_rotated_bytes_are_one_snapshot(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    service = ShellCommandService(state_root, backend=VirtualBackend(), source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    operation = service.run(str(ensured["handle"]), "rotating", script="true", owner=owner)
    handle = str(operation["operation_handle"])
    directory, _, op_id = service._load(handle)
    paths = service._paths(directory, str(op_id))
    paths["log"].write_bytes(b"old content")
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": 11, "available": True})
    locked = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    result = {}

    def rotate() -> None:
        with log_lock(paths["cursor"]):
            locked.set()
            assert release.wait(3)
            replacement = paths["log"].with_name("rotation.tmp")
            replacement.write_bytes(b"new content")
            os.replace(replacement, paths["log"])
            atomic_json(paths["cursor"], {"base_offset": 100, "end_offset": 111,
                                          "available": True})

    def observe() -> None:
        result.update(service.logs(handle, cursor=0))
        completed.set()

    writer = threading.Thread(target=rotate)
    reader = threading.Thread(target=observe)
    writer.start()
    assert locked.wait(3)
    reader.start()
    assert not completed.wait(0.1)
    release.set()
    writer.join(3)
    reader.join(3)
    assert completed.is_set()
    assert result["gap"] is True
    assert result["base_cursor"] == 100
    assert result["output"] == "new content"


def test_cursor_logs_respect_requested_line_limit(tmp_path: Path) -> None:
    backend = VirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    operation = service.run(str(ensured["handle"]), "line-limits", script="true", owner=owner)
    directory, _, op_id = service._load(operation["operation_handle"])
    paths = service._paths(directory, op_id)
    raw = b"".join(f"line-{index}\n".encode() for index in range(300))
    paths["log"].write_bytes(raw)
    paths["log"].chmod(0o600)
    atomic_json(paths["cursor"], {"base_offset": 0, "end_offset": len(raw), "available": True})
    first = service.logs(operation["operation_handle"], cursor=0)
    assert len(first["output"].splitlines()) == 200
    next_part = service.logs(operation["operation_handle"], cursor=first["cursor"], lines=3)
    assert next_part["output"].splitlines() == ["line-200", "line-201", "line-202"]
    assert first["truncated"] is True


def test_gc_preserves_unknown_owner_identity(tmp_path: Path) -> None:
    now = [10_000.0]
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT,
                                  clock=lambda: now[0])
    ensured, _ = _ensure(service, tmp_path / "home", tmp_path)
    directory, session, _ = service._load(str(ensured["handle"]))
    session["last_activity_at"] = now[0] - 100_000
    session["owner"]["machine_generation"] = "different-boot"
    atomic_json(directory / "session.json", session)

    result = service.gc()
    assert result["state"] == "completed"
    assert str(ensured["handle"]) in result["gc"]["preserved"]
    assert backend.closed is False


def test_gc_preserves_silent_live_owner_operation(tmp_path: Path) -> None:
    now = [10_000.0]
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT,
                                  clock=lambda: now[0])
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    service.run(str(ensured["handle"]), "silent", script="sleep 30", owner=owner)
    now[0] += 10_000
    result = service.gc()
    assert ensured["handle"] in result["gc"]["preserved"]
    assert backend.closed is False


def test_gc_finalizes_dead_owner_operation_then_waits_for_actual_quiet(tmp_path: Path) -> None:
    now = [10_000.0]
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT,
                                  clock=lambda: now[0])
    owner_process = subprocess.Popen(["sleep", "30"])
    runner_process = subprocess.Popen(["sleep", "30"])
    try:
        ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
        handle = str(ensured["handle"])
        running = service.run(handle, "finished-after-owner", script="true", owner=owner)
        directory, session, op_id = service._load(str(running["operation_handle"]))
        paths = service._paths(directory, str(op_id))
        operation = read_json(paths["record"])
        runner = process_info(runner_process.pid)
        former_owner = process_info(owner_process.pid)
        assert runner is not None
        assert former_owner is not None
        session["owner"] = {**session["owner"], "pid": owner_process.pid,
                            "starttime": former_owner["starttime"]}
        session["owner_fingerprint"] = owner_fingerprint(session["owner"])
        backend.owner_marker = session["owner_fingerprint"]
        atomic_json(directory / "session.json", session)
        operation.update(state="draining", process_scope="collected",
                         runner_pid=runner_process.pid, runner_starttime=runner["starttime"],
                         command_finished_at=now[0], output_drain="pending",
                         owner_fingerprint=session["owner_fingerprint"])
        atomic_json(paths["record"], operation)
        atomic_json(paths["drained"], {"state": "drained", "storage": "available", "bytes_seen": 0})
        owner_process.terminate()
        owner_process.wait(timeout=3)

        first = service.gc()
        assert handle in first["gc"]["preserved"]
        assert read_json(paths["finalize"])["state"] == "finalize"
        runner_process.terminate()
        runner_process.wait(timeout=3)
        backend.operation["dead"] = True
        backend.operation["pane_pid"] = runner_process.pid
        now[0] += 899
        assert handle in service.gc()["gc"]["preserved"]
        assert read_json(paths["record"])["state"] == "completed"
        assert backend.closed is False
        now[0] += 2
        assert handle in service.gc()["gc"]["removed"]
        assert backend.closed is True
    finally:
        for process in (owner_process, runner_process):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)


def test_dead_owner_gc_receipt_allows_same_agent_successor(monkeypatch, tmp_path: Path) -> None:
    clock = [10_000.0]
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT,
                                  clock=lambda: clock[0])
    owner = _stable_owner(monkeypatch)
    profile = _profile(tmp_path / "home")
    ensured = service.ensure(cwd=tmp_path, settings=profile["settings"],
                             settings_digest=profile["digest"], owner=owner, layout_mode="legacy-v1")
    handle = str(ensured["handle"])
    previous_process = subprocess.Popen(["sleep", "30"])
    try:
        _replace_recorded_owner(service, handle, owner, previous_process)
        backend.owner_marker = service._load(handle)[1]["owner_fingerprint"]
        previous_process.terminate()
        previous_process.wait(timeout=3)
        clock[0] += 901
        assert handle in service.gc()["gc"]["removed"]
        successor = service.ensure(cwd=tmp_path, settings=profile["settings"],
                                   settings_digest=profile["digest"], owner=owner, layout_mode="legacy-v1")
        assert successor["handle"] != handle
        assert successor["previous_cleanup"] == {
            "handle": handle, "state": "removed", "cleanup_state": "removed",
            "at": clock[0], "source": "gc",
        }
        assert service.status(str(successor["handle"]))["previous_cleanup"] == successor["previous_cleanup"]
        with pytest.raises(ShellCommandError) as changed_agent:
            service.recover(handle, owner=_stable_owner(monkeypatch, "different-agent"), adopt=True)
        assert changed_agent.value.code == "recovery_scope_mismatch"
    finally:
        if previous_process.poll() is None:
            previous_process.terminate()
            previous_process.wait(timeout=3)


@pytest.mark.parametrize("occupant", ["malformed_record", "rogue_window", "live_status"])
def test_unknown_private_resources_block_close_and_gc(monkeypatch, tmp_path: Path, occupant: str) -> None:
    clock = [10_000.0]
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT,
                                  clock=lambda: clock[0])
    owner = _stable_owner(monkeypatch)
    profile = _profile(tmp_path / "home")
    ensured = service.ensure(cwd=tmp_path, settings=profile["settings"],
                             settings_digest=profile["digest"], owner=owner, layout_mode="legacy-v1")
    handle = str(ensured["handle"])
    directory, session, _ = service._load(handle)
    if occupant == "malformed_record":
        atomic_json(directory / "operations" / "op-unknown.json", {"operation_id": "missing"})
    elif occupant == "rogue_window":
        backend.operation = {"window_id": "@rogue", "pane_id": "%rogue",
                             "operation_id": "unknown", "request_digest": "0" * 64}
    else:
        backend.status_dead = False
    with pytest.raises(ShellCommandError) as refused:
        service.close(handle, owner=owner)
    assert refused.value.code in {"operation_records_unknown", "native_inventory_unknown"}
    assert backend.closed is False
    previous_process = subprocess.Popen(["sleep", "30"])
    try:
        _replace_recorded_owner(service, handle, owner, previous_process)
        backend.owner_marker = service._load(handle)[1]["owner_fingerprint"]
        previous_process.terminate()
        previous_process.wait(timeout=3)
        clock[0] += 901
        result = service.gc()
        assert handle in result["gc"]["pending"]
        assert backend.closed is False
    finally:
        if previous_process.poll() is None:
            previous_process.terminate()
            previous_process.wait(timeout=3)


def test_reconcile_preserves_completed_window_with_unrecorded_split_pane(tmp_path: Path) -> None:
    clock = [10_000.0]
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT,
                                  clock=lambda: clock[0])
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    handle = str(ensured["handle"])
    op_handle, _ = _complete_virtual_operation(service, backend, handle, owner, "split-protected")
    backend.operation["extra_pane"] = True
    observed = service.status(op_handle)
    assert observed["state"] == "completed"
    assert observed["cleanup_state"] == "unknown"
    assert backend.operation is not None
    with pytest.raises(ShellCommandError) as refused:
        service.close(handle, owner=owner)
    assert refused.value.code == "native_inventory_unknown"
    previous_process = subprocess.Popen(["sleep", "30"])
    try:
        _replace_recorded_owner(service, handle, owner, previous_process)
        backend.owner_marker = service._load(handle)[1]["owner_fingerprint"]
        previous_process.terminate()
        previous_process.wait(timeout=3)
        clock[0] += 901
        assert handle in service.gc()["gc"]["pending"]
        assert backend.operation is not None
    finally:
        if previous_process.poll() is None:
            previous_process.terminate()
            previous_process.wait(timeout=3)
    backend.operation["extra_pane"] = False
    assert service.status(op_handle)["cleanup_state"] == "removed"
    assert backend.operation is None


@pytest.mark.parametrize("observer", ["pin", "client"])
def test_reconcile_waits_for_observer_before_removing_completed_pane(
    tmp_path: Path, observer: str,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    handle = str(ensured["handle"])
    op_handle, _ = _complete_virtual_operation(service, backend, handle, owner, "observed-complete")
    directory, _, _ = service._load(handle)
    pin_path = directory / "pins" / "fixture-observer.json"
    if observer == "pin":
        atomic_json(pin_path, {**make_owner_binding(owner_pid=os.getpid()),
                               "pin_id": "fixture-observer", "kind": "attachment"})
    else:
        backend.client_count = 1
    observed = service.status(op_handle)
    assert observed["state"] == "completed"
    assert observed["cleanup_state"] == "pending"
    assert backend.operation is not None
    if observer == "pin":
        pin_path.unlink()
    else:
        backend.client_count = 0
    assert service.status(op_handle)["cleanup_state"] == "removed"
    assert backend.operation is None


def test_close_acknowledges_exact_missing_endpoint(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    backend.closed = True
    closed = service.close(str(ensured["handle"]), owner=owner)
    assert closed["state"] == "removed"
    assert closed["removed"] is True


def test_normal_close_receipt_allows_dead_owner_successor(monkeypatch, tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    owner = _stable_owner(monkeypatch)
    profile = _profile(tmp_path / "home")
    ensured = service.ensure(cwd=tmp_path, settings=profile["settings"],
                             settings_digest=profile["digest"], owner=owner, layout_mode="legacy-v1")
    handle = str(ensured["handle"])
    assert service.close(handle, owner=owner)["state"] == "removed"
    previous_process = subprocess.Popen(["sleep", "30"])
    try:
        _replace_recorded_owner(service, handle, owner, previous_process)
        previous_process.terminate()
        previous_process.wait(timeout=3)
        successor = service.ensure(cwd=tmp_path, settings=profile["settings"],
                                   settings_digest=profile["digest"], owner=owner, layout_mode="legacy-v1")
        assert successor["handle"] != handle
        assert successor["previous_cleanup"]["source"] == "close"
        assert successor["previous_cleanup"]["handle"] == handle
    finally:
        if previous_process.poll() is None:
            previous_process.terminate()
            previous_process.wait(timeout=3)


def test_legacy_operation_id_returns_original_receipt_without_replay(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = VirtualBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
    original = service.run(str(ensured["handle"]), "old-request", script="true", owner=owner)
    directory, _, _ = service._load(str(original["operation_handle"]))
    paths = service._paths(directory, "old-request")
    old_request = read_json(paths["payload"], limit=16 * 1024 * 1024)
    old_request.pop("inherited_pythonpath")
    old_digest = _digest(old_request)
    atomic_json(paths["payload"], old_request)
    operation = read_json(paths["record"])
    operation["request_digest"] = old_digest
    atomic_json(paths["record"], operation)
    backend.operation["request_digest"] = old_digest
    repeated = service.run(str(ensured["handle"]), "old-request", script="true", owner=owner)
    assert repeated["operation_handle"] == original["operation_handle"]
    assert repeated["request_digest"] == old_digest
    assert backend.created_operations == 1


def test_active_recovery_reconciles_lost_owner_marker_ack(monkeypatch, tmp_path: Path) -> None:
    class LostAckBackend(VirtualBackend):
        lose_once = True

        def set_owner_identity(self, record, *, allowed_previous_owners):
            super().set_owner_identity(record, allowed_previous_owners=allowed_previous_owners)
            if self.lose_once:
                self.lose_once = False
                raise ShellCommandError("native_ack_unknown", "Owner marker acknowledgement was lost.", effect="unknown")

    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = LostAckBackend()
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    owner = _stable_owner(monkeypatch)
    profile = _profile(tmp_path / "home")
    ensured = service.ensure(cwd=tmp_path, settings=profile["settings"],
                             settings_digest=profile["digest"], owner=owner, layout_mode="legacy-v1")
    handle = str(ensured["handle"])
    running = service.run(handle, "adopt-running", script="sleep 30", owner=owner)
    op_handle = str(running["operation_handle"])
    previous_process = subprocess.Popen(["sleep", "30"])
    runner_process = subprocess.Popen(["sleep", "30"])
    try:
        directory, session = _replace_recorded_owner(service, handle, owner, previous_process)
        backend.owner_marker = session["owner_fingerprint"]
        paths = service._paths(directory, "adopt-running")
        operation = read_json(paths["record"])
        runner = process_info(runner_process.pid)
        assert runner is not None
        operation.update(owner_fingerprint=session["owner_fingerprint"], process_scope="tracking",
                         runner_pid=runner_process.pid, runner_starttime=runner["starttime"])
        atomic_json(paths["record"], operation)
        backend.operation["pane_pid"] = runner_process.pid
        previous_process.terminate()
        previous_process.wait(timeout=3)
        backend.client_count = 1
        with pytest.raises(ShellCommandError) as observing:
            service.recover(handle, owner=owner, adopt=True)
        assert observing.value.code == "observer_blocks_recovery"
        backend.client_count = 0
        with pytest.raises(ShellCommandError) as lost:
            service.recover(handle, owner=owner, adopt=True)
        assert lost.value.code == "native_ack_unknown"
        assert read_json(directory / "session.json")["adoption_intent"]["owner_fingerprint"] == owner_fingerprint(owner)
        adopted = service.recover(handle, owner=owner, adopt=True)
        assert adopted["recovered"] is True
        assert service.status(op_handle, owner=owner)["state"] == "running"
        assert read_json(paths["record"])["owner_fingerprint"] == session["owner_fingerprint"]
        if hasattr(os, "pidfd_open") and hasattr(__import__("signal"), "pidfd_send_signal"):
            cancelled = service.cancel(op_handle, owner=owner)
            assert cancelled["cancel_requested"] is True
            runner_process.wait(timeout=3)
    finally:
        for process in (previous_process, runner_process):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)


def test_isolated_tmux_session_runs_captures_and_closes(tmp_path: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    state_root.chmod(0o700)
    # tmux's Unix socket path limit is short; pytest's deeply nested tmp_path
    # is too long. Use a unique private /tmp fixture directory and remove only
    # that directory and its task-owned socket during teardown.
    socket_root = Path(tempfile.mkdtemp(prefix="lss-"))
    socket_root.chmod(0o700)
    backend = TmuxCommandBackend(tmux, config_file="/dev/null", socket_root=socket_root)
    if backend.capability().get("state") != "available":
        socket_root.rmdir()

        pytest.skip("tmux does not satisfy the shell backend capability profile")
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    handle: str | None = None
    try:
        ensured, owner = _ensure(service, tmp_path / "home", tmp_path)
        handle = str(ensured["handle"])
        session = service.status(handle, owner=owner)
        assert session["native_state"] == "ready"
        assert session["native_windows"] == 1
        assert session["status_window_id"]
        _, record, _ = service._load(handle)
        status_window = next(
            window for window in backend.inspect_session(record)["windows"]
            if window["window_id"] == session["status_window_id"]
        )
        assert status_window["panes"][0]["dead"] is True

        result = service.run(handle, "native-output", script="printf 'native-lifecycle-ok\\n'",
                             cwd=tmp_path, owner=owner)
        observed = service.wait(str(result["operation_handle"]), timeout=15, owner=owner)
        assert observed["state"] == "completed"
        assert observed["exit_code"] == 0
        assert "native-lifecycle-ok" in observed["output"]
        assert observed["output_drain"] == "available"
        assert observed["cleanup_state"] == "removed"
        logs = service.logs(str(result["operation_handle"]))
        assert "native-lifecycle-ok" in logs["output"]
        screen = service.screen(handle)
        assert screen["view_kind"] == "screen"
        recent = service.view(handle)
        assert recent["view_kind"] == "recent_output"
        assert "native-lifecycle-ok" in recent["lines"]
        assert backend.inspect_session(record)["applied_settings"]["pane_history_limit"][status_window["panes"][0]["pane_id"]] == record["settings"]["history_limit"]

        reading = service.run(handle, "native-tty-read", script="read -r value; printf 'reply:%s\\n' \"$value\"",
                              cwd=tmp_path, owner=owner)
        reading_handle = str(reading["operation_handle"])
        ready_deadline = time.monotonic() + 5
        while time.monotonic() < ready_deadline:
            _, _, ready_op, _ = service._load_operation(reading_handle)
            if ready_op.get("child_pid"):
                break
            time.sleep(0.05)
        assert ready_op.get("child_pid")
        # Make the session's selected window differ from the operation pane.
        # A handle-only observer must still land on this exact command window.
        backend._checked(record["endpoint"], ["select-window", "-t", record["status_window_id"]], mutation=True)
        observer_master, observer_slave = pty.openpty()
        observer_tty = os.ttyname(observer_slave)
        observer_code = (
            "import os,sys; from ls.core.terminal_ops.command_backend import TmuxCommandBackend; "
            "fd=os.open(sys.argv[1],os.O_RDWR); "
            "os.dup2(fd,0); os.dup2(fd,1); os.dup2(fd,2); os.close(fd); "
            "raise SystemExit(TmuxCommandBackend(sys.argv[4]).attach("
            "{'endpoint':sys.argv[2],'native_session_id':sys.argv[3]},"
            "{'window_id':sys.argv[5],'pane_id':sys.argv[6]}))"
        )
        observer_process = subprocess.Popen(
            [sys.executable, "-c", observer_code, observer_tty, record["endpoint"],
             record["native_session_id"], tmux, reading["window_id"], reading["pane_id"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(observer_slave)
        try:
            attached_deadline = time.monotonic() + 5
            while time.monotonic() < attached_deadline:
                clients = backend._checked(record["endpoint"], ["list-clients", "-F", "#{client_flags}\t#{window_id}"]).splitlines()
                if clients:
                    break
                time.sleep(0.05)
            assert any("read-only" in client and client.split("\t")[-1] == reading["window_id"]
                       for client in clients), (clients, observer_process.poll(), reading["window_id"])
            os.write(observer_master, b"injected-by-observer\n")
            time.sleep(0.1)
            assert service.status(reading_handle)["state"] == "running"
            while True:
                try:
                    sent = service.input(reading_handle, "answer", "typed\n", owner=owner)
                    break
                except ShellCommandError as exc:
                    if exc.code != "busy" or time.monotonic() >= ready_deadline:
                        raise
                    time.sleep(0.05)
            read_result = service.wait(reading_handle, timeout=10, owner=owner)
            assert read_result["state"] == "completed"
            diagnosed = service.doctor(reading_handle, owner=owner, repair=True)
            assert any(item["action"] == "label_exact_completed_pane" and item["result"] == "verified"
                       for item in diagnosed["attempts"])
            assert backend._checked(record["endpoint"], ["show-options", "-p", "-v", "-t",
                                                            reading["pane_id"], "remain-on-exit-format"]).strip() == ""
            assert backend._checked(record["endpoint"], ["show-options", "-p", "-qv", "-t",
                                                            reading["pane_id"], "@localsetup_shell_state"]).strip() == "Completed"
            assert backend._checked(record["endpoint"], ["show-options", "-w", "-qv", "-t",
                                                            reading["window_id"], "pane-border-status"]).strip() == "top"
            title_format = backend._checked(record["endpoint"], ["show-options", "-w", "-qv", "-t",
                                                                   reading["window_id"], "pane-border-format"]).strip()
            rendered_title = backend._checked(record["endpoint"], ["display-message", "-p", "-t",
                                                                   reading["pane_id"], "-F", title_format]).strip()
            assert reading["pane_label"] in rendered_title and "Completed" in rendered_title
        finally:
            observer_process.terminate()
            observer_process.wait(timeout=3)
            os.close(observer_master)
        assert sent["state"] == "sent"
        assert read_result["state"] == "completed"
        assert read_result["exit_code"] == 0
        assert "reply:typed" in read_result["output"]

        cancelling = service.run(handle, "native-cancel-descendant",
                                 script="setsid /bin/sleep 30 & wait", cwd=tmp_path, owner=owner)
        cancel_handle = str(cancelling["operation_handle"])
        ready_deadline = time.monotonic() + 5
        while time.monotonic() < ready_deadline:
            _, _, cancel_op, _ = service._load_operation(cancel_handle)
            if cancel_op.get("child_pid"):
                break
            time.sleep(0.05)
        assert cancel_op.get("child_pid")
        while True:
            try:
                cancel_result = service.cancel(cancel_handle, owner=owner)
                break
            except ShellCommandError as exc:
                if exc.code != "busy" or time.monotonic() >= ready_deadline:
                    raise
                time.sleep(0.05)
        assert cancel_result["cancel_requested"] is True
        cancelled = service.wait(cancel_handle, timeout=10, owner=owner)
        assert cancelled["state"] == "completed"
        assert cancelled["process_scope"] == "collected"

        draining = service.run(handle, "native-cancel-draining",
                               script="setsid /bin/sleep 30 & printf 'spawned\\n'",
                               cwd=tmp_path, owner=owner)
        draining_handle = str(draining["operation_handle"])
        drain_deadline = time.monotonic() + 5
        while time.monotonic() < drain_deadline:
            _, _, drain_op, _ = service._load_operation(draining_handle)
            if drain_op.get("state") == "draining":
                break
            time.sleep(0.05)
        assert drain_op["state"] == "draining"
        while True:
            try:
                drain_cancel = service.cancel(draining_handle, owner=owner)
                break
            except ShellCommandError as exc:
                if exc.code != "busy" or time.monotonic() >= drain_deadline:
                    raise
                time.sleep(0.05)
        assert drain_cancel["cancel_requested"] is True
        drained_cancel = service.wait(draining_handle, timeout=10, owner=owner)
        assert drained_cancel["state"] == "completed"
        assert drained_cancel["process_scope"] == "collected"

        escaped = service.run(
            handle, "native-descendant", script="setsid /bin/sleep 3 & printf 'foreground-exited\\n'",
            cwd=tmp_path, owner=owner,
        )
        early = None
        deadline = time.monotonic() + 1.5
        while early is None and time.monotonic() < deadline:
            try:
                early = service.status(str(escaped["operation_handle"]), owner=owner)
            except ShellCommandError as exc:
                if exc.code != "busy":
                    raise
                time.sleep(0.05)
        assert early is not None
        assert early["state"] in {"running", "draining"}
        assert early["process_scope"] != "collected"
        descendant_result = service.wait(str(escaped["operation_handle"]), timeout=10, owner=owner)
        assert descendant_result["state"] == "completed"
        assert descendant_result["process_scope"] == "collected"

        split_operation = service.run(handle, "native-split-protected",
                                      script="read -r value; printf 'split:%s\\n' \"$value\"",
                                      cwd=tmp_path, owner=owner)
        split_handle = str(split_operation["operation_handle"])
        split_deadline = time.monotonic() + 5
        while time.monotonic() < split_deadline:
            _, _, split_record, _ = service._load_operation(split_handle)
            if split_record.get("child_pid"):
                break
            time.sleep(0.05)
        assert split_record.get("child_pid")
        extra_pane = backend._checked(
            record["endpoint"],
            ["split-window", "-d", "-P", "-F", "#{pane_id}",
             "-t", split_operation["pane_id"], "/bin/sleep 30"], mutation=True).strip()
        try:
            service.input(split_handle, "native-split-input", "typed\n", owner=owner)
            split_result = service.wait(split_handle, timeout=10, owner=owner)
            assert split_result["state"] == "completed"
            assert split_result["process_scope"] == "collected"
            assert split_result["cleanup_state"] in {"pending", "unknown"}
            split_native = backend.inspect_session(record)
            split_windows = [window for window in split_native["windows"]
                             if window["window_id"] == split_operation["window_id"]]
            assert len(split_windows) == 1 and split_windows[0]["pane_count"] == 2
            with pytest.raises(ShellCommandError) as split_close:
                service.close(handle, owner=owner)
            assert split_close.value.code == "native_inventory_unknown"
        finally:
            backend._checked(record["endpoint"], ["kill-pane", "-t", extra_pane], mutation=True)
        assert service.status(split_handle)["cleanup_state"] == "removed"

        rogue_window = backend._checked(
            record["endpoint"],
            ["new-window", "-d", "-P", "-F", "#{window_id}", "-t", record["native_session_id"],
             "-n", "unrecorded-fixture", "/bin/sleep 30"], mutation=True).strip()
        try:
            with pytest.raises(ShellCommandError) as refused_close:
                service.close(handle, owner=owner)
            assert refused_close.value.code == "native_inventory_unknown"
        finally:
            backend._checked(record["endpoint"], ["kill-window", "-t", rogue_window], mutation=True)
        closed = service.close(handle, owner=owner)
        assert closed["state"] == "removed"
        assert closed["cleanup_state"] == "removed"
    finally:
        if handle is not None:
            try:
                _, record, _ = service._load(handle)
            except ShellCommandError:
                record = None
            if record is not None and record.get("state") not in {"removed", "closed"}:
                try:
                    backend.close_session(
                        record, expected_inventory=TmuxCommandBackend.inventory_identity(
                            backend.inspect_session(record)))
                except ShellCommandError:
                    pass
        # A failed create may have acknowledged the socket but not returned a
        # session record. This cleanup still targets only the private fixture
        # socket created above, never the user's tmux server.
        for endpoint in socket_root.glob("s-*.sock"):
            subprocess.run([tmux, "-f", "/dev/null", "-S", str(endpoint), "kill-server"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3, check=False)
            endpoint.unlink(missing_ok=True)
        socket_root.rmdir()
def test_doctor_quits_only_exact_controlled_less_once(tmp_path: Path) -> None:
    tmux, less = shutil.which("tmux"), shutil.which("less")
    if tmux is None or less is None:
        pytest.skip("tmux and less are required")
    socket_root = Path(tempfile.mkdtemp(prefix="ls-pager-"))
    socket_root.chmod(0o700)
    backend = TmuxCommandBackend(tmux, config_file="/dev/null", socket_root=socket_root)
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    handle = None
    try:
        session, owner = _ensure(service, tmp_path / "home", tmp_path)
        handle = str(session["handle"])
        document = tmp_path / "pager.txt"
        document.write_text("\n".join(f"line {n}" for n in range(1000)))
        started = service.run(handle, "controlled-pager", argv=[str(document)],
                              controlled_app="less", owner=owner)
        op_handle = str(started["operation_handle"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, _, operation, _ = service._load_operation(op_handle)
            if operation.get("child_pid") and service._qualified_less(operation) is None:
                break
            time.sleep(.05)
        assert service._qualified_less(operation) is None
        lease = service.input_lease(op_handle, grantee="human", ttl=30, owner=owner)
        assert shlex.split(lease["input_command"])[0:4] == [
            "localsetup", "shell", "--state-root", str(service.root.parent),
        ]
        blocked = service.doctor(op_handle, owner=owner, repair=True)
        assert any(item.get("code") == "human_input_lease_active" for item in blocked["attempts"])
        assert service.status(op_handle, owner=owner)["state"] == "running"
        assert lease["state"] == "requested"
        service.input_lease(op_handle, grantee="human", revoke=True, owner=owner)
        first = service.doctor(op_handle, owner=owner, repair=True)
        assert any(item.get("action") == "controlled_less_quit" and item.get("result") == "sent_once"
                   for item in first["attempts"]), first["attempts"]
        assert service.wait(op_handle, timeout=5, owner=owner)["state"] == "completed"
        second = service.doctor(op_handle, owner=owner, repair=True)
        assert all(item.get("result") != "sent_once" for item in second["attempts"])
        directory, _, _, _ = service._load_operation(op_handle)
        assert len(list(service._paths(directory, "controlled-pager")["inputs"].glob("*.json"))) == 1
        closed = service.close(handle, owner=owner)
        assert closed["state"] == "removed"
    finally:
        if handle is not None:
            try:
                _, record, _ = service._load(handle)
                if record.get("state") not in {"removed", "closed"}:
                    backend.close_session(record, expected_inventory=TmuxCommandBackend.inventory_identity(
                        backend.inspect_session(record)))
            except ShellCommandError:
                pass
        for endpoint in socket_root.glob("s-*.sock"):
            subprocess.run([tmux, "-f", "/dev/null", "-S", str(endpoint), "kill-server"],
                           capture_output=True, timeout=3)
            endpoint.unlink(missing_ok=True)
        socket_root.rmdir()
