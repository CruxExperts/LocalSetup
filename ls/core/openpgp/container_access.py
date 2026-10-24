"""Resolve configured container reading beneath the ordinary file interface."""
from dataclasses import replace
import hashlib
from pathlib import Path
import time

from ..agent.runtime_lock import runtime_use
from . import container_runtime as runtime
from .container_preparation import ContainerPreparationLimits
from .container_reader import ContainerReader, ContainerReadError
from .container_storage import ensure_private_directory
from .secrets import SecretProvider
from .trust_state import load_trust_state


def runtime_environment(root: Path, environment) -> dict[str, str]:
    """Select only configured ENV passphrases for the protected supervisor hop."""
    config = runtime.load_runtime_config(root)
    selected = {}
    for key in (config.custom_publisher, config.custom_owner):
        if key is not None and key.reference.provider is SecretProvider.ENV:
            name = key.reference.name
            if name in environment:
                value = environment[name]
                if type(value) is not str or len(value.encode()) > 4096:
                    raise ContainerReadError("unauthorized")
                selected[name] = value
    return selected


def _binding(root, config):
    with runtime_use(root, create=False, timeout=0):
        current = runtime._load_config_locked(root)
        identity = runtime._load_identity_locked(root)
        if current != config or not current.enabled or identity is None or identity["phase"] != "ready":
            raise ContainerReadError("unauthorized")
        observed = runtime._observed_binding()
        if not runtime._same_binding(identity["binding"], observed):
            raise ContainerReadError("unauthorized")
        local = load_trust_state(root / "local-trust.sqlite3", expected_scope=tuple(identity["scope"]))
        # Identity certificates are already bounded private configuration. None
        # of this material is returned to a model or embedded in a cursor.
        return hashlib.sha256(runtime._canonical(runtime._config_dict(current))
            + runtime._canonical(identity)
            + runtime._canonical({"catalog_trust": [local.store_id, local.revision, local.epoch]})).hexdigest()


def _resolve_service(root: Path, *, workspace: Path | None, deadline: float,
                     check=lambda: None, env_values=None):
    """Resolve an enabled identity and current incoming policy; never enroll."""
    from ..agent.container_reads import validate_reader
    from ..agent.session_owner import _separate
    try:
        check()
        if time.monotonic() >= deadline:
            raise ContainerReadError("expired")
        root = runtime._root(root)
        if workspace is not None:
            _separate(root, workspace)
        config = runtime.load_runtime_config(root)
        if not config.enabled or config.read_policy is None or config.remote_content is None:
            raise ContainerReadError("unauthorized")
        if Path(config.read_policy.boundary) != root / "reads":
            raise ContainerReadError("unauthorized")
        if workspace is not None:
            _separate(config.remote_content.path, workspace)
        for key in (config.custom_publisher, config.custom_owner):
            if key is not None and workspace is not None:
                _separate(key.home, workspace)
                if key.protected_file_root is not None:
                    _separate(key.protected_file_root, workspace)
        before = _binding(root, config)
        contexts = runtime.resolve_runtime_contexts(root, env_values=env_values)
        remote = contexts.remote_content
        if remote is None or (remote.store_id, remote.revision, remote.epoch) != (
                config.read_policy.trust_store_id, config.read_policy.trust_revision,
                config.read_policy.governance_epoch):
            raise ContainerReadError("unauthorized")
        def current():
            check()
            if time.monotonic() >= deadline:
                raise ContainerReadError("expired")
            observed = _binding(root, config)
            if observed != before:
                raise ContainerReadError("changed")
            return observed
        catalog = contexts.catalog
        limits = ContainerPreparationLimits()
        limits = replace(limits, container_limits=replace(limits.container_limits,
                         max_metadata_bytes=config.max_metadata_bytes))
        reader = ContainerReader(config.read_policy, config.remote_content.path,
            Path(catalog.gnupg_home), catalog.passphrase_reference, catalog.secret_resolver,
            limits=limits, max_sources=1024, binding_check=current,
            delta_codec=config.delta_codec, max_delta_depth=config.max_delta_depth)
        if workspace is not None:
            validate_reader(reader, workspace)
        reader.check()
        if config.source_ids and not config.source_directories:
            raise ContainerReadError("unavailable")
        if current() != before:
            raise ContainerReadError("changed")
        ensure_private_directory(root / "reads", create=True)
        from .container_catalog_runtime import CatalogRuntime
        return CatalogRuntime(root, config, catalog, reader)
    except ContainerReadError:
        raise
    except Exception:
        raise ContainerReadError("unavailable") from None


def resolve_reader(root: Path, *, workspace: Path, deadline: float,
                   check=lambda: None, env_values=None) -> ContainerReader:
    """Resolve grant-separated ordinary reads through the common runtime owner."""
    if not isinstance(workspace, Path):
        raise ContainerReadError("unauthorized")
    service = _resolve_service(root, workspace=workspace, deadline=deadline,
                               check=check, env_values=env_values)
    return replace(service.reader, metadata_resolver=service.bind_metadata,
                   reference_resolver=service.read_reference)


def resolve_catalog(root: Path, *, deadline: float, check=lambda: None):
    """Resolve local metadata maintenance without granting a workspace read."""
    return _resolve_service(root, workspace=None, deadline=deadline, check=check)
