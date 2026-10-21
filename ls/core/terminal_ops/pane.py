"""Versioned lifecycle service shared by managed Herdr and tmux panes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import fcntl
from pathlib import Path
import json
import os
import tempfile
import threading
from typing import Any, Mapping
import uuid

from .backends import PaneAdapter
from .models import PaneBinding, PaneError, PaneRequest, PaneStatus, PaneTransition
from .shell import _read_json, _read_operation, reconcile_transition, stage_transition


_LOCAL_LOCK_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


class FilePaneStore:
    """Small pane-binding adapter in the caller's existing private state root."""

    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir).expanduser() / "terminal_ops" / "bindings"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_uid != os.getuid():
            raise PaneError("unsafe_state_dir", "The managed pane binding root is not owner-private.")
        self.root.chmod(0o700)

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise PaneError("invalid_binding_key", "The pane binding key is invalid.")
        return self.root / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if path.is_symlink():
            raise PaneError("unsafe_state_file", "The pane binding record is a symbolic link.")
        try:
            info = path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PaneError("unsafe_state_file", "The pane binding record is not owner-private.")
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise PaneError("invalid_store_record", "The pane binding record cannot be read.") from exc
        if not isinstance(value, dict):
            raise PaneError("invalid_store_record", "The pane binding record is invalid.")
        return value

    def records(self):
        for path in self.root.glob("*.json"):
            value = self.get(path.stem)
            if value is not None:
                yield value

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        path = self._path(key)
        if path.is_symlink():
            raise PaneError("unsafe_state_file", "The pane binding record is a symbolic link.")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{key}.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(dict(value), stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def lock(self, key: str):
        path = self._path(key).with_suffix(".lock")
        if path.is_symlink():
            raise PaneError("unsafe_state_file", "The pane binding lock is a symbolic link.")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise PaneError("unsafe_state_file", "The pane binding lock cannot be opened safely.") from exc
        try:
            if self.root.stat().st_uid != os.getuid() or os.fstat(descriptor).st_uid != os.getuid():
                raise PaneError("unsafe_state_file", "The pane binding lock has another owner.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PaneError("pane_busy", "Another managed-pane operation owns this binding.") from exc
            yield
        finally:
            os.close(descriptor)


class ManagedPaneService:
    """Coordinate exact scope, native pane identity and single-use shell transitions.

    ``store`` is the caller's existing private state adapter. It may implement
    ``get(key)``, ``put(key, mapping)`` and an optional nonblocking ``lock(key)``
    context manager. The service does not create another job or pane database.
    """

    def __init__(self, store: Any, adapters: Mapping[str, PaneAdapter], *, state_dir: str | Path):
        if store is None or not isinstance(adapters, Mapping):
            raise PaneError("invalid_service", "An existing private store and explicit native adapters are required.")
        root = Path(state_dir).expanduser()
        if not root.is_absolute() or root.is_symlink():
            raise PaneError("invalid_state_dir", "Managed pane runtime state must use an absolute private root.")
        self.store = store
        self.adapters = dict(adapters)
        self.state_dir = root

    def _adapter(self, request: PaneRequest) -> PaneAdapter:
        adapter = self.adapters.get(request.backend)
        if adapter is None:
            raise PaneError("adapter_unavailable", f"No native {request.backend} pane adapter was supplied.")
        return adapter

    def _get(self, key: str) -> dict[str, Any] | None:
        if isinstance(self.store, dict):
            value = self.store.get(key)
        else:
            method = getattr(self.store, "get", None)
            if not callable(method):
                raise PaneError("invalid_store", "The existing pane store must implement get(key).")
            value = method(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise PaneError("invalid_store_record", "The stored managed pane binding is not a JSON object.")
        return value

    def _put(self, key: str, value: Mapping[str, Any]) -> None:
        if isinstance(self.store, dict):
            self.store[key] = dict(value)
            return
        method = getattr(self.store, "put", None)
        if not callable(method):
            raise PaneError("invalid_store", "The existing pane store must implement put(key, mapping).")
        method(key, dict(value))

    def _lock(self, key: str):
        locker = getattr(self.store, "lock", None)
        if callable(locker):
            try:
                return locker(key)
            except Exception as exc:
                raise PaneError("pane_busy", "The existing pane store is already locked by another operation.") from exc
        with _LOCAL_LOCK_GUARD:
            lock = _LOCAL_LOCKS.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise PaneError("pane_busy", "Another managed-pane operation is active for this exact binding.")

        class _Release:
            def __enter__(self):
                return None

            def __exit__(self, *_: Any) -> None:
                lock.release()

        return _Release()

    def _record(self, request: PaneRequest, status: PaneStatus, *, operation: dict[str, Any] | None = None,
                creation: dict[str, Any] | None = None) -> None:
        previous = self._get(request.binding_key)
        last_binding = status.binding.to_dict() if status.binding is not None else (
            previous.get("last_binding") if isinstance(previous, dict) else None
        )
        payload = {
            "schema_version": 1,
            "api_version": 1,
            "binding_key": request.binding_key,
            "request": request.to_dict(),
            "status": status.to_dict(),
        }
        if isinstance(last_binding, dict):
            payload["last_binding"] = last_binding
        if operation is not None:
            payload["operation"] = dict(operation)
        if creation is not None:
            payload["creation"] = dict(creation)
        self._put(request.binding_key, payload)

    def _check_stored(self, request: PaneRequest, status: PaneStatus) -> PaneStatus:
        stored = self._get(request.binding_key)
        if stored is None:
            return status
        if stored.get("binding_key") != request.binding_key or stored.get("request", {}).get("binding_key") != request.binding_key:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="The existing private pane record does not match this exact binding.")
        creation = stored.get("creation")
        if creation is not None:
            return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                              detail="A pane creation was reserved before native dispatch; reconcile it without creating another pane.")
        previous = stored.get("status")
        previous_binding = stored.get("last_binding")
        if not isinstance(previous_binding, dict):
            previous_binding = previous.get("binding") if isinstance(previous, dict) else None
        if status.state == "missing" and isinstance(previous_binding, dict):
            pending = stored.get("operation")
            lease = self.state_dir / "terminal_ops" / request.binding_key / "writer-lease"
            if ((isinstance(pending, dict) and pending.get("state") != "completed")
                    or lease.exists() or lease.is_symlink()):
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  detail="A prior operation or writer lease prevents replacement of the closed pane.")
            # The adapter's exact native inventory proved the old pane absent.
            # Its binding is invalidated and one replacement may be allocated.
            return status
        if status.binding is not None and isinstance(previous_binding, dict):
            if (previous_binding.get("pane_id") != status.binding.pane_id
                    or previous_binding.get("shell_pid") != status.binding.shell_pid
                    or previous_binding.get("pane_tty") != status.binding.pane_tty
                    or previous_binding.get("machine_generation") != status.binding.machine_generation):
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  detail="The native pane or controlled shell differs from the stored exact binding.")
        lease = self.state_dir / "terminal_ops" / request.binding_key / "writer-lease"
        if lease.exists() or lease.is_symlink():
            if lease.is_symlink() or not lease.is_dir():
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  status.binding, detail="The managed pane writer lease is unsafe.")
            try:
                lease_record = _read_json(lease / "lease.json")
                operation_id = lease_record["operation_id"]
                if lease_record.get("binding_key") != request.binding_key or not isinstance(operation_id, str):
                    raise ValueError("mismatched writer lease")
                _, operation = _read_operation(str(self.state_dir), request.binding_key, operation_id)
                if operation.get("binding_key") != request.binding_key or operation.get("operation_id") != operation_id:
                    raise ValueError("mismatched operation receipt")
            except (OSError, ValueError, KeyError, TypeError, PaneError):
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  status.binding, detail="The managed pane writer lease cannot be reconciled.")
            state = "busy" if operation.get("state") in {"staged", "running", "claimed"} else "unknown"
            return PaneStatus(state, request.backend, request.binding_key, request.name,
                              status.binding, operation_id,
                              "A prior managed operation still owns the pane writer lease.")
        return status

    def inspect(self, request: PaneRequest) -> PaneStatus:
        status = self._adapter(request).inspect(request)
        return self._check_stored(request, status)

    def ensure(self, request: PaneRequest) -> PaneStatus:
        with self._lock(request.binding_key):
            native = self._adapter(request).inspect(request)
            stored = self._get(request.binding_key)
            if isinstance(stored, dict) and stored.get("creation") is not None:
                if (native.state == "ready" and native.binding is not None
                        and stored.get("binding_key") == request.binding_key
                        and stored.get("request", {}).get("binding_key") == request.binding_key):
                    # Reconcile a prior uncertain creation only when the native
                    # adapter independently proves its exact owned pane ready.
                    self._record(request, native)
                    return native
                return self._check_stored(request, native)
            status = self._check_stored(request, native)
            if status.state == "ready":
                self._record(request, status)
                return status
            if status.state != "missing":
                return status
            self._record(request, PaneStatus("unknown", request.backend, request.binding_key,
                                             request.name, detail="Native pane creation is reserved."),
                         creation={"state": "reserved", "token": uuid.uuid4().hex})
            try:
                binding = self._adapter(request).create(request)
            except PaneError as exc:
                if exc.effect == "none":
                    # The adapter proved no native mutation was attempted.
                    self._record(request, PaneStatus("missing", request.backend, request.binding_key,
                                                     request.name, detail=exc.message))
                raise
            verified = self._adapter(request).inspect(request)
            if verified.state != "ready" or verified.binding is None or verified.binding.pane_id != binding.pane_id:
                return PaneStatus(
                    "unknown" if verified.state == "unknown" else "busy",
                    request.backend,
                    request.binding_key,
                    request.name,
                    binding=verified.binding,
                    detail=verified.detail or "The newly created pane is not yet verified at its primary prompt.",
                    changed=True,
                )
            verified = PaneStatus(verified.state, verified.backend, verified.binding_key,
                                  verified.name, verified.binding, verified.operation_id,
                                  verified.detail, changed=True)
            self._record(request, verified)
            return verified

    def transition(self, request: PaneRequest, transition: PaneTransition) -> PaneStatus:
        with self._lock(request.binding_key):
            status = self._check_stored(request, self._adapter(request).inspect(request))
            if status.state != "ready" or status.binding is None:
                return status
            prompt_nonce = status.binding.metadata.get("prompt_nonce")
            if not isinstance(prompt_nonce, str) or not prompt_nonce:
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  binding=status.binding, detail="The verified primary prompt has no current nonce.")
            if transition.kind == "command" and transition.cwd is None:
                transition = replace(transition, cwd=request.cwd)
            operation = stage_transition(self.state_dir, status.binding.to_dict(), transition,
                                         prompt_nonce=prompt_nonce)
            try:
                # Native adapters are constrained to this reserved trigger only.
                self._adapter(request).send_trigger(request, status.binding)
            except PaneError as exc:
                try:
                    operation = reconcile_transition(self.state_dir, binding_key=request.binding_key,
                                                      operation_id=transition.operation_id)
                except PaneError:
                    operation = {"state": "unknown", "detail": exc.message}
                unknown = PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                     binding=status.binding, operation_id=transition.operation_id,
                                     detail=exc.message)
                self._record(request, unknown, operation=operation)
                return unknown
            dispatched = PaneStatus("busy", request.backend, request.binding_key, request.name,
                                    binding=status.binding, operation_id=transition.operation_id,
                                    detail="The reserved trigger was sent; reconcile the single-use operation receipt.")
            self._record(request, dispatched, operation=operation)
            return dispatched

    def reconcile(self, request: PaneRequest, operation_id: str) -> PaneStatus:
        with self._lock(request.binding_key):
            try:
                operation = reconcile_transition(self.state_dir, binding_key=request.binding_key,
                                                  operation_id=operation_id)
            except PaneError as exc:
                return PaneStatus("unknown", request.backend, request.binding_key, request.name,
                                  operation_id=operation_id, detail=exc.message)
            current = self._check_stored(request, self._adapter(request).inspect(request))
            state = operation.get("state")
            if state == "completed":
                if current.state == "ready":
                    current = PaneStatus(current.state, current.backend, current.binding_key,
                                         current.name, current.binding, operation_id,
                                         "The operation receipt records completion.")
                elif current.state != "unknown":
                    current = PaneStatus("busy", current.backend, current.binding_key,
                                         current.name, current.binding, operation_id,
                                         "The operation completed; the shell has not announced its next primary prompt.")
            elif state == "unknown":
                current = PaneStatus("unknown", request.backend, request.binding_key,
                                     request.name, current.binding, operation_id,
                                     str(operation.get("detail") or "The operation outcome remains uncertain; replay is forbidden."))
            else:
                current = PaneStatus("busy", request.backend, request.binding_key,
                                     request.name, current.binding, operation_id,
                                     "The operation has no completion receipt yet.")
            self._record(request, current, operation=operation)
            return current


def inspect(service: ManagedPaneService, request: PaneRequest) -> PaneStatus:
    return service.inspect(request)


def ensure(service: ManagedPaneService, request: PaneRequest) -> PaneStatus:
    return service.ensure(request)


def transition(service: ManagedPaneService, request: PaneRequest, value: PaneTransition) -> PaneStatus:
    return service.transition(request, value)


def reconcile(service: ManagedPaneService, request: PaneRequest, operation_id: str) -> PaneStatus:
    return service.reconcile(request, operation_id)

