"""Focused root/member and stacked-pane checks for family shell sessions."""

from __future__ import annotations

import os
import fcntl
import json
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService, _family_capacity
from ls.core.terminal_ops import command_state
from ls.core.terminal_ops.command_state import (
    ShellCommandError, atomic_json, make_owner_binding, owner_fingerprint,
    process_info, read_json,
)


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def test_native_utf8_history_bound_after_resize_clear_and_alternate(tmp_path: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    socket_root = Path(tempfile.mkdtemp(prefix="ls-hb-"))
    socket_root.chmod(0o700)
    socket = socket_root / ("s-" + uuid.uuid4().hex[:8])
    ready, proceed, alternate = (tmp_path / name for name in ("ready", "proceed", "alternate"))
    source = (
        "import os,pathlib,time; "
        "line=('a'+'\\u0301'*10)*80+'\\n'; "
        "os.write(1,(line*11000).encode()); "
        f"pathlib.Path({str(ready)!r}).touch(); "
        f"go=pathlib.Path({str(proceed)!r}); "
        "deadline=time.monotonic()+30; "
        "exec('while not go.exists() and time.monotonic()<deadline: time.sleep(.02)'); "
        "os.write(1,b'\\x1b[2J\\x1b[?1049h'+(('a'+'\\u0301'*10)*80).encode()); "
        f"pathlib.Path({str(alternate)!r}).touch(); time.sleep(10)"
    )
    def native(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([tmux, "-f", "/dev/null", "-S", str(socket), *args],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result
    try:
        native("start-server", ";", "set-option", "-g", "history-limit", "9000", ";",
               "set-window-option", "-g", "remain-on-exit", "on", ";",
               "new-session", "-d", "-x", "80", "-y", "512", "-s", "bound",
               shlex.join([sys.executable, "-c", source]))
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists()
        pane = native("display-message", "-p", "-F", "#{pane_id}").stdout.strip()
        native("resize-window", "-t", "bound", "-x", "80", "-y", "1")
        proceed.touch()
        while not alternate.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert alternate.exists()
        native("resize-window", "-t", "bound", "-x", "80", "-y", "512")
        fields = native("display-message", "-p", "-t", pane,
                        "-F", "#{pane_width}|#{pane_height}|#{history_size}|#{history_limit}").stdout.strip().split("|")
        assert fields[0] == "80" and 1 <= int(fields[1]) <= 512
        assert int(fields[2]) <= 9000 + 511 and fields[3] == "9000"
        capture = native("capture-pane", "-p", "-S", "-", "-t", pane).stdout
        assert len(capture.encode("utf-8")) < 20_000_000
        print(f"native H9000 width={fields[0]} height={fields[1]} history={fields[2]} capture_bytes={len(capture.encode('utf-8'))}")
        # tmux 3.4 UTF8_SIZE=21; resize can add at most 511 history rows,
        # and alternate screen duplicates at most 512 visible rows.
        assert (9000 + 511 + 512 + 512) * (81 * 21 + 1) < 20_000_000
    finally:
        if socket.exists():
            subprocess.run([tmux, "-f", "/dev/null", "-S", str(socket), "kill-server"],
                           capture_output=True, timeout=5)
            for _ in range(40):
                check = subprocess.run([tmux, "-f", "/dev/null", "-S", str(socket), "has-session"],
                                       capture_output=True, timeout=2)
                if check.returncode != 0:
                    break
                time.sleep(.05)
            assert check.returncode != 0
            socket.unlink(missing_ok=True)
        socket_root.rmdir()


def test_native_small_history_controlled_resize_churn(tmp_path: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    socket_root = Path(tempfile.mkdtemp(prefix="ls-small-h-"))
    socket_root.chmod(0o700)
    socket = socket_root / "socket"
    source = (
        "import os,pathlib,time; root=pathlib.Path(" + repr(str(tmp_path)) + "); "
        "line=('x'+'\\u0301'*10)*80+'\\n'; os.write(1,(line*150).encode()); "
        "exec('for index in range(12):\\n"
        " marker=root/str(index); deadline=time.monotonic()+10\\n"
        " while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)\\n"
        " os.write(1,b\\\"\\x1b[2J\\x1b[?1049h\\\"+line.encode()+b\\\"\\x1b[?1049l\\\")\\n"
        " (root/(str(index)+\\\".done\\\")).touch()'); time.sleep(1)"
    )
    base = [tmux, "-f", "/dev/null", "-S", str(socket)]
    def native(*args: str) -> str:
        result = subprocess.run([*base, *args], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result.stdout
    try:
        native("start-server", ";", "set-option", "-g", "history-limit", "100", ";",
               "new-session", "-d", "-x", "80", "-y", "10", "-s", "small",
               shlex.join([sys.executable, "-c", source]))
        for index in range(12):
            native("resize-window", "-t", "small", "-x", "80", "-y", "1")
            (tmp_path / str(index)).touch()
            deadline = time.monotonic() + 5
            while not (tmp_path / f"{index}.done").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert (tmp_path / f"{index}.done").exists()
            native("resize-window", "-t", "small", "-x", "80", "-y", "10")
            fields = native("display-message", "-p", "-F", "#{window_width}|#{window_height}|#{history_size}").strip().split("|")
            assert fields[:2] == ["80", "10"] and int(fields[2]) <= 109
        capture_bytes = len(native("capture-pane", "-p", "-S", "-").encode("utf-8"))
        assert capture_bytes < 20_000_000
        print(f"native H100 width={fields[0]} height={fields[1]} history={fields[2]} capture_bytes={capture_bytes}")
    finally:
        if socket.exists():
            subprocess.run([*base, "kill-server"], capture_output=True, timeout=5)
            socket.unlink(missing_ok=True)
        socket_root.rmdir()


class FamilyVirtualBackend:
    """Deterministic family topology adapter for authorization tests."""

    def __init__(self) -> None:
        self.closed = False
        self.created_operations = 0
        self.client_count = 0
        self.height = 24
        self.panes: list[dict[str, object]] = []
        self.owner_marker: str | None = None
        self.arrangements = 0
        self.unknown_pane = False
        self.layout = "family-v1"

    def capability(self):
        return {"state": "available", "backend": "family-virtual", "version": "tmux 3.4"}

    def socket_path(self, session_id: str) -> Path:
        return Path("/tmp/localsetup-family-fake") / f"{session_id}.sock"

    def create_session(self, record, *, cwd, settings):
        self.closed = False
        self.owner_marker = record["owner_fingerprint"]
        self.layout = record.get("layout_mode", "legacy-v1")
        self.height = record["primary_viewport_height"]
        return {
            "native_session_id": "$family",
            "server_session_name": record["native_name"],
            "status_window_id": "@status",
            "status_pane_id": "%status",
            "status_pane_pid": None,
            "session_created": "virtual",
        }

    def inspect_session(self, record):
        if self.closed:
            return {"state": "missing", "windows": []}
        panes = [{
            "pane_id": "%status", "pane_pid": None, "dead": True,
            "width": 80, "height": self.height, "input_off": True, "history_size": 0,
            "metadata": {"session": None, "operation": None, "request": None, "label": None},
        }]
        panes.extend({
            "pane_id": p["pane_id"], "pane_pid": p["pane_pid"],
            "dead": p["dead"], "current_command": "python",
            "width": 80, "height": self.height, "input_off": True, "history_size": 0,
            "metadata": dict(p.get("metadata", {})),
        } for p in self.panes)
        if self.unknown_pane:
            panes.append({
                "pane_id": "%rogue", "pane_pid": os.getpid(), "dead": False,
                "width": 80, "height": self.height, "input_off": True, "history_size": 0,
                "metadata": {"session": None, "operation": None, "request": None, "label": None},
            })
        window = {
            "window_id": "@status", "pane_count": len(panes), "width": 80,
            "height": self.height,
            "metadata": {"status": "1", "session": record["handle"]},
            "panes": panes,
        }
        options = {
            "api": "1", "handle": record["handle"], "key": record["session_key"],
            "machine": record["machine_generation"], "owner": self.owner_marker,
            "profile": record["settings_digest"], "status_window": "@status",
            "layout": self.layout, "window_size": "manual",
        }
        return {
            "state": "ready", "options": options, "session": {"attached": self.client_count},
            "windows": [window],
            "applied_settings": {
                "mouse": record["settings"]["mouse"], "status": record["settings"]["status"],
                "status_position": record["settings"]["status_position"],
                "history_limit": {"@status": record["settings"]["history_limit"]},
                "pane_history_limit": {p["pane_id"]: record["settings"]["history_limit"] for p in self.panes},
            },
        }

    def create_operation_window(self, record, operation, *, command):
        self.created_operations += 1
        pane_id = f"%op{self.created_operations}"
        self.panes.append({
            "pane_id": pane_id, "pane_pid": os.getpid(), "dead": False,
            "metadata": {},
        })
        return {"window_id": "@status", "pane_id": pane_id}

    def tag_operation_window(self, record, operation, native):
        pane = next(p for p in self.panes if p["pane_id"] == native["pane_id"])
        pane["metadata"] = {
            "session": record["handle"], "operation": operation["operation_id"],
            "request": operation["request_digest"], "label": operation["pane_label"],
        }

    def arrange_family_window(self, record, *, expected_window):
        observed = self.inspect_session(record)
        window = observed["windows"][0]
        assert TmuxCommandBackend.window_identity(window) == expected_window
        self.arrangements += 1

    def resize_family_window(self, record, *, expected_window, height):
        observed = self.inspect_session(record)
        assert TmuxCommandBackend.window_identity(observed["windows"][0]) == expected_window
        self.height = height
        return self.inspect_session(record)["windows"][0]

    def start_capture(self, record, operation, *, command):
        return None

    def select_operation(self, record, operation):
        return None

    def capture_screen(self, record, operation, *, lines):
        if operation is None:
            return "root overview\n"
        return f"pane:{operation['operation_id']}\n"

    def kill_window(self, record, operation, *, expected_window):
        observed = self.inspect_session(record)
        window = observed["windows"][0]
        assert TmuxCommandBackend.window_identity(window) == expected_window
        pane = next(p for p in self.panes if p["pane_id"] == operation["pane_id"])
        assert pane["metadata"]["operation"] == operation["operation_id"]
        self.panes.remove(pane)

    def close_session(self, record, *, expected_inventory):
        assert TmuxCommandBackend.inventory_identity(self.inspect_session(record)) == expected_inventory
        self.closed = True

    def set_owner_identity(self, record, *, allowed_previous_owners):
        assert self.owner_marker in allowed_previous_owners
        self.owner_marker = record["owner_fingerprint"]

    def apply_session_settings(self, record, settings, profile_digest):
        return {"state": "applied", "settings": dict(settings)}

    def remove_stale_endpoint(self, record):
        return True

    def attach(self, record, operation=None):  # pragma: no cover - family member attach is refused
        raise AssertionError("the virtual family backend must not attach members to the shared window")


def _owner(monkeypatch, agent_id: str) -> dict[str, object]:
    monkeypatch.setattr(command_state, "discover_agent_id", lambda _pid: None)
    return make_owner_binding(owner_pid=os.getpid(), agent_id=agent_id)


def _profile(home: Path) -> dict[str, object]:
    return resolve_profile("tmux", "agent-shell", source_root=SOURCE_ROOT, home=home)


def _start_unrelated_tty(code: str, slave: int, pid_path: Path) -> int:
    """Run the advertised CLI from an orphan process, outside root ancestry."""
    wrapper = (
        "import os,time\nfrom pathlib import Path\n"
        "child=os.fork()\n"
        f"if child:\n    Path({str(pid_path)!r}).write_text(str(child))\n    os._exit(0)\n"
        "os.setsid()\ntime.sleep(.2)\n" + code
    )
    launcher = subprocess.Popen([sys.executable, "-c", wrapper],
                                stdin=slave, stdout=slave, stderr=slave,
                                env={key: value for key, value in {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}.items()
                                     if not key.startswith("LOCALSETUP_SHELL_OWNER_")},
                                start_new_session=True)
    launcher.wait(timeout=3)
    assert launcher.returncode == 0
    return int(pid_path.read_text())


def _drain_tty(master: int, stop: threading.Event) -> None:
    os.set_blocking(master, False)
    while not stop.is_set():
        if select.select([master], [], [], .1)[0]:
            try:
                os.read(master, 65536)
            except OSError:
                break


def _ensure_family(service, owner, home: Path, cwd: Path):
    profile = _profile(home)
    result = service.ensure(cwd=cwd, settings=profile["settings"],
                           settings_digest=profile["digest"], owner=owner,
                           profile_name=profile["name"], profile_base=profile["base_profile"])
    return result


def _mark_tracking(service, backend: FamilyVirtualBackend, result, owner) -> tuple[str, dict[str, object]]:
    operation_id = str(result["operation_id"])
    handle = str(result["session_handle"])
    operation = service.run(handle, operation_id, script="sleep 30", owner=owner)
    directory, _, _ = service._load(str(operation["operation_handle"]))
    paths = service._paths(directory, operation_id)
    record = read_json(paths["record"])
    info = process_info(os.getpid())
    assert info is not None
    record.update(state="running", process_scope="tracking", runner_pid=os.getpid(),
                  runner_starttime=info["starttime"])
    atomic_json(paths["record"], record)
    pane = next(p for p in backend.panes if p["pane_id"] == record["pane_id"])
    pane["pane_pid"] = os.getpid()
    return str(operation["operation_handle"]), record


def test_family_default_admission_and_member_scope(monkeypatch, tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = _owner(monkeypatch, "root-native")
    created = _ensure_family(service, root, tmp_path / "home", tmp_path)
    handle = str(created["handle"])
    assert created["layout_mode"] == "family-v1"

    invitation = service.admit_member(handle, agent_id="scout-native", label_slug="scout", owner=root)
    member = _owner(monkeypatch, "scout-native")
    joined = service.join_family(handle, invitation=invitation["invitation"], owner=member)
    assert joined["role"] == "member"
    ensured_member = _ensure_family(service, member, tmp_path / "home", tmp_path)
    assert ensured_member["handle"] == handle
    assert ensured_member["joined"] is True and ensured_member["role"] == "member"
    assert backend.closed is False

    root_handle, root_op = _mark_tracking(
        service, backend,
        {"session_handle": handle, "operation_id": "root-check"}, root)
    member_handle, member_op = _mark_tracking(
        service, backend,
        {"session_handle": handle, "operation_id": "scout-check"}, member)
    assert root_op["pane_label"] == "root · root-check"
    assert member_op["pane_label"] == "subagent scout · scout-check"
    assert root_op["window_id"] == member_op["window_id"] == created["status_window_id"]
    assert root_op["pane_id"] != member_op["pane_id"]
    assert root_op["request_digest"] and member_op["request_digest"]
    assert backend.arrangements == 2

    root_status = service.status(handle, owner=root)
    member_status = service.status(handle, owner=member)
    assert {op["operation_id"] for op in root_status["operations"]} == {"root-check", "scout-check"}
    assert [op["operation_id"] for op in member_status["operations"]] == ["scout-check"]
    assert service.screen(member_handle, owner=member)["lines"] == ["pane:scout-check"]
    assert service.logs(member_handle, owner=member)["operation_id"] == "scout-check"

    with pytest.raises(ShellCommandError) as sibling_status:
        service.status(root_handle, owner=member)
    assert sibling_status.value.code == "operation_member_mismatch"
    with pytest.raises(ShellCommandError) as sibling_logs:
        service.logs(root_handle, owner=member)
    assert sibling_logs.value.code == "operation_member_mismatch"
    with pytest.raises(ShellCommandError) as member_close:
        service.close(handle, owner=member)
    assert member_close.value.code == "owner_identity_mismatch"
    with pytest.raises(ShellCommandError) as member_admit:
        service.admit_member(handle, agent_id="other", label_slug="other", owner=member)
    assert member_admit.value.code == "owner_identity_mismatch"
    with pytest.raises(ShellCommandError) as member_view:
        service.screen(handle, owner=member)
    assert member_view.value.code == "operation_handle_required"
    with pytest.raises(ShellCommandError) as member_attach:
        service.attach(member_handle, owner=member)
    assert member_attach.value.code == "family_member_attach_unavailable"

    before = service.status(handle, owner=root)["operation_count"]
    service.gc(owner=member)
    after = service.status(handle, owner=root)["operation_count"]
    assert before == after == 2
    assert backend.closed is False


def test_family_completed_pane_is_removed_while_viewer_remains(tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = make_owner_binding(owner_pid=os.getpid(), agent_id="root-native")
    created = _ensure_family(service, root, tmp_path / "home", tmp_path)
    operation = service.run(str(created["handle"]), "finished", script="true", owner=root)
    directory, _, _ = service._load(str(operation["operation_handle"]))
    paths = service._paths(directory, "finished")
    record = read_json(paths["record"])
    info = process_info(os.getpid())
    assert info is not None
    record.update(state="completed", process_scope="collected", output_drain="available",
                  runner_pid=os.getpid(), runner_starttime=info["starttime"], exit_code=0)
    atomic_json(paths["record"], record)
    atomic_json(paths["drained"], {"state": "drained", "storage": "available", "bytes_seen": 0})
    pane = next(p for p in backend.panes if p["pane_id"] == record["pane_id"])
    pane.update(pane_pid=os.getpid(), dead=True)
    backend.client_count = 1

    observed = service.status(str(operation["operation_handle"]), owner=root)
    assert observed["state"] == "completed"
    assert observed["cleanup_state"] == "removed"
    assert backend.closed is False
    assert backend.panes == []
    assert service.status(str(created["handle"]), owner=root)["native_windows"] == 1


def test_doctor_repairs_only_completed_owned_pane(tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = make_owner_binding(owner_pid=os.getpid(), agent_id="root-doctor")
    created = _ensure_family(service, root, tmp_path / "home", tmp_path)
    active_handle, _ = _mark_tracking(service, backend,
                                      {"session_handle": created["handle"], "operation_id": "working"}, root)
    protected = service.doctor(active_handle, owner=root, repair=True)
    assert protected["classification"] == "working"
    assert protected["attempts"] == [{"action": "application_recovery", "result": "preserved",
                                      "code": "readiness_or_clean_buffer_unproven"}]
    assert protected["handoff_required"] is True and len(backend.panes) == 1

    directory, _, current, _ = service._load_operation(active_handle)
    paths = service._paths(directory, "working")
    current.update(state="completed", process_scope="collected", output_drain="available", exit_code=0)
    atomic_json(paths["record"], current)
    atomic_json(paths["drained"], {"state": "drained", "storage": "available", "bytes_seen": 0})
    backend.panes[0]["dead"] = True
    repaired = service.doctor(active_handle, owner=root, repair=True)
    assert repaired["attempts"] == [{"action": "reconcile_exact_pane", "operation_id": "working",
                                     "result": "removed"}]
    assert backend.panes == []
    assert repaired["automatic_replay"] is False


def test_family_small_requested_history_keeps_safe_effective_bound(tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = make_owner_binding(owner_pid=os.getpid(), agent_id="root-small-history")
    profile = _profile(tmp_path / "home")
    requested = dict(profile["settings"], history_limit=100)
    from ls.core.terminal_ops.command_service import _profile_digest
    created = service.ensure(cwd=tmp_path, settings=requested,
                             settings_digest=_profile_digest(requested), owner=root,
                             profile_name="agent-shell", profile_base=profile["base_profile"])
    assert created["requested_settings"]["history_limit"] == 100
    assert created["effective_settings"]["history_limit"] == 100
    assert service.viewport(str(created["handle"]), height=10, owner=root)["height"] == 10
    with pytest.raises(ShellCommandError) as unsafe_height:
        service.viewport(str(created["handle"]), height=11, owner=root)
    assert unsafe_height.value.code == "invalid_viewport"
    assert service.viewport(str(created["handle"]), height=1, owner=root)["height"] == 1


def test_family_capacity_is_derived_from_viewport_and_unknown_panes_block(tmp_path: Path) -> None:
    assert _family_capacity(24) == 5
    assert _family_capacity(7) == 1
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = make_owner_binding(owner_pid=os.getpid(), agent_id="root-native")
    created = _ensure_family(service, root, tmp_path / "home", tmp_path)
    handle = str(created["handle"])
    directory, record, _ = service._load(handle)
    record["primary_viewport_height"] = 11
    atomic_json(directory / "session.json", record)
    backend.height = 11

    first = {"session_handle": handle, "operation_id": "one"}
    _, first_op = _mark_tracking(service, backend, first, root)
    second = {"session_handle": handle, "operation_id": "two"}
    _, _second_op = _mark_tracking(service, backend, second, root)
    with pytest.raises(ShellCommandError) as full:
        service.run(handle, "three", script="true", owner=root)
    assert full.value.code == "family_capacity"
    assert backend.created_operations == 2
    with pytest.raises(ShellCommandError) as too_small:
        service.viewport(handle, height=10, owner=root)
    assert too_small.value.code == "family_capacity"
    resized = service.viewport(handle, height=12, owner=root)
    assert resized["height"] == 12
    assert backend.height == 12

    other_backend = FamilyVirtualBackend()
    other_service = ShellCommandService(tmp_path / "other-state", backend=other_backend, source_root=SOURCE_ROOT)
    other = _ensure_family(other_service, root, tmp_path / "other-home", tmp_path)
    other_backend.unknown_pane = True
    with pytest.raises(ShellCommandError) as unknown:
        other_service.run(str(other["handle"]), "blocked", script="true", owner=root)
    assert unknown.value.code == "native_inventory_unknown"
    assert other_backend.created_operations == 0
    assert first_op["request_digest"]


def test_family_version_contract_rejects_unproved_tmux(monkeypatch, tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    backend.capability = lambda: {"state": "available", "backend": "family-virtual", "version": "tmux 3.5"}
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = _owner(monkeypatch, "version-root")
    with pytest.raises(ShellCommandError) as unqualified:
        _ensure_family(service, root, tmp_path / "home", tmp_path)
    assert unqualified.value.code == "family_tmux_unqualified"
    assert backend.owner_marker is None
    profile = _profile(tmp_path / "home")
    legacy = service.ensure(cwd=tmp_path, settings=profile["settings"],
                            settings_digest=profile["digest"], owner=root,
                            profile_name=profile["name"], profile_base=profile["base_profile"],
                            layout_mode="legacy-v1")
    assert legacy["layout_mode"] == "legacy-v1"


def test_dead_family_root_closes_member_mutations_but_preserves_observation(monkeypatch, tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = _owner(monkeypatch, "root-death")
    created = _ensure_family(service, root, tmp_path / "home", tmp_path)
    handle = str(created["handle"])
    invite = service.admit_member(handle, agent_id="live-member", label_slug="member", owner=root)
    member = _owner(monkeypatch, "live-member")
    service.join_family(handle, invitation=invite["invitation"], owner=member)
    member_handle, _ = _mark_tracking(service, backend,
                                      {"session_handle": handle, "operation_id": "running"}, member)
    directory, record, _ = service._load(handle)
    record["owner"] = dict(record["owner"], pid=99999999)
    atomic_json(directory / "session.json", record)
    assert service.status(member_handle, owner=member)["operation_id"] == "running"
    for action in (
        lambda: service.run(handle, "after-root-death", script="true", owner=member),
        lambda: service.input(member_handle, "after-root-death", "x", owner=member),
        lambda: service.input_lease(member_handle, grantee="human", owner=member),
        lambda: service.cancel(member_handle, owner=member),
        lambda: _ensure_family(service, member, tmp_path / "home", tmp_path),
    ):
        with pytest.raises(ShellCommandError) as closed:
            action()
        assert closed.value.code in {"family_root_not_live", "family_reconcile_required"}
    assert backend.created_operations == 1


def test_dead_family_root_is_collectible_by_same_user_collector(monkeypatch, tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = _owner(monkeypatch, "gc-root")
    created = _ensure_family(service, root, tmp_path / "home", tmp_path)
    handle = str(created["handle"])
    directory, record, _ = service._load(handle)
    record["owner"] = dict(record["owner"], pid=99999999)
    record["settings"]["idle_grace_seconds"] = 0
    record["last_activity_at"] = time.time() - 1000
    atomic_json(directory / "session.json", record)
    result = service.gc()
    assert handle in result["gc"]["removed"]
    assert backend.closed is True


def test_native_family_capacity_includes_dividers_and_anchor(tmp_path: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    socket_root = Path(tempfile.mkdtemp(prefix="ls-cap-"))
    socket_root.chmod(0o700)
    backend = TmuxCommandBackend(tmux, config_file="/dev/null", socket_root=socket_root)
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    owner = make_owner_binding(owner_pid=os.getpid(), agent_id="capacity-native")
    handle = None
    try:
        created = _ensure_family(service, owner, tmp_path / "home", tmp_path)
        handle = str(created["handle"])
        for index in range(5):
            for attempt in range(40):
                try:
                    started = service.run(handle, f"capacity-{index}", script="sleep 30",
                                          cwd=tmp_path, owner=owner)
                    break
                except ShellCommandError as exc:
                    if exc.code != "busy" or attempt == 39:
                        raise
                    time.sleep(.025)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, _, item, _ = service._load_operation(str(started["operation_handle"]))
                if item.get("process_scope") == "tracking":
                    break
                time.sleep(.05)
            assert item.get("process_scope") == "tracking"
        native = backend.inspect_session(service._load(handle)[1])
        panes = native["windows"][0]["panes"]
        assert len(panes) == 6 and all(pane["height"] >= 3 for pane in panes)
        for attempt in range(40):
            try:
                service.run(handle, "capacity-rejected", script="sleep 30", cwd=tmp_path, owner=owner)
            except ShellCommandError as exc:
                if exc.code == "busy" and attempt < 39:
                    time.sleep(.025)
                    continue
                full = exc
                break
            pytest.fail("a sixth operation was admitted into the 24-row family")
        assert full.code == "family_capacity"
        assert not service._paths(service._load(handle)[0], "capacity-rejected")["record"].exists()
    finally:
        if handle is not None:
            try:
                record = service._load(handle)[1]
                native = backend.inspect_session(record)
                if native.get("state") == "ready":
                    backend.close_session(record, expected_inventory=TmuxCommandBackend.inventory_identity(native))
            except ShellCommandError:
                pass
        for endpoint in socket_root.glob("s-*.sock"):
            subprocess.run([tmux, "-f", "/dev/null", "-S", str(endpoint), "kill-server"],
                           capture_output=True, timeout=3, check=False)
            endpoint.unlink(missing_ok=True)
        socket_root.rmdir()


def test_existing_legacy_layout_is_not_silently_changed(monkeypatch, tmp_path: Path) -> None:
    backend = FamilyVirtualBackend()
    service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
    root = _owner(monkeypatch, "root-native")
    profile = _profile(tmp_path / "home")
    legacy = service.ensure(cwd=tmp_path, settings=profile["settings"],
                            settings_digest=profile["digest"], owner=root,
                            profile_name=profile["name"], profile_base=profile["base_profile"],
                            layout_mode="legacy-v1")
    with pytest.raises(ShellCommandError) as conflict:
        service.ensure(cwd=tmp_path, settings=profile["settings"],
                       settings_digest=profile["digest"], owner=root,
                       profile_name=profile["name"], profile_base=profile["base_profile"])
    assert conflict.value.code == "layout_mode_conflict"
    _, record, _ = service._load(str(legacy["handle"]))
    assert record["layout_mode"] == "legacy-v1"


def test_isolated_tmux_family_stacks_concurrent_operations(tmp_path: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    socket_root = Path(tempfile.mkdtemp(prefix="lsf-"))
    socket_root.chmod(0o700)
    backend = TmuxCommandBackend(tmux, config_file="/dev/null", socket_root=socket_root)
    if backend.capability().get("state") != "available":
        socket_root.rmdir()

        pytest.skip("tmux does not satisfy the shell backend capability profile")
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    owner = make_owner_binding(owner_pid=os.getpid(), agent_id="family-native")
    handle: str | None = None
    viewer_pid: int | None = None
    viewer_starttime: int | None = None
    viewer_master: int | None = None
    secondary_master: int | None = None
    secondary_pid: int | None = None
    viewer_drain_stop = threading.Event()
    secondary_drain_stop = threading.Event()
    viewer_drain_thread: threading.Thread | None = None
    secondary_drain_thread: threading.Thread | None = None
    try:
        created = _ensure_family(service, owner, tmp_path / "home", tmp_path)
        handle = str(created["handle"])
        initial_record = service._load(handle)[1]
        backend._checked(initial_record["endpoint"], ["set-option", "-t", initial_record["native_session_id"],
                                                       "status-interval", "1"], mutation=True)
        joined = _ensure_family(service, owner, tmp_path / "home", tmp_path)
        assert joined["joined"] is True
        assert backend._checked(initial_record["endpoint"], ["show-options", "-t", initial_record["native_session_id"],
                                                             "-qv", "status-interval"]).strip() == "5"
        assert backend._checked(initial_record["endpoint"], ["show-options", "-t", initial_record["native_session_id"],
                                                             "-qv", "prefix2"]).strip() == "None"
        backend._checked(initial_record["endpoint"], ["set-option", "-t", initial_record["native_session_id"],
                                                      "prefix2", "C-a"], mutation=True)
        backend._checked(initial_record["endpoint"], ["bind-key", "-T", "prefix", ":", "command-prompt"], mutation=True)
        backend._checked(initial_record["endpoint"], ["bind-key", "-T", "copy-mode", ":", "command-prompt"], mutation=True)
        with pytest.raises(ShellCommandError) as escaped:
            backend.verify_observer_keys(initial_record)
        assert escaped.value.code == "observer_key_guard_unknown"
        joined = _ensure_family(service, owner, tmp_path / "home", tmp_path)
        assert joined["joined"] is True
        backend.verify_observer_keys(service._load(handle)[1])
        endpoint = initial_record["endpoint"]
        for tampered in (
            ["bind-key", "-T", "root", "Any",
             'display-message "Observer input is disabled" ; run-shell "true"'],
            ["bind-key", "-T", "root", "PPage", "copy-mode", "-e"],
            ["bind-key", "-T", "copy-mode", "PPage",
             'send-keys -X page-up ; run-shell "true"'],
            ["bind-key", "-T", "copy-mode", "PPage", "send-keys", "-X", "page-down"],
        ):
            backend._checked(endpoint, tampered, mutation=True)
            with pytest.raises(ShellCommandError) as unsafe_binding:
                backend.verify_observer_keys(initial_record)
            assert unsafe_binding.value.code == "observer_key_guard_unknown"
            with pytest.raises(ShellCommandError) as unsafe_promotion:
                backend.promote_observer(initial_record, 0)
            assert unsafe_promotion.value.code == "observer_key_guard_unknown"
            assert _ensure_family(service, owner, tmp_path / "home", tmp_path)["joined"] is True
            backend.verify_observer_keys(service._load(handle)[1])
        custom_runtime = json.loads(json.dumps(created["runtime_settings"]))
        custom_runtime["baseline"]["status_interval_seconds"] = 7
        profile = _profile(tmp_path / "home")
        service.ensure(cwd=tmp_path, settings=profile["settings"], settings_digest=profile["digest"],
                       owner=owner, profile_name=profile["name"], profile_base=profile["base_profile"],
                       runtime_settings=custom_runtime)
        assert backend._checked(initial_record["endpoint"], ["show-options", "-t", initial_record["native_session_id"],
                                                             "-qv", "status-interval"]).strip() == "7"
        assert service.viewport(handle, height=512, owner=owner)["height"] == 512
        large = backend.inspect_session(service._load(handle)[1])["windows"][0]
        assert large["width"] == 80 and large["height"] == 512
        assert service.viewport(handle, height=24, owner=owner)["height"] == 24
        first = service.run(handle, "native-one", script="sleep 0.8; printf 'first\\n'",
                            cwd=tmp_path, owner=owner)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, _, operation, _ = service._load_operation(str(first["operation_handle"]))
            if operation.get("process_scope") == "tracking" and operation.get("runner_pid"):
                break
            time.sleep(0.05)
        assert operation.get("runner_pid")
        # The runner briefly takes the same lifecycle lock while recording its
        # process identity. A busy acknowledgement happens before any operation
        # record or native pane is created, so retrying that exact request is
        # safe and exercises serialized topology admission.
        for attempt in range(40):
            try:
                second = service.run(handle, "native-two", script="sleep 0.8; printf 'second\\n'",
                                     cwd=tmp_path, owner=owner)
                break
            except ShellCommandError as exc:
                if exc.code != "busy" or attempt == 39:
                    raise
                time.sleep(0.025)
        _, _, second_record, _ = service._load_operation(str(second["operation_handle"]))
        native = backend.inspect_session(service._load(handle)[1])
        assert native["state"] == "ready"
        assert native["options"]["window_size"] == "manual"
        target = service._load(handle)[1]["native_session_id"]
        assert backend._checked(service._load(handle)[1]["endpoint"],
                                ["show-options", "-t", target, "-qv", "status"]).strip() == "2"
        join_row = backend._checked(service._load(handle)[1]["endpoint"],
                                    ["show-options", "-t", target, "-qv", "status-format[1]"])
        assert created["join_command"] in join_row
        assert len(native["windows"]) == 1
        window = native["windows"][0]
        assert window["width"] == 80 and window["height"] == 24
        assert window["pane_count"] == 3
        assert second_record["window_id"] == first["window_id"] == created["status_window_id"]
        labels = {pane["metadata"]["label"] for pane in window["panes"]}
        assert labels == {"root · Idle", "root · native-one", "root · native-two"}
        assert all(pane["height"] >= 3 for pane in window["panes"])
        assert backend._checked(initial_record["endpoint"], ["show-options", "-w", "-t", window["window_id"],
                                                           "-qv", "pane-border-status"]).strip() == "top"
        assert backend._checked(initial_record["endpoint"], ["show-options", "-w", "-t", window["window_id"],
                                                           "-qv", "pane-border-format"]).strip() == "#{@localsetup_shell_label}"
        assert TmuxCommandBackend.inventory_identity(native)["windows"][0]["pane_count"] == 3

        # Neither the client nor a status poll should be required to finish,
        # drain, remove, and equalize completed family panes.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            _, _, first_record, first_path = service._load_operation(str(first["operation_handle"]))
            _, _, second_record, second_path = service._load_operation(str(second["operation_handle"]))
            first_record = read_json(first_path, limit=2_000_000)
            second_record = read_json(second_path, limit=2_000_000)
            if (first_record.get("state") == second_record.get("state") == "completed"
                    and first_record.get("cleanup_state") == second_record.get("cleanup_state") == "removed"):
                break
            time.sleep(0.05)
        assert first_record.get("cleanup_state") == second_record.get("cleanup_state") == "removed"

        first_done = service.wait(str(first["operation_handle"]), timeout=10, owner=owner)
        second_done = service.wait(str(second["operation_handle"]), timeout=10, owner=owner)
        assert first_done["state"] == second_done["state"] == "completed"
        assert first_done["cleanup_state"] == second_done["cleanup_state"] == "removed"
        telemetry = backend._checked(service._load(handle)[1]["endpoint"],
                                     ["show-options", "-t", target, "-qv", "@localsetup_shell_done"])
        assert telemetry.strip() == "2"
        first_logs = service.logs(str(first["operation_handle"]), owner=owner)
        assert "first" in first_done.get("output", ""), {
            "reply": {key: first_done.get(key) for key in (
                "output", "output_unavailable", "output_drain", "output_storage",
                "output_base_cursor", "output_end_cursor")},
            "logs": {key: first_logs.get(key) for key in (
                "output", "storage", "output_drain", "base_cursor", "end_cursor")},
        }
        assert "second" in second_done.get("output", ""), (
            second_done, service.logs(str(second["operation_handle"]), owner=owner))
        final = backend.inspect_session(service._load(handle)[1])
        assert final["state"] == "ready" and final["windows"][0]["pane_count"] == 1
        interactive = service.run(handle, "native-input", script="printf 'prompt\\n'; read -r answer; printf '%s\\n' \"$answer\"",
                                  cwd=tmp_path, owner=owner)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, _, interactive_record, _ = service._load_operation(str(interactive["operation_handle"]))
            if interactive_record.get("process_scope") == "tracking":
                break
            time.sleep(.05)
        assert interactive_record.get("process_scope") == "tracking"
        family_record = service._load(handle)[1]
        viewer_master, viewer_slave = pty.openpty()
        viewer_drain_thread = threading.Thread(target=_drain_tty, args=(viewer_master, viewer_drain_stop), daemon=True)
        viewer_drain_thread.start()
        fcntl.ioctl(viewer_slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        viewer_done = tmp_path / "human-viewer.exit"
        attach_code = (
            "from pathlib import Path; from ls.core.terminal_ops.command_cli import main; "
            f"result=main(['--state-root',{str(state_root)!r},'attach','--session',"
            f"{str(created['native_session_name'])!r}],source_root=Path({str(SOURCE_ROOT)!r}),"
            f"home=Path({str(tmp_path / 'home')!r}));"
            f"Path({str(viewer_done)!r}).write_text(str(result)); raise SystemExit(result)"
        )
        viewer_pid = _start_unrelated_tty(attach_code, viewer_slave, tmp_path / "viewer.pid")
        viewer_info = process_info(viewer_pid)
        assert viewer_info is not None and viewer_info["ppid"] != os.getpid()
        viewer_starttime = viewer_info["starttime"]
        os.close(viewer_slave)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not backend._clients(family_record):
            time.sleep(.05)
        if not backend._clients(family_record):
            import select
            output = os.read(viewer_master, 8192) if select.select([viewer_master], [], [], 0)[0] else b""
            pytest.fail(f"unrelated observer did not attach: {viewer_pid} {output!r}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "read-only" in backend._clients(family_record)[0]["flags"]:
            time.sleep(.05)
        assert "read-only" not in backend._clients(family_record)[0]["flags"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current_height = backend.inspect_session(service._load(handle)[1])["windows"][0]["height"]
            if current_height == 40:
                break
            time.sleep(.05)
        assert current_height == 40
        assert backend.inspect_session(service._load(handle)[1])["windows"][0]["width"] == 80
        secondary_master, secondary_slave = pty.openpty()
        secondary_drain_thread = threading.Thread(target=_drain_tty, args=(secondary_master, secondary_drain_stop), daemon=True)
        secondary_drain_thread.start()
        fcntl.ioctl(secondary_slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 120, 0, 0))
        secondary_done = tmp_path / "second-viewer.exit"
        secondary_code = attach_code.replace(str(viewer_done), str(secondary_done))
        secondary_pid = _start_unrelated_tty(secondary_code, secondary_slave, tmp_path / "second-viewer.pid")
        os.close(secondary_slave)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(backend._clients(family_record)) < 2:
            time.sleep(.05)
        assert len(backend._clients(family_record)) == 2
        time.sleep(.6)
        assert backend.inspect_session(service._load(handle)[1])["windows"][0]["height"] == 40
        secondary_client = next(client for client in backend._clients(family_record)
                                if (process_info(client["pid"]) or {}).get("ppid") == secondary_pid)
        backend._checked(family_record["endpoint"], ["detach-client", "-t", secondary_client["name"]], mutation=True)
        deadline = time.monotonic() + 5
        import select
        while (not secondary_done.exists() or secondary_done.read_text() != "0") and time.monotonic() < deadline:
            if select.select([secondary_master], [], [], .05)[0]:
                try:
                    os.read(secondary_master, 65536)
                except OSError:
                    pass
        assert secondary_done.exists() and secondary_done.read_text() == "0"
        secondary_drain_stop.set()
        secondary_drain_thread.join(timeout=1)
        os.close(secondary_master)
        secondary_master = secondary_pid = None
        fcntl.ioctl(viewer_master, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
        os.kill(viewer_pid, signal.SIGWINCH)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current_height = backend.inspect_session(service._load(handle)[1])["windows"][0]["height"]
            if current_height == 32:
                break
            time.sleep(.05)
        assert current_height == 32
        assert backend.inspect_session(service._load(handle)[1])["windows"][0]["width"] == 80
        backend._checked(family_record["endpoint"], ["bind-key", "-T", "root", "Any",
                                                     'display-message "Observer input is disabled" ; run-shell "true"'],
                         mutation=True)
        refused_bytes = tmp_path / "refused-human-input"
        refused_bytes.write_bytes(b"blocked\n")
        with pytest.raises(ShellCommandError) as refused_input:
            backend.send_input(family_record, interactive_record, buffer_name="ls-refused",
                               input_path=str(refused_bytes), eof=False)
        assert refused_input.value.code == "observer_key_guard_unknown"
        assert all("read-only" in client["flags"] for client in backend._clients(family_record))
        pane_guard = backend._checked(family_record["endpoint"], ["display-message", "-p", "-t",
                                                             interactive_record["pane_id"],
                                                             "-F", "#{pane_input_off}"]).strip()
        assert pane_guard == "1"
        for attempt in range(40):
            try:
                repaired = _ensure_family(service, owner, tmp_path / "home", tmp_path)
                break
            except ShellCommandError as exc:
                if exc.code != "busy" or attempt == 39:
                    raise
                time.sleep(.025)
        assert repaired["joined"] is True
        family_record = service._load(handle)[1]
        primary_client = backend._clients(family_record)[0]
        backend.promote_observer(family_record, primary_client["pid"])
        assert "read-only" not in backend._clients(family_record)[0]["flags"]
        start_elapsed = int(backend._checked(family_record["endpoint"], ["show-options", "-t", target,
                                                                      "-qv", "@localsetup_shell_elapsed"]).strip())
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            live_bytes = int(backend._checked(family_record["endpoint"], ["show-options", "-t", target,
                                                                        "-qv", "@localsetup_shell_output_bytes"]).strip())
            live_elapsed = int(backend._checked(family_record["endpoint"], ["show-options", "-t", target,
                                                                          "-qv", "@localsetup_shell_elapsed"]).strip())
            if live_bytes >= len("prompt\n") and live_elapsed > start_elapsed:
                break
            time.sleep(.1)
        assert live_bytes >= len("prompt\n") and live_elapsed > start_elapsed
        short_lease = service.input_lease(str(interactive["operation_handle"]),
                                          grantee="human", ttl=1, owner=owner)
        assert short_lease["state"] == "requested"
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            state = backend._checked(family_record["endpoint"], ["show-options", "-t", target,
                                                                "-qv", "@localsetup_shell_input_state"]).strip()
            if state == "RO":
                break
            time.sleep(.1)
        assert state == "RO"
        revocable = service.input_lease(str(interactive["operation_handle"]),
                                        grantee="human", ttl=30, owner=owner)
        assert revocable["state"] == "requested"
        service.input_lease(str(interactive["operation_handle"]),
                            grantee="human", revoke=True, owner=owner)
        assert backend._checked(family_record["endpoint"], ["show-options", "-t", target, "-qv",
                                                          "@localsetup_shell_input_state"]).strip() == "RO"
        lease = service.input_lease(str(interactive["operation_handle"]),
                                    grantee="human", ttl=30, owner=owner)
        assert lease["state"] == "requested"
        assert backend._checked(family_record["endpoint"], ["show-options", "-t", target, "-qv",
                                                          "@localsetup_shell_input_state"]).strip() == "REQUESTED"
        input_master, input_slave = pty.openpty()
        try:
            input_done = tmp_path / "human-input.exit"
            input_code = (
                "from pathlib import Path; from ls.core.terminal_ops.command_cli import main; "
                f"result=main(['--state-root',{str(state_root)!r},'input','--handle',"
                f"{str(interactive['operation_handle'])!r},'--lease',{str(lease['lease_id'])!r},"
                "'--grantee','human','--id','human-typed','--data','typed\\n'],"
                f"source_root=Path({str(SOURCE_ROOT)!r}),home=Path({str(tmp_path / 'home')!r}));"
                f"Path({str(input_done)!r}).write_text(str(result)); raise SystemExit(result)"
            )
            input_pid = _start_unrelated_tty(input_code, input_slave, tmp_path / "human-input.pid")
            input_info = process_info(input_pid)
            assert input_info is not None and input_info["ppid"] != os.getpid()
            deadline = time.monotonic() + 10
            while not input_done.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert input_done.exists() and input_done.read_text() == "0"
        finally:
            os.close(input_slave)
            os.close(input_master)
        assert "read-only" not in backend._clients(family_record)[0]["flags"]
        interactive_done = service.wait(str(interactive["operation_handle"]), timeout=10, owner=owner)
        assert interactive_done["state"] == "completed" and "typed" in interactive_done["output"]
        assert "blocked" not in interactive_done["output"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, _, interactive_record, _ = service._load_operation(str(interactive["operation_handle"]))
            if interactive_record.get("cleanup_state") == "removed":
                break
            time.sleep(.05)
        assert interactive_record.get("cleanup_state") == "removed"
        for attempt in range(40):
            try:
                retiring = service.close(handle, owner=owner)
                break
            except ShellCommandError as exc:
                if exc.code != "busy" or attempt == 39:
                    raise
                time.sleep(.05)
        assert retiring["state"] == "retiring" and retiring["closed_for_dispatch"] is True
        with pytest.raises(ShellCommandError) as closed_for_dispatch:
            service.run(handle, "after-close", script="true", owner=owner)
        assert closed_for_dispatch.value.code == "session_not_ready"
        assert process_info(viewer_pid) is not None
        client_name = backend._clients(family_record)[0]["name"]
        backend._checked(family_record["endpoint"], ["detach-client", "-t", client_name], mutation=True)
        import select
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([viewer_master], [], [], .1)[0]:
                try:
                    os.read(viewer_master, 65536)
                except OSError:
                    pass
            if backend._endpoint_state(family_record["endpoint"]) != "live":
                break
        deadline = time.monotonic() + 2
        while (not viewer_done.exists() or viewer_done.read_text() != "0") and time.monotonic() < deadline:
            time.sleep(.05)
        assert viewer_done.exists() and viewer_done.read_text() == "0"
        viewer_drain_stop.set()
        viewer_drain_thread.join(timeout=1)
        os.close(viewer_master)
        viewer_pid = viewer_master = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, session, _ = service._load(handle)
            if session.get("state") == "removed" and not Path(session["endpoint"]).exists():
                break
            time.sleep(.05)
        assert not Path(session["endpoint"]).exists(), (session["state"], backend._endpoint_state(session["endpoint"]), backend.inspect_session(session).get("session"))
        assert session["state"] == "removed"
    finally:
        if viewer_pid is not None:
            current_viewer = process_info(viewer_pid)
            if current_viewer is not None and current_viewer.get("starttime") == viewer_starttime:
                os.kill(viewer_pid, 15)
        if viewer_master is not None:
            viewer_drain_stop.set()
            if viewer_drain_thread is not None:
                viewer_drain_thread.join(timeout=1)
            os.close(viewer_master)
        if secondary_pid is not None:
            current_secondary = process_info(secondary_pid)
            if current_secondary is not None:
                os.kill(secondary_pid, signal.SIGTERM)
        if secondary_master is not None:
            secondary_drain_stop.set()
            if secondary_drain_thread is not None:
                secondary_drain_thread.join(timeout=1)
            os.close(secondary_master)
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
        for endpoint in socket_root.glob("s-*.sock"):
            subprocess.run([tmux, "-f", "/dev/null", "-S", str(endpoint), "kill-server"],
                           capture_output=True, timeout=3, check=False)
            endpoint.unlink(missing_ok=True)
        socket_root.rmdir()
def test_retiring_legacy_view_rolls_to_same_owner_family(tmp_path: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    socket_root = Path(tempfile.mkdtemp(prefix="ls-roll-"))
    socket_root.chmod(0o700)
    backend = TmuxCommandBackend(tmux, config_file="/dev/null", socket_root=socket_root)
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    owner = make_owner_binding(owner_pid=os.getpid(), agent_id="rollover-root")
    profile = _profile(tmp_path / "home")
    master = child = old = new = None
    try:
        legacy = service.ensure(cwd=tmp_path, settings=profile["settings"], settings_digest=profile["digest"],
                                owner=owner, profile_name=profile["name"], profile_base=profile["base_profile"],
                                layout_mode="legacy-v1")
        old = str(legacy["handle"])
        old_record = service._load(old)[1]
        master, slave = pty.openpty()
        attach_code = (
            "import json,pathlib; from ls.core.terminal_ops.command_service import ShellCommandService; "
            f"ShellCommandService({str(state_root)!r}, source_root=pathlib.Path({str(SOURCE_ROOT)!r}))"
            f".attach({old!r}, owner=json.loads({json.dumps(owner)!r}))"
        )
        child = subprocess.Popen([sys.executable, "-c", attach_code], stdin=slave, stdout=slave, stderr=slave,
                                 env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}, start_new_session=True)
        os.close(slave)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not backend._clients(old_record):
            time.sleep(.05)
        assert backend._clients(old_record)
        retiring = service.close(old, owner=owner)
        assert retiring["state"] == "retiring" and retiring["closed_for_dispatch"] is True
        new_session = service.ensure(cwd=tmp_path, settings=profile["settings"],
                                     settings_digest=profile["digest"], owner=owner,
                                     profile_name=profile["name"], profile_base=profile["base_profile"],
                                     replace_retiring=True)
        new = str(new_session["handle"])
        assert new != old and new_session["layout_mode"] == "family-v1"
        assert backend.inspect_session(old_record)["state"] == "ready"
        operation = service.run(new, "new-family", script="printf 'family-ready\\n'", owner=owner)
        assert service.wait(str(operation["operation_handle"]), timeout=10, owner=owner)["state"] == "completed"
        name = backend._clients(old_record)[0]["name"]
        backend._checked(old_record["endpoint"], ["detach-client", "-t", name], mutation=True)
        child.wait(timeout=3)
        os.close(master)
        child = master = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and service._load(old)[1]["state"] != "removed":
            time.sleep(.05)
        assert service._load(old)[1]["state"] == "removed"
        for attempt in range(40):
            try:
                new_status = service.status(new, owner=owner)
                break
            except ShellCommandError as exc:
                if exc.code != "busy" or attempt == 39:
                    raise
                time.sleep(.05)
        assert new_status["native_state"] == "ready"
        owner_index = read_json(service._owner_dir(service._load(new)[1]["scope_key"]) / "owner.json")
        assert owner_index["active_handle"] == new
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
        if master is not None:
            os.close(master)
        for handle in (old, new):
            if handle is None:
                continue
            try:
                _, record, _ = service._load(handle)
                if backend.inspect_session(record).get("state") == "ready":
                    backend.close_session(record, expected_inventory=TmuxCommandBackend.inventory_identity(
                        backend.inspect_session(record)))
            except ShellCommandError:
                pass
        for endpoint in socket_root.glob("s-*.sock"):
            subprocess.run([tmux, "-f", "/dev/null", "-S", str(endpoint), "kill-server"], capture_output=True)
            endpoint.unlink(missing_ok=True)
        socket_root.rmdir()
