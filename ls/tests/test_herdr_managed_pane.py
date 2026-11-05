"""Focused regressions for managed Herdr pane ownership and observation."""

from __future__ import annotations
from contextlib import contextmanager
import fcntl

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ls.core.herdr_ops.errors import HerdrOpsError
from ls.core.terminal_ops import backends
from ls.core.terminal_ops.models import PaneBinding, PaneError, PaneRequest, PaneStatus, PaneTransition
from ls.core.terminal_ops.pane import FilePaneStore, ManagedPaneService
from ls.core.terminal_ops.shell import finish_transition


@pytest.fixture
def herdr_request(tmp_path: Path) -> PaneRequest:
    return PaneRequest(
        "herdr", str(tmp_path / "herdr.sock"), "project", "controller-current",
        "parent-pane", str(tmp_path), workspace_id="workspace", machine_id="machine",
    )


def _completed_prior_state(
    tmp_path: Path, request: PaneRequest, binding: PaneBinding,
) -> tuple[Path, dict[str, dict], PaneStatus, dict]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    operations_dir = state_dir / "terminal_ops"
    operations_dir.mkdir(mode=0o700)
    directory = operations_dir / request.binding_key
    directory.mkdir(mode=0o700)
    operation_id = "completed-before-reboot"
    lease_token = "completed-lease-token"
    lease = directory / "writer-lease"
    lease.mkdir(mode=0o700)
    (lease / "lease.json").write_text(json.dumps({
        "binding_key": request.binding_key,
        "operation_id": operation_id,
        "token": lease_token,
    }), encoding="utf-8")
    operation = {
        "schema_version": 1,
        "binding_key": request.binding_key,
        "operation_id": operation_id,
        "state": "running",
        "pane_id": binding.pane_id,
        "shell_pid": binding.shell_pid,
        "pane_tty": binding.pane_tty,
        "shell_path": binding.shell_path,
        "machine_generation": binding.machine_generation,
        "target": request.host_alias or request.repository,
        "endpoint": request.endpoint,
        "controller_id": request.controller_id,
        "working_directory": request.cwd,
        "lease_token": lease_token,
        "transition": PaneTransition(operation_id, ("/usr/bin/true",)).to_dict(),
    }
    receipt_path = directory / f"{operation_id}.json"
    receipt_path.write_text(json.dumps(operation), encoding="utf-8")
    receipt_path.chmod(0o600)
    (lease / "lease.json").chmod(0o600)
    completed = finish_transition(
        state_dir, binding_key=request.binding_key, operation_id=operation_id, exit_code=0,
    )
    ready = PaneStatus("ready", request.backend, request.binding_key, request.name,
                       binding=binding, operation_id=operation_id)
    store = {request.binding_key: {
        "binding_key": request.binding_key,
        "request": request.to_dict(),
        "status": ready.to_dict(),
        "last_binding": binding.to_dict(),
        "operation": completed,
    }}
    return state_dir, store, ready, completed


def _owner_record(state_dir: Path, endpoint: str, pane_id: str, *, controller: str,
                  parent: str, workspace: str = "workspace") -> None:
    record = {
        "schema_version": 1,
        "owner_id": pane_id,
        "purpose": "managed_ops",
        "endpoint": str(Path(endpoint).absolute()),
        "workspace_id": workspace,
        "caller_pane_id": parent,
        "pane_id": pane_id,
        "terminal_id": f"terminal-{pane_id}",
        "tab_id": "tab-1",
        "cwd": "/tmp",
        "ratio": 1 / 3,
        "state": "owned",
        "owner_metadata": {
            "controller_id": controller,
            "parent_id": parent,
        },
    }
    owners = state_dir / "panes"
    owners.mkdir(parents=True, exist_ok=True, mode=0o700)
    (owners / f"{pane_id}.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize(
    ("owned", "other_controller", "expected"),
    [
        (True, True, "missing"),  # same label/parent, but an exact other controller owns it
        (False, False, "unknown"),  # an unowned label collision cannot be adopted
        (True, False, "unknown"),  # a same-controller collision is not an exact binding
    ],
)
def test_same_label_inventory_isolated_only_for_foreign_owned_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, herdr_request: PaneRequest,
    owned: bool, other_controller: bool, expected: str,
) -> None:
    request = herdr_request
    adapter = backends.HerdrAdapter(state_dir=tmp_path / "state")
    monkeypatch.setattr(adapter, "_endpoint", lambda _request: request.endpoint)
    pane = {
        "pane_id": "other-managed-pane", "workspace_id": request.workspace_id,
        "tab_id": "tab-1", "label": request.name,
    }
    inventory = [
        {"pane_id": request.parent_id, "workspace_id": request.workspace_id, "tab_id": "tab-1"},
        pane,
    ]
    monkeypatch.setattr(adapter, "_inventory", lambda _request: inventory)
    if owned:
        _owner_record(
            tmp_path / "state", request.endpoint, pane["pane_id"],
            controller="controller-other" if other_controller else request.controller_id,
            parent=request.parent_id,
        )

    status = adapter.inspect(request)

    assert status.state == expected
    assert status.binding is None
    assert status.binding_key == request.binding_key


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux process environment required")
def test_startup_home_and_nonempty_sudo_key_names_come_from_real_server_process(
    tmp_path: Path, herdr_request: PaneRequest,
) -> None:
    home = tmp_path / "server-home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "PATH": os.defpath,
        "GENERIC_SUDO_KEY": "dummy-generic-secret",
        "ALIEN_SUDO_KEY": "dummy-host-secret",
    }
    server = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        adapter = backends.HerdrAdapter(state_dir=tmp_path / "state")

        class EndpointClient:
            @staticmethod
            def endpoint_identity() -> dict[str, int]:
                return {"pid": server.pid}

        adapter.client = EndpointClient()
        names, startup_home = adapter._server_startup_environment(herdr_request)

        assert startup_home == str(home)
        assert names == {"GENERIC_SUDO_KEY", "ALIEN_SUDO_KEY"}
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_busy_original_shell_binding_is_observable_but_never_dispatchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = PaneRequest("herdr", str(tmp_path / "herdr.sock"), "project", "controller",
                          "parent", str(tmp_path), workspace_id="workspace")
    observed_binding = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="child",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, tab_id="tab", terminal_id="terminal",
        pane_tty="/dev/pts/7", shell_pid=421,
        shell_path="/bin/bash", machine_generation="generation", cwd=str(tmp_path),
        state="busy", metadata={"prompt_nonce": "busy-nonce", "phase": "busy"},
    )
    monkeypatch.setattr(backends, "read_prompt_state", lambda *_args: {
        "pane_id": observed_binding.pane_id,
        "shell_pid": observed_binding.shell_pid,
        "pane_tty": observed_binding.pane_tty,
        "shell_path": observed_binding.shell_path,
        "cwd": observed_binding.cwd,
        "machine_generation": observed_binding.machine_generation,
        "phase": "busy",
        "keymap_ready": True,
        "prompt_nonce": "busy-nonce",
    })
    monkeypatch.setattr(backends, "machine_generation", lambda: "generation")
    monkeypatch.setattr(backends, "_process_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(backends, "_process_tty_matches", lambda _pid, _tty: True)
    monkeypatch.setattr(backends, "_same_path", lambda left, right: left == right)
    monkeypatch.setattr(backends, "_credential_names_in_process", lambda _pid: set())
    monkeypatch.setattr(backends, "_shell_owns_foreground", lambda _pid, _tty: False)
    native = backends.HerdrAdapter(state_dir=tmp_path)
    native.client = type("NativeClient", (), {
        "process_info": lambda self, pane: {
            "process_info": {"shell_pid": 421, "foreground_processes": [{"pid": 422}]},
        },
    })()
    monkeypatch.setattr(native, "_endpoint", lambda _: request.endpoint)
    monkeypatch.setattr(native, "_owned", lambda _: (
        {"caller_pane_id": request.parent_id, "workspace_id": request.workspace_id,
         "tab_id": "tab", "cwd": str(tmp_path)},
        {"pane_id": "child", "workspace_id": request.workspace_id, "tab_id": "tab",
         "terminal_id": "terminal", "label": request.name},
    ))
    monkeypatch.setattr(native, "_inventory", lambda _: [
        {"pane_id": request.parent_id, "tab_id": "tab"},
    ])
    readlink = os.readlink
    monkeypatch.setattr(os, "readlink", lambda path: (
        "/dev/pts/7" if str(path) == "/proc/421/fd/0" else readlink(path)
    ))
    status = native.inspect(request)
    assert status.state == "busy"
    assert status.binding == observed_binding

    class BusyAdapter:
        triggered = False

        def inspect(self, _request: PaneRequest) -> PaneStatus:
            return status

        def send_trigger(self, _request: PaneRequest, _binding: PaneBinding) -> None:
            self.triggered = True

    adapter = BusyAdapter()
    service = ManagedPaneService({}, {"herdr": adapter}, state_dir=tmp_path)
    result = service.transition(request, PaneTransition("must-not-dispatch", ("true",)))

    assert result.state == "busy"
    assert result.binding == observed_binding
    assert result.operation_id is None
    assert not adapter.triggered
    assert not (tmp_path / "terminal_ops" / request.binding_key / "must-not-dispatch.json").exists()


@pytest.mark.parametrize("first_effect", ["unknown", "none"])
def test_completed_operation_and_absence_proof_are_required_before_replacement(
    tmp_path: Path, herdr_request: PaneRequest, first_effect: str,
) -> None:
    request = herdr_request
    previous = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="old-pane",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, tab_id="old-tab", terminal_id="old-terminal",
        pane_tty="/dev/pts/200", shell_pid=900001, shell_path="/bin/bash",
        machine_generation="old-generation", cwd="/work/previous", state="ready",
    )
    state_dir, store, _ready, _receipt = _completed_prior_state(tmp_path, request, previous)

    class Native:
        def __init__(self) -> None:
            self.created: list[PaneBinding | None] = []

        def inspect(
            self, _request: PaneRequest, *, previous_binding: PaneBinding | None = None,
        ) -> PaneStatus:
            if previous_binding is not None:
                return PaneStatus("missing", request.backend, request.binding_key, request.name)
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="ordinary owner inspection cannot prove the missing generation")

        def create(
            self, _request: PaneRequest, *, previous_binding: PaneBinding | None = None,
        ) -> PaneBinding:
            self.created.append(previous_binding)
            effect = first_effect if len(self.created) == 1 else "unknown"
            raise PaneError("split_result", "simulated native result", effect=effect)

    native = Native()
    service = ManagedPaneService(store, {"herdr": native}, state_dir=state_dir)

    observed = service.inspect(request)
    assert observed.state == "missing"

    with pytest.raises(PaneError) as caught:
        service.ensure(request)
    assert caught.value.effect == first_effect
    assert len(native.created) == 1

    if first_effect == "unknown":
        assert store[request.binding_key]["creation"]["state"] == "reserved"
        fenced = service.ensure(request)
        assert fenced.state == "unknown"
        assert len(native.created) == 1
    else:
        assert "creation" not in store[request.binding_key]
        assert store[request.binding_key]["status"]["state"] == "missing"
        assert store[request.binding_key]["status"]["operation_id"] == "completed-before-reboot"
        with pytest.raises(PaneError) as retry:
            service.ensure(request)
        assert retry.value.effect == "unknown"
        assert len(native.created) == 2
        assert store[request.binding_key]["creation"]["state"] == "reserved"


def test_ready_reuse_preserves_completion_proof_for_one_replacement(
    tmp_path: Path, herdr_request: PaneRequest,
) -> None:
    request = herdr_request
    previous = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="old-pane",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, tab_id="old-tab", terminal_id="old-terminal",
        pane_tty="/dev/pts/200", shell_pid=900001, shell_path="/bin/bash",
        machine_generation="old-generation", cwd="/work/previous", state="ready",
    )
    replacement = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="new-pane",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, tab_id="new-tab", terminal_id="new-terminal",
        pane_tty="/dev/pts/201", shell_pid=900002, shell_path="/bin/bash",
        machine_generation="new-generation", cwd=request.cwd, state="ready",
    )
    state_dir, store, _ready, _receipt = _completed_prior_state(tmp_path, request, previous)

    class Native:
        def __init__(self) -> None:
            self.current: PaneBinding | None = previous
            self.created: list[PaneBinding | None] = []

        def inspect(
            self, _request: PaneRequest, *, previous_binding: PaneBinding | None = None,
        ) -> PaneStatus:
            if self.current is None:
                if previous_binding is not None:
                    return PaneStatus("missing", request.backend, request.binding_key, request.name)
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  detail="ordinary inspection cannot prove the missing generation")
            return PaneStatus("ready", request.backend, request.binding_key, request.name,
                              binding=self.current)

        def create(
            self, _request: PaneRequest, *, previous_binding: PaneBinding | None = None,
        ) -> PaneBinding:
            self.created.append(previous_binding)
            self.current = replacement
            return replacement

    native = Native()
    service = ManagedPaneService(store, {"herdr": native}, state_dir=state_dir)

    reused = service.ensure(request)
    assert reused.state == "ready"
    assert reused.binding == previous
    assert store[request.binding_key]["operation"]["state"] == "completed"
    assert store[request.binding_key]["status"]["operation_id"] == "completed-before-reboot"

    native.current = None
    missing = service.inspect(request)
    assert missing.state == "missing"
    assert not native.created
    assert store[request.binding_key]["operation"]["state"] == "completed"

    created = service.ensure(request)
    assert created.state == "ready"
    assert created.binding == replacement
    assert len(native.created) == 1
    assert native.created[0] is not None
    assert native.created[0].pane_id == previous.pane_id

    reused_replacement = service.ensure(request)
    assert reused_replacement.state == "ready"
    assert reused_replacement.binding == replacement
    assert len(native.created) == 1


def test_replacement_never_uses_prior_binding_for_a_changed_stored_request(
    tmp_path: Path, herdr_request: PaneRequest,
) -> None:
    request = herdr_request
    previous = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="old-pane",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, tab_id="old-tab", terminal_id="old-terminal",
        pane_tty="/dev/pts/200", shell_pid=900001, shell_path="/bin/bash",
        machine_generation="old-generation", cwd="/work/previous", state="ready",
    )
    state_dir, store, _ready, _receipt = _completed_prior_state(tmp_path, request, previous)
    store[request.binding_key]["request"]["cwd"] = "/different"

    class Native:
        def __init__(self) -> None:
            self.inspected: list[PaneBinding | None] = []

        def inspect(
            self, _request: PaneRequest, *, previous_binding: PaneBinding | None = None,
        ) -> PaneStatus:
            self.inspected.append(previous_binding)
            return PaneStatus("unknown", request.backend, request.binding_key, request.name)

    native = Native()
    service = ManagedPaneService(store, {"herdr": native}, state_dir=state_dir)

    assert service.inspect(request).state == "unknown"
    assert native.inspected == [None, None]


@pytest.mark.parametrize(("proof_succeeds", "expected"), [(True, "missing"), (False, "unknown")])
def test_herdr_adapter_requires_layout_proof_for_previous_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, herdr_request: PaneRequest,
    proof_succeeds: bool, expected: str,
) -> None:
    request = herdr_request
    previous = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="old-pane",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, tab_id="old-tab", terminal_id="old-terminal",
        pane_tty="/dev/pts/200", shell_pid=900001, shell_path="/bin/bash",
        machine_generation="old-generation", cwd="/work/previous", state="ready",
    )
    adapter = backends.HerdrAdapter(state_dir=tmp_path / "state")
    monkeypatch.setattr(adapter, "_endpoint", lambda _request: request.endpoint)

    def missing_owner(_request: PaneRequest):
        raise HerdrOpsError("managed_pane_missing", "old pane missing", effect="unknown")

    proof_calls = []

    def verify(*args, **kwargs):
        proof_calls.append((args, kwargs))
        if not proof_succeeds:
            raise HerdrOpsError("inventory_unknown", "inventory is not complete")

    monkeypatch.setattr(adapter, "_owned", missing_owner)
    monkeypatch.setattr(backends.herdr_layout, "verify_missing_managed_pane", verify)

    status = adapter.inspect(request, previous_binding=previous)

    assert status.state == expected
    assert len(proof_calls) == 1


@pytest.mark.parametrize("marker_state", ["creating", "unknown"])
def test_herdr_adapter_does_not_adopt_an_unresolved_replacement_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, herdr_request: PaneRequest,
    marker_state: str,
) -> None:
    request = herdr_request
    adapter = backends.HerdrAdapter(state_dir=tmp_path / "state")
    monkeypatch.setattr(adapter, "_endpoint", lambda _request: request.endpoint)
    monkeypatch.setattr(adapter, "_owned", lambda _request: (
        {"replacement": {"state": marker_state}},
        {"pane_id": "new-pane", "workspace_id": request.workspace_id},
    ))

    status = adapter.inspect(request)

    assert status.state == "unknown"
    assert "replacement is reserved" in (status.detail or "")


@pytest.fixture
def reserved_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, herdr_request: PaneRequest):
    request = herdr_request
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    store = FilePaneStore(state)
    runtime = state / "terminal_ops" / request.binding_key
    runtime.mkdir(mode=0o700)
    (runtime / "bashrc").write_text("# bootstrap\n", encoding="utf-8")
    token = "a" * 32
    status = PaneStatus("unknown", "herdr", request.binding_key, request.name)
    store.put(request.binding_key, {
        "schema_version": 1, "api_version": 1, "binding_key": request.binding_key,
        "request": request.to_dict(), "status": status.to_dict(),
        "creation": {"state": "reserved", "token": token},
    })
    original = store._path(request.binding_key).read_bytes()
    owner_id = request.binding_key[:32]
    owners = state / "panes"
    owners.mkdir(mode=0o700)
    owner = {
        "schema_version": 1, "owner_id": owner_id, "purpose": "managed_ops",
        "endpoint": request.endpoint, "workspace_id": request.workspace_id,
        "caller_pane_id": request.parent_id, "pane_id": "child", "terminal_id": "terminal",
        "created_at": "2026-01-01T00:00:00Z", "before_layout": {"tab_id": "tab"},
        "tab_id": "tab", "cwd": request.cwd, "ratio": 1 / 3, "state": "owned",
        "owner_metadata": backends._managed_owner_metadata(request),
    }
    owner_path = owners / f"{owner_id}.json"
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    owner_path.chmod(0o600)
    original_owner = owner_path.read_bytes()
    parent = {"pane_id": request.parent_id, "terminal_id": "parent-terminal",
              "workspace_id": request.workspace_id, "tab_id": "tab"}
    child = {"pane_id": "child", "terminal_id": "terminal",
             "workspace_id": request.workspace_id, "tab_id": "tab", "label": request.name}
    native = {"panes": [parent, child], "closes": 0, "lost": False, "remove": True}

    def native_request(_endpoint, method, params=None, **_kwargs):
        if method == "session.snapshot":
            return {"snapshot": {"panes": list(native["panes"])}}
        if method == "pane.close":
            assert params == {"pane_id": "child"}
            native["closes"] += 1
            if native["remove"]:
                native["panes"] = [parent]
            if native["lost"]:
                raise HerdrOpsError("response_lost", "lost", "unknown")
            return {"type": "pane_closed"}
        if method == "pane.get":
            return {"pane": next(pane for pane in native["panes"] if pane["pane_id"] == params["pane_id"])}
        if method == "pane.layout":
            return {"layout": {"tab_id": "tab", "workspace_id": request.workspace_id}}
        if method == "pane.split":
            pane = {"pane_id": "new-child", "terminal_id": "new-terminal",
                    "workspace_id": request.workspace_id, "tab_id": "tab"}
            native["panes"].append(pane)
            return {"pane": pane}
        raise AssertionError(method)

    monkeypatch.setattr(backends.herdr_layout, "request", native_request)
    adapter = backends.HerdrAdapter(state_dir=state)
    monkeypatch.setattr(adapter, "_endpoint", lambda _request: request.endpoint)
    monkeypatch.setattr(adapter, "_retirement_observation", lambda _request, _pane: {
        "shell_pid": 123, "pane_tty": "/dev/pts/1", "shell_starttime": 42,
        "server_pid": 7, "machine_generation": "boot",
    })
    service = ManagedPaneService(store, {"herdr": adapter}, state_dir=state)
    args = {"retirement_id": "retire-1", "expected_creation_token": token,
            "expected_pane_id": "child", "expected_terminal_id": "terminal",
            "discard_uncontrolled_shell": True}
    return service, store, owner_path, request, args, native, original, original_owner


class RawFlockPaneStore(FilePaneStore):
    """Store whose lock exposes raw nonblocking flock errors."""

    @contextmanager
    def lock(self, key):
        with self._path(key).with_suffix(".lock").open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def test_reserved_creation_retirement_archives_and_is_idempotent(reserved_creation):
    service, store, owner_path, request, args, native, original, original_owner = reserved_creation
    result = service.retire_creation(request, **args)
    assert result["state"] == "retired"
    assert result["changed"] is True
    assert result["pane_id"] == "child" and result["terminal_id"] == "terminal"
    assert native["closes"] == 1
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    marker = owner["retirement"]
    assert (owner_path.parent.parent / marker["owner_archive"]).read_bytes() == original_owner
    assert (owner_path.parent.parent / marker["reservation_archive"]).read_bytes() == original
    assert store.get(request.binding_key)["creation"]["token"] == args["expected_creation_token"]
    again = service.retire_creation(request, **args)
    assert again["state"] == "retired" and again["changed"] is False
    assert native["closes"] == 1


@pytest.mark.parametrize("remove", [False, True])
def test_retirement_lost_close_response_reconciles_without_replay(reserved_creation, remove):
    service, _store, _owner_path, request, args, native, _original, _original_owner = reserved_creation
    native["lost"] = True
    native["remove"] = remove
    result = service.retire_creation(request, **args)
    assert result["state"] == "unknown"
    result = service.reconcile_creation_retirement(request, args["retirement_id"])
    assert result["state"] == ("retired" if remove else "unknown")
    service.retire_creation(request, **args)
    assert native["closes"] == 1


@pytest.mark.parametrize("conflict", ["token", "terminal", "operation", "lease", "duplicate"])
def test_retirement_rejects_unqualified_reservation(reserved_creation, conflict):
    service, store, owner_path, request, args, native, _original, _original_owner = reserved_creation
    if conflict == "token":
        args["expected_creation_token"] = "b" * 32
    elif conflict == "terminal":
        args["expected_terminal_id"] = "other-terminal"
    elif conflict == "operation":
        record = store.get(request.binding_key)
        record["operation"] = {"state": "unknown"}
        store.put(request.binding_key, record)
    elif conflict == "lease":
        (service.state_dir / "terminal_ops" / request.binding_key / "writer-lease").mkdir()
    else:
        other = owner_path.parent / "another.json"
        other.write_bytes(owner_path.read_bytes())
        other.chmod(0o600)
    with pytest.raises(PaneError):
        service.retire_creation(request, **args)
    assert native["closes"] == 0


def test_retirement_respects_binding_writer_lock(reserved_creation):
    service, store, _owner_path, request, args, native, _original, _original_owner = reserved_creation
    with store.lock(request.binding_key), pytest.raises(PaneError) as error:
        service.retire_creation(request, **args)
    assert error.value.code == "pane_busy"
    assert native["closes"] == 0


def test_retirement_normalizes_raw_store_lock_contention_before_close(reserved_creation):
    service, store, owner_path, request, args, native, original, original_owner = reserved_creation
    service.store = RawFlockPaneStore(service.state_dir)
    lock_path = store._path(request.binding_key).with_suffix(".lock")
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PaneError) as error:
            service.retire_creation(request, **args)
    assert error.value.code == "pane_busy"
    assert error.value.effect == "none"
    assert isinstance(error.value.__cause__, BlockingIOError)
    assert native["closes"] == 0
    assert store._path(request.binding_key).read_bytes() == original
    assert owner_path.read_bytes() == original_owner


def test_retirement_normalizes_store_lock_factory_contention(reserved_creation, monkeypatch):
    service, store, owner_path, request, args, native, original, original_owner = reserved_creation
    failure = BlockingIOError("lock factory")

    def fail_lock(_key):
        raise failure

    monkeypatch.setattr(store, "lock", fail_lock)
    with pytest.raises(PaneError) as error:
        service.retire_creation(request, **args)
    assert error.value.code == "pane_busy"
    assert error.value.effect == "none"
    assert error.value.__cause__ is failure
    assert native["closes"] == 0
    assert store._path(request.binding_key).read_bytes() == original
    assert owner_path.read_bytes() == original_owner


def test_raw_store_lock_does_not_normalize_operation_body_error(reserved_creation, monkeypatch):
    service, _store, _owner_path, request, args, native, _original, _original_owner = reserved_creation
    store = RawFlockPaneStore(service.state_dir)
    service.store = store
    failure = BlockingIOError("operation body")

    def fail_get(_key):
        raise failure

    monkeypatch.setattr(store, "get", fail_get)
    with pytest.raises(BlockingIOError) as error:
        service.retire_creation(request, **args)
    assert error.value is failure
    assert native["closes"] == 0
    with store.lock(request.binding_key):
        pass


def test_raw_store_lock_does_not_normalize_cleanup_error_after_close(reserved_creation):
    service, _store, _owner_path, request, args, native, _original, _original_owner = reserved_creation
    failure = BlockingIOError("lock cleanup")

    class CleanupFailureStore(RawFlockPaneStore):
        @contextmanager
        def lock(self, key):
            with super().lock(key):
                yield
            raise failure

    service.store = CleanupFailureStore(service.state_dir)
    with pytest.raises(BlockingIOError) as error:
        service.retire_creation(request, **args)
    assert error.value is failure
    assert native["closes"] == 1


def test_completed_retirement_allows_new_generation(reserved_creation, monkeypatch):
    service, store, owner_path, request, args, native, _original, _original_owner = reserved_creation
    assert service.retire_creation(request, **args)["state"] == "retired"
    adapter = service.adapters["herdr"]
    binding = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="new-child",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, terminal_id="new-terminal", tab_id="tab",
        pane_tty="/dev/pts/2", shell_pid=456, shell_path="/bin/bash",
        machine_generation="boot", cwd=request.cwd, state="ready",
    )

    def create(_request, *, retirement_proof):
        result = backends.herdr_layout.ensure_owned_pane(
            request.endpoint, request.workspace_id, request.parent_id, request.cwd,
            request.binding_key[:32], state_dir=service.state_dir, ratio=1 / 3,
            purpose="managed_ops", owner_metadata=backends._managed_owner_metadata(request),
            retirement_proof=retirement_proof,
        )
        assert result["pane"]["pane_id"] == binding.pane_id
        return binding

    monkeypatch.setattr(adapter, "create", create)
    monkeypatch.setattr(adapter, "inspect", lambda _request: PaneStatus(
        "ready", "herdr", request.binding_key, request.name, binding=binding,
    ))
    result = service.ensure(request)
    assert result.state == "ready" and result.binding.pane_id == "new-child"
    assert native["closes"] == 1
    assert json.loads(owner_path.read_text(encoding="utf-8"))["retired_creation"]["state"] == "retired"
    assert store.get(request.binding_key)["retirement"]["state"] == "retired"


def test_changed_shell_generation_fences_without_close(reserved_creation, monkeypatch):
    service, _store, _owner_path, request, args, native, _original, _original_owner = reserved_creation
    adapter = service.adapters["herdr"]
    observations = iter([{"shell_pid": 123, "shell_starttime": 1},
                         {"shell_pid": 123, "shell_starttime": 2}])
    monkeypatch.setattr(adapter, "_retirement_observation", lambda *_: next(observations))
    assert service.retire_creation(request, **args)["state"] == "unknown"
    assert service.retire_creation(request, **args)["state"] == "unknown"
    assert native["closes"] == 0
    assert service.ensure(request).state == "unknown"


def test_reservation_change_during_qualification_refuses_before_fence(reserved_creation, monkeypatch):
    service, store, _owner_path, request, args, native, _original, _original_owner = reserved_creation
    adapter = service.adapters["herdr"]

    def change_reservation(*_):
        record = store.get(request.binding_key)
        record["operation"] = {"state": "unknown"}
        store.put(request.binding_key, record)
        return {"shell_pid": 123, "shell_starttime": 1}

    monkeypatch.setattr(adapter, "_retirement_observation", change_reservation)
    with pytest.raises(PaneError) as error:
        service.retire_creation(request, **args)
    assert error.value.code == "retirement_reservation_mismatch"
    assert native["closes"] == 0
    assert "retirement" not in store.get(request.binding_key)


def test_retirement_refuses_other_backends_before_native_work(reserved_creation):
    service, _store, _owner_path, request, args, native, _original, _original_owner = reserved_creation
    tmux = PaneRequest("tmux", request.endpoint, request.repository, request.controller_id,
                       request.parent_id, request.cwd, session_id="session")
    with pytest.raises(PaneError) as error:
        service.retire_creation(tmux, **args)
    assert error.value.code == "retirement_backend_unsupported"
    assert native["closes"] == 0


def test_retirement_refuses_changed_archive_without_replay(reserved_creation):
    service, store, owner_path, request, args, native, _original, _original_owner = reserved_creation
    native["lost"] = True
    assert service.retire_creation(request, **args)["state"] == "unknown"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    archive = owner_path.parent.parent / owner["retirement"]["owner_archive"]
    archive.write_text("{}", encoding="utf-8")
    result = service.reconcile_creation_retirement(request, args["retirement_id"])
    assert result["state"] == "unknown"
    assert store.get(request.binding_key)["retirement"]["state"] == "unknown"
    assert native["closes"] == 1
