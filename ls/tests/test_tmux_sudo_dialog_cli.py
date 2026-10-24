"""CLI routing for operation-bound sudo dialogs, without a live pane."""

from __future__ import annotations

import json
import sys
import types
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from ls.core.terminal_ops import PaneError
from ls.core.terminal_ops import runtime
from ls.core.tmux_ops import cli, managed


def _invoke(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str):
    monkeypatch.setattr(sys, "argv", ["tmux_ops", "run", "-t", "%1", *args])
    code = cli.main()
    return code, json.loads(capsys.readouterr().out)


def test_dialog_flags_bind_owner_and_forward_context(monkeypatch, capsys) -> None:
    owner = {"pid": 123, "starttime": 456}
    seen = {}

    def binding(**kwargs):
        seen["binding"] = kwargs
        return owner

    class Context:
        def __init__(self, handle, *, owner, state_root):
            seen["context"] = (handle, owner, state_root)

    monkeypatch.setattr(cli, "make_owner_binding", binding)
    monkeypatch.setitem(sys.modules, "ls.core.terminal_ops.sudo_dialog",
                        types.SimpleNamespace(SudoDialogContext=Context))
    monkeypatch.setattr(managed, "run", lambda target, argv, **kwargs: seen.update(
        target=target, argv=argv, kwargs=kwargs) or {"state": "accepted"})

    code, result = _invoke(monkeypatch, capsys, "--dialog-handle", "lsshell1.test",
                           "--dialog-state-root", "/private/state", "--owner-pid", "123",
                           "--owner-starttime", "456", "--agent-id", "controller",
                           "--single-password-qualified", "--", "sudo", "id")

    assert code == 0 and result == {"state": "accepted"}
    assert seen["binding"] == {"owner_pid": 123, "owner_starttime": 456, "agent_id": "controller"}
    assert seen["context"] == ("lsshell1.test", owner, "/private/state")
    assert seen["target"] == "%1" and seen["argv"] == ["sudo", "id"]
    assert isinstance(seen["kwargs"]["dialog_context"], Context)
    assert seen["kwargs"]["single_password_qualified"] is True


def test_dialog_owner_flags_require_handle_and_legacy_rejects_dialog(monkeypatch, capsys) -> None:
    monkeypatch.setattr(managed, "run", lambda *_args, **_kwargs: pytest.fail("must reject before dispatch"))
    code, result = _invoke(monkeypatch, capsys, "--owner-pid", "123", "--", "sudo", "id")
    assert code == 1 and result["error"] == "dialog_handle_required"
    code, result = _invoke(monkeypatch, capsys, "--legacy-session", "--single-password-qualified",
                           "--", "sudo", "id")
    assert code == 1 and result["error"] == "legacy_dialog_unsupported"


def test_unadorned_managed_run_keeps_existing_call_shape(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(managed, "run", lambda _target, _argv, **kwargs: seen.update(kwargs) or {"state": "ok"})
    code, result = _invoke(monkeypatch, capsys, "--", "sudo", "id")
    assert code == 0 and result["state"] == "ok"
    assert set(seen) == {"timeout", "authentication_timeout", "state_dir"}


def test_managed_requires_sudo_and_forwards_only_requested_options(monkeypatch) -> None:
    with pytest.raises(PaneError, match="require direct sudo argv"):
        managed.run("%1", ["id"], timeout=10, single_password_qualified=True)

    request = SimpleNamespace()
    service = SimpleNamespace(inspect=lambda _request: SimpleNamespace(
        state="ready", binding=SimpleNamespace(pane_id="%1")))
    monkeypatch.setattr(managed, "_selected", lambda *_args: (None, service, request))
    monkeypatch.setattr(managed, "_owner_matches", lambda _request: True)
    seen = {}
    monkeypatch.setattr(managed, "run_visible_sudo", lambda *_args, **kwargs: seen.update(kwargs) or {"state": "ok"})
    managed.run("%1", ["sudo", "id"], timeout=10)
    assert "dialog_context" not in seen and "single_password_qualified" not in seen
    context = object()
    managed.run("%1", ["sudo", "id"], timeout=10, dialog_context=context,
                single_password_qualified=True)
    assert seen["dialog_context"] is context and seen["single_password_qualified"] is True


def test_unknown_sudo_launch_keeps_reconciliation_handle_and_receipt(tmp_path, monkeypatch) -> None:
    request = SimpleNamespace(binding_key="a" * 64, repository="fixture", host_alias=None)
    operation_id = "b" * 32
    directory = tmp_path / "terminal_ops" / request.binding_key
    directory.mkdir(parents=True)
    receipt = {"status": "unknown", "detail": "sudo start receipt could not be verified after launch"}
    (directory / f"{operation_id}.sudo.json").write_text(json.dumps(receipt))
    service = SimpleNamespace(
        state_dir=tmp_path,
        inspect=lambda _: SimpleNamespace(state="ready", binding=SimpleNamespace(shell_pid=123, pane_id="%1")),
        transition=lambda *_: SimpleNamespace(state="busy"),
        reconcile=lambda *_: SimpleNamespace(to_dict=lambda: {"state": "unknown"}),
    )
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: SimpleNamespace(hex=operation_id))
    monkeypatch.setattr(runtime, "_credential_server", lambda *_args, **_kwargs: nullcontext("/private/c.sock"))
    monkeypatch.setattr(runtime, "_read_operation", lambda *_: (directory, {"state": "unknown"}))

    result = runtime.run_visible_sudo(service, request, ["id"], timeout=1, validate_current=lambda: True)

    assert result["status"] == "unknown"
    assert result["status_handle"] == f"lsop1.{request.binding_key}.{operation_id}"
    assert result["sudo"] == receipt
