"""Private grant-to-runtime wiring, never part of the model file tool schema."""
import json
import os
from pathlib import Path

from ..openpgp.container_access import resolve_reader, runtime_environment
from ..openpgp.container_reader import ContainerReadError
from .session_owner import _separate

SECRET_ENV = "LOCALSETUP_RUN_CONTAINER_SECRETS"


def runtime_root(value, workspace):
    if value is None:
        return None
    if type(value) is not str or len(value) > 4096 or "\x00" in value:
        raise ValueError("Invalid container runtime grant")
    root = Path(value)
    from ..openpgp.container_runtime import _root
    try:
        _root(root)
    except Exception:
        raise ValueError("Invalid container runtime grant") from None
    _separate(root, workspace)
    return root


def launch_environment(value, workspace):
    root = runtime_root(value, workspace)
    if root is None:
        return {}
    # Never install source-selected names as environment variable names in the
    # sealed interpreter. They are data under one fixed supervisor-only key.
    return {SECRET_ENV: json.dumps(runtime_environment(root, os.environ, workspace=workspace),
                                  sort_keys=True, separators=(",", ":"))}


def reader(value, workspace, *, deadline, check=lambda: None, protected=False):
    root = runtime_root(value, workspace)
    if root is None:
        return None
    environment = None
    if protected:
        raw = os.environ.get(SECRET_ENV, "{}")
        try:
            if len(raw.encode()) > 50_000:
                raise ValueError
            environment = json.loads(raw)
            if (type(environment) is not dict or len(environment) > 8
                    or any(type(key) is not str or type(item) is not str
                           or len(key) > 128 or len(item.encode()) > 4096
                           for key, item in environment.items())):
                raise ValueError
        except (ValueError, TypeError):
            raise ContainerReadError("unauthorized") from None
    return resolve_reader(root, workspace=workspace, deadline=deadline,
                          check=check, env_values=environment)
