"""Opt-in secure-container identity configuration and crash-safe initialization.

This module owns only private runtime configuration and local identity state.
It does not scan sources, schedule work, or enroll incoming content peers.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

from ..agent.runtime_lock import runtime_use
from ..paths import global_layout
from . import trust_state
from .artifact_records import validate_opaque_id
from .artifacts import ArtifactDiscoveryPolicy
from .container_consolidation_config import ConsolidationBinding, parse_binding
from .container_catalog import CatalogContext
from .container_delta_codec import DeltaCodec
from .container_storage import ensure_private_directory, read_private_file, write_private_file
from .container_identity import (
    IdentityError, MachineBinding, default_key_identity, machine_binding,
    private_possession_challenge,
)
from .contracts import KeyCapability, normalize_fingerprint, RSA_4096_TWO_YEAR_PROFILE
from .generation import KeyIdentity, generate_key, inspect_generation_candidate
from .keys import inspect_key
from .secrets import SecretProvider, SecretReference, SecretResolver, create_protected_secret


_CONFIG_SCHEMA = "localsetup.secure-container.runtime-config"
_IDENTITY_SCHEMA = "localsetup.secure-container.local-identity"
_MAX_JSON = 64 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-f0-9]{24}\Z")
_PHASES = ("prepared", "secret_ready", "publisher_ready", "owner_ready", "trust_ready", "ready")


class RuntimeErrorCode(RuntimeError):
    """Safe runtime error; code never contains paths, certificates, or secrets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class KeySelection:
    fingerprint: str
    certificate: bytes
    home: Path
    reference: SecretReference
    protected_file_root: Path | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "fingerprint", normalize_fingerprint(self.fingerprint))
            _absolute(self.home)
            if not isinstance(self.certificate, bytes) or not 0 < len(self.certificate) <= 32_768:
                raise ValueError
            if not isinstance(self.reference, SecretReference):
                raise ValueError
            if self.reference.provider is SecretProvider.PROTECTED_FILE:
                _absolute(self.protected_file_root)
            elif self.protected_file_root is not None:
                raise ValueError
        except Exception:
            raise RuntimeErrorCode("config_invalid") from None


@dataclass(frozen=True, slots=True)
class RemoteContentTrust:
    path: Path
    store_id: str
    scope: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            _absolute(self.path)
            if _HEX64.fullmatch(self.store_id) is None:
                raise ValueError
            _scope(self.scope)
        except Exception:
            raise RuntimeErrorCode("config_invalid") from None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    runtime_id: str
    revision: int = 0
    enabled: bool = False
    source_ids: tuple[str, ...] = ()
    catalog_recipients: tuple[str, ...] = ()
    custom_publisher: KeySelection | None = None
    custom_owner: KeySelection | None = None
    remote_content: RemoteContentTrust | None = None
    scan_interval_seconds: int = 900
    max_scan_objects: int = 10_000
    max_metadata_bytes: int = 1_048_576
    source_directories: tuple[Path, ...] = ()
    read_policy: ArtifactDiscoveryPolicy | None = None
    delta_codec: DeltaCodec | None = None
    max_delta_depth: int = 16
    consolidation: ConsolidationBinding | None = None

    def __post_init__(self) -> None:
        try:
            validate_opaque_id(self.runtime_id)
            if type(self.revision) is not int or self.revision < 0:
                raise ValueError
            if type(self.enabled) is not bool:
                raise ValueError
            if (self.consolidation is not None and (not isinstance(self.consolidation, ConsolidationBinding)
                    or self.read_policy is None or self.remote_content is None)):
                raise ValueError
            if (self.delta_codec is not None and not isinstance(self.delta_codec, DeltaCodec)
                    or type(self.max_delta_depth) is not int or not 1 <= self.max_delta_depth <= 64):
                raise ValueError
            if type(self.source_ids) is not tuple or len(self.source_ids) > 64:
                raise ValueError
            for value in self.source_ids:
                validate_opaque_id(value)
            if len(set(self.source_ids)) != len(self.source_ids):
                raise ValueError
            if (type(self.source_directories) is not tuple
                    or self.source_directories and len(self.source_directories) != len(self.source_ids)
                    or len(set(self.source_directories)) != len(self.source_directories)):
                raise ValueError
            for directory in self.source_directories:
                _absolute(directory)
            if self.read_policy is not None:
                if not isinstance(self.read_policy, ArtifactDiscoveryPolicy) or self.remote_content is None:
                    raise ValueError
                if (self.read_policy.trust_store_id != self.remote_content.store_id
                        or self.read_policy.scope != self.remote_content.scope):
                    raise ValueError
            if type(self.catalog_recipients) is not tuple or len(self.catalog_recipients) > 64:
                raise ValueError
            recipients = tuple(normalize_fingerprint(value) for value in self.catalog_recipients)
            if len(set(recipients)) != len(recipients):
                raise ValueError
            object.__setattr__(self, "catalog_recipients", recipients)
            if self.custom_publisher is not None and not isinstance(self.custom_publisher, KeySelection):
                raise ValueError
            if self.custom_owner is not None and not isinstance(self.custom_owner, KeySelection):
                raise ValueError
            if (self.custom_publisher is not None and self.custom_owner is not None
                    and self.custom_publisher.fingerprint == self.custom_owner.fingerprint):
                raise ValueError
            if self.remote_content is not None and not isinstance(self.remote_content, RemoteContentTrust):
                raise ValueError
            if (type(self.scan_interval_seconds) is not int or not 60 <= self.scan_interval_seconds <= 86_400
                    or type(self.max_scan_objects) is not int or not 1 <= self.max_scan_objects <= 1_000_000
                    or type(self.max_metadata_bytes) is not int or not 1024 <= self.max_metadata_bytes <= 16_777_216):
                raise ValueError
        except Exception:
            raise RuntimeErrorCode("config_invalid") from None


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: str
    config_revision: int
    enabled: bool
    label: str | None = None
    publisher_fingerprint: str | None = None
    owner_fingerprint: str | None = None
    backup_status: str = "backup_not_configured"


@dataclass(frozen=True, slots=True)
class RuntimeContexts:
    catalog: CatalogContext
    remote_content: trust_state.TrustSnapshot | None


def _absolute(value: Path) -> Path:
    if (not isinstance(value, Path) or not value.is_absolute() or ".." in value.parts
            or len(value.parts) > 128 or len(os.fspath(value)) > 4096):
        raise ValueError("path must be absolute")
    return value


def _root(value: Path) -> Path:
    try:
        root = _absolute(value)
        validate_opaque_id(root.name)
        return root
    except Exception:
        raise RuntimeErrorCode("runtime_root_invalid") from None


def global_runtime_root(home: Path, runtime_id: str) -> Path:
    try:
        validate_opaque_id(runtime_id)
        return global_layout(home).state_root / "secure-containers" / runtime_id
    except Exception:
        raise RuntimeErrorCode("runtime_root_invalid") from None


def _scope(scope: tuple[str, ...]) -> None:
    if (type(scope) is not tuple or not scope or len(scope) > 32
            or scope != tuple(sorted(set(scope)))
            or any(type(item) is not str or not item or len(item.encode("utf-8")) > 128
                   for item in scope)):
        raise ValueError("invalid scope")


def _private_directory(path: Path, *, create: bool) -> None:
    try:
        ensure_private_directory(path, create=create)
    except Exception:
        raise RuntimeErrorCode("state_unavailable") from None


def _canonical(value: dict[str, Any]) -> bytes:
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise RuntimeErrorCode("state_invalid") from None
    if len(data) > _MAX_JSON:
        raise RuntimeErrorCode("state_invalid")
    return data


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = read_private_file(path, maximum=_MAX_JSON)
    except FileNotFoundError:
        return None
    except ValueError:
        raise RuntimeErrorCode("state_invalid") from None
    except Exception:
        raise RuntimeErrorCode("state_unavailable") from None
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict or _canonical(value) != data:
            raise ValueError
        return value
    except (ValueError, UnicodeError, RuntimeErrorCode, RecursionError):
        raise RuntimeErrorCode("state_invalid") from None


def _write(path: Path, value: dict[str, Any]) -> None:
    data = _canonical(value)
    try:
        write_private_file(path, data, replace=True)
    except Exception:
        raise RuntimeErrorCode("state_unavailable") from None


def _key_dict(value: KeySelection | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"fingerprint": value.fingerprint,
            "certificate": base64.b64encode(value.certificate).decode("ascii"),
            "home": str(value.home),
            "protected_file_root": None if value.protected_file_root is None else str(value.protected_file_root),
            "reference": {"provider": value.reference.provider.value, "name": value.reference.name}}


def _parse_key(value: Any) -> KeySelection | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"fingerprint", "certificate", "home", "reference", "protected_file_root"}:
        raise RuntimeErrorCode("config_invalid")
    reference = value["reference"]
    if type(reference) is not dict or set(reference) != {"provider", "name"}:
        raise RuntimeErrorCode("config_invalid")
    try:
        certificate = base64.b64decode(value["certificate"], validate=True)
        if base64.b64encode(certificate).decode("ascii") != value["certificate"]:
            raise ValueError
        return KeySelection(value["fingerprint"], certificate, Path(value["home"]),
                            SecretReference(SecretProvider(reference["provider"]), reference["name"]),
                            None if value["protected_file_root"] is None else Path(value["protected_file_root"]))
    except Exception:
        raise RuntimeErrorCode("config_invalid") from None


def _config_dict(value: RuntimeConfig) -> dict[str, Any]:
    remote = value.remote_content
    worker_required = value.read_policy is not None and value.read_policy.require_worker_snapshots
    return {"schema": _CONFIG_SCHEMA, "version": 5 if value.consolidation is not None else 4 if worker_required else 3,
            **({'consolidation': value.consolidation.record()} if value.consolidation is not None else {}),
            "runtime_id": value.runtime_id,
            "revision": value.revision, "enabled": value.enabled,
            "source_ids": list(value.source_ids),
            "catalog_recipients": list(value.catalog_recipients),
            "custom_publisher": _key_dict(value.custom_publisher),
            "custom_owner": _key_dict(value.custom_owner),
            "remote_content": None if remote is None else {
                "path": str(remote.path), "store_id": remote.store_id, "scope": list(remote.scope)},
            "scan_interval_seconds": value.scan_interval_seconds,
            "max_scan_objects": value.max_scan_objects,
            "max_metadata_bytes": value.max_metadata_bytes,
            "source_directories": [str(path) for path in value.source_directories],
            "delta_codec": None if value.delta_codec is None else {
                **asdict(value.delta_codec), "executable": str(value.delta_codec.executable)},
            "max_delta_depth": value.max_delta_depth,
            "read_policy": None if value.read_policy is None else {
                key: list(item) if isinstance(item, tuple) else item
                for key, item in asdict(value.read_policy).items()
                if key != 'require_worker_snapshots' or item is True}}


def _parse_config(value: dict[str, Any], runtime_id: str) -> RuntimeConfig:
    # Read the original configuration without rewriting its bytes or activating it.
    if value.get("version") == 1 and type(value.get("version")) is int:
        legacy_keys = set(_config_dict(RuntimeConfig(runtime_id))) - {
            "source_directories", "read_policy", "delta_codec", "max_delta_depth"}
        if set(value) != legacy_keys:
            raise RuntimeErrorCode("config_invalid")
        value = {**value, "version": 2, "source_directories": [], "read_policy": None}
    if value.get("version") == 2 and type(value.get("version")) is int:
        legacy_keys = set(_config_dict(RuntimeConfig(runtime_id))) - {"delta_codec", "max_delta_depth"}
        if set(value) != legacy_keys:
            raise RuntimeErrorCode("config_invalid")
        value = {**value, "version": 3, "delta_codec": None, "max_delta_depth": 16}
    fields = set(_config_dict(RuntimeConfig(runtime_id)))
    if value.get('version') == 5:
        fields.add('consolidation')
    if set(value) != fields or value.get("schema") != _CONFIG_SCHEMA or type(value.get("version")) is not int or value["version"] not in (3, 4, 5):
        raise RuntimeErrorCode("config_invalid")
    remote = value["remote_content"]
    if remote is not None and (type(remote) is not dict or set(remote) != {"path", "store_id", "scope"}):
        raise RuntimeErrorCode("config_invalid")
    try:
        config = RuntimeConfig(
            runtime_id=value["runtime_id"], revision=value["revision"], enabled=value["enabled"],
            source_ids=tuple(value["source_ids"]),
            catalog_recipients=tuple(value["catalog_recipients"]),
            custom_publisher=_parse_key(value["custom_publisher"]),
            custom_owner=_parse_key(value["custom_owner"]),
            remote_content=None if remote is None else RemoteContentTrust(
                Path(remote["path"]), remote["store_id"], tuple(remote["scope"])),
            scan_interval_seconds=value["scan_interval_seconds"],
            max_scan_objects=value["max_scan_objects"],
            max_metadata_bytes=value["max_metadata_bytes"],
            source_directories=tuple(Path(item) for item in value["source_directories"]),
            read_policy=_parse_read_policy(value["read_policy"]),
            delta_codec=None if value['delta_codec'] is None else DeltaCodec(**{
                **value['delta_codec'], 'executable': Path(value['delta_codec']['executable'])}),
            max_delta_depth=value['max_delta_depth'],
            consolidation=parse_binding(value.get('consolidation')),
        )
        if config.runtime_id != runtime_id or _config_dict(config) != value:
            raise ValueError
        return config
    except Exception:
        raise RuntimeErrorCode("config_invalid") from None


def _parse_read_policy(value):
    if value is None:
        return None
    if type(value) is not dict:
        raise RuntimeErrorCode("config_invalid")
    return ArtifactDiscoveryPolicy(**{key: tuple(item) if key in {"scope", "audience"} else item
                                     for key, item in value.items()})


def _load_config_locked(root: Path) -> RuntimeConfig:
    value = _read(root / "runtime.json")
    return RuntimeConfig(root.name) if value is None else _parse_config(value, root.name)


def load_runtime_config(root: Path) -> RuntimeConfig:
    """Observe configuration without creating files or activating trust events."""
    selected = _root(root)
    try:
        selected.lstat()
    except FileNotFoundError:
        return RuntimeConfig(selected.name)
    except OSError:
        raise RuntimeErrorCode("state_unavailable") from None
    _private_directory(selected, create=False)
    try:
        with runtime_use(selected, create=False, timeout=0):
            return _load_config_locked(selected)
    except (OSError, ValueError, TimeoutError):
        raise RuntimeErrorCode("state_unavailable") from None


def configure_runtime(root: Path, *, expected_revision: int, proposed: RuntimeConfig) -> RuntimeConfig:
    """Write one authorized config revision by exact compare-and-swap."""
    selected = _root(root)
    if (type(expected_revision) is not int or expected_revision < 0
            or not isinstance(proposed, RuntimeConfig) or proposed.runtime_id != selected.name):
        raise RuntimeErrorCode("config_invalid")
    _private_directory(selected, create=True)
    try:
        with runtime_use(selected, exclusive=True, timeout=1):
            current = _load_config_locked(selected)
            if current.revision != expected_revision:
                raise RuntimeErrorCode("revision_conflict")
            result = replace(proposed, revision=expected_revision + 1)
            _write(selected / "runtime.json", _config_dict(result))
            return result
    except TimeoutError:
        raise RuntimeErrorCode("busy") from None


def _selection_digest(config: RuntimeConfig) -> str:
    material = {"publisher": _key_dict(config.custom_publisher),
                "owner": _key_dict(config.custom_owner)}
    return hashlib.sha256(_canonical(material)).hexdigest()


def _parse_identity(value: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "version", "attempt_id", "phase", "selection_digest", "kind",
                "binding", "label", "year", "scope", "publisher", "owner", "trust_store_id"}
    if (set(value) != required or value.get("schema") != _IDENTITY_SCHEMA
            or value.get("version") != 1 or value.get("phase") not in _PHASES
            or value.get("kind") not in {"automatic", "custom", "mixed"}
            or type(value.get("attempt_id")) is not str or _ID.fullmatch(value["attempt_id"]) is None
            or type(value.get("selection_digest")) is not str or _HEX64.fullmatch(value["selection_digest"]) is None
            or type(value.get("label")) is not str or re.fullmatch(r"[a-f0-9]{12}", value["label"]) is None
            or type(value.get("year")) is not int or not 2000 <= value["year"] <= 9999
            or type(value.get("scope")) is not list):
        raise RuntimeErrorCode("state_invalid")
    try:
        _scope(tuple(value["scope"]))
        binding = value["binding"]
        if binding is not None:
            if type(binding) is not dict or set(binding) != {"provider", "digest"}:
                raise ValueError
            MachineBinding(**binding)
        if value["kind"] in {"automatic", "mixed"} and binding is None:
            raise ValueError
        if value["trust_store_id"] is not None and _HEX64.fullmatch(value["trust_store_id"]) is None:
            raise ValueError
        for role in ("publisher", "owner"):
            item = value[role]
            if type(item) is not dict or set(item) != {"home", "reference", "fingerprint", "certificate", "protected_file_root", "recipe"}:
                raise ValueError
            recipe = item["recipe"]
            if recipe is not None:
                if type(recipe) is not dict or set(recipe) != {"name", "email", "comment", "certification_notations"}:
                    raise ValueError
                if type(recipe["certification_notations"]) is not list:
                    raise ValueError
                notation_pairs = tuple(
                    tuple(pair) if type(pair) is list and len(pair) == 2 else None
                    for pair in recipe["certification_notations"]
                )
                selected_recipe = KeyIdentity(
                    recipe["name"], recipe["email"], recipe["comment"], notation_pairs,
                )
                if (selected_recipe.name != f"LocalSetup Encryption {value['label']} {role.title()}"
                        or selected_recipe.email != "pgp@cruxexperts.com"
                        or not any(name == "description@localsetup.cruxexperts.com"
                                   and all(part in description for part in (
                                       "https://github.com/CruxExperts/LocalSetup",
                                       f"Copyright © {value['year']} Crux Experts LLC",
                                       "https://www.cruxexperts.com/",
                                       "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, distribution, or publication without prior written permission is prohibited.",
                                   )) for name, description in selected_recipe.certification_notations)):
                    raise ValueError
            _absolute(Path(item["home"]))
            reference = item["reference"]
            if type(reference) is not dict or set(reference) != {"provider", "name"}:
                raise ValueError
            SecretReference(SecretProvider(reference["provider"]), reference["name"])
            protected = item["protected_file_root"]
            if reference["provider"] == SecretProvider.PROTECTED_FILE.value:
                if protected is None:
                    raise ValueError
                _absolute(Path(protected))
            elif protected is not None:
                raise ValueError
            if item["fingerprint"] is not None:
                if normalize_fingerprint(item["fingerprint"]) != item["fingerprint"]:
                    raise ValueError
            if item["certificate"] is not None:
                certificate = base64.b64decode(item["certificate"], validate=True)
                if not certificate or len(certificate) > 32_768 or base64.b64encode(certificate).decode("ascii") != item["certificate"]:
                    raise ValueError
            if (item["fingerprint"] is None) != (item["certificate"] is None):
                raise ValueError
        recipe_count = sum(value[role]["recipe"] is not None for role in ("publisher", "owner"))
        if recipe_count != {"custom": 0, "mixed": 1, "automatic": 2}[value["kind"]]:
            raise ValueError
        phase = _PHASES.index(value["phase"])
        if (phase >= _PHASES.index("publisher_ready")
                and value["publisher"]["fingerprint"] is None):
            raise ValueError
        if (phase >= _PHASES.index("owner_ready")
                and value["owner"]["fingerprint"] is None):
            raise ValueError
        if phase >= _PHASES.index("trust_ready") and value["trust_store_id"] is None:
            raise ValueError
        return value
    except Exception:
        raise RuntimeErrorCode("state_invalid") from None


def _load_identity_locked(root: Path) -> dict[str, Any] | None:
    value = _read(root / "identity.json")
    return None if value is None else _parse_identity(value)


def _save_identity(root: Path, value: dict[str, Any]) -> None:
    _parse_identity(value)
    _write(root / "identity.json", value)


def _reference(item: dict[str, Any]) -> SecretReference:
    value = item["reference"]
    return SecretReference(SecretProvider(value["provider"]), value["name"])


def _role_resolver(item: dict[str, Any], fallback: SecretResolver) -> SecretResolver:
    if item["reference"]["provider"] == SecretProvider.PROTECTED_FILE.value:
        return SecretResolver(protected_file_root=Path(item["protected_file_root"]))
    return fallback


def _same_binding(saved: Any, observed: MachineBinding | None) -> bool:
    return saved == (None if observed is None else {"provider": observed.provider, "digest": observed.digest})


def _observed_binding() -> MachineBinding | None:
    try:
        return machine_binding()
    except IdentityError:
        return None


def inspect_runtime_status(root: Path) -> RuntimeStatus:
    """Read private records only; recorded_ready is not live key proof."""
    selected = _root(root)
    try:
        selected.lstat()
    except FileNotFoundError:
        return RuntimeStatus("unconfigured", 0, False)
    except OSError:
        raise RuntimeErrorCode("state_unavailable") from None
    _private_directory(selected, create=False)
    try:
        with runtime_use(selected, create=False, timeout=0):
            config = _load_config_locked(selected)
            identity = _load_identity_locked(selected)
    except (TimeoutError, OSError, ValueError):
        raise RuntimeErrorCode("state_unavailable") from None
    if identity is None:
        return RuntimeStatus("configured", config.revision, config.enabled)
    observed = _observed_binding()
    state = "recorded_ready" if identity["phase"] == "ready" else identity["phase"]
    if identity["binding"] is not None and observed is None:
        state = "binding_unavailable"
    elif not _same_binding(identity["binding"], observed):
        state = "foreign_binding"
    return RuntimeStatus(state, config.revision, config.enabled, identity["label"],
                         identity["publisher"]["fingerprint"], identity["owner"]["fingerprint"])


def _new_attempt(root: Path, config: RuntimeConfig, binding: MachineBinding | None) -> dict[str, Any]:
    custom_count = sum(value is not None for value in (config.custom_publisher, config.custom_owner))
    kind = {0: "automatic", 1: "mixed", 2: "custom"}[custom_count]
    if kind != "custom" and binding is None:
        raise RuntimeErrorCode("binding_unavailable")
    attempt_id = secrets.token_hex(12)
    label = secrets.token_hex(6)
    year = datetime.now(timezone.utc).year
    def role_item(role: str, custom: KeySelection | None) -> dict[str, Any]:
        if custom is not None:
            return {"home": str(custom.home), "reference": {
                "provider": custom.reference.provider.value, "name": custom.reference.name},
                "protected_file_root": None if custom.protected_file_root is None else str(custom.protected_file_root),
                "recipe": None,
                "fingerprint": custom.fingerprint,
                "certificate": base64.b64encode(custom.certificate).decode("ascii")}
        recipe = default_key_identity(label, role.title(), year)
        return {"home": str(root / "keys" / attempt_id / role),
                "reference": {"provider": SecretProvider.PROTECTED_FILE.value,
                              "name": f"LSC_{attempt_id}_{role}"},
                "protected_file_root": str(root / "secrets"),
                "recipe": {"name": recipe.name, "email": recipe.email,
                           "comment": recipe.comment,
                           "certification_notations": [list(pair) for pair in recipe.certification_notations]},
                "fingerprint": None, "certificate": None}
    return {"schema": _IDENTITY_SCHEMA, "version": 1, "attempt_id": attempt_id,
            "phase": "prepared", "selection_digest": _selection_digest(config),
            "kind": kind,
            "binding": None if binding is None else {"provider": binding.provider, "digest": binding.digest},
            "label": label, "year": year, "scope": [config.runtime_id],
            "publisher": role_item("publisher", config.custom_publisher),
            "owner": role_item("owner", config.custom_owner), "trust_store_id": None}


def _verify_role(identity: dict[str, Any], role: str, resolver: SecretResolver,
                 *, ready: bool) -> bytes:
    item = identity[role]
    home = Path(item["home"])
    reference = _reference(item)
    resolver = _role_resolver(item, resolver)
    fingerprint = item["fingerprint"]
    try:
        if item["recipe"] is not None:
            recipe = item["recipe"]
            expected = KeyIdentity(
                recipe["name"], recipe["email"], recipe["comment"],
                tuple(tuple(pair) for pair in recipe["certification_notations"]),
            )
            candidate = inspect_generation_candidate(
                keyring_home=home, fingerprint=fingerprint, expected_identity=expected,
                profile=RSA_4096_TWO_YEAR_PROFILE,
            )
            if candidate is None:
                if fingerprint is not None or ready:
                    raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
                generated = generate_key(
                    identity=expected, profile=RSA_4096_TWO_YEAR_PROFILE,
                    passphrase_reference=reference, secret_resolver=resolver,
                    keyring_home=home,
                )
                candidate = generated
            if fingerprint is not None and candidate.primary_fingerprint != fingerprint:
                raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
            certificate = candidate.public_certificate
            selected = candidate.primary_fingerprint
        else:
            if fingerprint is None or item["certificate"] is None:
                raise RuntimeErrorCode("partial_requires_repair")
            certificate = base64.b64decode(item["certificate"], validate=True)
            inspected = inspect_key(certificate=certificate)
            if (inspected.primary_fingerprint != fingerprint or inspected.revoked or inspected.expired
                    or not {KeyCapability.SIGN, KeyCapability.ENCRYPT}.issubset(inspected.capabilities)):
                raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
            selected = fingerprint
        private_possession_challenge(home=home, fingerprint=selected,
                                     reference=reference, resolver=resolver)
        encoded = base64.b64encode(certificate).decode("ascii")
        if item["certificate"] is not None and item["certificate"] != encoded:
            raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
        item["fingerprint"] = selected
        item["certificate"] = encoded
        return certificate
    except RuntimeErrorCode:
        raise
    except Exception:
        raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair") from None


def _trust_snapshot(root: Path, identity: dict[str, Any], certificates: tuple[bytes, bytes],
                    *, ready: bool) -> trust_state.TrustSnapshot:
    path = root / "local-trust.sqlite3"
    scope = tuple(identity["scope"])
    try:
        if not path.exists():
            if ready or identity["trust_store_id"] is not None:
                raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
            snapshot = trust_state.initialize_trust_state(
                path, scope=scope, publisher_certificate=certificates[0],
                owner_certificate=certificates[1],
            )
        else:
            snapshot = trust_state.load_trust_state(path, expected_scope=scope)
        if (snapshot.publisher is None or snapshot.owner is None
                or snapshot.publisher.fingerprint != identity["publisher"]["fingerprint"]
                or snapshot.owner.fingerprint != identity["owner"]["fingerprint"]
                or snapshot.publisher.certificate != certificates[0]
                or snapshot.owner.certificate != certificates[1]
                or (identity["trust_store_id"] is not None
                    and snapshot.store_id != identity["trust_store_id"])):
            raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
        return snapshot
    except RuntimeErrorCode:
        raise
    except Exception:
        raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair") from None


def setup_identity(root: Path) -> RuntimeStatus:
    """Authorize one default or explicit custom identity, resumable by phase.

    This is the sole key-generating entry point. The operator invokes it
    explicitly; configuration or installation does not call it.
    """
    selected = _root(root)
    _private_directory(selected, create=False)
    try:
        with runtime_use(selected, exclusive=True, timeout=1):
            config = _load_config_locked(selected)
            if config.revision == 0:
                raise RuntimeErrorCode("config_required")
            observed = _observed_binding()
            identity = _load_identity_locked(selected)
            if identity is None:
                identity = _new_attempt(selected, config, observed)
                _save_identity(selected, identity)
            ready = identity["phase"] == "ready"
            if identity["binding"] is not None and observed is None:
                raise RuntimeErrorCode("binding_unavailable")
            if not _same_binding(identity["binding"], observed):
                raise RuntimeErrorCode("foreign_binding")
            if identity["selection_digest"] != _selection_digest(config):
                raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
            if identity["scope"] != [config.runtime_id]:
                raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair")
            if identity["kind"] in {"automatic", "mixed"}:
                secrets_root = selected / "secrets"
                creating = identity["phase"] == "prepared"
                try:
                    _private_directory(secrets_root, create=creating)
                    _private_directory(selected / "keys", create=creating)
                    _private_directory(selected / "keys" / identity["attempt_id"], create=creating)
                except RuntimeErrorCode:
                    raise RuntimeErrorCode("lifecycle_required" if ready else "partial_requires_repair") from None
                if identity["phase"] == "prepared":
                    for role in ("publisher", "owner"):
                        if identity[role]["recipe"] is not None:
                            create_protected_secret(secrets_root, identity[role]["reference"]["name"])
                    identity["phase"] = "secret_ready"
                    _save_identity(selected, identity)
                resolver = SecretResolver(protected_file_root=secrets_root)
            else:
                if identity["phase"] == "prepared":
                    identity["phase"] = "secret_ready"
                    _save_identity(selected, identity)
                resolver = SecretResolver(protected_file_root=selected / "secrets"
                                          if (selected / "secrets").exists() else None)
            if ready:
                publisher = _verify_role(identity, "publisher", resolver, ready=True)
                owner = _verify_role(identity, "owner", resolver, ready=True)
                _trust_snapshot(selected, identity, (publisher, owner), ready=True)
            else:
                if identity["phase"] == "secret_ready":
                    _verify_role(identity, "publisher", resolver, ready=False)
                    identity["phase"] = "publisher_ready"
                    _save_identity(selected, identity)
                if identity["phase"] == "publisher_ready":
                    _verify_role(identity, "owner", resolver, ready=False)
                    identity["phase"] = "owner_ready"
                    _save_identity(selected, identity)
                if identity["phase"] in {"owner_ready", "trust_ready"}:
                    publisher = _verify_role(identity, "publisher", resolver, ready=False)
                    owner = _verify_role(identity, "owner", resolver, ready=False)
                    if publisher == owner or identity["publisher"]["fingerprint"] == identity["owner"]["fingerprint"]:
                        raise RuntimeErrorCode("partial_requires_repair")
                    snapshot = _trust_snapshot(selected, identity, (publisher, owner), ready=False)
                    identity["trust_store_id"] = snapshot.store_id
                    identity["phase"] = "trust_ready"
                    _save_identity(selected, identity)
                    identity["phase"] = "ready"
                    _save_identity(selected, identity)
            return RuntimeStatus("ready", config.revision, config.enabled, identity["label"],
                                 identity["publisher"]["fingerprint"],
                                 identity["owner"]["fingerprint"])
    except TimeoutError:
        raise RuntimeErrorCode("busy") from None


def resolve_runtime_contexts(root: Path, *, env_values: dict[str, str] | None = None) -> RuntimeContexts:
    """Resolve current LOCAL catalog and separately configured REMOTE content trust.

    This actual-use path may activate due trust transitions. It cannot create
    an identity, enroll a remote peer, or silently enable a disabled runtime.
    """
    selected = _root(root)
    try:
        selected.lstat()
    except FileNotFoundError:
        raise RuntimeErrorCode("runtime_disabled")
    except OSError:
        raise RuntimeErrorCode("state_unavailable") from None
    _private_directory(selected, create=False)
    try:
        with runtime_use(selected, create=False, timeout=0):
            config = _load_config_locked(selected)
            if not config.enabled:
                raise RuntimeErrorCode("runtime_disabled")
            identity = _load_identity_locked(selected)
            if identity is None or identity["phase"] != "ready":
                raise RuntimeErrorCode("identity_not_ready")
            observed = _observed_binding()
            if identity["binding"] is not None and observed is None:
                raise RuntimeErrorCode("binding_unavailable")
            if not _same_binding(identity["binding"], observed):
                raise RuntimeErrorCode("foreign_binding")
            if identity["selection_digest"] != _selection_digest(config):
                raise RuntimeErrorCode("lifecycle_required")
            resolver = SecretResolver(env_values=env_values, protected_file_root=selected / "secrets"
                                      if identity["kind"] in {"automatic", "mixed"} else None)
            publisher = _verify_role(identity, "publisher", resolver, ready=True)
            owner = _verify_role(identity, "owner", resolver, ready=True)
            local = _trust_snapshot(selected, identity, (publisher, owner), ready=True)
            remote = None
            if config.remote_content is not None:
                remote = trust_state.load_trust_state(
                    config.remote_content.path, expected_scope=config.remote_content.scope)
                if (remote.store_id != config.remote_content.store_id
                        or remote.store_id == local.store_id):
                    raise RuntimeErrorCode("remote_content_mismatch")
            recipients = config.catalog_recipients or (identity["publisher"]["fingerprint"],)
            catalog = CatalogContext(
                catalog_id=config.runtime_id,
                trust_state_path=selected / "local-trust.sqlite3", trust_store_id=local.store_id,
                expected_scope=local.scope, role="publisher", trust_revision=local.revision,
                governance_epoch=local.epoch,
                signer_fingerprint=identity["publisher"]["fingerprint"],
                recipient_fingerprints=recipients,
                gnupg_home=Path(identity["publisher"]["home"]),
                passphrase_reference=_reference(identity["publisher"]),
                secret_resolver=_role_resolver(identity["publisher"], resolver),
            )
            return RuntimeContexts(catalog, remote)
    except TimeoutError:
        raise RuntimeErrorCode("busy") from None
    except RuntimeErrorCode:
        raise
    except Exception:
        raise RuntimeErrorCode("runtime_unavailable") from None
