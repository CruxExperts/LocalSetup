"""Versioned, bounded contracts for protected container metadata."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


PROTECTED_METADATA_SCHEMA = "localsetup.secure-container.protected-metadata"
PROTECTED_METADATA_MAJOR = 1
SUPPORTED_REQUIRED_FEATURES: frozenset[str] = frozenset()
_FEATURE_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")


class ProtectedSchemaError(ValueError):
    """Protected metadata does not follow its versioned schema contract."""


class UnsupportedProtectedSchemaVersion(ProtectedSchemaError):
    """The protected metadata uses an unsupported major schema version."""

    def __init__(self, major: int) -> None:
        self.major = major
        super().__init__(f"unsupported protected metadata schema major: {major}")


class UnsupportedRequiredFeature(ProtectedSchemaError):
    """The schema requires a feature this reader does not implement."""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(f"unsupported required feature: {feature}")


@dataclass(frozen=True)
class ProtectedSchemaView:
    major: int
    minor: int
    required_features: tuple[str, ...]
    ignored_optional_fields: tuple[str, ...]
    extension_bytes: int


def validate_protected_schema(
    payload: Mapping[str, Any],
    *,
    supported_major: int = PROTECTED_METADATA_MAJOR,
    supported_required_features: frozenset[str] = SUPPORTED_REQUIRED_FEATURES,
    max_required_features: int = 64,
    max_extensions: int = 64,
    max_extension_depth: int = 12,
    max_extension_bytes: int = 64 * 1024,
) -> ProtectedSchemaView:
    """Validate the protected-schema contract without interpreting extensions.

    Minor-version additions may remain optional in the bounded extensions map.
    A required feature is accepted only when the caller explicitly supplies it
    in supported_required_features. No extension can change trust semantics.
    """
    if not isinstance(payload, Mapping):
        raise ProtectedSchemaError("protected metadata must be a mapping")
    if payload.get("schema") != PROTECTED_METADATA_SCHEMA:
        raise ProtectedSchemaError("unsupported protected metadata schema identifier")
    major = _nonnegative_int(payload.get("major"), "major")
    minor = _nonnegative_int(payload.get("minor"), "minor")
    if major != supported_major:
        raise UnsupportedProtectedSchemaVersion(major)

    features_value = payload.get("required_features", [])
    if not isinstance(features_value, list) or len(features_value) > max_required_features:
        raise ProtectedSchemaError("required_features must be a bounded list")
    features: list[str] = []
    for value in features_value:
        if not isinstance(value, str) or _FEATURE_RE.fullmatch(value) is None:
            raise ProtectedSchemaError("required_features contains an invalid feature name")
        if value in features:
            raise ProtectedSchemaError("required_features contains a duplicate feature")
        features.append(value)
    unknown = sorted(set(features) - supported_required_features)
    if unknown:
        raise UnsupportedRequiredFeature(unknown[0])

    extension_value = payload.get("extensions", {})
    if not isinstance(extension_value, dict) or len(extension_value) > max_extensions:
        raise ProtectedSchemaError("extensions must be a bounded mapping")
    if any(not isinstance(key, str) or not key for key in extension_value):
        raise ProtectedSchemaError("extension names must be non-empty strings")
    _validate_json_tree(extension_value, depth=0, max_depth=max_extension_depth)
    try:
        encoded = json.dumps(
            extension_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtectedSchemaError("extensions must contain finite JSON values") from exc
    if len(encoded) > max_extension_bytes:
        raise ProtectedSchemaError("extensions exceed the configured byte limit")

    known_fields = {"schema", "major", "minor", "required_features", "extensions"}
    extra_fields = set(payload) - known_fields
    if extra_fields:
        raise ProtectedSchemaError("optional schema additions must be inside extensions")
    return ProtectedSchemaView(
        major=major,
        minor=minor,
        required_features=tuple(features),
        ignored_optional_fields=tuple(sorted(extension_value)),
        extension_bytes=len(encoded),
    )


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtectedSchemaError(f"{name} must be a non-negative integer")
    return value


def _validate_json_tree(value: Any, *, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise ProtectedSchemaError("extensions exceed the configured nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtectedSchemaError("extensions must contain finite numbers")
        return
    if isinstance(value, list):
        for child in value:
            _validate_json_tree(child, depth=depth + 1, max_depth=max_depth)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ProtectedSchemaError("extension object keys must be strings")
        for child in value.values():
            _validate_json_tree(child, depth=depth + 1, max_depth=max_depth)
        return
    raise ProtectedSchemaError("extensions must contain JSON values")
