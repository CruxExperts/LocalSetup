"""Read, validate, and manage private overrides of application profiles."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Any
import uuid

import fcntl

from ..paths import global_layout, repo_path
from ..schema import validate_json_schema


REGISTRY_RELATIVE_PATH = "ls/config/application-profiles/registry.json"
REGISTRY_SCHEMA_RELATIVE_PATH = "ls/config/application-profiles.schema.json"
ENVELOPE_SCHEMA_RELATIVE_PATH = "ls/config/application-profile.schema.json"
MAX_REGISTRY_BYTES = 256 * 1024
MAX_PROFILE_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_LEGACY_PROFILE_KEYS = {"schema_version", "settings"}


class ApplicationProfileError(ValueError):
    """A profile, registry, or private preference is invalid."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApplicationProfileError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ApplicationProfileError(f"non-finite JSON number is not supported: {value}")


def _bounded_json(raw: bytes, *, label: str, limit: int) -> Any:
    if len(raw) > limit:
        raise ApplicationProfileError(f"{label} exceeds {limit} bytes")
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ApplicationProfileError(f"{label} must be valid UTF-8 JSON") from exc

    nodes = 0

    def inspect(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_JSON_DEPTH:
            raise ApplicationProfileError(f"{label} exceeds the maximum JSON nesting depth")
        if nodes > MAX_JSON_NODES:
            raise ApplicationProfileError(f"{label} exceeds the maximum JSON value count")
        if isinstance(value, dict):
            for key, item in value.items():
                inspect(key, depth + 1)
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)

    inspect(payload, 0)
    return payload


def _read_regular_json(path: Path, *, label: str, limit: int) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ApplicationProfileError(f"{label} must be a regular file")
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise ApplicationProfileError(f"{label} cannot be read") from exc
    return _bounded_json(raw, label=label, limit=limit)


def _config_root(source_root: Path) -> Path:
    root = source_root.expanduser().resolve(strict=False)
    candidate = root / "ls" / "config"
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ApplicationProfileError("profile configuration escapes the source root") from exc
    return candidate


def _safe_config_file(source_root: Path, relative: str) -> Path:
    try:
        return repo_path(source_root, relative, "application profile resource")
    except (OSError, ValueError) as exc:
        raise ApplicationProfileError(f"invalid framework profile resource path: {relative}") from exc


def _validate_schema_file(payload: dict[str, Any], path: Path, *, label: str) -> list[str]:
    if path.is_symlink() or not path.is_file():
        raise ApplicationProfileError(f"{label} schema is missing or not a regular file")
    return validate_json_schema(payload, path, label=label, required=True)


def _load_registry(source_root: Path) -> dict[str, Any]:
    registry_path = _safe_config_file(source_root, REGISTRY_RELATIVE_PATH)
    registry_schema = _safe_config_file(source_root, REGISTRY_SCHEMA_RELATIVE_PATH)
    payload = _read_regular_json(registry_path, label="application profile registry", limit=MAX_REGISTRY_BYTES)
    if not isinstance(payload, dict):
        raise ApplicationProfileError("application profile registry must be a JSON object")
    issues = _validate_schema_file(payload, registry_schema, label="application profile registry")
    if issues:
        raise ApplicationProfileError("; ".join(issues))
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ApplicationProfileError("unsupported application profile registry schema version")
    return payload


def _spec(source_root: Path, application: str, name: str) -> tuple[Path, Path]:
    if not isinstance(application, str) or not _NAME_RE.fullmatch(application):
        raise ApplicationProfileError("invalid application profile application name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ApplicationProfileError("invalid application profile name")
    registry = _load_registry(source_root)
    app = registry["applications"].get(application)
    if app is None:
        raise ApplicationProfileError(f"unknown application profile application: {application}")
    entry = app["profiles"].get(name)
    if entry is None:
        raise ApplicationProfileError(f"unknown profile for {application}: {name}")
    config_root = _config_root(source_root)
    defaults_rel = entry["defaults"]
    schema_rel = entry["settings_schema"]
    # Registry paths are validated by schema and containment checks before they
    # are used to access framework-owned resources.
    defaults_path = _safe_config_file(source_root, f"ls/config/{defaults_rel}")
    settings_schema_path = _safe_config_file(source_root, f"ls/config/{schema_rel}")
    if defaults_path.parent.resolve(strict=False) != (config_root / "application-profiles" / application).resolve(strict=False):
        raise ApplicationProfileError("profile defaults must be stored under their application directory")
    if settings_schema_path.parent.resolve(strict=False) != config_root.resolve(strict=False):
        raise ApplicationProfileError("profile settings schema must be stored under ls/config")
    return defaults_path, settings_schema_path


def _envelope_schema(source_root: Path) -> Path:
    return _safe_config_file(source_root, ENVELOPE_SCHEMA_RELATIVE_PATH)


def _validate_payload(
    source_root: Path,
    settings_schema_path: Path,
    payload: Any,
    *,
    label: str,
    expected_application: str | None = None,
    expected_name: str | None = None,
    expected_base_profile: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApplicationProfileError(f"{label} must be a JSON object")
    envelope_issues = _validate_schema_file(payload, _envelope_schema(source_root), label=label)
    if envelope_issues:
        raise ApplicationProfileError("; ".join(envelope_issues))
    version = payload.get("schema_version")
    if type(version) is not int or version != 1:
        raise ApplicationProfileError(f"unsupported {label} schema version")
    expected_identity = {
        "application": expected_application,
        "name": expected_name,
        "base_profile": expected_base_profile,
    }
    for field, expected in expected_identity.items():
        if expected is not None and payload.get(field) != expected:
            raise ApplicationProfileError(f"{label}.{field} does not match its bound profile identity")
    base_profile = payload.get("base_profile")
    if not isinstance(base_profile, str) or not _NAME_RE.fullmatch(base_profile):
        raise ApplicationProfileError(f"{label}.base_profile is invalid")
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ApplicationProfileError(f"{label}.settings must be an object")
    settings_issues = _validate_schema_file(settings, settings_schema_path, label=f"{label}.settings")
    if settings_issues:
        raise ApplicationProfileError("; ".join(settings_issues))
    return settings


def _load_user_document(
    path: Path,
    *,
    application: str,
    name: str,
    source_root: Path,
    settings_schema_path: Path,
    registered_base_profile: str | None = None,
) -> tuple[str, dict[str, Any]]:
    payload = _read_regular_json(path, label=f"user {application}/{name} profile", limit=MAX_PROFILE_BYTES)
    if not isinstance(payload, dict):
        raise ApplicationProfileError(f"user {application}/{name} profile must be a JSON object")
    if set(payload) == _LEGACY_PROFILE_KEYS:
        if registered_base_profile != name:
            raise ApplicationProfileError("legacy profile documents are accepted only at a registered base-profile path")
        payload = {
            "schema_version": payload.get("schema_version"),
            "application": application,
            "name": name,
            "base_profile": registered_base_profile,
            "settings": payload.get("settings"),
        }
    if payload.get("application") != application or payload.get("name") != name:
        raise ApplicationProfileError("user profile identity does not match its private path")
    base_profile = payload.get("base_profile")
    if not isinstance(base_profile, str) or not _NAME_RE.fullmatch(base_profile):
        raise ApplicationProfileError("user profile base_profile is invalid")
    if registered_base_profile is not None and base_profile != registered_base_profile:
        raise ApplicationProfileError("user profile base_profile does not match the selected registered profile")
    # Resolving this spec proves that the declared base is registered and prevents
    # a saved variant from inheriting from another user variant.
    _, declared_schema_path = _spec(source_root, application, base_profile)
    if declared_schema_path.resolve(strict=False) != settings_schema_path.resolve(strict=False):
        raise ApplicationProfileError("user profile settings schema does not match its registered base")
    settings = _validate_payload(
        source_root,
        settings_schema_path,
        payload,
        label=f"user {application}/{name} profile",
        expected_application=application,
        expected_name=name,
        expected_base_profile=base_profile,
    )
    return base_profile, settings


def _user_document_base(
    path: Path,
    *,
    application: str,
    name: str,
    source_root: Path,
) -> str:
    payload = _read_regular_json(path, label=f"user {application}/{name} profile", limit=MAX_PROFILE_BYTES)
    if not isinstance(payload, dict) or set(payload) == _LEGACY_PROFILE_KEYS:
        raise ApplicationProfileError("named user profiles require a bound profile envelope")
    issues = _validate_schema_file(payload, _envelope_schema(source_root), label=f"user {application}/{name} profile")
    if issues:
        raise ApplicationProfileError("; ".join(issues))
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ApplicationProfileError("unsupported user application profile schema version")
    if payload.get("application") != application or payload.get("name") != name:
        raise ApplicationProfileError("user profile identity does not match its private path")
    base_profile = payload.get("base_profile")
    if not isinstance(base_profile, str) or not _NAME_RE.fullmatch(base_profile):
        raise ApplicationProfileError("user profile base_profile is invalid")
    _spec(source_root, application, base_profile)
    return base_profile


def _normalize_settings_input(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ApplicationProfileError("settings must be a JSON object")
    nodes = 0

    def inspect(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_JSON_DEPTH:
            raise ApplicationProfileError("settings exceed the maximum JSON nesting depth")
        if nodes > MAX_JSON_NODES:
            raise ApplicationProfileError("settings exceed the maximum JSON value count")
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ApplicationProfileError("settings object property names must be strings")
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)
        elif value is None or isinstance(value, (str, bool, int)):
            return
        elif isinstance(value, float) and math.isfinite(value):
            return
        else:
            raise ApplicationProfileError("settings must contain only finite JSON values")

    inspect(settings, 0)
    try:
        raw = json.dumps(settings, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ApplicationProfileError("settings must contain only finite JSON values") from exc
    return _bounded_json(raw, label="settings", limit=MAX_PROFILE_BYTES)


def _user_file(home: Path, application: str, name: str) -> Path:
    if not _NAME_RE.fullmatch(application) or not _NAME_RE.fullmatch(name):
        raise ApplicationProfileError("invalid application profile path segment")
    app_dir = _validate_user_directories(home, application, create=False)
    candidate = app_dir / f"{name}.json"
    if candidate.is_symlink():
        raise ApplicationProfileError("private application profile path must not contain symlinks")
    return candidate


def _validate_user_directories(home: Path, application: str, *, create: bool) -> Path:
    if not _NAME_RE.fullmatch(application):
        raise ApplicationProfileError("invalid application profile application name")
    home_path = Path(home).expanduser()
    if not home_path.is_absolute():
        home_path = Path.cwd() / home_path
    if not home_path.exists() and create:
        try:
            home_path.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise ApplicationProfileError("private application profile home cannot be created") from exc
    if home_path.is_symlink():
        raise ApplicationProfileError("private application profile home must not be a symlink")
    if home_path.exists():
        try:
            home_info = home_path.lstat()
        except OSError as exc:
            raise ApplicationProfileError("private application profile home is unavailable") from exc
        if (
            not stat.S_ISDIR(home_info.st_mode)
            or home_info.st_uid != os.getuid()
            or stat.S_IMODE(home_info.st_mode) & 0o022
        ):
            raise ApplicationProfileError("private application profile home has unsafe ownership or permissions")
    localsetup_home = global_layout(home_path).localsetup_home
    relative_dirs = (
        (".local", False),
        (".local/share", False),
        (".local/share/localsetup", False),
        (".local/share/localsetup/config", True),
        (".local/share/localsetup/config/application-profiles", True),
        (f".local/share/localsetup/config/application-profiles/{application}", True),
    )
    resolved_home = home_path.resolve(strict=False)
    for relative, private in relative_dirs:
        path = resolved_home / relative
        if path.is_symlink():
            raise ApplicationProfileError("private application profile path must not contain symlinks")
        if not path.exists():
            if not create:
                break
            try:
                path.mkdir(mode=0o700, exist_ok=True)
                os.chmod(path, 0o700)
            except OSError as exc:
                raise ApplicationProfileError("private application profile directories cannot be created") from exc
        try:
            info = path.lstat()
        except OSError as exc:
            raise ApplicationProfileError("private application profile directory is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ApplicationProfileError("private application profile ancestors must be owned real directories")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022 or (private and (mode & 0o077 or mode & 0o700 != 0o700)):
            raise ApplicationProfileError("private application profile ancestors have unsafe permissions")
    return localsetup_home / "config" / "application-profiles" / application


@contextmanager
def _profile_lock(path: Path, *, home: Path) -> Any:
    app_dir = _validate_user_directories(home, path.parent.name, create=True)
    if app_dir.resolve(strict=False) != path.parent.resolve(strict=False):
        raise ApplicationProfileError("private application profile directory changed during validation")
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ApplicationProfileError("private application profile lock is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ApplicationProfileError("private application profile lock has unsafe ownership or permissions")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _profile_digest(application: str, name: str, settings: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"schema_version": 1, "application": application, "name": name, "settings": settings},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _profile_record(
    application: str,
    name: str,
    base_profile: str,
    settings: dict[str, Any],
    origins: dict[str, str],
    settings_schema_path: Path,
) -> dict[str, Any]:
    issues = _validate_schema_file(settings, settings_schema_path, label=f"resolved {application}/{name}.settings")
    if issues:
        raise ApplicationProfileError("; ".join(issues))
    return {
        "schema_version": 1,
        "application": application,
        "name": name,
        "base_profile": base_profile,
        "settings": settings,
        "origins": origins,
        "digest": _profile_digest(application, name, settings),
    }


def _load_defaults(
    source_root: Path,
    application: str,
    base_profile: str,
) -> tuple[dict[str, Any], Path]:
    defaults_path, settings_schema_path = _spec(source_root, application, base_profile)
    payload = _read_regular_json(
        defaults_path,
        label=f"{application}/{base_profile} defaults",
        limit=MAX_PROFILE_BYTES,
    )
    defaults = _validate_payload(
        source_root,
        settings_schema_path,
        payload,
        label=f"{application}/{base_profile} defaults",
        expected_application=application,
        expected_name=base_profile,
        expected_base_profile=base_profile,
    )
    schema = _read_regular_json(
        settings_schema_path,
        label=f"{application}/{base_profile} settings schema",
        limit=MAX_REGISTRY_BYTES,
    )
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or set(defaults) != set(properties):
        raise ApplicationProfileError(
            f"{application}/{base_profile} defaults must define every registered setting"
        )
    return defaults, settings_schema_path


def _merge_settings(
    defaults: dict[str, Any],
    override: dict[str, Any],
    *,
    origin: str = "default",
) -> tuple[dict[str, Any], dict[str, str]]:
    origins: dict[str, str] = {}

    def merge(base: Any, incoming: Any, pointer: str, incoming_origin: str) -> Any:
        if isinstance(base, dict) and isinstance(incoming, dict):
            result: dict[str, Any] = {}
            for key in sorted(set(base) | set(incoming)):
                child_pointer = f"{pointer}/{_escape_pointer(str(key))}"
                if key in base and key in incoming:
                    result[key] = merge(base[key], incoming[key], child_pointer, incoming_origin)
                elif key in incoming:
                    result[key] = merge({}, incoming[key], child_pointer, incoming_origin)
                else:
                    result[key] = merge(base[key], base[key], child_pointer, "default")
            if not result:
                origins[pointer or "/settings"] = incoming_origin if incoming else "default"
            return result
        if isinstance(incoming, dict):
            result = {key: merge({}, value, f"{pointer}/{_escape_pointer(str(key))}", incoming_origin) for key, value in sorted(incoming.items())}
            if not result:
                origins[pointer or "/settings"] = incoming_origin
            return result
        origins[pointer or "/settings"] = incoming_origin
        return incoming

    effective = merge(defaults, override, "/settings", origin)
    return effective, dict(sorted(origins.items()))


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def resolve_profile(
    application: str = "tmux",
    name: str = "agent-shell",
    *,
    source_root: Path,
    home: Path,
) -> dict[str, Any]:
    """Resolve registered defaults and one path-bound user override or variant."""
    if not isinstance(application, str) or not _NAME_RE.fullmatch(application):
        raise ApplicationProfileError("invalid application profile application name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ApplicationProfileError("invalid application profile name")
    registry = _load_registry(source_root)
    app = registry["applications"].get(application)
    if app is None:
        raise ApplicationProfileError(f"unknown application profile application: {application}")
    registered = app["profiles"]
    user_path = _user_file(home, application, name)
    if name in registered:
        base_profile = name
    else:
        if not user_path.exists():
            raise ApplicationProfileError(f"unknown profile for {application}: {name}")
        base_profile = _user_document_base(
            user_path, application=application, name=name, source_root=source_root,
        )
    defaults, settings_schema_path = _load_defaults(source_root, application, base_profile)
    override: dict[str, Any] = {}
    if user_path.exists():
        bound_base, override = _load_user_document(
            user_path,
            application=application,
            name=name,
            source_root=source_root,
            settings_schema_path=settings_schema_path,
            registered_base_profile=base_profile if name in registered else None,
        )
        if bound_base != base_profile:
            raise ApplicationProfileError("user profile base_profile changed while it was being resolved")
    effective, origins = _merge_settings(defaults, override, origin="user")
    return _profile_record(application, name, base_profile, effective, origins, settings_schema_path)


def load_tmux_runtime_defaults(*, source_root: Path, name: str = "agent-shell") -> dict[str, Any]:
    """Load schema-checked packaged native defaults without touching private state."""
    base_name = name
    defaults_path = _safe_config_file(
        source_root, f"ls/config/application-profiles/tmux/runtime/{base_name}.json")
    schema_path = _safe_config_file(source_root, "ls/config/application-profile-tmux-runtime.schema.json")
    defaults = _read_regular_json(defaults_path, label="tmux runtime defaults", limit=MAX_PROFILE_BYTES)
    schema = _read_regular_json(schema_path, label="tmux runtime schema", limit=MAX_REGISTRY_BYTES)
    if (not isinstance(defaults, dict) or not isinstance(schema, dict)
            or defaults.get("name") != base_name
            or _validate_schema_file(defaults, schema_path, label="tmux runtime defaults")):
        raise ApplicationProfileError("packaged tmux runtime defaults are invalid")
    return defaults


def resolve_tmux_runtime_profile(*, source_root: Path, home: Path,
                                 name: str = "agent-shell") -> dict[str, Any]:
    """Initialize and resolve private runtime preferences without changing legacy profiles."""
    from jsonschema import Draft202012Validator

    profile = resolve_profile("tmux", name, source_root=source_root, home=home)
    base_name = str(profile["base_profile"])
    defaults = load_tmux_runtime_defaults(source_root=source_root, name=base_name)
    schema_path = _safe_config_file(source_root, "ls/config/application-profile-tmux-runtime.schema.json")
    schema = _read_regular_json(schema_path, label="tmux runtime schema", limit=MAX_REGISTRY_BYTES)
    legacy_path = _user_file(home, "tmux", name)
    warnings: list[dict[str, str]] = []
    with _profile_lock(legacy_path, home=home):
        runtime_dir = legacy_path.parent / "runtime"
        if not runtime_dir.exists() and not runtime_dir.is_symlink():
            runtime_dir.mkdir(mode=0o700, exist_ok=True)
        info = runtime_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ApplicationProfileError("private tmux runtime directory is unsafe")
        path = runtime_dir / f"{name}.json"
        if not path.exists() and not path.is_symlink():
            created_defaults = defaults | {"name": name}
            raw_defaults = (json.dumps(created_defaults, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            _atomic_private_write(path, raw_defaults)
            warnings.append({"pointer": "", "reason": "private runtime profile created from packaged defaults"})
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            try:
                file_info = os.fstat(descriptor)
                if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid()
                        or stat.S_IMODE(file_info.st_mode) & 0o077):
                    raise ApplicationProfileError("private tmux runtime file is unsafe")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_PROFILE_BYTES + 1)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ApplicationProfileError("private tmux runtime file cannot be read safely") from exc
    try:
        candidate = _bounded_json(raw, label="private tmux runtime profile", limit=MAX_PROFILE_BYTES)
    except ApplicationProfileError:
        candidate = defaults | {"name": name}
        warnings.append({"pointer": "", "reason": "malformed private runtime file; packaged defaults used without changing it"})
    if not isinstance(candidate, dict):
        candidate = defaults | {"name": name}
        warnings.append({"pointer": "", "reason": "runtime document is not an object; packaged defaults used"})
    if (candidate.get("application") != "tmux" or candidate.get("name") != name
            or type(candidate.get("schema_version")) is not int or candidate["schema_version"] < 1):
        raise ApplicationProfileError("private tmux runtime identity or schema version is unsupported")
    if any(key not in schema["properties"] for key in candidate):
        raise ApplicationProfileError("private tmux runtime has an unknown top-level field")
    required = candidate.get("required_features", [])
    if not isinstance(required, list) or required:
        raise ApplicationProfileError("private tmux runtime requires unsupported capabilities")
    extension = candidate.get("extensions", {})
    if not isinstance(extension, dict) or len(extension) > 32:
        raise ApplicationProfileError("private tmux runtime extensions are invalid")
    settings_schema = schema["properties"]["settings"]

    def selected(default: Any, incoming: Any, sub_schema: dict[str, Any], pointer: str) -> Any:
        if pointer == "/settings/theme":
            from ..terminal_ops.command_theme import validate_theme
            try:
                if list(Draft202012Validator(sub_schema).iter_errors(incoming)):
                    raise ValueError("Invalid theme definition")
                validate_theme(incoming)
            except (ValueError, TypeError, AttributeError):
                warnings.append({"pointer": pointer, "reason": "invalid theme; packaged defaults used"})
                return default
            return incoming if "roles" in incoming else default | incoming
        if isinstance(default, dict):
            if not isinstance(incoming, dict):
                warnings.append({"pointer": pointer, "reason": "invalid object; packaged defaults used"})
                return default
            result = dict(default)
            properties = sub_schema.get("properties", {})
            for key, item in incoming.items():
                child = f"{pointer}/{key}"
                if key not in default or key not in properties:
                    warnings.append({"pointer": child, "reason": "unknown preference ignored"})
                    continue
                result[key] = selected(default[key], item, properties[key], child)
            return result
        if list(Draft202012Validator(sub_schema).iter_errors(incoming)):
            warnings.append({"pointer": pointer, "reason": "invalid preference; packaged default used"})
            return default
        return incoming

    effective = selected(defaults["settings"], candidate.get("settings"), settings_schema, "/settings")
    return {"profile": profile, "settings": effective, "warnings": warnings,
            "digest": hashlib.sha256(json.dumps(effective, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True).encode("ascii")).hexdigest(),
            "extensions": extension, "schema_version": candidate["schema_version"]}


def list_profiles(
    *,
    source_root: Path,
    home: Path,
    application: str | None = None,
) -> list[dict[str, Any]]:
    """List registered profiles and valid named user variants."""
    registry = _load_registry(source_root)
    if application is not None:
        if not isinstance(application, str) or application not in registry["applications"]:
            raise ApplicationProfileError(f"unknown application profile application: {application}")
        applications = {application: registry["applications"][application]}
    else:
        applications = registry["applications"]
    rows: list[dict[str, Any]] = []
    for app in sorted(applications):
        registered_names = set(applications[app]["profiles"])
        for name in sorted(registered_names):
            profile = resolve_profile(app, name, source_root=source_root, home=home)
            rows.append({
                "application": app,
                "name": name,
                "base_profile": profile["base_profile"],
                "has_user_override": _user_file(home, app, name).exists(),
                "digest": profile["digest"],
            })
        app_dir = _user_file(home, app, "profile-scan").parent
        if not app_dir.exists():
            continue
        _validate_user_directories(home, app, create=False)
        try:
            entries = sorted(app_dir.iterdir(), key=lambda entry: entry.name)
        except OSError as exc:
            raise ApplicationProfileError(f"user {app} profiles cannot be listed") from exc
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            name = entry.name[:-5]
            if not _NAME_RE.fullmatch(name):
                raise ApplicationProfileError(f"invalid user application profile filename: {entry.name}")
            if name in registered_names:
                continue
            profile = resolve_profile(app, name, source_root=source_root, home=home)
            rows.append({
                "application": app,
                "name": name,
                "base_profile": profile["base_profile"],
                "has_user_override": True,
                "digest": profile["digest"],
            })
    return rows


def validate_profile(
    application: str,
    name: str,
    settings: dict[str, Any],
    *,
    source_root: Path,
    base_profile: str | None = None,
) -> dict[str, Any]:
    """Validate partial settings using an explicitly registered base profile."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ApplicationProfileError("invalid application profile name")
    registry = _load_registry(source_root)
    app = registry["applications"].get(application) if isinstance(application, str) else None
    if app is None:
        raise ApplicationProfileError(f"unknown application profile application: {application}")
    profiles = app["profiles"]
    if base_profile is None:
        if name not in profiles:
            raise ApplicationProfileError(f"unknown profile for {application}: {name}")
        base_profile = name
    elif base_profile not in profiles:
        raise ApplicationProfileError(f"unknown base profile for {application}: {base_profile}")
    elif name in profiles and name != base_profile:
        raise ApplicationProfileError("profile name and base_profile do not match the registered identity")
    _, settings_schema_path = _spec(source_root, application, base_profile)
    payload = {"schema_version": 1, "settings": _normalize_settings_input(settings)}
    issues = _validate_schema_file(payload["settings"], settings_schema_path, label=f"{application}/{name}.settings")
    if issues:
        raise ApplicationProfileError("; ".join(issues))
    return {
        "ok": True,
        "application": application,
        "name": name,
        "base_profile": base_profile,
        "settings": payload["settings"],
        "issues": [],
    }


def _atomic_private_write(path: Path, raw: bytes) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ApplicationProfileError("private application profile directory is unsafe")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ApplicationProfileError("private application profile could not be written atomically") from exc


def _backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ApplicationProfileError("private application profile target must be a regular file")
    try:
        with path.open("rb") as stream:
            current = stream.read(MAX_PROFILE_BYTES + 1)
    except OSError as exc:
        raise ApplicationProfileError("existing private application profile cannot be backed up") from exc
    if len(current) > MAX_PROFILE_BYTES:
        raise ApplicationProfileError("existing private application profile exceeds the supported size")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f".{path.name}.localsetup-backup-{timestamp}-{uuid.uuid4().hex}.tar.gz")
    buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as archive:
            member = tarfile.TarInfo(path.name)
            member.size = len(current)
            member.mode = 0o600
            member.mtime = int(datetime.now(timezone.utc).timestamp())
            archive.addfile(member, io.BytesIO(current))
    except (OSError, tarfile.TarError) as exc:
        raise ApplicationProfileError("existing private application profile could not be archived") from exc
    _atomic_private_write(backup, buffer.getvalue())
    return backup


def _merge_overrides(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(set(base) | set(incoming)):
        if key not in incoming:
            result[key] = base[key]
        elif key in base and isinstance(base[key], dict) and isinstance(incoming[key], dict):
            result[key] = _merge_overrides(base[key], incoming[key])
        else:
            result[key] = incoming[key]
    return result


def save_profile(
    application: str,
    name: str,
    settings: dict[str, Any],
    *,
    source_root: Path,
    home: Path,
    as_name: str | None = None,
) -> dict[str, Any]:
    """Save partial settings for a registered base or an immutable-base variant."""
    if not isinstance(application, str) or not _NAME_RE.fullmatch(application):
        raise ApplicationProfileError("invalid application profile application name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ApplicationProfileError("invalid application profile name")
    registry = _load_registry(source_root)
    app = registry["applications"].get(application)
    if app is None:
        raise ApplicationProfileError(f"unknown application profile application: {application}")
    registered = app["profiles"]
    if as_name is not None:
        if name not in registered:
            raise ApplicationProfileError("--as requires an explicitly registered base profile")
        if not isinstance(as_name, str) or not _NAME_RE.fullmatch(as_name):
            raise ApplicationProfileError("invalid saved application profile name")
        if as_name in registered:
            raise ApplicationProfileError("saved profile name conflicts with a registered base profile")
        target_name = as_name
        base_profile = name
    elif name in registered:
        target_name = name
        base_profile = name
    else:
        target_name = name
        target_path = _user_file(home, application, target_name)
        if not target_path.exists():
            raise ApplicationProfileError(f"unknown profile for {application}: {name}")
        base_profile = _user_document_base(
            target_path, application=application, name=target_name, source_root=source_root,
        )

    path = _user_file(home, application, target_name)
    _, settings_schema_path = _spec(source_root, application, base_profile)
    supplied = validate_profile(
        application,
        target_name,
        settings,
        source_root=source_root,
        base_profile=base_profile,
    )["settings"]

    with _profile_lock(path, home=home):
        current: dict[str, Any] = {}
        if path.exists():
            prior_base, current = _load_user_document(
                path,
                application=application,
                name=target_name,
                source_root=source_root,
                settings_schema_path=settings_schema_path,
                registered_base_profile=base_profile if target_name == base_profile else None,
            )
            if prior_base != base_profile:
                raise ApplicationProfileError("saved profile cannot change its registered base profile")
        merged = _merge_overrides(current, supplied)
        validation = validate_profile(
            application, target_name, merged,
            source_root=source_root, base_profile=base_profile,
        )
        defaults, settings_schema_path = _load_defaults(source_root, application, base_profile)
        effective, origins = _merge_settings(defaults, validation["settings"], origin="user")
        _profile_record(application, target_name, base_profile, effective, origins, settings_schema_path)
        payload = {
            "schema_version": 1,
            "application": application,
            "name": target_name,
            "base_profile": base_profile,
            "settings": validation["settings"],
        }
        raw = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(raw) > MAX_PROFILE_BYTES:
            raise ApplicationProfileError(f"user {application}/{target_name} profile exceeds {MAX_PROFILE_BYTES} bytes")
        if path.exists():
            try:
                if path.read_bytes() == raw:
                    return {
                        "ok": True, "changed": False, "path": str(path), "backup": None,
                        "profile": resolve_profile(application, target_name, source_root=source_root, home=home),
                    }
            except OSError as exc:
                raise ApplicationProfileError("existing private application profile cannot be read") from exc
        backup = _backup_existing(path)
        _atomic_private_write(path, raw)
        return {
            "ok": True,
            "changed": True,
            "path": str(path),
            "backup": str(backup) if backup else None,
            "profile": resolve_profile(application, target_name, source_root=source_root, home=home),
        }


def reset_profile(
    application: str,
    name: str,
    *,
    source_root: Path,
    home: Path,
) -> dict[str, Any]:
    """Remove a user's saved override or variant, retaining a private backup."""
    registry = _load_registry(source_root)
    app = registry["applications"].get(application) if isinstance(application, str) else None
    if app is None:
        raise ApplicationProfileError(f"unknown application profile application: {application}")
    registered = app["profiles"]
    path = _user_file(home, application, name)
    with _profile_lock(path, home=home):
        if name in registered:
            base_profile = name
            if path.exists():
                payload = _read_regular_json(path, label=f"user {application}/{name} profile", limit=MAX_PROFILE_BYTES)
                if not isinstance(payload, dict):
                    raise ApplicationProfileError(f"user {application}/{name} profile must be a JSON object")
                if set(payload) != _LEGACY_PROFILE_KEYS:
                    bound_base = _user_document_base(
                        path, application=application, name=name, source_root=source_root,
                    )
                    if bound_base != base_profile:
                        raise ApplicationProfileError("user profile base_profile does not match the registered profile")
        else:
            if not path.exists():
                raise ApplicationProfileError(f"unknown profile for {application}: {name}")
            current = resolve_profile(application, name, source_root=source_root, home=home)
            base_profile = current["base_profile"]
        backup = _backup_existing(path)
        removed = False
        if path.exists():
            try:
                path.unlink()
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                removed = True
            except OSError as exc:
                raise ApplicationProfileError("private application profile could not be reset") from exc
        defaults, settings_schema_path = _load_defaults(source_root, application, base_profile)
        effective, origins = _merge_settings(defaults, {}, origin="default")
        profile = _profile_record(application, name, base_profile, effective, origins, settings_schema_path)
        return {
            "ok": True,
            "changed": removed,
            "path": str(path),
            "backup": str(backup) if backup else None,
            "profile": profile,
        }


def plan_profile(
    application: str = "tmux",
    name: str = "agent-shell",
    *,
    source_root: Path,
    home: Path,
) -> dict[str, Any]:
    """Describe profile settings without implying a native target was inspected."""
    profile = resolve_profile(application, name, source_root=source_root, home=home)
    return {
        "ok": True,
        "application": application,
        "name": name,
        "base_profile": profile["base_profile"],
        "plan_scope": "profile-only",
        "native_adapter_registered": application == "tmux",
        "target_readback_available": False,
        "settings": profile["settings"],
        "origins": profile["origins"],
        "digest": profile["digest"],
        "actions": [{"kind": "apply_profile_settings", "status": "planned"}],
    }
