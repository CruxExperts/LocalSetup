"""Focused regressions for native tmux canvas-v3 placement primitives."""

from __future__ import annotations

import pytest

from ls.core.terminal_ops.command_backend import TmuxCommandBackend, _family_width
from ls.core.terminal_ops.command_native_backend import (
    NativeCanvasV3, _history_retention_report, native_control_bindings,
)
from ls.core.terminal_ops.command_state import ShellCommandError


def _pane_row(
    window_id: str, index: int, pane_id: str, role: str, *, height: int,
    active: bool, dead: bool, operation_id: str = "", request: str = "",
    y: int = 0, in_mode: bool = False,
) -> str:
    fields = [
        window_id, str(index), pane_id, "2001", "/dev/pts/1",
        "1" if dead else "0", "1" if active else "0", "1",
        "120", str(height), "1" if in_mode else "0", "copy-mode" if in_mode else "",
        "0", "1000", "0", "0",
        "0", str(y), role, "1", "canvas-handle", operation_id, request,
        "placeholder" if role == "placeholder" else "operation" if role == "operation" else "root",
    ]
    return "P\t" + "\t".join(fields)


def _inspect_output(*, copy_mode: bool = False) -> str:
    request = "a" * 64
    return "\n".join([
        "S\t$0\tsmoke\t1\t0\t1\tcanvas-handle\tcanvas-key\tmachine\towner\tprofile\t@0\tcanvas-v3\t1",
        "L\t$0\tsmoke",
        "W\t@0\tstatus\t1\t2\t120\t24\tstatus-layout\t0\tstatus\t1\tcanvas-handle",
        "W\t@1\tparked\t0\t1\t120\t21\tparked-layout\t0\tparking\t1\tcanvas-handle",
        _pane_row("@0", 1, "%0", "status", height=1, active=False, dead=False, y=23),
        _pane_row("@0", 0, "%1", "operation", height=21, active=True, dead=False,
                  operation_id="op", request=request, y=1, in_mode=copy_mode),
        _pane_row("@1", 0, "%2", "placeholder", height=21, active=True, dead=True,
                  operation_id="op", request=request),
    ])


class _ReadOnlyTmux:
    def __init__(self, *, copy_mode: bool = False) -> None:
        self.tmux_binary = "/usr/bin/tmux"
        self.config_file = "/dev/null"
        self.mutations = 0
        self.copy_mode = copy_mode

    @staticmethod
    def _endpoint_state(_endpoint: str) -> str:
        return "live"

    @staticmethod
    def _private_target(_session_id: str) -> str:
        return "$0"

    def _checked(self, _endpoint: str, _args: list[str], *, mutation: bool = False,
                 allow_create: bool = False) -> str:
        if mutation:
            self.mutations += 1
        return _inspect_output(copy_mode=self.copy_mode)


@pytest.mark.parametrize("helper_live,expected", [(False, "not_open"), (True, "pending")])
def test_overlay_cleanup_does_not_touch_a_reused_client_pid(monkeypatch, helper_live, expected) -> None:
    from ls.core.terminal_ops import command_native_backend as native_module

    class Backend:
        def _clients(self, record):
            return [{"pid": 71, "name": "/dev/pts/99", "flags": {"ignore-size"}}]

        def _checked(self, *args, **kwargs):
            raise AssertionError("A different client generation must not receive native commands.")

    record = {"endpoint": "/private/test.sock", "native_layout": {"overlay_lock": {
        "client_pid": 71, "client_starttime": 10, "client_tty_nr": 7,
    }}}
    monkeypatch.setattr(native_module, "process_info", lambda pid:
                        {"pid": 71, "starttime": 11, "tty_nr": 7} if pid == 71 else
                        {"pid": 81, "starttime": 20} if helper_live else None)
    monkeypatch.setattr(native_module, "process_state", lambda pid: "R")
    result = NativeCanvasV3(Backend()).dismiss_overlay(record, 71, 81, 20)
    assert result["state"] == expected
    assert result["effect_certainty"] == "none"


def _record() -> dict[str, object]:
    return {
        "endpoint": "/tmp/native-canvas-test.sock",
        "native_session_id": "$0", "layout_mode": "canvas-v3",
        "handle": "canvas-handle", "session_key": "canvas-key",
        "machine_generation": "machine", "owner_fingerprint": "owner",
        "settings_digest": "profile", "status_window_id": "@0",
        "status_pane_id": "%0", "canvas_width": 120,
        "primary_viewport_height": 24,
        "native_layout": {"desired_generation": 1},
    }


def test_quick_native_clicks_keep_the_same_qualified_down_route() -> None:
    record = _record() | {"session_record_path": "/tmp/native-canvas-session/session.json"}
    bindings = native_control_bindings(record)
    for suffix in ("Status", "Pane", "Border"):
        down = bindings["MouseDown1" + suffix]
        assert bindings["DoubleClick1" + suffix] == down
        assert bindings["TripleClick1" + suffix] == down
    assert "--mouse-x" not in bindings["MouseDown1Border"][2]
    assert "--mouse-y" not in bindings["MouseDown1Border"][2]


def _plan() -> dict[str, object]:
    return {
        "version": 1, "generation": 1, "mode": "stable",
        "viewport": {"columns": 120, "rows": 24},
        "anchor_rows": 1, "divider_rows": 1,
        "selected_operation_id": "op", "page": 0,
        "assignments": [{"operation_id": "op", "rows": 21,
                         "order": 0, "visible": True}],
    }


def test_native_inspection_keeps_window_and_pane_role_tags_distinct() -> None:
    native = NativeCanvasV3(_ReadOnlyTmux())

    observed = native.inspect(_record())

    assert observed["state"] == "ready"
    assert observed["identity"]["handle"] == "canvas-handle"
    windows = observed["inventory"]["windows"]
    assert [(window["role"], window["panes"][0]["role"])
            for window in windows] == [("status", "status"), ("parking", "placeholder")]
    operation = observed["windows"][0]["panes"][1]
    assert operation["tagged"] is True
    assert operation["pane_id"] == "%1"
    assert operation["operation_id"] == "op"


def test_native_layout_noop_performs_zero_tmux_mutations() -> None:
    backend = _ReadOnlyTmux()
    native = NativeCanvasV3(backend)
    observed = native.inspect(_record())

    result = native.apply(_record(), _plan(), observed["inventory"])

    assert result["state"] == "unchanged"
    assert result["applied_generation"] == 1
    assert backend.mutations == 0


def test_native_layout_rejects_stale_inventory_before_mutating() -> None:
    backend = _ReadOnlyTmux()
    native = NativeCanvasV3(backend)

    with pytest.raises(ShellCommandError, match="inventory changed"):
        native.apply(_record(), _plan(), {})

    assert backend.mutations == 0


def test_copy_mode_freezes_layout_without_losing_identity() -> None:
    backend = _ReadOnlyTmux(copy_mode=True)
    native = NativeCanvasV3(backend)
    observed = native.inspect(_record())

    assert observed["state"] == "ready"
    assert observed["controls"]["copy_mode"] is True
    assert observed["controls"]["panes"][0]["pane_id"] == "%1"
    assert native.apply(_record(), _plan(), observed["inventory"])["state"] == "frozen"
    assert backend.mutations == 0


def test_native_menu_client_requires_exact_interactive_primary() -> None:
    backend = _ReadOnlyTmux()
    backend._clients = lambda _record: [
        {"pid": 42, "name": "/dev/pts/42", "flags": {"ignore-size"}},
    ]
    native = NativeCanvasV3(backend)

    assert native._client(_record(), 42)["name"] == "/dev/pts/42"
    backend._clients = lambda _record: [
        {"pid": 42, "name": "/dev/pts/42", "flags": {"ignore-size", "read-only"}},
    ]
    with pytest.raises(ShellCommandError, match="primary control client"):
        native._client(_record(), 42)


def test_history_counter_changes_never_claim_exact_lost_rows() -> None:
    old = {"pane_id": "%1", "history_size": 100, "history_limit": 100,
           "history_bytes": 4000, "width": 120, "height": 20}
    shrunk = {**old, "history_size": 100, "width": 80, "height": 15}
    pressured = _history_retention_report({"a": old}, {"a": shrunk})
    assert pressured["state"] == "limit" and pressured["lost_rows"] is None
    grown = _history_retention_report({"a": old},
                                     {"a": {**old, "history_size": 80,
                                            "width": 500, "height": 40}})
    assert grown["state"] == "reflow" and grown["lost_rows"] is None
    offset = _history_retention_report(
        {"a": old, "b": {**old, "pane_id": "%2", "history_size": 20}},
        {"a": shrunk, "b": {**old, "pane_id": "%2", "history_size": 50}},
    )
    assert offset["state"] == "limit" and offset["lost_rows"] is None


def test_canvas_v3_width_accepts_the_qualified_500_column_limit() -> None:
    record = _record() | {"canvas_width": 500}
    assert _family_width(record) == 500

    with pytest.raises(ShellCommandError):
        _family_width(record | {"canvas_width": 501})
