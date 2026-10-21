"""Stable, JSON-safe models for managed terminal panes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Literal


_REPOSITORY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_HOST_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PaneError(RuntimeError):
    """A safe managed-pane error with an explicit mutation outcome."""

    def __init__(self, code: str, message: str, *, effect: Literal["none", "unknown", "applied"] = "none") -> None:
        self.code = code
        self.message = message
        self.effect = effect
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "effect": self.effect}


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        raise PaneError("invalid_request", f"{field_name} must be non-empty single-line text.")
    return value


@dataclass(frozen=True, slots=True)
class PaneRequest:
    """One explicit repository/controller/parent binding for a managed pane."""

    backend: Literal["herdr", "tmux"]
    endpoint: str
    repository: str
    controller_id: str
    parent_id: str
    cwd: str
    host_alias: str | None = None
    workspace_id: str | None = None
    session_id: str | None = None
    machine_id: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in {"herdr", "tmux"}:
            raise PaneError("invalid_backend", "Choose an explicit supported pane backend.")
        if not isinstance(self.endpoint, str) or not self.endpoint or _CONTROL.search(self.endpoint):
            raise PaneError("invalid_endpoint", "The native endpoint must be explicit single-line text.")
        if not _REPOSITORY.fullmatch(self.repository or ""):
            raise PaneError("invalid_repository", "The canonical repository name must be a lowercase slug.")
        _required_text(self.controller_id, "controller identity")
        _required_text(self.parent_id, "parent pane ID")
        if not isinstance(self.cwd, str) or not self.cwd.startswith("/") or _CONTROL.search(self.cwd):
            raise PaneError("invalid_cwd", "The working directory must be an absolute local path.")
        if self.host_alias is not None and not _HOST_ALIAS.fullmatch(self.host_alias):
            raise PaneError("invalid_host_alias", "The host alias must be the exact admitted single-line alias.")
        for value, label in ((self.workspace_id, "workspace ID"), (self.session_id, "session ID"), (self.machine_id, "machine ID")):
            if value is not None:
                _required_text(value, label)
        if self.backend == "herdr" and not self.workspace_id:
            raise PaneError("workspace_required", "A Herdr pane requires an explicit workspace ID.")
        if self.backend == "tmux" and not self.session_id:
            raise PaneError("session_required", "A tmux pane requires an explicit session ID.")

    @property
    def name(self) -> str:
        suffix = f"-{self.host_alias}" if self.host_alias is not None else ""
        return f"{self.repository}{suffix}-sudo"

    @property
    def binding_key(self) -> str:
        identity = {
            "backend": self.backend,
            "endpoint": self.endpoint,
            "parent_id": self.parent_id,
            "repository": self.repository,
            "controller_id": self.controller_id,
            "host_alias": self.host_alias,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "machine_id": self.machine_id,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"name": self.name, "binding_key": self.binding_key}


@dataclass(frozen=True, slots=True)
class PaneBinding:
    """Verified native pane identity and its full managed scope."""

    name: str
    binding_key: str
    backend: Literal["herdr", "tmux"]
    endpoint: str
    parent_id: str
    pane_id: str
    repository: str
    controller_id: str
    host_alias: str | None = None
    workspace_id: str | None = None
    session_id: str | None = None
    tab_id: str | None = None
    terminal_id: str | None = None
    pane_tty: str | None = None
    shell_pid: int | None = None
    shell_path: str | None = None
    machine_generation: str | None = None
    cwd: str | None = None
    state: Literal["ready", "busy", "unknown", "missing"] = "ready"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PaneBinding:
        try:
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise PaneError("invalid_binding", "Stored pane binding is incomplete or malformed.") from exc


@dataclass(frozen=True, slots=True)
class PaneStatus:
    """JSON-safe result returned by all lifecycle operations."""

    state: Literal["missing", "ready", "busy", "unknown"]
    backend: Literal["herdr", "tmux"]
    binding_key: str
    name: str
    binding: PaneBinding | None = None
    operation_id: str | None = None
    detail: str | None = None
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": 1,
            "state": self.state,
            "backend": self.backend,
            "binding_key": self.binding_key,
            "name": self.name,
            "binding": self.binding.to_dict() if self.binding else None,
            "operation_id": self.operation_id,
            "detail": self.detail,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class PaneTransition:
    """A single-use argv transition; shell command strings are not accepted."""

    operation_id: str
    argv: tuple[str, ...]
    cwd: str | None = None
    kind: Literal["command", "cd"] = "command"
    requires_sudo_receipt: bool = False

    def __post_init__(self) -> None:
        if not _OPERATION_ID.fullmatch(self.operation_id or ""):
            raise PaneError("invalid_operation_id", "A stable operation ID is required.")
        if self.kind not in {"command", "cd"}:
            raise PaneError("invalid_transition", "The transition kind is not supported.")
        if (not isinstance(self.argv, tuple) or not self.argv or not isinstance(self.argv[0], str)
                or not self.argv[0] or any(not isinstance(arg, str) or "\x00" in arg for arg in self.argv)):
            raise PaneError("invalid_argv", "A transition requires a non-empty executable and NUL-free argv values.")
        if self.cwd is not None and (not self.cwd.startswith("/") or _CONTROL.search(self.cwd)):
            raise PaneError("invalid_cwd", "The transition working directory must be absolute single-line text.")
        if self.kind == "cd" and len(self.argv) != 1:
            raise PaneError("invalid_cd", "A directory transition accepts exactly one path argument.")
        if type(self.requires_sudo_receipt) is not bool:
            raise PaneError("invalid_transition", "The sudo receipt requirement must be a boolean.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"argv": list(self.argv)}


def json_safe(value: Any) -> Any:
    """Reject native values that cannot be serialized in an operation record."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PaneError("invalid_record", "Pane records must contain JSON-safe values.") from exc
    return value
