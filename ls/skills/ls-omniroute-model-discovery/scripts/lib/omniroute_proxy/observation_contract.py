from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ADAPTER_VERSION = "3.8.48"
ADAPTER_COMMIT = "7ee5bbc64dbb03e967521227f2afffeb7c9dad1e"
OBSERVATION_SCHEMA_VERSION = 1
UNKNOWN = "unknown"
MAX_MODELS = 256
MAX_ENDPOINT_OBSERVATIONS = 256
MAX_CONFLICTS = 128
SOURCE_ENDPOINTS = ("/api/models/catalog", "/v1/models")
SOURCE_PRIORITY = {path: index for index, path in enumerate(SOURCE_ENDPOINTS)}


class ObservationError(RuntimeError):
    """Stable, sanitized model-observation failure."""


def schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "references" / "model-observation.schema.json"


def _load_schema(path: Path | None = None) -> dict[str, Any]:
    target = path or schema_path()
    if not target.is_file() or target.is_symlink():
        raise ObservationError("observation_schema_missing")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ObservationError("observation_schema_unreadable") from None
    if not isinstance(payload, dict):
        raise ObservationError("observation_schema_invalid")
    return payload


def _manual_validate(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise ObservationError("observation_invariant_failed")
    models = payload.get("models")
    endpoints = payload.get("endpoint_observations")
    conflicts = payload.get("conflicts")
    if not isinstance(models, list) or len(models) > MAX_MODELS:
        raise ObservationError("observation_invariant_failed")
    if not isinstance(endpoints, list) or len(endpoints) > MAX_ENDPOINT_OBSERVATIONS:
        raise ObservationError("observation_invariant_failed")
    if not isinstance(conflicts, list) or len(conflicts) > MAX_CONFLICTS:
        raise ObservationError("observation_invariant_failed")
    model_keys = [
        (row.get("provider_id"), row.get("model_id"), row.get("identity"))
        for row in models
    ]
    endpoint_keys = [
        (row.get("route"), row.get("source_endpoint")) for row in endpoints
    ]
    if model_keys != sorted(model_keys) or len(model_keys) != len(set(model_keys)):
        raise ObservationError("observation_invariant_failed")
    if endpoint_keys != sorted(endpoint_keys) or len(endpoint_keys) != len(
        set(endpoint_keys)
    ):
        raise ObservationError("observation_invariant_failed")
    try:
        json.dumps(payload, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise ObservationError("observation_invariant_failed") from None


def validate_observation(
    payload: dict[str, Any],
    *,
    schema_file: Path | None = None,
) -> dict[str, Any]:
    _manual_validate(payload)
    schema = _load_schema(schema_file)
    try:
        errors = list(Draft202012Validator(schema).iter_errors(payload))
    except Exception:
        raise ObservationError("observation_schema_validation_failed") from None
    if errors:
        raise ObservationError("observation_schema_validation_failed")
    return payload
