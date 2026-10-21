from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from .common import join_url
from .observation_contract import (
    ADAPTER_COMMIT,
    ADAPTER_VERSION,
    MAX_CONFLICTS,
    MAX_ENDPOINT_OBSERVATIONS,
    MAX_MODELS,
    OBSERVATION_SCHEMA_VERSION,
    SOURCE_ENDPOINTS,
    SOURCE_PRIORITY,
    UNKNOWN,
    ObservationError,
    validate_observation,
)
from .observation_rows import (
    MODEL_FIELDS,
    ReceiptEncoder,
    candidate,
    canonical,
    opaque_id,
    receipt_encoder,
    source_rows,
    source_payload_accepted,
)
from .probe import fetch_json


MAX_CONFLICT_VALUE_FINGERPRINTS = 8

OBSERVATION_NOTES = (
    "Runtime version, commit, and catalog revision are intentionally withheld and emitted as unknown, even when an approved endpoint reports them.",
    "Unknown model fields may be unreported, unsupported, or rejected by validation.",
    "Runtime provider, root, and alias strings emit only keyed opaque identifiers; raw values and keys are never emitted.",
    "With the same inputs and receipt key, model ordering and reviewed adapter/source provenance are deterministic.",
    "Opaque identifiers are deterministic only with the same non-exported key; unauthenticated observations use a fresh key.",
    "Only reviewed modality and logical endpoint enums emit verbatim.",
    "Pricing remains unknown because these endpoints have no approved public-price provenance class.",
    "This observation contains no equivalence or task-routing decisions.",
)


def _known(value: Any) -> bool:
    return value != UNKNOWN and value != [UNKNOWN]


def _select_field(
    identity: str,
    field: str,
    candidates: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    encoder: ReceiptEncoder,
) -> Any:
    known = [item for item in candidates if _known(item[field])]
    if not known:
        return UNKNOWN
    selected = known[0][field]
    if len({canonical(item[field]) for item in known}) > 1:
        fingerprints = [
            encoder.fingerprint(value)
            for value in sorted(
                {canonical(item[field]): item[field] for item in known}.values(),
                key=canonical,
            )
        ]
        conflicts.append(
            {
                "identity": identity,
                "field": field,
                "source_endpoints": sorted(
                    {item["source_endpoint"] for item in known}
                ),
                "value_fingerprints": fingerprints[:MAX_CONFLICT_VALUE_FINGERPRINTS],
                "value_fingerprint_truncation": {
                    "available": len(fingerprints),
                    "retained": min(
                        len(fingerprints), MAX_CONFLICT_VALUE_FINGERPRINTS
                    ),
                    "dropped": max(
                        0, len(fingerprints) - MAX_CONFLICT_VALUE_FINGERPRINTS
                    ),
                    "truncated": len(fingerprints) > MAX_CONFLICT_VALUE_FINGERPRINTS,
                },
            }
        )
    return selected


def _runtime_identity(catalog_payload: Any, openai_payload: Any) -> dict[str, Any]:
    """Report endpoint availability without attesting untrusted runtime identity."""
    payloads = [
        ("/api/models/catalog", catalog_payload),
        ("/v1/models", openai_payload),
    ]
    sources: list[str] = []
    for endpoint, payload in payloads:
        if source_payload_accepted(payload, endpoint):
            sources.append(endpoint)
    return {
        "version": UNKNOWN,
        "commit": UNKNOWN,
        "catalog_revision": UNKNOWN,
        "source_endpoints": sorted(sources),
    }


def _model_row(
    identity_key: tuple[str | None, str],
    candidates: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    encoder: ReceiptEncoder,
) -> dict[str, Any]:
    identity = encoder.fingerprint(identity_key, length=24)
    selected = {
        field: _select_field(identity, field, candidates, conflicts, encoder)
        for field in MODEL_FIELDS
    }
    provider, canonical_root = identity_key
    emitted_model = opaque_id("model", identity_key, encoder=encoder)
    return {
        "identity": identity,
        "model_id": emitted_model,
        "provider_id": opaque_id("provider", provider, encoder=encoder),
        "provider_model_id": opaque_id(
            "provider-model", identity_key, encoder=encoder
        ),
        "endpoints": selected["endpoints"],
        "modalities": {
            "input": selected["modalities.input"],
            "output": selected["modalities.output"],
        },
        "limits": {
            "context_tokens": selected["limits.context_tokens"],
            "output_tokens": selected["limits.output_tokens"],
        },
        "capabilities": {
            key.removeprefix("capabilities."): selected[key]
            for key in MODEL_FIELDS
            if key.startswith("capabilities.")
        },
        "reasoning_effort_options": selected["reasoning_effort_options"],
        "cost_evidence": {
            "currency": UNKNOWN,
            "input_per_million": UNKNOWN,
            "output_per_million": UNKNOWN,
            "source_endpoint": UNKNOWN,
            "provenance_class": "unavailable",
        },
        "source_endpoints": sorted(
            {item["source_endpoint"] for item in candidates}
        ),
    }


def _reconciled_groups(
    candidates: list[dict[str, Any]],
) -> dict[tuple[str | None, str], list[dict[str, Any]]]:
    anchors = {
        item["identity_key"]
        for item in candidates
        if item["has_canonical_root"]
    }
    token_anchors: dict[
        tuple[str | None, str], set[tuple[str | None, str]]
    ] = defaultdict(set)
    for item in candidates:
        if not item["has_canonical_root"]:
            continue
        for token in item["reconcile_tokens"]:
            token_anchors[token].add(item["identity_key"])

    grouped: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        identity_key = item["identity_key"]
        if not item["has_canonical_root"]:
            matches = {
                anchor
                for token in item["reconcile_tokens"]
                for anchor in token_anchors.get(token, set())
                if anchor in anchors
            }
            if len(matches) == 1:
                identity_key = next(iter(matches))
        grouped[identity_key].append(item)
    return grouped


def _truncation(
    input_rows: int,
    invalid_rows: int,
    duplicate_rows: int,
    models: list[dict[str, Any]],
    endpoints: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "models": {
            "input_rows": input_rows,
            "invalid_rows": invalid_rows,
            "duplicate_rows": duplicate_rows,
            "unique_models": len(models),
            "retained": min(len(models), MAX_MODELS),
            "dropped": max(0, len(models) - MAX_MODELS),
            "truncated": len(models) > MAX_MODELS,
        },
        "endpoint_observations": {
            "available": len(endpoints),
            "retained": min(len(endpoints), MAX_ENDPOINT_OBSERVATIONS),
            "dropped": max(0, len(endpoints) - MAX_ENDPOINT_OBSERVATIONS),
            "truncated": len(endpoints) > MAX_ENDPOINT_OBSERVATIONS,
        },
        "conflicts": {
            "available": len(conflicts),
            "retained": min(len(conflicts), MAX_CONFLICTS),
            "dropped": max(0, len(conflicts) - MAX_CONFLICTS),
            "truncated": len(conflicts) > MAX_CONFLICTS,
        },
    }


def build_model_observation(
    catalog_payload: Any,
    openai_payload: Any,
    *,
    observed_at: str,
    schema_path: Path | None = None,
    receipt_key: str | bytes | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Build a sanitized observation; ``receipt_key`` is test-only injection."""
    try:
        parsed_time = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        raise ObservationError("observation_time_invalid") from None
    if parsed_time.strftime("%Y-%m-%dT%H:%M:%SZ") != observed_at:
        raise ObservationError("observation_time_invalid")
    # Authentication authorizes endpoint requests only; it must not determine
    # public receipt material or persist in the resulting observation.
    del api_key
    encoder = receipt_encoder(test_key=receipt_key)

    parsed_candidates: list[dict[str, Any]] = []
    input_rows = 0
    invalid_rows = 0
    for endpoint, payload in zip(
        SOURCE_ENDPOINTS,
        (catalog_payload, openai_payload),
        strict=True,
    ):
        rows, invalid = source_rows(payload, endpoint)
        input_rows += len(rows) + invalid
        invalid_rows += invalid
        for provider_hint, row in rows:
            item = candidate(row, provider_hint, endpoint, encoder=encoder)
            if item is None:
                invalid_rows += 1
                continue
            parsed_candidates.append(item)

    grouped = _reconciled_groups(parsed_candidates)

    conflicts: list[dict[str, Any]] = []
    endpoint_evidence: set[tuple[str, str]] = set()
    duplicate_rows = 0
    models: list[dict[str, Any]] = []
    for identity_key in sorted(grouped, key=canonical):
        candidates = sorted(
            grouped[identity_key],
            key=lambda item: (
                SOURCE_PRIORITY[item["source_endpoint"]],
                canonical(item),
            ),
        )
        if any(item["source_endpoint"] == "/v1/models" for item in candidates):
            for item in candidates:
                if item["catalog_type_endpoint_fallback"]:
                    item["endpoints"] = UNKNOWN
        duplicate_rows += max(0, len(candidates) - 1)
        for item in candidates:
            endpoint_evidence.update(
                (route, item["source_endpoint"])
                for route in item["endpoint_observations"]
            )
        models.append(_model_row(identity_key, candidates, conflicts, encoder))

    models.sort(key=lambda row: (row["provider_id"], row["model_id"], row["identity"]))
    endpoints = [
        {"route": route, "source_endpoint": endpoint}
        for route, endpoint in sorted(endpoint_evidence)
    ]
    conflicts.sort(
        key=lambda row: (
            row["identity"],
            row["field"],
            row["source_endpoints"],
            row["value_fingerprints"],
        )
    )
    observation = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "adapter_source": {
            "name": "omniroute",
            "version": ADAPTER_VERSION,
            "commit": ADAPTER_COMMIT,
            "endpoints": list(SOURCE_ENDPOINTS),
        },
        "runtime": _runtime_identity(catalog_payload, openai_payload),
        "observed_at": observed_at,
        "models": models[:MAX_MODELS],
        "endpoint_observations": endpoints[:MAX_ENDPOINT_OBSERVATIONS],
        "conflicts": conflicts[:MAX_CONFLICTS],
        "truncation": _truncation(
            input_rows,
            invalid_rows,
            duplicate_rows,
            models,
            endpoints,
            conflicts,
        ),
        "notes": list(OBSERVATION_NOTES),
    }
    return validate_observation(observation, schema_file=schema_path)


def run_model_observation(
    base_url: str,
    api_key: str | None,
    timeout: float,
    *,
    observed_at: str,
) -> dict[str, Any]:
    effective_api_key = api_key if isinstance(api_key, str) and api_key.strip() else None
    payloads: dict[str, Any] = {}
    endpoint_status: list[dict[str, Any]] = []
    with requests.Session() as session:
        for path in SOURCE_ENDPOINTS:
            result = fetch_json(
                session,
                join_url(base_url, path),
                effective_api_key,
                timeout,
                include_payload=True,
            )
            payload = result.pop("_payload", None) if result.get("ok") else None
            payloads[path] = payload
            available = bool(result.get("ok")) and source_payload_accepted(payload, path)
            endpoint_status.append(
                {
                    "path": path,
                    "available": available,
                    "status": (
                        result.get("status")
                        if isinstance(result.get("status"), int)
                        else UNKNOWN
                    ),
                }
            )
    if not any(status["available"] for status in endpoint_status):
        raise ObservationError("observation_sources_unavailable")
    observation = build_model_observation(
        payloads["/api/models/catalog"],
        payloads["/v1/models"],
        observed_at=observed_at,
        api_key=effective_api_key,
    )
    observation["endpoint_status"] = endpoint_status
    return validate_observation(observation)
