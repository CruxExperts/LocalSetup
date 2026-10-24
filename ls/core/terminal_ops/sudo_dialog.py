"""Controller-owned association between one managed operation and its private UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .command_service import ShellCommandService
from .command_state import ShellCommandError
from .dialog_model import Field, Form, Response
from .models import PaneError


class SudoDialogContext:
    """Explicitly bind an existing family root; labels never select a session."""

    def __init__(self, handle: str, *, owner: Mapping[str, Any], state_root: str | None = None):
        self.service = ShellCommandService(state_root)
        self.handle, self.owner = handle, dict(owner)
        try:
            _, session, operation = self.service._load(handle)
            self.service._authorize(session, self.owner)
            if operation is not None or session.get("layout_mode") != "canvas-v3" or session.get("state") != "ready":
                raise ValueError("An owned native session is required.")
            if self.service._verify(session).get("state") != "ready":
                raise ValueError("The owned native session is unavailable.")
            self.identity = {key: session.get(key) for key in (
                "handle", "endpoint", "machine_generation", "owner_fingerprint", "native_session_id",
            )}
        except (ShellCommandError, OSError, ValueError, TypeError):
            raise PaneError("dialog_owner_unavailable", "The explicit private dialog session is not owned and ready.") from None

    def current(self) -> bool:
        try:
            _, session, operation = self.service._load(self.handle)
            self.service._authorize(session, self.owner)
            return (operation is None and session.get("state") == "ready"
                    and session.get("layout_mode") == "canvas-v3"
                    and all(session.get(key) == value for key, value in self.identity.items()))
        except Exception:
            return False

    def present(self, *, operation_id: str, target: str, deadline: float,
                validate_current: Callable[[], bool], deliver: Callable[[bytearray], bool]) -> dict[str, Any]:
        """Only the existing socket consumer sees the mutable password buffer."""
        form = Form("Sudo authentication", (Field("password", "Password", kind="password",
                                                  required=True, max_bytes=4096),),
                    description=f"Target: {target}\nAuthorized operation: {operation_id}")

        def current() -> bool:
            return self.current() and validate_current() is True

        def consume(response: Response) -> bool:
            value = response.values.get("password")
            return isinstance(value, bytearray) and current() and deliver(value) is True

        try:
            return self.service.dialog(self.handle, operation_id=operation_id, form=form,
                                       deadline=deadline, validate_current=current,
                                       deliver=consume, owner=self.owner)
        except ShellCommandError as exc:
            unknown = exc.effect == "unknown"
            return {"state": "delivery_unknown" if unknown else "unavailable",
                    "effect_certainty": "unknown" if unknown else "none", "cleanup_pending": unknown}
        except (OSError, ValueError, TypeError):
            return {"state": "delivery_unknown", "effect_certainty": "unknown", "cleanup_pending": True}
