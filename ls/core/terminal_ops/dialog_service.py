"""Bind private form delivery to the existing owned native overlay lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .command_state import ShellCommandError, process_info, read_json, session_lock, validate_identifier
from .dialog_channel import ChannelClosed, DialogChannel
from .dialog_model import Form, Response


def native_dialog(service: Any, handle: str, *, operation_id: str, form: Form,
                  deadline: float, validate_current: Callable[[], bool],
                  deliver: Callable[[Response], bool], client_pid: int | None = None,
                  owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Called by trusted operation owners; submissions confer no new authority."""
    validate_identifier(operation_id, "operation_id")
    if not isinstance(form, Form) or not callable(validate_current) or not callable(deliver):
        raise ShellCommandError("invalid_dialog_request", "The dialog needs a form and its operation owner.")
    directory, initial, op_id = service._load(handle)
    if op_id is not None or initial.get("layout_mode") != "canvas-v3":
        raise ShellCommandError("native_dialog_session_required", "Dialogs require an owned native session.")
    _caller, role, _slug, _fingerprint = service._family_caller(initial, owner, require_root_live=True)
    if role != "root":
        raise ShellCommandError("native_dialog_root_required", "Only the controller can request private input.")
    service._authorize(initial, owner)
    if client_pid is None:
        pins, unknown = service._pins(directory, remove_dead=False)
        primary = [pin for pin in pins if pin.get("primary_viewport") is True]
        if unknown or len(primary) != 1:
            raise ShellCommandError("native_dialog_viewer_required", "One qualified primary viewer is required.")
        client_pid = primary[0].get("viewer_pid")
    client = process_info(client_pid)
    if client is None or not service._native_primary_client(directory, initial, client_pid):
        raise ShellCommandError("native_dialog_viewer_required", "The exact primary viewer is unavailable.")
    show = getattr(service.backend, "show_native_dialog", None)
    if not callable(show):
        raise ShellCommandError("native_dialog_unavailable", "This native backend cannot present private forms.")
    path = directory / "session.json"
    identity_keys = ("handle", "endpoint", "machine_generation", "owner_fingerprint", "native_session_id")
    binding = {key: initial.get(key) for key in identity_keys}
    channel: DialogChannel | None = None
    request_id = ""

    def current() -> bool:
        try:
            session = read_json(path, limit=2_000_000)
            current_client = process_info(client_pid)
            lock = session.get("native_layout", {}).get("overlay_lock")
            if (session.get("state") != "ready" or any(session.get(key) != value for key, value in binding.items())
                    or current_client is None or current_client["starttime"] != client["starttime"]
                    or current_client["tty_nr"] != client["tty_nr"]
                    or not isinstance(lock, Mapping) or lock.get("token") != request_id
                    or lock.get("client_pid") != client_pid
                    or not service._native_primary_client(directory, session, client_pid)):
                return False
            service._authorize(session, owner)
            return validate_current() is True
        except Exception:
            return False

    def deliver_once(response: Response) -> bool:
        # No session lock is held while waiting for a human. Serialize only the
        # final admission and consumer handoff with session revocation/layout.
        with session_lock(directory, blocking=True):
            if channel is None or not channel.current():
                return False
            return deliver(response) is True

    def present(session: Mapping[str, Any], on_spawn: Callable[[int, int], None]) -> Mapping[str, Any]:
        nonlocal channel, request_id
        request_id = session["native_layout"]["overlay_lock"]["token"]
        try:
            channel = DialogChannel(form, request_id=request_id, deadline=deadline,
                                    validate_current=current, deliver=deliver_once)
            channel.open()
        except (ValueError, OSError, ChannelClosed):
            if channel is not None:
                channel.close()
            raise ShellCommandError("native_dialog_unavailable", "The private request could not be admitted.", effect="none") from None
        try:
            return show(session, client_pid, channel, on_spawn=on_spawn)
        finally:
            channel.close()

    outcome = service._native_open_overlay(path, client_pid, "private_form", present)
    status = channel.status if channel is not None else "unavailable"
    return {
        "api_version": 1, "ok": status in {"delivered", "cancelled", "expired"} and outcome.get("cleanup_verified") is True,
        "state": status, "effect_certainty": "unknown" if status == "delivery_unknown" else "known" if status == "delivered" else "none",
        "handle": handle, "operation_id": operation_id, "request_id": request_id,
        "overlay_state": outcome.get("state"),
        "cleanup_pending": outcome.get("cleanup_verified") is not True or (channel.cleanup_pending if channel is not None else False),
    }
