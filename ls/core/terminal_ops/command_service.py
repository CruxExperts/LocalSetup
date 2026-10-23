"""Owned, durable command sessions for observable tmux-backed shell work."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from ..application_profiles.service import ApplicationProfileError, validate_profile
from .command_backend import TmuxCommandBackend
from .command_state import (
    ShellCommandError, atomic_json, make_owner_binding, owner_fingerprint,
    machine_generation, owner_is_current, owner_liveness, private_root,
    private_root_path, process_info, process_state, read_json,
    session_lock, log_lock, validate_identifier,
)
API_VERSION = 1
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_SESSION_HANDLE = re.compile(r"^lsshell1\.([a-f0-9]{24})\.([a-f0-9]{32})$")
_OP_HANDLE = re.compile(r"^lsopcmd1\.([a-f0-9]{24})\.([a-f0-9]{32})\.([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
_SETTING_KEYS = {
    "mouse", "history_limit", "status_position", "status", "preserve_theme",
    "default_lines", "max_response_bytes", "log_max_bytes",
    "completed_log_retention_days", "idle_grace_seconds",
    "collector_interval_seconds", "observer_pins",
}


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _profile_digest(settings: Mapping[str, Any], profile_name: str = "agent-shell") -> str:
    return _digest({
        "schema_version": 1, "application": "tmux", "name": profile_name,
        "settings": dict(settings),
    })


def _settings(
    value: Mapping[str, Any], *, source_root: Path, profile_name: str,
    profile_base: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SETTING_KEYS:
        raise ShellCommandError("invalid_profile_settings", "A fully resolved tmux agent-shell profile is required.")
    try:
        options = {"base_profile": profile_base} if profile_base is not None else {}
        validated = validate_profile("tmux", profile_name, dict(value), source_root=source_root,
                                     **options)
    except ApplicationProfileError as exc:
        raise ShellCommandError("invalid_profile_settings", "The resolved profile failed its registered schema.") from exc
    result = validated["settings"]
    if set(result) != _SETTING_KEYS:
        raise ShellCommandError("invalid_profile_settings", "The resolved profile is missing required shell settings.")
    return result


def _owner(owner: Mapping[str, Any] | None) -> dict[str, Any]:
    if owner is None:
        return make_owner_binding()
    if not isinstance(owner, Mapping):
        raise ShellCommandError("owner_identity_invalid", "The shell owner binding is malformed.")
    return make_owner_binding(owner_pid=owner.get("pid"), owner_starttime=owner.get("starttime"),
                              agent_id=owner.get("agent_id"))


def _scope(owner: Mapping[str, Any]) -> str:
    identity = {
        "machine_generation": owner["machine_generation"], "host": owner["host"],
        "uid": owner["uid"], "agent_id": owner.get("agent_id"),
    }
    if owner.get("agent_id") is None:
        identity.update(pid=owner["pid"], starttime=owner["starttime"])
    return _digest(identity)[:24]


def _reply(state: str, *, ok: bool = True, effect: str = "none", **fields: Any) -> dict[str, Any]:
    return {"api_version": API_VERSION, "ok": ok, "state": state,
            "effect_certainty": effect, **fields}


def _decode_cursor_output(raw: bytes, maximum: int, *, final: bool) -> tuple[str, int]:
    """Bound UTF-8 output while advancing only past bytes represented in it."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pieces: list[str] = []
    output_bytes = 0
    consumed = 0
    for offset, value in enumerate(raw, 1):
        emitted = decoder.decode(bytes((value,)))
        if emitted:
            size = len(emitted.encode("utf-8"))
            if output_bytes + size > maximum:
                break
            pieces.append(emitted)
            output_bytes += size
            consumed = offset
    else:
        trailing = decoder.decode(b"", final=final)
        if trailing:
            size = len(trailing.encode("utf-8"))
            if output_bytes + size <= maximum:
                pieces.append(trailing)
                consumed = len(raw)
        elif not decoder.getstate()[0]:
            consumed = len(raw)
    return "".join(pieces), consumed


def _decode_tail_output(raw: bytes, maximum: int) -> tuple[str, bool]:
    """Keep the newest complete UTF-8 characters from an explicit tail read."""
    decoded = raw.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    if len(encoded) <= maximum:
        return decoded, False
    return encoded[-maximum:].decode("utf-8", errors="ignore"), True


class ShellCommandService:
    """Own one isolated tmux server and command lifecycle per verified agent."""

    def __init__(
        self,
        state_root: str | os.PathLike[str] | None = None,
        *,
        backend: Any | None = None,
        source_root: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_root = state_root
        self.root = private_root_path(state_root) / "agent-shells"
        self.backend = backend or TmuxCommandBackend()
        self.source_root = Path(source_root).resolve() if source_root is not None else Path(__file__).resolve().parents[3]
        self.clock = clock

    def _prepare_root(self) -> None:
        private_root(self.state_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ShellCommandError("unsafe_state_root", "Agent shell state must be owner-private.")
        self.root.chmod(0o700)

    def capabilities(self) -> dict[str, Any]:
        capability = self.backend.capability()
        return _reply("available" if capability.get("state") == "available" else "unavailable",
                      ok=capability.get("state") == "available", backend=capability)

    def _owners(self, *, create: bool = True) -> Path:
        path = self.root / "owners"
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        return path

    def _sessions(self, *, create: bool = True) -> Path:
        path = self.root / "sessions"
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        return path

    def _owner_dir(self, scope: str) -> Path:
        path = self._owners() / scope
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    def _session_dir(self, scope: str, session_id: str, *, create: bool = False) -> Path:
        scope_dir = self._sessions(create=create) / scope
        if create:
            scope_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            scope_dir.chmod(0o700)
        path = scope_dir / session_id
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        return path

    @staticmethod
    def _handle(scope: str, session_id: str) -> str:
        return f"lsshell1.{scope}.{session_id}"

    @staticmethod
    def _op_handle(scope: str, session_id: str, op_id: str) -> str:
        return f"lsopcmd1.{scope}.{session_id}.{op_id}"

    def _parse_handle(self, handle: str) -> tuple[str, str, str | None]:
        if not isinstance(handle, str):
            raise ShellCommandError("invalid_handle", "A shell session or operation handle is required.")
        match = _SESSION_HANDLE.fullmatch(handle)
        if match:
            return match.group(1), match.group(2), None
        match = _OP_HANDLE.fullmatch(handle)
        if match:
            return match.group(1), match.group(2), match.group(3)
        raise ShellCommandError("invalid_handle", "The shell handle is malformed.")

    def _load(self, handle: str) -> tuple[Path, dict[str, Any], str | None]:
        scope, sid, op_id = self._parse_handle(handle)
        directory = self._session_dir(scope, sid)
        if directory.is_symlink() or not directory.is_dir():
            raise ShellCommandError("session_missing", "The exact shell session record is unavailable.")
        record = read_json(directory / "session.json")
        if record.get("handle") != self._handle(scope, sid) or record.get("scope_key") != scope or record.get("session_id") != sid:
            raise ShellCommandError("session_identity_mismatch", "The handle does not match its private session record.", effect="unknown")
        if record.get("machine_generation") != machine_generation():
            raise ShellCommandError("wrong_machine", "This exact shell handle belongs to a different machine generation.")
        return directory, record, op_id

    def _authorize(self, record: Mapping[str, Any], supplied: Mapping[str, Any] | None) -> dict[str, Any]:
        caller = _owner(supplied)
        if (owner_fingerprint(caller) != record.get("owner_fingerprint")
                or not owner_is_current(record.get("owner", {}))):
            raise ShellCommandError("owner_identity_mismatch", "This live caller is not the exact shell session owner.")
        return caller

    def _verify(self, record: Mapping[str, Any]) -> dict[str, Any]:
        result = self.backend.inspect_session(record)
        if result.get("state") != "ready":
            return result
        opts = result.get("options", {})
        expected = {
            "api": "1", "handle": record["handle"], "key": record["session_key"],
            "machine": record["machine_generation"], "owner": record["owner_fingerprint"],
            "profile": record.get("settings_digest"), "status_window": record.get("status_window_id"),
        }
        if any(opts.get(k) != v for k, v in expected.items()):
            return {"state": "unknown", "windows": [], "reason": "native session identity mismatch"}
        return result

    def _remove_stale_native(self, record: Mapping[str, Any]) -> bool:
        remover = getattr(self.backend, "remove_stale_endpoint", None)
        if callable(remover):
            return bool(remover(record))
        return not Path(str(record.get("endpoint", ""))).exists()

    def _session_reply(self, record: Mapping[str, Any], *, created: bool, joined: bool) -> dict[str, Any]:
        handle = str(record["handle"])
        return _reply(
            str(record.get("state", "unknown")), effect=str(record.get("effect_certainty", "known")),
            handle=handle, session_handle=handle,
            machine={"host": record.get("host"), "generation": record.get("machine_generation")},
            created=created, joined=joined,
            native_session_name=record.get("server_session_name", record.get("native_name")),
            startup_message=("Created" if created else "Joined") + " owned shell session " +
                            str(record.get("server_session_name", record.get("native_name", "unknown"))) + ".",
            join_command=f"localsetup shell attach --handle {shlex.quote(handle)}",
            settings_digest=record.get("settings_digest"),
            profile_name=record.get("profile_name", "agent-shell"),
            profile_base=record.get("profile_base"),
            cleanup_state=record.get("cleanup_state", "none"),
            gc_at=record.get("gc_at"),
            previous_cleanup=record.get("previous_cleanup"),
            status_window_id=record.get("status_window_id"),
        )

    def ensure(
        self, *, cwd: str | os.PathLike[str], settings: Mapping[str, Any],
        settings_digest: str, owner: Mapping[str, Any] | None = None,
        profile_name: str = "agent-shell", profile_base: str | None = None,
    ) -> dict[str, Any]:
        capability = self.backend.capability()
        if capability.get("state") != "available":
            return _reply("unavailable", ok=False, backend=capability)
        resolved = _settings(settings, source_root=self.source_root, profile_name=profile_name,
                             profile_base=profile_base)
        if not isinstance(settings_digest, str) or not _HEX64.fullmatch(settings_digest):
            raise ShellCommandError("invalid_profile_digest", "A validated profile digest is required.")
        if _profile_digest(resolved, profile_name) != settings_digest:
            raise ShellCommandError("profile_digest_mismatch", "The profile digest does not match the resolved settings.")
        try:
            workdir = Path(cwd).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ShellCommandError("invalid_cwd", "The shell working directory is unavailable.") from exc
        if not workdir.is_dir():
            raise ShellCommandError("invalid_cwd", "The shell working directory must be a directory.")
        binding = _owner(owner)
        scope = _scope(binding)
        self._prepare_root()
        owner_dir = self._owner_dir(scope)
        with session_lock(owner_dir):
            owner_path = owner_dir / "owner.json"
            previous_cleanup: dict[str, Any] | None = None
            if owner_path.exists():
                prior = read_json(owner_path)
                old = prior.get("owner")
                if not isinstance(old, dict):
                    raise ShellCommandError("owner_identity_unknown", "The previous shell owner binding is incomplete.", effect="unknown")
                changed_owner = owner_fingerprint(old) != owner_fingerprint(binding)
                if changed_owner:
                    life = owner_liveness(old)
                    try:
                        same_scope = _scope(old) == scope
                    except (KeyError, TypeError):
                        same_scope = False
                    if life != "dead" or not same_scope:
                        raise ShellCommandError(
                            "recovery_required" if life == "dead" else "owner_identity_unknown",
                            "An earlier owner binding exists; explicit recovery is required before takeover.",
                            effect="unknown" if life == "unknown" else "none",
                        )
                old_handle = prior.get("active_handle")
                if not isinstance(old_handle, str):
                    raise ShellCommandError("owner_identity_unknown", "The prior active session handle is missing.", effect="unknown")
                try:
                    directory, record, old_op_id = self._load(old_handle)
                except ShellCommandError as exc:
                    raise ShellCommandError("owner_identity_unknown", "The prior session cannot be resolved.", effect="unknown") from exc
                if (old_op_id is not None or record.get("scope_key") != scope
                        or record.get("owner_fingerprint") != owner_fingerprint(old)
                        or record.get("handle") != old_handle):
                    raise ShellCommandError("owner_identity_unknown", "The prior session identity is inconsistent.", effect="unknown")
                if record.get("state") not in {"closed", "removed"}:
                    if changed_owner:
                        raise ShellCommandError("recovery_required", "The previous live session requires explicit adoption.")
                    else:
                        self._authorize(record, binding)
                        if record.get("settings_digest") != settings_digest:
                            raise ShellCommandError("profile_conflict", "The active session profile is frozen; apply it explicitly.")
                        if record.get("state") != "ready":
                            raise ShellCommandError("session_reconcile_required", "The owned session has uncertain lifecycle state.", effect="unknown")
                        if self._verify(record).get("state") != "ready":
                            raise ShellCommandError("session_identity_unknown", "The exact owned tmux session could not be verified.", effect="unknown")
                        with session_lock(directory):
                            record["last_activity_at"] = self.clock()
                            atomic_json(directory / "session.json", record)
                        return self._session_reply(record, created=False, joined=True)
                with session_lock(directory):
                    record = read_json(directory / "session.json")
                    if (record.get("handle") != old_handle or record.get("scope_key") != scope
                            or record.get("owner_fingerprint") != owner_fingerprint(old)):
                        raise ShellCommandError("owner_identity_unknown", "The prior session changed identity.", effect="unknown")
                    operations = self._strict_ops(directory, record)
                    pins, pin_unknown = self._pins(directory)
                    observed = self._verify(record)
                    cleanup_at = (record.get("gc_at") if type(record.get("gc_at")) in {int, float}
                                  else record.get("closed_at"))
                    if (record.get("state") not in {"closed", "removed"}
                            or record.get("cleanup_state") != "removed"
                            or not self._terminal_ops_collected(operations)
                            or pins or pin_unknown or observed.get("state") != "missing"
                            or not self._endpoint_absent(record)
                            or type(cleanup_at) not in {int, float}):
                        raise ShellCommandError("previous_cleanup_unknown", "The prior session cleanup cannot be proven.", effect="unknown")
                    previous_cleanup = {
                        "handle": old_handle, "state": record["state"],
                        "cleanup_state": "removed", "at": cleanup_at,
                        "source": "gc" if type(record.get("gc_at")) in {int, float} else "close",
                    }

            sid = uuid.uuid4().hex
            handle = self._handle(scope, sid)
            record: dict[str, Any] = {
                "schema_version": 1, "handle": handle, "scope_key": scope, "session_id": sid,
                "session_key": uuid.uuid4().hex, "native_name": f"ls-{scope[:8]}-{sid[:8]}",
                "endpoint": str(self.backend.socket_path(sid)),
                "machine_generation": binding["machine_generation"], "host": binding["host"],
                "uid": binding["uid"], "owner": binding, "owner_fingerprint": owner_fingerprint(binding),
                "settings": resolved, "settings_digest": settings_digest,
                "profile_name": profile_name, "profile_base": profile_base,
                "state": "creating", "status": "creating", "created_at": self.clock(),
                "last_activity_at": self.clock(), "last_activity_kind": "ensure",
                "previous_cleanup": previous_cleanup,
            }
            directory = self._session_dir(scope, sid, create=True)
            (directory / "operations").mkdir(mode=0o700)
            (directory / "pins").mkdir(mode=0o700)
            atomic_json(directory / "session.json", record, exclusive=True)
            atomic_json(owner_path, {"schema_version": 1, "scope_key": scope, "owner": binding,
                                     "owner_fingerprint": owner_fingerprint(binding),
                                     "active_handle": handle, "updated_at": self.clock()})
            try:
                native = self.backend.create_session(record, cwd=str(workdir), settings=resolved)
                record.update(native)
                record.update(state="ready", status="ready", effect_certainty="known",
                              last_activity_at=self.clock())
                atomic_json(directory / "session.json", record)
            except ShellCommandError as exc:
                record.update(state="unknown", status="unknown",
                              effect_certainty="unknown" if exc.effect == "unknown" else "known",
                              create_error=exc.code)
                atomic_json(directory / "session.json", record)
                raise ShellCommandError(exc.code, exc.message, effect=record["effect_certainty"]) from exc
            return self._session_reply(record, created=True, joined=False)

    def _ops(self, directory: Path) -> list[tuple[Path, dict[str, Any]]]:
        result = []
        try:
            paths = sorted((directory / "operations").glob("*.json"))
        except OSError:
            return result
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                operation = read_json(path, limit=2_000_000)
                if (isinstance(operation.get("operation_id"), str)
                        and isinstance(operation.get("operation_handle"), str)
                        and isinstance(operation.get("request_digest"), str)
                        and isinstance(operation.get("session_handle"), str)):
                    result.append((path, operation))
            except ShellCommandError:
                continue
        return result

    def _strict_ops(self, directory: Path, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Enumerate every operation receipt before any destructive lifecycle step."""
        operation_dir = directory / "operations"
        sidecars = (".payload.json", ".release.json", ".cursor.json",
                    ".drained.json", ".finalize.json")
        try:
            records = [path for path in operation_dir.iterdir()
                       if path.name.endswith(".json") and not path.name.endswith(sidecars)]
        except OSError as exc:
            raise ShellCommandError("operation_records_unknown", "Operation records cannot be enumerated safely.", effect="unknown") from exc
        operations: list[dict[str, Any]] = []
        for path in records:
            if path.is_symlink() or not path.is_file():
                raise ShellCommandError("operation_records_unknown", "An operation record is not a regular file.", effect="unknown")
            op = read_json(path, limit=2_000_000)
            op_id = op.get("operation_id")
            if (not isinstance(op_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", op_id)
                    or not _OP_HANDLE.fullmatch(str(op.get("operation_handle", "")))
                    or self._paths(directory, op_id)["record"] != path
                    or op.get("operation_handle") != self._op_handle(
                        str(session["scope_key"]), str(session["session_id"]), op_id)
                    or op.get("session_handle") != session.get("handle")
                    or op.get("session_key") != session.get("session_key")
                    or not isinstance(op.get("request_digest"), str)
                    or not _HEX64.fullmatch(op["request_digest"])):
                raise ShellCommandError("operation_records_unknown", "An operation record has uncertain identity.", effect="unknown")
            operations.append(op)
        return operations

    @staticmethod
    def _terminal_ops_collected(operations: Sequence[Mapping[str, Any]]) -> bool:
        return all(op.get("state") in {"completed", "failed", "collected"}
                   and op.get("process_scope") == "collected"
                   and op.get("output_drain") in {"available", "unavailable"}
                   for op in operations)

    @staticmethod
    def _endpoint_absent(session: Mapping[str, Any]) -> bool:
        try:
            Path(str(session["endpoint"])).lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _native_inventory(
        self, session: Mapping[str, Any], observed: Mapping[str, Any],
        operations: Sequence[Mapping[str, Any]], *, allow_active: bool = False,
    ) -> dict[str, Any]:
        """Reject unknown panes before adoption or deletion of a private server."""
        if observed.get("state") != "ready":
            raise ShellCommandError("native_inventory_unknown", "The private session inventory is unavailable.", effect="unknown")
        windows = observed.get("windows", [])
        status_windows = [window for window in windows
                          if window.get("window_id") == session.get("status_window_id")]
        if len(status_windows) != 1:
            raise ShellCommandError("native_inventory_unknown", "The status window identity changed.", effect="unknown")
        status = status_windows[0]
        status_panes = status.get("panes", [])
        if (status.get("metadata", {}).get("status") != "1"
                or status.get("metadata", {}).get("session") != session.get("handle")
                or status.get("pane_count") != 1 or len(status_panes) != 1
                or status_panes[0].get("pane_id") != session.get("status_pane_id")
                or status_panes[0].get("dead") is not True):
            raise ShellCommandError("native_inventory_unknown", "The status pane is no longer an inert owned pane.", effect="unknown")
        by_window: dict[str, Mapping[str, Any]] = {}
        for operation in operations:
            state = operation.get("state")
            if state in {"completed", "failed", "collected"}:
                if not self._terminal_ops_collected((operation,)):
                    raise ShellCommandError("operation_records_unknown", "A terminal command has uncollected work or output.", effect="unknown")
            elif allow_active and state in {"running", "draining"}:
                if (operation.get("effect_certainty") != "known"
                        or operation.get("process_scope") not in {"tracking", "draining", "collected"}
                        or (state == "running" and operation.get("process_scope") != "tracking")):
                    raise ShellCommandError("operation_records_unknown", "An active command lacks durable supervision proof.", effect="unknown")
            else:
                raise ShellCommandError("operation_records_unknown", "An active or uncertain command blocks lifecycle changes.", effect="unknown")
            window_id = operation.get("window_id")
            if window_id is not None:
                if not isinstance(window_id, str) or window_id in by_window:
                    raise ShellCommandError("operation_records_unknown", "Command window identity is ambiguous.", effect="unknown")
                by_window[window_id] = operation
        seen: set[str] = set()
        for window in windows:
            window_id = window.get("window_id")
            if window_id == session.get("status_window_id"):
                continue
            operation = by_window.get(window_id)
            panes = window.get("panes", [])
            if (operation is None or window_id in seen or window.get("pane_count") != 1
                    or len(panes) != 1 or panes[0].get("pane_id") != operation.get("pane_id")
                    or window.get("metadata", {}).get("session") != session.get("handle")
                    or window.get("metadata", {}).get("operation") != operation.get("operation_id")
                    or window.get("metadata", {}).get("request") != operation.get("request_digest")):
                raise ShellCommandError("native_inventory_unknown", "An unknown command pane occupies the private server.", effect="unknown")
            seen.add(str(window_id))
            pane = panes[0]
            runner_pid = operation.get("runner_pid")
            if type(runner_pid) is not int or pane.get("pane_pid") != runner_pid:
                raise ShellCommandError("native_inventory_unknown", "The command pane process identity changed.", effect="unknown")
            if operation.get("state") in {"running", "draining"} or not pane.get("dead"):
                runner = process_info(runner_pid)
                if (runner is None or runner.get("starttime") != operation.get("runner_starttime")
                        or pane.get("pane_pid") != runner_pid
                        or process_state(runner_pid) in {None, "Z", "X"}):
                    raise ShellCommandError("native_inventory_unknown", "The command runner no longer matches its pane.", effect="unknown")
        if allow_active and any(op.get("state") in {"running", "draining"}
                                and op.get("window_id") not in seen for op in operations):
            raise ShellCommandError("native_inventory_unknown", "An active command pane is missing.", effect="unknown")
        return TmuxCommandBackend.inventory_identity(observed)

    def _paths(self, directory: Path, op_id: str) -> dict[str, Path]:
        # Encode the complete identifier in a dot-free filename stem. This
        # prevents both dotted IDs and IDs ending in a sidecar suffix from
        # naming another operation's record or payload.
        stem = "op-" + base64.urlsafe_b64encode(op_id.encode("ascii")).decode("ascii").rstrip("=")
        base = directory / "operations" / stem
        def suffixed(suffix: str) -> Path:
            return base.with_name(base.name + suffix)
        return {
            "record": suffixed(".json"), "payload": suffixed(".payload.json"),
            "release": suffixed(".release.json"), "log": suffixed(".log"),
            "cursor": suffixed(".cursor.json"), "drained": suffixed(".drained.json"),
            "finalize": suffixed(".finalize.json"),
            "inputs": directory / "operations" / f"{stem}.inputs",
        }

    def _runner_command(self, paths: Mapping[str, Path]) -> list[str]:
        entries = [str(self.source_root), *(p for p in sys.path if p)]
        if os.environ.get("PYTHONPATH"):
            entries.append(os.environ["PYTHONPATH"])
        return ["env", "PYTHONPATH=" + os.pathsep.join(dict.fromkeys(entries)), sys.executable,
                "-m", "ls.core.terminal_ops.command_runner", "--operation", str(paths["record"]),
                "--payload", str(paths["payload"]), "--release", str(paths["release"]),
                "--drained", str(paths["drained"]), "--finalize", str(paths["finalize"])]

    def run(
        self, handle: str, operation_id: str, *, argv: Sequence[str] | None = None,
        script: str | None = None, cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None, stdin: bytes | str | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        op_id = validate_identifier(operation_id, "operation_id", maximum=128)
        if (argv is None) == (script is None):
            raise ShellCommandError("invalid_command", "Supply exactly one argv vector or explicit Bash script.")
        if argv is not None:
            if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv or len(argv) > 4096:
                raise ShellCommandError("invalid_argv", "The command argv must be a non-empty bounded string vector.")
            command: dict[str, Any] = {"argv": list(argv)}
            if any(not isinstance(a, str) or "\0" in a for a in command["argv"]):
                raise ShellCommandError("invalid_argv", "Every argv item must be a string without NUL characters.")
        else:
            if not isinstance(script, str) or "\0" in script or len(script.encode()) > 4_000_000:
                raise ShellCommandError("invalid_script", "The explicit Bash script must be bounded text without NUL.")
            command = {"script": script}
        try:
            workdir = (Path(cwd).expanduser() if cwd is not None else Path.cwd()).resolve(strict=True)
        except OSError as exc:
            raise ShellCommandError("invalid_cwd", "The command working directory is unavailable.") from exc
        if not workdir.is_dir():
            raise ShellCommandError("invalid_cwd", "The command working directory must be a directory.")
        explicit_env: dict[str, str] = {}
        if env is not None:
            if not isinstance(env, Mapping) or len(env) > 4096:
                raise ShellCommandError("invalid_env", "Command environment must be a bounded string map.")
            for key, value in env.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                        or not isinstance(value, str) or "\0" in value or key.endswith("_SUDO_KEY")):
                    raise ShellCommandError("invalid_env", "The environment contains an invalid or credential variable.")
                explicit_env[key] = value
        if isinstance(stdin, str):
            stdin_bytes = stdin.encode()
        elif stdin is None or isinstance(stdin, bytes):
            stdin_bytes = stdin
        else:
            raise ShellCommandError("invalid_stdin", "Command stdin must be bytes or text.")
        if stdin_bytes is not None and len(stdin_bytes) > 16 * 1024 * 1024:
            raise ShellCommandError("invalid_stdin", "Command stdin exceeds the supported size.")
        request = {**command, "cwd": str(workdir), "env": explicit_env,
                   "inherited_pythonpath": os.environ.get("PYTHONPATH"),
                   "stdin_b64": base64.b64encode(stdin_bytes).decode("ascii") if stdin_bytes is not None else None}
        if len(json.dumps(request, ensure_ascii=True).encode("utf-8")) > 15 * 1024 * 1024:
            raise ShellCommandError("invalid_payload", "The complete command request exceeds the supported size.")
        request_digest = _digest(request)
        legacy_request_digest = _digest({key: value for key, value in request.items()
                                         if key != "inherited_pythonpath"})
        directory, session, op_from_handle = self._load(handle)
        if op_from_handle is not None:
            raise ShellCommandError("session_handle_required", "Starting a command requires a session handle.")
        self._authorize(session, owner)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            self._authorize(session, owner)
            if session.get("state") != "ready" or self._verify(session).get("state") != "ready":
                raise ShellCommandError("session_not_ready", "The exact owned shell session is not ready.", effect="unknown")
            paths = self._paths(directory, op_id)
            if paths["record"].exists():
                old = read_json(paths["record"], limit=2_000_000)
                # Earlier receipts did not include inherited PYTHONPATH in the
                # request digest. Recognize that exact old request, but always
                # return its original receipt rather than dispatching again.
                if old.get("request_digest") not in {request_digest, legacy_request_digest}:
                    raise ShellCommandError("operation_id_conflict", "This operation ID already names different command data.")
                return self._operation_reply(session, self._reconcile(directory, session, old), directory)
            active = [op for _, op in self._ops(directory)
                      if op.get("state") not in {"completed", "failed", "collected"}]
            if active:
                raise ShellCommandError("operation_busy", "A command is active or has an uncertain outcome.", effect="unknown")
            paths["inputs"].mkdir(mode=0o700, exist_ok=True)
            op: dict[str, Any] = {
                "schema_version": 1, "operation_id": op_id,
                "operation_handle": self._op_handle(session["scope_key"], session["session_id"], op_id),
                "session_handle": session["handle"], "session_key": session["session_key"],
                "machine_generation": session["machine_generation"],
                "owner_fingerprint": session["owner_fingerprint"], "request_digest": request_digest,
                "native_window_name": f"op-{op_id[:65]}-{uuid.uuid4().hex[:8]}",
                "cwd": str(workdir), "state": "intent", "effect_certainty": "unknown",
                "created_at": self.clock(), "updated_at": self.clock(), "cleanup_state": "pending",
                "output_drain": "pending",
            }
            atomic_json(paths["record"], op, exclusive=True)
            atomic_json(paths["payload"], request, exclusive=True)
            try:
                native = self.backend.create_operation_window(session, op, command=self._runner_command(paths))
                op.update(native)
                self.backend.tag_operation_window(session, op, native)
                capture_command = [
                    "env", "PYTHONPATH=" + os.pathsep.join((str(self.source_root), *[p for p in sys.path if p])),
                    sys.executable, "-m", "ls.core.terminal_ops.command_capture",
                    "--log", str(paths["log"]), "--cursor", str(paths["cursor"]),
                    "--drained", str(paths["drained"]),
                    "--max-bytes", str(session["settings"]["log_max_bytes"]),
                ]
                self.backend.start_capture(session, op, command=capture_command)
                self.backend.select_operation(session, op)
                op.update(state="running", effect_certainty="known",
                          dispatch_ack_at=self.clock(), updated_at=self.clock())
                atomic_json(paths["record"], op)
                atomic_json(paths["release"], {"state": "release", "released_at": self.clock()}, exclusive=True)
                session.update(last_activity_at=self.clock(), last_activity_kind="run")
                atomic_json(directory / "session.json", session)
            except Exception as exc:
                op.update(state="unknown", effect_certainty="unknown",
                          dispatch_error=getattr(exc, "code", exc.__class__.__name__),
                          updated_at=self.clock())
                try:
                    atomic_json(paths["record"], op)
                except ShellCommandError:
                    pass
                if isinstance(exc, ShellCommandError):
                    raise ShellCommandError(exc.code, exc.message, effect="unknown") from exc
                raise ShellCommandError("native_ack_unknown", "Dispatch acknowledgement is uncertain; the command was not replayed.", effect="unknown") from exc
            return self._operation_reply(session, op, directory)

    def _operation_reply(
        self, session: Mapping[str, Any], operation: Mapping[str, Any], directory: Path,
        *, include_output: bool = True,
    ) -> dict[str, Any]:
        handle = str(operation.get("operation_handle") or self._op_handle(
            str(session["scope_key"]), str(session["session_id"]), str(operation["operation_id"])))
        session_join = f"localsetup shell attach --handle {shlex.quote(str(session['handle']))}"
        pane_available = (operation.get("state") in {"running", "draining"}
                          and operation.get("process_scope") != "collected"
                          and bool(operation.get("window_id")) and bool(operation.get("pane_id")))
        operation_join = f"localsetup shell attach --handle {shlex.quote(handle)}" if pane_available else None
        result = _reply(
            str(operation.get("state", "unknown")), effect=str(operation.get("effect_certainty", "unknown")),
            handle=handle, operation_handle=handle, session_handle=session["handle"],
            operation_id=operation["operation_id"], request_digest=operation.get("request_digest"),
            exit_code=operation.get("exit_code"), process_scope=operation.get("process_scope", "unknown"),
            output_drain=operation.get("output_drain", "pending"),
            cleanup_state=operation.get("cleanup_state", "pending"),
            logs_command=f"localsetup shell logs --handle {shlex.quote(handle)}",
            machine={"host": session.get("host"), "generation": session.get("machine_generation")},
            native_session_name=session.get("server_session_name", session.get("native_name")),
            join_command=operation_join or session_join,
            operation_join_command=operation_join, session_join_command=session_join,
            join_target="operation_pane" if pane_available else "session",
            pane_available=pane_available,
            window_id=operation.get("window_id"), pane_id=operation.get("pane_id"),
        )
        # Include a small recent output window in the normal run/status payload
        # so a caller does not need a second command merely to see the result.
        # logs() reads a bounded tail and never drains or mutates the operation.
        if not include_output:
            return result
        try:
            recent = self.logs(handle, lines=session["settings"]["default_lines"],
                               max_bytes=session["settings"]["max_response_bytes"])
            result.update(
                output=recent.get("output", ""),
                output_cursor=recent.get("cursor", 0),
                output_base_cursor=recent.get("base_cursor", 0),
                output_end_cursor=recent.get("end_cursor", 0),
                output_truncated=recent.get("truncated", False),
                output_gap=recent.get("gap", False),
                output_storage=recent.get("storage", False),
            )
        except (OSError, ShellCommandError):
            # Output storage is observational. A transient read failure must
            # not turn an already-recorded dispatch or status into failure.
            result.update(output="", output_unavailable=True, output_truncated=False)
        return result

    def _load_operation(self, handle: str) -> tuple[Path, dict[str, Any], dict[str, Any], Path]:
        directory, session, op_id = self._load(handle)
        if op_id is None:
            raise ShellCommandError("operation_handle_required", "This action requires an operation handle.")
        path = self._paths(directory, op_id)["record"]
        op = read_json(path, limit=2_000_000)
        if op.get("operation_id") != op_id or op.get("session_handle") != session.get("handle"):
            raise ShellCommandError("operation_identity_mismatch", "The operation handle does not match private state.", effect="unknown")
        return directory, session, op, path

    def _reconcile(
        self, directory: Path, session: dict[str, Any], op: dict[str, Any],
    ) -> dict[str, Any]:
        paths = self._paths(directory, str(op["operation_id"]))
        if op.get("state") not in {"completed", "failed", "collected"}:
            native = self._verify(session)
            matches = [window for window in native.get("windows", [])
                       if window.get("window_id") == op.get("window_id")
                       and window.get("metadata", {}).get("session") == session.get("handle")
                       and window.get("metadata", {}).get("operation") == op.get("operation_id")]
            runner = process_info(op.get("runner_pid")) if type(op.get("runner_pid")) is int else None
            runner_state = process_state(int(op["runner_pid"])) if type(op.get("runner_pid")) is int else None
            runner_live = bool(runner and runner.get("starttime") == op.get("runner_starttime")
                               and runner_state is not None and runner_state not in {"Z", "X"})
            if not runner_live:
                try:
                    latest = read_json(paths["record"], limit=2_000_000)
                except ShellCommandError:
                    latest = {}
                if (latest.get("operation_id") == op.get("operation_id")
                        and latest.get("session_key") == op.get("session_key")
                        and latest.get("state") in {"completed", "failed", "unknown"}):
                    op = latest
            if op.get("state") in {"intent", "dispatching"}:
                op.update(state="unknown", effect_certainty="unknown",
                          detail="dispatch outcome is not durable; replay is prohibited",
                          reconciled_at=self.clock())
                atomic_json(paths["record"], op)
            elif op.get("state") == "running" and not runner_live and (op.get("runner_pid") is not None or not matches):
                op.update(state="unknown", effect_certainty="unknown", process_scope="unknown",
                          detail="the exact runner exited without a completion receipt",
                          reconciled_at=self.clock())
                atomic_json(paths["record"], op)
            elif (op.get("state") == "draining" and not runner_live
                  and op.get("process_scope") != "collected"):
                op.update(state="unknown", effect_certainty="unknown", process_scope="unknown",
                          detail="the process scope did not confirm collection",
                          reconciled_at=self.clock())
                atomic_json(paths["record"], op)
            elif op.get("state") == "draining" and op.get("process_scope") == "collected":
                drain = {}
                if paths["drained"].exists() and not paths["drained"].is_symlink():
                    try:
                        drain = read_json(paths["drained"], limit=4096)
                    except ShellCommandError:
                        drain = {}
                if drain.get("state") == "drained":
                    op["output_drain"] = drain.get("storage", "available")
                    op["output_bytes_seen"] = drain.get("bytes_seen")
                elif not op.get("capture_stop_requested_at"):
                    # The runner remains alive in the exact pane after all
                    # workload descendants are collected. Close its pipe here,
                    # then wait for the collector's EOF receipt.
                    try:
                        pane_matches = [
                            pane for window in matches
                            for pane in window.get("panes", [])
                            if pane.get("pane_id") == op.get("pane_id")
                            and pane.get("pane_pid") == op.get("runner_pid")
                            and not pane.get("dead")
                        ]
                        if len(pane_matches) == 1:
                            self.backend.stop_capture(session, op)
                        else:
                            op["capture_stop_state"] = "pane_identity_unknown"
                    except ShellCommandError:
                        op["capture_stop_state"] = "unknown"
                    op["capture_stop_requested_at"] = self.clock()
                elif self.clock() - float(op["capture_stop_requested_at"]) >= 3.0:
                    op["output_drain"] = "unavailable"
                    op["capture_stop_state"] = "collector_unavailable"
                if op.get("output_drain") in {"available", "unavailable"}:
                    if runner_live:
                        final_payload = {
                            "state": "finalize", "operation_id": op.get("operation_id"),
                            "session_key": session.get("session_key"),
                            "output_drain": op["output_drain"], "finalized_at": self.clock(),
                        }
                        if not paths["finalize"].exists():
                            try:
                                atomic_json(paths["finalize"], final_payload, exclusive=True)
                            except ShellCommandError:
                                # Another status call can only reach this under a
                                # separate process after taking the same lock.
                                pass
                    else:
                        # The runner durably proved its full descendant scope
                        # collected before it exited. A collector receipt (or
                        # bounded unavailability) is sufficient to finish the
                        # operation without replaying the command.
                        completed_at = op.get("command_finished_at")
                        if type(completed_at) not in {int, float}:
                            completed_at = self.clock()
                        drained_at = drain.get("drained_at")
                        if type(drained_at) in {int, float}:
                            completed_at = max(float(completed_at), float(drained_at))
                        op.update(state="completed", effect_certainty="known",
                                  finished_at=completed_at)
                atomic_json(paths["record"], op)
            if op.get("state") not in {"completed", "failed", "collected"}:
                op["native_window_state"] = (
                    "present" if matches else ("unknown" if native.get("state") != "ready" else "missing")
                )
        if op.get("state") in {"completed", "failed"}:
            drained = {}
            if paths["drained"].exists() and not paths["drained"].is_symlink():
                try:
                    drained = read_json(paths["drained"], limit=4096)
                except ShellCommandError:
                    drained = {}
            if drained.get("state") == "drained":
                op["output_drain"] = drained.get("storage", "available")
                op["output_bytes_seen"] = drained.get("bytes_seen")
            else:
                op["output_drain"] = "pending"
                if (op.get("window_id") and op.get("pane_id")
                        and not op.get("capture_stop_requested_at")):
                    try:
                        self.backend.stop_capture(session, op)
                        op["capture_stop_requested_at"] = self.clock()
                        atomic_json(paths["record"], op)
                    except ShellCommandError:
                        op["capture_stop_state"] = "unknown"
                elif (op.get("capture_stop_requested_at")
                      and self.clock() - float(op["capture_stop_requested_at"]) >= 3.0):
                    op["output_drain"] = "unavailable"
                    op["capture_stop_state"] = "collector_unavailable"
            if op.get("output_drain") in {"available", "unavailable"}:
                native = self._verify(session)
                candidates = [window for window in native.get("windows", [])
                              if window.get("window_id") == op.get("window_id")]
                if candidates:
                    window = candidates[0]
                    panes = window.get("panes", [])
                    pane = panes[0] if len(panes) == 1 else {}
                    runner_pid = op.get("runner_pid")
                    runner = process_info(runner_pid) if type(runner_pid) is int else None
                    exact = (
                        native.get("state") == "ready" and len(candidates) == 1
                        and op.get("process_scope") == "collected"
                        and window.get("metadata", {}).get("session") == session.get("handle")
                        and window.get("metadata", {}).get("operation") == op.get("operation_id")
                        and window.get("metadata", {}).get("request") == op.get("request_digest")
                        and window.get("pane_count") == 1 and len(panes) == 1
                        and pane.get("pane_id") == op.get("pane_id")
                        and type(runner_pid) is int and pane.get("pane_pid") == runner_pid
                        and (pane.get("dead") is True
                             or (runner is not None
                                 and runner.get("starttime") == op.get("runner_starttime")
                                 and process_state(runner_pid) not in {None, "Z", "X"}))
                    )
                    if not exact:
                        op["cleanup_state"] = "unknown"
                    else:
                        pins, pin_unknown = self._pins(directory)
                        attached = native.get("session", {}).get("attached")
                        if pins or pin_unknown or type(attached) is not int or attached != 0:
                            op["cleanup_state"] = "pending"
                        else:
                            try:
                                self.backend.kill_window(
                                    session, op,
                                    expected_window=TmuxCommandBackend.window_identity(window))
                                after = self._verify(session)
                                present = any(w.get("window_id") == op.get("window_id")
                                              for w in after.get("windows", []))
                                op["cleanup_state"] = "pending" if present else "removed"
                            except ShellCommandError:
                                op["cleanup_state"] = "pending"
                else:
                    op["cleanup_state"] = "removed" if native.get("state") == "ready" else "unknown"
                if op.get("cleanup_state") == "removed":
                    op.setdefault("collected_at", self.clock())
            atomic_json(paths["record"], op)
        return op

    def status(self, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        if owner is not None:
            self._authorize(session, owner)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            native = self._verify(session)
            if op_id is not None:
                op_path = self._paths(directory, op_id)["record"]
                op = self._reconcile(directory, session, read_json(op_path, limit=2_000_000))
                result = self._operation_reply(session, op, directory)
                result.update(session_state=session.get("state"), native_state=native.get("state"))
                return result
            all_operations = sorted(self._ops(directory),
                                    key=lambda item: item[1].get("created_at", 0))
            recent = all_operations[-20:]
            operations = [self._operation_reply(session, self._reconcile(directory, session, op),
                                                directory, include_output=False)
                          for _, op in recent]
            return self._session_reply(session, created=False, joined=True) | {
                "native_state": native.get("state"),
                "native_windows": len(native.get("windows", [])),
                "profile_readback": native.get("applied_settings"),
                "profile_settings": session.get("settings"),
                "profile_target": session.get("profile_target"),
                "profile_state": session.get("profile_state", "applied"),
                "operations": operations,
                "operation_count": len(all_operations),
                "operations_truncated": len(all_operations) > len(recent),
                "owner_liveness": owner_liveness(session.get("owner", {})),
            }

    def wait(
        self, handle: str, *, timeout: float = 60.0,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Observe one operation until a terminal receipt or bounded timeout."""
        _, _, op_id = self._load(handle)
        if op_id is None:
            raise ShellCommandError("operation_handle_required", "Waiting requires an operation handle.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 86_400:
            raise ShellCommandError("invalid_timeout", "Wait timeout must be from 0 through 86400 seconds.")
        deadline = time.monotonic() + float(timeout)
        while True:
            try:
                observed = self.status(handle, owner=owner)
            except ShellCommandError as exc:
                if exc.code != "busy":
                    raise
                observed = _reply("busy", ok=False, handle=handle, wait_timed_out=False)
            if observed.get("state") in {"completed", "failed", "collected", "unknown"}:
                return observed | {"wait_timed_out": False}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return observed | {"wait_timed_out": True}
            time.sleep(min(0.1, remaining))

    def screen(
        self, handle: str, *, lines: int | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        operation = read_json(self._paths(directory, op_id)["record"], limit=2_000_000) if op_id else None
        count = session["settings"]["default_lines"] if lines is None else lines
        if type(count) is not int or not 1 <= count <= 10_000:
            raise ShellCommandError("invalid_lines", "Screen view lines must be from 1 through 10000.")
        count = min(count, max(1, session["settings"]["max_response_bytes"] // 8))
        native = self._verify(session)
        if native.get("state") != "ready":
            raise ShellCommandError("session_identity_unknown", "The exact owned tmux session could not be verified.", effect="unknown")
        screen = self.backend.capture_screen(session, operation, lines=count)
        raw = screen.encode("utf-8", errors="replace")
        limit = session["settings"]["max_response_bytes"]
        truncated = len(raw) > limit
        if truncated:
            screen = raw[-limit:].decode("utf-8", errors="replace")
        return _reply("observed", handle=handle, view_kind="screen",
                      lines=screen.splitlines()[-count:], truncated=truncated)

    def view(
        self, handle: str, *, lines: int | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Show recent captured output; screen() shows the pane's current rendering."""
        directory, session, op_id = self._load(handle)
        if op_id is None:
            operations = self._ops(directory)
            if not operations:
                return _reply("observed", handle=handle, view_kind="recent_output",
                              lines=[], truncated=False, operation_handle=None)
            op_id = str(max(operations, key=lambda item: item[1].get("created_at", 0))[1]["operation_id"])
        op_handle = self._op_handle(str(session["scope_key"]), str(session["session_id"]), op_id)
        observed = self.logs(op_handle, lines=lines, owner=owner)
        return _reply("observed", handle=handle, view_kind="recent_output",
                      operation_handle=op_handle, lines=observed["output"].splitlines(),
                      truncated=observed["truncated"], base_cursor=observed["base_cursor"],
                      end_cursor=observed["end_cursor"])

    def logs(
        self, handle: str, *, cursor: int | None = None,
        lines: int | None = None, max_bytes: int | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory, session, op, _ = self._load_operation(handle)
        paths = self._paths(directory, str(op["operation_id"]))
        settings = session["settings"]
        limit = settings["max_response_bytes"] if max_bytes is None else max_bytes
        if type(limit) is not int or not 1 <= limit <= min(settings["max_response_bytes"], 262_144):
            raise ShellCommandError("invalid_max_bytes", "Log output is limited by the profile and 256 KiB cap.")
        line_count = settings["default_lines"] if lines is None else lines
        if type(line_count) is not int or not 1 <= line_count <= 10_000:
            raise ShellCommandError("invalid_lines", "Log line count must be from 1 through 10000.")
        with log_lock(paths["cursor"]):
            meta = read_json(paths["cursor"], limit=4096) if paths["cursor"].exists() else {
                "base_offset": 0, "end_offset": 0, "available": False,
            }
            base, end = meta.get("base_offset", 0), meta.get("end_offset", 0)
            if type(base) is not int or type(end) is not int or base < 0 or end < base:
                base, end = 0, 0
            if cursor is not None and (type(cursor) is not int or cursor < 0):
                raise ShellCommandError("invalid_cursor", "The log cursor must be a non-negative integer.")
            gap = cursor is not None and cursor < base
            if not paths["log"].exists():
                start, data = max(base, cursor if cursor is not None else base), b""
            elif cursor is not None:
                start = max(base, cursor)
                with paths["log"].open("rb") as stream:
                    stream.seek(start - base)
                    data = stream.read(limit)[:limit]
                data = b"".join(data.splitlines(keepends=True)[:line_count])
            else:
                collected = bytearray()
                position = paths["log"].stat().st_size
                newline_count = 0
                with paths["log"].open("rb") as stream:
                    while position and len(collected) < limit and newline_count <= line_count:
                        size = min(64 * 1024, position, limit - len(collected))
                        position -= size
                        stream.seek(position)
                        block = stream.read(size)
                        collected[:0] = block
                        newline_count += block.count(b"\n")
                parts = bytes(collected).splitlines(keepends=True)
                data = b"".join(parts[-line_count:])
                start = base + max(0, paths["log"].stat().st_size - len(data))
            if cursor is None:
                output, decode_truncated = _decode_tail_output(data, limit)
                consumed = len(data)
            else:
                output, consumed = _decode_cursor_output(
                    data, limit,
                    final=op.get("output_drain") == "available" and start + len(data) >= end,
                )
                decode_truncated = consumed < len(data)
            next_cursor = start + consumed
            tail_omitted = cursor is None and start > base
            return _reply(
                str(op.get("state", "unknown")), effect=str(op.get("effect_certainty", "unknown")),
                handle=handle, operation_handle=op.get("operation_handle", handle),
                operation_id=op["operation_id"], output=output,
                cursor=next_cursor, base_cursor=base, end_cursor=end, gap=gap,
                truncated=decode_truncated or tail_omitted or next_cursor < end,
                storage=bool(meta.get("available")),
                output_drain=op.get("output_drain", "pending"),
            )

    def input(
        self, handle: str, input_id: str, data: bytes | str, *,
        eof: bool = False, owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        input_id = validate_identifier(input_id, "input_id", maximum=128)
        raw = data.encode() if isinstance(data, str) else data
        if not isinstance(raw, bytes) or len(raw) > 1_048_576 or type(eof) is not bool:
            raise ShellCommandError("invalid_input", "Interactive input must be bounded text or bytes with a Boolean EOF.")
        directory, session, op, op_path = self._load_operation(handle)
        self._authorize(session, owner)
        digest = _digest({"data_b64": base64.b64encode(raw).decode("ascii"), "eof": eof})
        with session_lock(directory):
            paths = self._paths(directory, str(op["operation_id"]))
            input_dir = paths["inputs"]
            input_dir.mkdir(mode=0o700, exist_ok=True)
            item_path = input_dir / f"{input_id}.json"
            if item_path.exists():
                previous = read_json(item_path, limit=4096)
                if previous.get("request_digest") != digest:
                    raise ShellCommandError("input_id_conflict", "This input ID already names different bytes.")
                return _reply(previous.get("state", "sent"), effect=previous.get("effect_certainty", "known"),
                              handle=handle, input_id=input_id, repeated=True)
            current = read_json(op_path, limit=2_000_000)
            if current.get("state") != "running" or current.get("process_scope") not in {"tracking", None}:
                raise ShellCommandError("operation_not_interactive", "The exact command is not an active PTY operation.")
            runner = process_info(current.get("runner_pid")) if type(current.get("runner_pid")) is int else None
            child = process_info(current.get("child_pid")) if type(current.get("child_pid")) is int else None
            runner_state = process_state(int(current["runner_pid"])) if type(current.get("runner_pid")) is int else None
            child_state = process_state(int(current["child_pid"])) if type(current.get("child_pid")) is int else None
            if (runner is None or runner.get("starttime") != current.get("runner_starttime")
                    or runner_state is None or runner_state in {"Z", "X"}
                    or child is None or child.get("starttime") != current.get("child_starttime")
                    or child_state is None or child_state in {"Z", "X"}
                    or child.get("pgrp") != current.get("child_pgrp")):
                raise ShellCommandError("operation_identity_unknown", "The active command process identity cannot be verified.", effect="unknown")
            native = self._verify(session)
            panes = [pane for window in native.get("windows", [])
                     if window.get("window_id") == current.get("window_id")
                     and window.get("metadata", {}).get("session") == session.get("handle")
                     and window.get("metadata", {}).get("operation") == current.get("operation_id")
                     for pane in window.get("panes", []) if pane.get("pane_id") == current.get("pane_id")]
            if len(panes) != 1 or panes[0].get("dead") or panes[0].get("pane_pid") != current.get("runner_pid"):
                raise ShellCommandError("operation_identity_unknown", "The exact active command pane cannot be verified.", effect="unknown")
            item = {"schema_version": 1, "input_id": input_id, "request_digest": digest,
                    "state": "intent", "effect_certainty": "unknown", "eof": eof, "created_at": self.clock()}
            atomic_json(item_path, item, exclusive=True)
            input_file = input_dir / f"{input_id}.input"
            fd = os.open(input_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                self.backend.send_input(session, current, buffer_name=f"ls-{input_id[:80]}",
                                        input_path=str(input_file), eof=eof)
                item.update(state="sent", effect_certainty="known", sent_at=self.clock())
                atomic_json(item_path, item)
                current_session = read_json(directory / "session.json")
                current_session.update(last_activity_at=self.clock(), last_activity_kind="input")
                atomic_json(directory / "session.json", current_session)
                input_file.unlink(missing_ok=True)
                return _reply("sent", effect="known", handle=handle, input_id=input_id, repeated=False)
            except ShellCommandError as exc:
                item.update(state="unknown", effect_certainty="unknown", error=exc.code)
                atomic_json(item_path, item)
                raise ShellCommandError(exc.code, "PTY input acknowledgement is uncertain; bytes were not replayed.", effect="unknown") from exc

    def cancel(self, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        directory, session, op, op_path = self._load_operation(handle)
        self._authorize(session, owner)
        with session_lock(directory):
            current = read_json(op_path, limit=2_000_000)
            if current.get("state") in {"completed", "failed", "collected"}:
                return self._operation_reply(session, current, directory) | {"cancel_requested": False}
            if current.get("state") == "draining" and current.get("process_scope") == "collected":
                return self._operation_reply(session, current, directory) | {
                    "cancel_requested": False, "cancel_state": "process_scope_collected"}
            if current.get("state") not in {"running", "draining"} or current.get("process_scope") not in {"tracking", "draining"}:
                raise ShellCommandError("operation_not_cancellable", "Only a verified active command can be cancelled.",
                                        effect="unknown" if current.get("state") == "unknown" else "none")
            native = self._verify(session)
            panes = [pane for window in native.get("windows", [])
                     if window.get("window_id") == current.get("window_id")
                     and window.get("metadata", {}).get("session") == session.get("handle")
                     and window.get("metadata", {}).get("operation") == current.get("operation_id")
                     for pane in window.get("panes", []) if pane.get("pane_id") == current.get("pane_id")]
            runner_pid = current.get("runner_pid")
            runner = process_info(runner_pid) if type(runner_pid) is int else None
            if (len(panes) != 1 or panes[0].get("dead") or runner is None
                    or panes[0].get("pane_pid") != runner_pid
                    or runner.get("starttime") != current.get("runner_starttime")
                    or process_state(runner_pid) in {None, "Z", "X"}
                    or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal")):
                raise ShellCommandError("operation_identity_unknown", "The exact runner and pane cannot be verified for cancellation.", effect="unknown")
            if current.get("cancel_requested_at"):
                return self._operation_reply(session, current, directory) | {
                    "cancel_requested": True, "cancel_state": current.get("cancel_state", "requested")}
            current.update(cancel_state="requested", cancel_requested_at=self.clock())
            atomic_json(op_path, current)
            try:
                descriptor = os.pidfd_open(runner_pid)
                try:
                    fresh = process_info(runner_pid)
                    if fresh is None or fresh.get("starttime") != current["runner_starttime"]:
                        raise ShellCommandError("operation_identity_unknown", "The runner changed identity before cancellation.", effect="unknown")
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                finally:
                    os.close(descriptor)
            except (OSError, ShellCommandError) as exc:
                current.update(cancel_state="unknown", effect_certainty="unknown")
                atomic_json(op_path, current)
                raise ShellCommandError("cancel_signal_failed", "The verified runner did not accept cancellation.", effect="unknown") from exc
            result = self._operation_reply(session, current, directory)
            result.update(cancel_requested=True, cancel_state="requested")
            return result

    def _pins(self, directory: Path, *, remove_dead: bool = True) -> tuple[list[dict[str, Any]], bool]:
        found: list[dict[str, Any]] = []
        unknown = False
        pin_dir = directory / "pins"
        try:
            paths = list(pin_dir.glob("*.json"))
        except OSError:
            return [], True
        for path in paths:
            if path.is_symlink() or not path.is_file():
                unknown = True
                continue
            try:
                pin = read_json(path, limit=4096)
            except ShellCommandError:
                unknown = True
                continue
            life = owner_liveness(pin)
            if life == "live":
                found.append(pin)
            elif life == "unknown":
                unknown = True
            elif remove_dead:
                path.unlink(missing_ok=True)
        return found, unknown

    def _expire_completed_logs(self, directory: Path, session: Mapping[str, Any], now: float) -> list[str]:
        retention = int(session.get("settings", {}).get("completed_log_retention_days", 7))
        expired: list[str] = []
        for _, operation in self._ops(directory):
            if operation.get("state") not in {"completed", "failed", "collected"}:
                continue
            finished = operation.get("finished_at", operation.get("command_finished_at", operation.get("completed_at")))
            if type(finished) not in {int, float} or now - float(finished) < retention * 86_400:
                continue
            paths = self._paths(directory, str(operation.get("operation_id", "")))
            for key in ("log", "cursor"):
                try:
                    paths[key].unlink(missing_ok=True)
                except OSError:
                    continue
            operation["log_retention_state"] = "expired"
            operation["logs_expired_at"] = now
            atomic_json(paths["record"], operation)
            expired.append(str(operation.get("operation_id")))
        return expired

    def attach(
        self, handle: str, *, owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        pin_dir = directory / "pins"
        pin_id = uuid.uuid4().hex
        pin_path = pin_dir / f"attach-{pin_id}.json"
        current = make_owner_binding(owner_pid=os.getpid())
        pin = {**current, "pin_id": pin_id, "kind": "attachment", "created_at": self.clock()}
        operation = None
        with session_lock(directory):
            session = read_json(directory / "session.json")
            if op_id is not None:
                operation = read_json(self._paths(directory, op_id)["record"], limit=2_000_000)
                if (operation.get("operation_id") != op_id
                        or operation.get("session_handle") != session.get("handle")):
                    raise ShellCommandError("operation_identity_mismatch", "The operation handle does not match private state.", effect="unknown")
                if (operation.get("state") in {"completed", "failed", "collected"}
                        or operation.get("process_scope") == "collected"):
                    return self._operation_reply(session, operation, directory) | {
                        "attached": False,
                        "result": {"state": operation.get("state"), "exit_code": operation.get("exit_code")},
                    }
                if operation.get("state") not in {"running", "draining"}:
                    raise ShellCommandError("operation_identity_unknown", "The operation pane is not verified for attach.", effect="unknown")
            native = self._verify(session)
            if native.get("state") != "ready":
                raise ShellCommandError("session_identity_unknown", "The exact owned tmux session cannot be verified for attach.", effect="unknown")
            if operation is not None:
                windows = [window for window in native.get("windows", [])
                           if (window.get("window_id") == operation.get("window_id")
                               and window.get("metadata", {}).get("session") == session.get("handle")
                               and window.get("metadata", {}).get("operation") == op_id
                               and window.get("metadata", {}).get("request") == operation.get("request_digest"))]
                if len(windows) != 1 or len(windows[0].get("panes", [])) != 1:
                    raise ShellCommandError("operation_identity_unknown", "The exact owned operation window cannot be verified for attach.", effect="unknown")
                pane = windows[0]["panes"][0]
                if (pane.get("pane_id") != operation.get("pane_id") or pane.get("dead")
                        or (operation.get("runner_pid") is not None
                            and pane.get("pane_pid") != operation.get("runner_pid"))):
                    raise ShellCommandError("operation_identity_unknown", "The exact owned operation pane cannot be verified for attach.", effect="unknown")
            atomic_json(pin_path, pin, exclusive=True)
        try:
            returncode = self.backend.attach(session, operation)
            if operation is not None:
                current = read_json(self._paths(directory, op_id)["record"], limit=2_000_000)
                if (current.get("operation_id") != op_id
                        or current.get("session_handle") != session.get("handle")):
                    raise ShellCommandError("operation_identity_mismatch", "The operation changed identity during attach.", effect="unknown")
                receipt = self._operation_reply(session, current, directory, include_output=False)
                if returncode != 0:
                    receipt.update(join_command=receipt["session_join_command"],
                                   operation_join_command=None, join_target="session",
                                   pane_available=False)
                return receipt | {"state": "detached" if returncode == 0 else "attach_failed",
                                  "ok": returncode == 0,
                                  "attached": returncode == 0, "returncode": returncode}
            return _reply("detached" if returncode == 0 else "attach_failed", ok=returncode == 0,
                          effect="known", handle=handle, attached=returncode == 0,
                          returncode=returncode,
                          machine={"host": session.get("host"), "generation": session.get("machine_generation")},
                          join_command=f"localsetup shell attach --handle {shlex.quote(handle)}")
        finally:
            try:
                with session_lock(directory, blocking=True):
                    if pin_path.exists() and read_json(pin_path, limit=4096).get("pin_id") == pin_id:
                        current_session = read_json(directory / "session.json")
                        current_session.update(last_activity_at=self.clock(),
                                               last_activity_kind="observer_detach")
                        atomic_json(directory / "session.json", current_session)
                        pin_path.unlink()
            except (OSError, ShellCommandError):
                pass

    def close(self, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Close requires a session handle.")
        self._authorize(session, owner)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            operations = self._strict_ops(directory, session)
            active = [op for op in operations
                      if op.get("state") not in {"completed", "failed", "collected"}
                      or op.get("output_drain") == "pending"]
            pins, pin_unknown = self._pins(directory)
            observed = self._verify(session)
            attached = observed.get("session", {}).get("attached")
            if active or pins or pin_unknown:
                return _reply("busy", ok=False, handle=handle, cleanup_state="pending",
                              active_operations=len(active), observer_pins=len(pins),
                              observer_state="unknown" if pin_unknown or type(attached) is not int else attached)
            if observed.get("state") == "missing":
                if (not self._terminal_ops_collected(operations)
                        or not self._remove_stale_native(session)
                        or not self._endpoint_absent(session)):
                    session.update(state="cleanup_pending", cleanup_state="pending")
                    atomic_json(directory / "session.json", session)
                    return _reply("cleanup_pending", ok=False, effect="unknown", handle=handle,
                                  cleanup_state="pending", removed=False)
                if session.get("state") in {"removed", "closed"}:
                    return _reply("removed", effect="known", handle=handle,
                                  cleanup_state="removed", removed=True)
                session.update(state="removed", status="removed", cleanup_state="removed",
                               closed_at=self.clock(), last_activity_at=self.clock())
                atomic_json(directory / "session.json", session)
                return _reply("removed", effect="known", handle=handle,
                              cleanup_state="removed", removed=True)
            if observed.get("state") != "ready":
                session.update(state="cleanup_pending", cleanup_state="unknown")
                atomic_json(directory / "session.json", session)
                return _reply("cleanup_pending", ok=False, effect="unknown", handle=handle, cleanup_state="unknown")
            if type(attached) is not int or attached > 0:
                return _reply("busy", ok=False, handle=handle, cleanup_state="pending",
                              active_operations=0, observer_pins=0, observer_state=attached)
            expected_inventory = self._native_inventory(session, observed, operations)
            try:
                self.backend.close_session(session, expected_inventory=expected_inventory)
            except ShellCommandError as exc:
                exists = Path(session["endpoint"]).exists()
                session.update(state="cleanup_pending", status="cleanup_pending",
                               cleanup_state="pending" if exists else "unknown",
                               close_error=exc.code, closed_at=self.clock())
                atomic_json(directory / "session.json", session)
                return _reply("cleanup_pending", ok=False, effect=exc.effect,
                              handle=handle, cleanup_state=session["cleanup_state"], removed=False)
            exists = not self._endpoint_absent(session)
            session.update(state="cleanup_pending" if exists else "removed",
                           status="cleanup_pending" if exists else "removed",
                           cleanup_state="pending" if exists else "removed",
                           closed_at=self.clock(), last_activity_at=self.clock())
            atomic_json(directory / "session.json", session)
            return _reply(session["state"], effect="known", handle=handle,
                          cleanup_state=session["cleanup_state"], removed=not exists)

    def recover(
        self, handle: str, *, owner: Mapping[str, Any], adopt: bool = False,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Recovery requires a session handle.")
        replacement = _owner(owner)
        scope = str(session.get("scope_key", ""))
        if _scope(replacement) != scope:
            raise ShellCommandError("recovery_scope_mismatch", "Recovery requires the same admitted agent scope.")
        owner_dir = self._owner_dir(scope)
        # Keep the same owner-index -> session lock order used by ensure. The
        # intent survives a lost acknowledgement between native marker and
        # the two private receipts, so retry never dispatches a command.
        with session_lock(owner_dir):
            owner_path = owner_dir / "owner.json"
            prior = read_json(owner_path)
            if prior.get("active_handle") != handle or prior.get("scope_key") != scope:
                raise ShellCommandError("owner_identity_unknown", "The owner index no longer names this session.", effect="unknown")
            with session_lock(directory):
                current = read_json(directory / "session.json")
                if current.get("handle") != handle or current.get("scope_key") != scope:
                    raise ShellCommandError("session_identity_unknown", "The recovery session identity changed.", effect="unknown")
                if current.get("state") != "ready":
                    raise ShellCommandError("session_identity_unknown", "Only a ready native session can be adopted.", effect="unknown")
                intent = current.get("adoption_intent")
                new_fingerprint = owner_fingerprint(replacement)
                if intent is not None:
                    if (not isinstance(intent, dict) or intent.get("owner_fingerprint") != new_fingerprint
                            or intent.get("owner") != replacement):
                        raise ShellCommandError("adoption_identity_unknown", "A different recovery intent is pending.", effect="unknown")
                    former_owner = intent.get("previous_owner")
                    former_fingerprint = intent.get("previous_fingerprint")
                else:
                    former_owner = current.get("owner")
                    former_fingerprint = current.get("owner_fingerprint")
                if not isinstance(former_owner, dict) or owner_fingerprint(former_owner) != former_fingerprint:
                    raise ShellCommandError("owner_identity_unknown", "The previous owner receipt is inconsistent.", effect="unknown")
                prior_fingerprint = owner_fingerprint(prior.get("owner", {}))
                if prior_fingerprint not in ({former_fingerprint, new_fingerprint} if intent else {former_fingerprint}):
                    raise ShellCommandError("owner_identity_unknown", "The owner index changed during recovery.", effect="unknown")
                if intent is None and former_fingerprint == new_fingerprint:
                    if prior_fingerprint != new_fingerprint or self._verify(current).get("state") != "ready":
                        raise ShellCommandError("session_identity_unknown", "The adopted session cannot be verified.", effect="unknown")
                    return self._session_reply(current, created=False, joined=True) | {"recovered": True}
                if owner_liveness(former_owner) != "dead":
                    raise ShellCommandError("owner_not_proven_dead", "The previous owner did not remain proven dead.", effect="unknown")
                observed = self.backend.inspect_session(current)
                options = observed.get("options", {})
                expected = {
                    "api": "1", "handle": handle, "key": current.get("session_key"),
                    "machine": current.get("machine_generation"),
                    "profile": current.get("settings_digest"),
                    "status_window": current.get("status_window_id"),
                }
                permitted_markers = {former_fingerprint, new_fingerprint} if intent else {former_fingerprint}
                if (observed.get("state") != "ready"
                        or any(options.get(key) != value for key, value in expected.items())
                        or options.get("owner") not in permitted_markers):
                    if not adopt:
                        return _reply("observed", handle=handle, previous_owner="dead",
                                      native_state=observed.get("state"), adoption_required=True)
                    raise ShellCommandError("session_identity_unknown", "Recovery will not adopt an unverified native session.", effect="unknown")
                if not adopt:
                    return _reply("observed", handle=handle, previous_owner="dead",
                                  native_state="ready", adoption_required=True)
                operations = self._strict_ops(directory, current)
                pins, unknown = self._pins(directory)
                if pins or unknown:
                    raise ShellCommandError("observer_blocks_recovery", "Live or unknown observers prevent recovery.")
                attached = observed.get("session", {}).get("attached")
                if type(attached) is not int or attached != 0:
                    raise ShellCommandError("observer_blocks_recovery", "Native observer count is nonzero or unknown.", effect="unknown")
                self._native_inventory(current, observed, operations, allow_active=True)
                if intent is None:
                    intent = {
                        "owner": replacement, "owner_fingerprint": new_fingerprint,
                        "previous_owner": former_owner, "previous_fingerprint": former_fingerprint,
                        "started_at": self.clock(),
                    }
                    current["adoption_intent"] = intent
                    atomic_json(directory / "session.json", current)
                adopted = {**current, "owner": replacement, "owner_fingerprint": new_fingerprint}
                self.backend.set_owner_identity(adopted, allowed_previous_owners=permitted_markers | {new_fingerprint})
                current.update(owner=replacement, owner_fingerprint=new_fingerprint,
                               state="ready", status="ready", recovered_at=self.clock(),
                               last_activity_at=self.clock())
                atomic_json(directory / "session.json", current)
                atomic_json(owner_path, {
                    "schema_version": 1, "scope_key": scope, "owner": replacement,
                    "owner_fingerprint": new_fingerprint, "active_handle": handle,
                    "updated_at": self.clock(),
                })
                current["adoption_intent"] = None
                atomic_json(directory / "session.json", current)
                return self._session_reply(current, created=False, joined=True) | {"recovered": True}

    def apply_profile(
        self, session_handle: str, resolved_settings: Mapping[str, Any], profile_digest: str,
        *, owner: Mapping[str, Any] | None = None, profile_name: str = "agent-shell",
        profile_base: str | None = None,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(session_handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Applying a profile requires a session handle.")
        self._authorize(session, owner)
        new_settings = _settings(resolved_settings, source_root=self.source_root,
                                 profile_name=profile_name, profile_base=profile_base)
        if not isinstance(profile_digest, str) or not _HEX64.fullmatch(profile_digest):
            raise ShellCommandError("invalid_profile_digest", "A validated profile digest is required.")
        if _profile_digest(new_settings, profile_name) != profile_digest:
            raise ShellCommandError("profile_digest_mismatch", "The profile digest does not match the resolved settings.")
        with session_lock(directory):
            session = read_json(directory / "session.json")
            self._authorize(session, owner)
            if session.get("settings_digest") == profile_digest and session.get("settings") == new_settings:
                return _reply("unchanged", handle=session_handle, settings_digest=profile_digest,
                              cleanup_state=session.get("cleanup_state", "none"))
            active = [op for _, op in self._ops(directory)
                      if op.get("state") not in {"completed", "failed", "collected"}
                      or op.get("output_drain") == "pending"]
            pins, pin_unknown = self._pins(directory)
            observed = self._verify(session)
            attached = observed.get("session", {}).get("attached")
            if active or pins or pin_unknown or type(attached) is not int or attached > 0:
                raise ShellCommandError("profile_session_busy", "Profile settings cannot change during commands or observation.")
            if observed.get("state") != "ready":
                raise ShellCommandError("session_identity_unknown", "The exact session cannot be verified for profile changes.", effect="unknown")
            if new_settings["history_limit"] != session["settings"]["history_limit"]:
                raise ShellCommandError("profile_history_requires_new_session", "Existing tmux panes cannot change history capacity; create a new session with this profile.")
            session["profile_target"] = {
                "settings": new_settings, "digest": profile_digest, "started_at": self.clock(),
            }
            atomic_json(directory / "session.json", session)
            try:
                applied = self.backend.apply_session_settings(session, new_settings, profile_digest)
                if applied.get("state") != "applied" or applied.get("settings") != new_settings:
                    raise ShellCommandError("profile_readback_mismatch", "tmux did not read back the requested settings.", effect="unknown")
                session.update(settings=new_settings, settings_digest=profile_digest,
                               profile_name=profile_name, profile_base=profile_base,
                               profile_state="applied", profile_target=None,
                               profile_applied_at=self.clock(), last_activity_at=self.clock(),
                               last_activity_kind="profile_apply")
                atomic_json(directory / "session.json", session)
                return _reply("applied", effect="known", handle=session_handle,
                              settings_digest=profile_digest, cleanup_state="none",
                              readback=applied.get("settings"))
            except Exception as exc:
                session.update(profile_state="partial", cleanup_state="pending",
                               profile_error=getattr(exc, "code", exc.__class__.__name__))
                atomic_json(directory / "session.json", session)
                return _reply("cleanup_pending", ok=False, effect="unknown",
                              handle=session_handle, settings_digest=profile_digest,
                              cleanup_state="pending", error=session["profile_error"])

    def gc(self) -> dict[str, Any]:
        """Collect only proven dead-owner sessions after quiet grace; never starts a scheduler."""
        result: dict[str, list[str]] = {"removed": [], "pending": [], "preserved": [], "expired_logs": []}
        now = self.clock()
        if not self.root.exists():
            return _reply("completed", effect="known", gc=result)
        try:
            roots = [p for p in self._sessions(create=False).iterdir() if p.is_dir() and not p.is_symlink()]
            directories = [d for root in roots for d in root.iterdir() if d.is_dir() and not d.is_symlink()]
        except OSError:
            return _reply("partial", ok=False, effect="unknown", gc=result)
        for directory in directories:
            pending_handle = directory.name
            try:
                session = read_json(directory / "session.json")
                pending_handle = str(session.get("handle", directory.name))
                if self._handle(str(session.get("scope_key")), str(session.get("session_id"))) != session.get("handle"):
                    result["preserved"].append(str(directory.name))
                    continue
                with session_lock(directory):
                    session = read_json(directory / "session.json")
                    life = owner_liveness(session.get("owner", {}))
                    if life == "unknown":
                        result["preserved"].append(session["handle"])
                        continue
                    result["expired_logs"].extend(self._expire_completed_logs(directory, session, now))
                    if life != "dead":
                        result["preserved"].append(session["handle"])
                        continue
                    # A dead owner does not make its child runner dead. Advance
                    # only the exact recorded operation through drain/finalize
                    # before deciding whether the session is idle.
                    for _, operation in self._ops(directory):
                        self._reconcile(directory, session, operation)
                    operations = self._strict_ops(directory, session)
                    quiet_at = float(session.get("last_activity_at", session.get("created_at", now)))
                    for operation in operations:
                        for key in ("created_at", "started_at", "command_finished_at", "finished_at",
                                    "cancel_requested_at"):
                            value = operation.get(key)
                            if type(value) in {int, float}:
                                quiet_at = max(quiet_at, float(value))
                        cursor_path = self._paths(directory, str(operation["operation_id"]))["cursor"]
                        if cursor_path.exists():
                            with log_lock(cursor_path):
                                cursor_meta = read_json(cursor_path, limit=4096)
                            updated = cursor_meta.get("updated_at")
                            if type(updated) in {int, float}:
                                quiet_at = max(quiet_at, float(updated))
                    age = now - quiet_at
                    grace = int(session.get("settings", {}).get("idle_grace_seconds", 900))
                    active = [op for op in operations
                              if op.get("state") not in {"completed", "failed", "collected"}
                              or op.get("output_drain") == "pending"]
                    pins, pin_unknown = self._pins(directory)
                    native = self._verify(session) if session.get("native_session_id") else {"state": "missing"}
                    attached = native.get("session", {}).get("attached")
                    if (age < grace or active or pins or pin_unknown
                            or type(attached) is not int and native.get("state") == "ready"
                            or type(attached) is int and attached > 0):
                        result["preserved"].append(session["handle"])
                        continue
                    if native.get("state") == "ready":
                        try:
                            expected_inventory = self._native_inventory(session, native, operations)
                            self.backend.close_session(session, expected_inventory=expected_inventory)
                        except ShellCommandError:
                            session.update(state="cleanup_pending", cleanup_state="pending", gc_at=now)
                            atomic_json(directory / "session.json", session)
                            result["pending"].append(session["handle"])
                            continue
                    elif native.get("state") == "missing":
                        if (not self._terminal_ops_collected(operations)
                                or not self._remove_stale_native(session)
                                or not self._endpoint_absent(session)):
                            result["pending"].append(session["handle"])
                            continue
                    elif native.get("state") == "unknown":
                        result["pending"].append(session["handle"])
                        continue
                    exists = not self._endpoint_absent(session)
                    session.update(state="cleanup_pending" if exists else "removed",
                                   cleanup_state="pending" if exists else "removed", gc_at=now)
                    atomic_json(directory / "session.json", session)
                    result["pending" if exists else "removed"].append(session["handle"])
            except (OSError, ShellCommandError):
                result["pending"].append(pending_handle)
        return _reply("partial" if result["pending"] else "completed",
                      effect="unknown" if result["pending"] else "known", gc=result)
