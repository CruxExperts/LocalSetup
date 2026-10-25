"""CLI row width must not constrain the live native terminal canvas."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ls.core.terminal_ops import command_viewport as viewport
from ls.core.terminal_ops.command_state import atomic_json, read_json


def fixture(tmp_path, monkeypatch):
    pins = [{"pid": 10, "starttime": 1, "viewer_pid": 11, "created_at": 1,
             "pin_id": "a" * 32, "kind": "attachment", "primary_viewport": True},
            {"pid": 20, "starttime": 2, "viewer_pid": 21, "created_at": 2,
             "pin_id": "b" * 32, "kind": "attachment"}]
    processes = {10: {"starttime": 1}, 11: {"ppid": 10},
                 20: {"starttime": 2}, 21: {"ppid": 20}}
    clients = [{"pid": pid, "flags": {"ignore-size"}} for pid in (11, 21)]
    sizes = {11: (80, 24), 21: (180, 50)}
    monkeypatch.setattr(viewport, "process_info", lambda pid: processes.get(pid))
    unknown = [False]
    service = SimpleNamespace(_pins=lambda *_a, **_k: (pins, unknown[0]), backend=SimpleNamespace(
        _clients=lambda *_a: clients,
        observer_geometry=lambda _s, c: sizes[c["pid"]]))
    session = {"native_layout": {"columns": 80, "rows": 23, "desired_generation": 1,
                                "layout_state": "applied"},
               "runtime_settings": {"baseline": {"status_rows": 1}}}
    for pin in pins:
        atomic_json(tmp_path / "pins" / ("attach-" + pin["pin_id"] + ".json"), pin)
    return service, session, pins, sizes, clients, processes, unknown


def test_primary_resize_and_handoff_fill_remaining_terminal(tmp_path, monkeypatch):
    service, session, pins, sizes, *_ = fixture(tmp_path, monkeypatch)
    assert not viewport.reconcile_primary(service, tmp_path, session)
    sizes[11] = (140, 40)
    assert viewport.reconcile_primary(service, tmp_path, session)
    assert (session["canvas_width"], session["primary_viewport_height"]) == (140, 39)
    generation = session["native_layout"]["desired_generation"]
    assert not viewport.reconcile_primary(service, tmp_path, session)
    assert session["native_layout"]["desired_generation"] == generation
    pins.pop(0)  # Exact primary attachment has disconnected.
    assert viewport.reconcile_primary(service, tmp_path, session)
    assert read_json(tmp_path / "pins" / ("attach-" + "b" * 32 + ".json"))["primary_viewport"] is True
    assert (session["canvas_width"], session["primary_viewport_height"]) == (180, 49)
    assert session["native_layout"]["layout_state"] == "resize_pending"


@pytest.mark.parametrize("defect", ["unknown", "reused_pid", "foreign_child", "read_only", "two_primaries"])
def test_unqualified_viewers_cannot_take_viewport(tmp_path, monkeypatch, defect):
    service, session, pins, sizes, clients, processes, unknown = fixture(tmp_path, monkeypatch)
    sizes[11] = (160, 40)
    if defect == "unknown": unknown[0] = True
    if defect == "reused_pid": processes[10]["starttime"] = 99
    if defect == "foreign_child": processes[11]["ppid"] = 99
    if defect == "read_only": clients[0]["flags"].add("read-only")
    if defect == "two_primaries": pins[1]["primary_viewport"] = True
    before = deepcopy(session)
    assert not viewport.reconcile_primary(service, tmp_path, session)
    assert session == before


def test_narrow_secondary_and_narrow_primary_preserve_healthy_canvas(tmp_path, monkeypatch):
    service, session, pins, sizes, *_ = fixture(tmp_path, monkeypatch)
    sizes[11] = (180, 50)
    sizes[21] = (40, 10)
    assert viewport.reconcile_primary(service, tmp_path, session)
    before = deepcopy(session)
    assert not viewport.reconcile_primary(service, tmp_path, session)
    sizes[11] = (40, 10)
    assert not viewport.reconcile_primary(service, tmp_path, session)
    assert session == before
