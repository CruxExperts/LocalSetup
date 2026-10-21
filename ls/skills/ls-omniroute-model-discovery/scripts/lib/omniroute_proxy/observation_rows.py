from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

from .observation_contract import UNKNOWN


ALLOWED_MODALITIES = frozenset({"audio", "embedding", "image", "text", "video"})
ALLOWED_LOGICAL_ENDPOINTS = frozenset(
    {"audio", "chat", "embeddings", "images", "rerank", "responses", "video", "videos"}
)
ALLOWED_REASONING_EFFORTS = frozenset(
    {"high", "low", "medium", "minimal", "none", "xhigh"}
)
CATALOG_TYPE_ENDPOINTS = {
    "chat": ["chat"],
    "embedding": ["embeddings"],
}


# Keep untrusted identity components structured until they are fingerprinted.
# In particular, no delimiter can distinguish a raw component from a delimiter
# contained in a different component.
IdentityKey = tuple[str | None, str]
ReconcileToken = tuple[str | None, str]


def canonical(value: Any) -> str:
    """Return the injective, ASCII JSON representation used by receipt MACs."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


class ReceiptEncoder:
    """Create bounded opaque receipts without exporting their keyed material."""

    def __init__(self, key: bytes) -> None:
        if not key:
            raise ValueError("receipt_key_invalid")
        self._key = bytes(key)

    def fingerprint(self, value: Any, *, length: int = 16) -> str:
        return self._fingerprint_canonical(canonical(value), length=length)

    def _fingerprint_canonical(self, value: str, *, length: int) -> str:
        digest = hmac.new(
            self._key,
            value.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return digest[:length]


def receipt_encoder(
    *,
    test_key: str | bytes | None = None,
) -> ReceiptEncoder:
    """Create a per-observation encoder with test-only deterministic injection."""
    if test_key is not None:
        if isinstance(test_key, str):
            key = test_key.encode("utf-8", errors="surrogatepass")
        elif isinstance(test_key, bytes):
            key = test_key
        else:
            raise ValueError("receipt_key_invalid")
    else:
        key = secrets.token_bytes(32)
    return ReceiptEncoder(key)


def _bounded_raw(value: Any, *, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value


def _catalog_mapping(payload: dict[Any, Any]) -> dict[Any, Any] | None:
    """Return the catalog only when every provider bucket has a model list."""
    catalog = payload.get("catalog")
    if not isinstance(catalog, dict):
        return None
    if any(
        _bounded_raw(provider_key) is None
        or not isinstance(bucket, dict)
        or not isinstance(bucket.get("models"), list)
        for provider_key, bucket in catalog.items()
    ):
        return None
    return catalog


def opaque_id(kind: str, value: Any, *, encoder: ReceiptEncoder) -> str:
    if value is None or value == "":
        return UNKNOWN
    digest = encoder.fingerprint(value, length=20)
    return f"opaque-{kind}:{digest}"


def _nested(row: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value: Any = row
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if value is not None:
            return value
    return None


def _model_identity_provider_values(
    row: dict[str, Any],
    provider_hint: str | None,
) -> tuple[str, str | None, str | None] | None:
    alias_value = _nested(row, ("id",), ("model_id",), ("modelId",), ("name",))
    root_value = _nested(row, ("root",))
    alias_raw = _bounded_raw(alias_value)
    root_raw = _bounded_raw(root_value)
    if (isinstance(alias_value, str) and alias_raw is None) or (
        isinstance(root_value, str) and root_raw is None
    ):
        return None
    if alias_raw is None and root_raw is None:
        return None
    provider_value = (
        provider_hint
        if provider_hint is not None
        else _nested(
            row,
            ("owned_by",),
            ("provider_id",),
            ("providerId",),
            ("provider", "id"),
            ("provider",),
        )
    )
    provider_raw = _bounded_raw(provider_value)
    if provider_raw is None:
        return None
    return provider_raw, alias_raw, root_raw


def _catalog_model_lists_accepted(catalog: dict[Any, Any]) -> bool:
    """Return whether empty catalogs or any provider has usable model evidence."""
    has_nonempty_models = False
    for provider_key, bucket in catalog.items():
        provider = _bounded_raw(provider_key)
        models = bucket["models"]
        if not models:
            continue
        has_nonempty_models = True
        if provider is not None and any(
            isinstance(row, dict)
            and _model_identity_provider_values(row, provider) is not None
            for row in models
        ):
            return True
    return not has_nonempty_models


def _generic_model_list(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    return next(
        (
            payload[key]
            for key in ("data", "models", "items")
            if isinstance(payload.get(key), list)
        ),
        None,
    )


def source_payload_accepted(payload: Any, source_endpoint: str) -> bool:
    """Return whether an approved endpoint returned a usable catalog/list shape."""
    if (
        source_endpoint == "/api/models/catalog"
        and isinstance(payload, dict)
        and "catalog" in payload
    ):
        catalog = _catalog_mapping(payload)
        return catalog is not None and _catalog_model_lists_accepted(catalog)
    candidates = _generic_model_list(payload)
    if candidates is None:
        return False
    return not candidates or any(
        isinstance(row, dict)
        and _model_identity_provider_values(row, None) is not None
        for row in candidates
    )


def _explicit_endpoint_value(row: dict[str, Any]) -> tuple[bool, Any]:
    has_explicit_endpoints = False
    for key in ("supported_endpoints", "supportedEndpoints", "endpoints"):
        if key in row:
            has_explicit_endpoints = True
            if row[key] is not None:
                return True, row[key]
    return has_explicit_endpoints, None


def _enum_list(
    value: Any,
    *,
    allowed: frozenset[str],
    opaque_kind: str,
    encoder: ReceiptEncoder,
) -> list[str] | str:
    if not isinstance(value, list):
        return UNKNOWN
    values: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            continue
        values.add(
            item
            if item in allowed
            else opaque_id(opaque_kind, item, encoder=encoder)
        )
    return sorted(values)[:32] or UNKNOWN


def _reported_bool(value: Any) -> bool | str:
    return value if isinstance(value, bool) else UNKNOWN


def _reported_limit(value: Any) -> int | str:
    if isinstance(value, bool) or not isinstance(value, int):
        return UNKNOWN
    return value if 0 < value <= 100_000_000 else UNKNOWN


def _strip_provider_prefix(value: str, provider: str | None) -> str:
    prefix = f"{provider}/" if provider is not None else None
    return value[len(prefix) :] if prefix and value.startswith(prefix) else value


def _strip_one_prefix(value: str) -> str:
    return value.split("/", 1)[1] if "/" in value else value


def _identity_parts(
    provider_raw: str | None,
    alias_raw: str | None,
    root_raw: str | None,
) -> tuple[str | None, str, tuple[ReconcileToken, ...], bool]:
    provider = provider_raw
    if root_raw:
        canonical_root = _strip_provider_prefix(root_raw, provider)
        has_canonical_root = True
    else:
        canonical_root = _strip_provider_prefix(alias_raw or UNKNOWN, provider)
        has_canonical_root = False
    tokens = {
        candidate
        for candidate in (
            alias_raw,
            root_raw,
            canonical_root,
            _strip_one_prefix(alias_raw) if alias_raw else None,
            f"{provider}/{canonical_root}" if provider is not None else None,
        )
        if candidate
    }
    scoped_tokens = tuple(sorted(((provider, token) for token in tokens), key=canonical))
    return provider, canonical_root, scoped_tokens, has_canonical_root


def source_rows(
    payload: Any,
    source_endpoint: str,
) -> tuple[list[tuple[str | None, dict[str, Any]]], int]:
    rows: list[tuple[str | None, dict[str, Any]]] = []
    invalid = 0
    if (
        source_endpoint == "/api/models/catalog"
        and isinstance(payload, dict)
        and "catalog" in payload
    ):
        catalog = _catalog_mapping(payload)
        if catalog is None:
            return [], invalid + 1
        if not _catalog_model_lists_accepted(catalog):
            return [], invalid + sum(len(bucket["models"]) for bucket in catalog.values())
        for provider_key, bucket in catalog.items():
            models = bucket["models"]
            provider = _bounded_raw(provider_key)
            if provider is None:
                invalid += len(models)
                continue
            for row in models:
                if isinstance(row, dict):
                    rows.append((provider, row))
                else:
                    invalid += 1
        return rows, invalid
    candidates = _generic_model_list(payload)
    if candidates is None:
        return [], invalid + (1 if payload is not None else 0)
    if not source_payload_accepted(payload, source_endpoint):
        return [], invalid + len(candidates)
    for row in candidates:
        if isinstance(row, dict):
            rows.append((None, row))
        else:
            invalid += 1
    return rows, invalid


def candidate(
    row: dict[str, Any],
    provider_hint: str | None,
    source_endpoint: str,
    *,
    encoder: ReceiptEncoder,
) -> dict[str, Any] | None:
    identity_values = _model_identity_provider_values(row, provider_hint)
    if identity_values is None:
        return None
    provider_raw, alias_raw, root_raw = identity_values
    provider, canonical_root, reconcile_tokens, has_canonical_root = _identity_parts(
        provider_raw,
        alias_raw,
        root_raw,
    )
    has_explicit_endpoints, endpoints_value = _explicit_endpoint_value(row)
    catalog_type_endpoint_fallback = False
    if not has_explicit_endpoints and source_endpoint == "/api/models/catalog":
        catalog_type = row.get("type")
        if isinstance(catalog_type, str):
            catalog_type_endpoint_fallback = catalog_type in CATALOG_TYPE_ENDPOINTS
            endpoints_value = CATALOG_TYPE_ENDPOINTS.get(catalog_type)
    capabilities = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    return {
        "identity_key": (provider, canonical_root),
        "identity_provider": provider,
        "identity_root": canonical_root,
        "reconcile_tokens": reconcile_tokens,
        "has_canonical_root": has_canonical_root,
        "catalog_type_endpoint_fallback": catalog_type_endpoint_fallback,
        "endpoints": _enum_list(
            endpoints_value,
            allowed=ALLOWED_LOGICAL_ENDPOINTS,
            opaque_kind="endpoint",
            encoder=encoder,
        ),
        "endpoint_observations": [source_endpoint],
        "modalities.input": _enum_list(
            _nested(
                row,
                ("input_modalities",),
                ("inputModalities",),
                ("modalities", "input"),
            ),
            allowed=ALLOWED_MODALITIES,
            opaque_kind="modality",
            encoder=encoder,
        ),
        "modalities.output": _enum_list(
            _nested(
                row,
                ("output_modalities",),
                ("outputModalities",),
                ("modalities", "output"),
            ),
            allowed=ALLOWED_MODALITIES,
            opaque_kind="modality",
            encoder=encoder,
        ),
        "limits.context_tokens": _reported_limit(
            _nested(
                row,
                ("context_length",),
                ("contextLength",),
                ("context_window",),
            )
        ),
        "limits.output_tokens": _reported_limit(
            _nested(row, ("max_output_tokens",), ("maxOutputTokens",))
        ),
        "capabilities.tools": _reported_bool(
            _nested(
                row,
                ("capabilities", "tool_calling"),
                ("capabilities", "tools"),
                ("supports_tools",),
                ("supportsTools",),
            )
        ),
        "capabilities.vision": _reported_bool(
            _nested(row, ("capabilities", "vision"), ("supportsVision",))
        ),
        "capabilities.reasoning": _reported_bool(
            _nested(row, ("capabilities", "reasoning"), ("supportsReasoning",))
        ),
        "capabilities.embeddings": _reported_bool(capabilities.get("embeddings")),
        "capabilities.image_generation": _reported_bool(
            capabilities.get("image_generation")
        ),
        "capabilities.moderation": _reported_bool(capabilities.get("moderation")),
        "capabilities.rerank": _reported_bool(capabilities.get("rerank")),
        "capabilities.attachment": _reported_bool(capabilities.get("attachment")),
        "capabilities.structured_output": _reported_bool(
            capabilities.get("structured_output")
        ),
        "capabilities.temperature": _reported_bool(capabilities.get("temperature")),
        "capabilities.thinking": _reported_bool(capabilities.get("thinking")),
        "reasoning_effort_options": _enum_list(
            _nested(
                row,
                ("reasoning_efforts",),
                ("reasoningEfforts",),
                ("effort_options",),
            ),
            allowed=ALLOWED_REASONING_EFFORTS,
            opaque_kind="effort",
            encoder=encoder,
        ),
        "source_endpoint": source_endpoint,
    }


MODEL_FIELDS = (
    "endpoints",
    "modalities.input",
    "modalities.output",
    "limits.context_tokens",
    "limits.output_tokens",
    "capabilities.tools",
    "capabilities.vision",
    "capabilities.reasoning",
    "capabilities.embeddings",
    "capabilities.image_generation",
    "capabilities.moderation",
    "capabilities.rerank",
    "capabilities.attachment",
    "capabilities.structured_output",
    "capabilities.temperature",
    "capabilities.thinking",
    "reasoning_effort_options",
)
