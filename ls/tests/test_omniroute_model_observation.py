from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from ls.core.schema import validate_json_schema


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ls/skills/ls-omniroute-model-discovery/scripts/omniroute_discover.py"
FIXTURES = ROOT / "ls/tests/fixtures/omniroute"
SCHEMA = ROOT / "ls/skills/ls-omniroute-model-discovery/references/model-observation.schema.json"
SKILL = ROOT / "ls/skills/ls-omniroute-model-discovery/SKILL.md"
OBSERVED_AT = "2026-07-16T12:00:00Z"
TEST_RECEIPT_KEY = "omniroute-model-observation-test-key-v1"
UNKNOWN_MODEL_FIELD_NOTE = (
    "Unknown model fields may be unreported, unsupported, or rejected by validation."
)
EXCLUDED_ROUTE_SCOPE = (
    "WebSocket, OCR, audio, and plugin routes are excluded from this two-endpoint "
    "observation and never become inferred per-model capabilities."
)


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "omniroute_observation_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _build(probe, catalog: object, openai: object) -> dict[str, object]:
    result = probe.build_model_observation(
        catalog,
        openai,
        observed_at=OBSERVED_AT,
        receipt_key=TEST_RECEIPT_KEY,
    )
    assert validate_json_schema(result, SCHEMA, label="observation") == []
    return result


def test_source_shaped_observation_reconciles_variants_and_is_byte_stable() -> None:
    probe = _load_probe()
    catalog = _fixture("model-catalog.json")
    openai = _fixture("openai-models.json")
    expected = (FIXTURES / "expected-model-observation.json").read_text(
        encoding="utf-8"
    )

    first = _build(probe, catalog, openai)
    second = _build(probe, deepcopy(catalog), deepcopy(openai))
    rendered = json.dumps(first, indent=2, sort_keys=True) + "\n"

    assert rendered == expected
    assert first == second
    assert len(first["models"]) == 4
    assert first["truncation"]["models"]["duplicate_rows"] == 10
    assert first["endpoint_observations"] == [
        {
            "route": "/api/models/catalog",
            "source_endpoint": "/api/models/catalog",
        },
        {"route": "/v1/models", "source_endpoint": "/v1/models"},
    ]
    assert first["truncation"]["endpoint_observations"] == {
        "available": 2,
        "retained": 2,
        "dropped": 0,
        "truncated": False,
    }
    assert first["runtime"] == {
        "version": "unknown",
        "commit": "unknown",
        "catalog_revision": "unknown",
        "source_endpoints": ["/api/models/catalog", "/v1/models"],
    }
    assert first["notes"][0] == (
        "Runtime version, commit, and catalog revision are intentionally withheld "
        "and emitted as unknown, even when an approved endpoint reports them."
    )
    rich = next(row for row in first["models"] if row["capabilities"]["tools"] is True)
    assert rich["endpoints"] == ["chat", "responses"]
    assert rich["modalities"] == {"input": ["image", "text"], "output": ["text"]}
    for model in first["models"]:
        assert re.fullmatch(r"opaque-model:[0-9a-f]{20}", model["model_id"])
        assert re.fullmatch(r"opaque-provider:[0-9a-f]{20}", model["provider_id"])
        assert re.fullmatch(
            r"opaque-provider-model:[0-9a-f]{20}", model["provider_model_id"]
        )

    for forbidden in (
        "provider-a",
        "alias-a",
        "model-x",
        "auto/smart",
        "team-private",
        "provider-custom",
        "custom-model",
        "quota",
        "usage",
        "model_equivalence",
        "task_route",
    ):
        assert forbidden not in rendered


def test_public_observation_notes_and_endpoint_scope_are_exact() -> None:
    probe = _load_probe()
    observation = _build(
        probe,
        None,
        {"data": [{"id": "alias/model", "root": "model", "owned_by": "provider"}]},
    )
    expected = _fixture("expected-model-observation.json")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    skill_text = SKILL.read_text(encoding="utf-8")
    model_observation_section = skill_text.split("## Model observation", 1)[1].split(
        "\n## ", 1
    )[0]

    assert isinstance(expected, dict)
    assert observation["notes"][1] == UNKNOWN_MODEL_FIELD_NOTE
    assert expected["notes"] == observation["notes"]
    assert schema["properties"]["notes"]["const"] == observation["notes"]
    assert EXCLUDED_ROUTE_SCOPE in model_observation_section
    assert "remain endpoint observations" not in model_observation_section


@pytest.mark.parametrize(
    (
        "catalog",
        "openai",
        "expected_sources",
        "expected_count",
        "expected_endpoint_observations",
    ),
    [
        (
            {"catalog": {"provider-a": {"models": [{"id": "provider-a/a"}]}}},
            None,
            ["/api/models/catalog"],
            1,
            [
                {
                    "route": "/api/models/catalog",
                    "source_endpoint": "/api/models/catalog",
                }
            ],
        ),
        (
            None,
            {"data": [{"id": "alias/a", "root": "a", "owned_by": "provider-a"}]},
            ["/v1/models"],
            1,
            [{"route": "/v1/models", "source_endpoint": "/v1/models"}],
        ),
        (None, None, [], 0, []),
    ],
)
def test_each_endpoint_availability_mode(
    catalog: object,
    openai: object,
    expected_sources: list[str],
    expected_count: int,
    expected_endpoint_observations: list[dict[str, str]],
) -> None:
    observation = _build(_load_probe(), catalog, openai)

    assert observation["runtime"]["source_endpoints"] == expected_sources
    assert len(observation["models"]) == expected_count
    assert observation["endpoint_observations"] == expected_endpoint_observations


def test_source_shaped_openai_row_without_root_is_retained_and_stable() -> None:
    probe = _load_probe()
    payload = {"data": [{"id": "provider-a/no-root-model", "owned_by": "provider-a"}]}

    first = _build(probe, None, payload)
    second = _build(probe, None, deepcopy(payload))

    assert first == second
    assert first["runtime"]["source_endpoints"] == ["/v1/models"]
    assert len(first["models"]) == 1
    assert first["truncation"]["models"]["invalid_rows"] == 0


def test_top_level_model_list_is_recorded_as_runtime_source() -> None:
    observation = _build(
        _load_probe(),
        None,
        [{"id": "provider-a/list-model", "root": "list-model", "owned_by": "provider-a"}],
    )

    assert observation["runtime"]["source_endpoints"] == ["/v1/models"]
    assert len(observation["models"]) == 1


@pytest.mark.parametrize(
    ("payload", "accepted", "expected_invalid_rows"),
    [
        ([], True, 0),
        (["unauthorized"], False, 1),
        (["unauthorized", 7], False, 2),
    ],
)
def test_top_level_model_lists_require_mapping_rows_except_explicit_empty_lists(
    payload: list[object],
    accepted: bool,
    expected_invalid_rows: int,
) -> None:
    _load_probe()
    rows = sys.modules["omniroute_proxy.observation_rows"]

    assert rows.source_payload_accepted(payload, "/v1/models") is accepted
    assert rows.source_rows(payload, "/v1/models") == ([], expected_invalid_rows)


@pytest.mark.parametrize(
    "payload",
    [
        [{"error": "tenant-secret-row-error"}],
        {"data": [{"error": "tenant-secret-row-error"}]},
        {"models": [{"error": "tenant-secret-row-error"}]},
        {"items": [{"error": "tenant-secret-row-error"}]},
    ],
)
def test_generic_model_lists_require_usable_identity_provider_rows(payload: object) -> None:
    _load_probe()
    rows = sys.modules["omniroute_proxy.observation_rows"]

    assert rows.source_payload_accepted(payload, "/v1/models") is False


@pytest.mark.parametrize(
    ("catalog_runtime", "openai_runtime", "catalog_version", "forbidden_values"),
    [
        (
            {"version": "tenant-single-version", "commit": "tenant-single-commit"},
            None,
            "tenant-single-catalog-version",
            (
                "tenant-single-version",
                "tenant-single-commit",
                "tenant-single-catalog-version",
            ),
        ),
        (
            {"version": "tenant-equal-version", "commit": "tenant-equal-commit"},
            {"version": "tenant-equal-version", "commit": "tenant-equal-commit"},
            "tenant-equal-catalog-version",
            (
                "tenant-equal-version",
                "tenant-equal-commit",
                "tenant-equal-catalog-version",
            ),
        ),
        (
            {"version": "tenant-left-version", "commit": "tenant-left-commit"},
            {"version": "tenant-right-version", "commit": "tenant-right-commit"},
            "tenant-conflicting-catalog-version",
            (
                "tenant-left-version",
                "tenant-left-commit",
                "tenant-right-version",
                "tenant-right-commit",
                "tenant-conflicting-catalog-version",
            ),
        ),
    ],
)
def test_model_endpoints_intentionally_withhold_runtime_identity(
    catalog_runtime: dict[str, str],
    openai_runtime: dict[str, str] | None,
    catalog_version: str,
    forbidden_values: tuple[str, ...],
) -> None:
    catalog = {
        "runtime": catalog_runtime,
        "catalogVersion": catalog_version,
        "catalog": {},
    }
    openai = {"runtime": openai_runtime, "data": []} if openai_runtime else None

    observation = _build(_load_probe(), catalog, openai)

    assert observation["runtime"] == {
        "version": "unknown",
        "commit": "unknown",
        "catalog_revision": "unknown",
        "source_endpoints": ["/api/models/catalog", "/v1/models"]
        if openai_runtime
        else ["/api/models/catalog"],
    }
    rendered = json.dumps(observation, sort_keys=True, allow_nan=False)
    assert observation["notes"][0] == (
        "Runtime version, commit, and catalog revision are intentionally withheld "
        "and emitted as unknown, even when an approved endpoint reports them."
    )
    for value in forbidden_values:
        assert value not in rendered


def test_control_containing_identity_tuples_do_not_merge_and_are_stable() -> None:
    probe = _load_probe()
    rows = [
        {
            "id": "alias/first",
            "root": "model",
            "owned_by": "tenant\x1f\x00root",
        },
        {
            "id": "alias/second",
            "root": "root\x00model",
            "owned_by": "tenant\x1f",
        },
    ]

    first = _build(probe, None, {"data": rows})
    second = _build(probe, None, {"data": list(reversed(rows))})

    assert first == second
    assert len(first["models"]) == 2
    assert len({row["identity"] for row in first["models"]}) == 2
    assert len({row["model_id"] for row in first["models"]}) == 2
    assert len({row["provider_model_id"] for row in first["models"]}) == 2
    rendered = json.dumps(first, sort_keys=True, allow_nan=False)
    for forbidden in ("tenant", "root\\u0000model", "alias/first", "alias/second"):
        assert forbidden not in rendered


def test_unpaired_surrogate_provider_receipts_are_distinct_and_private() -> None:
    probe = _load_probe()
    providers = ("\ud800", "\ud801")
    rows = [
        {"id": f"alias/model-{index}", "root": "model", "owned_by": provider}
        for index, provider in enumerate(providers)
    ]

    first = _build(probe, None, {"data": rows})
    second = _build(probe, None, {"data": list(reversed(rows))})
    rendered = json.dumps(first, sort_keys=True, allow_nan=False)

    assert first == second
    assert len(first["models"]) == 2
    assert len({row["provider_id"] for row in first["models"]}) == 2
    assert len({row["identity"] for row in first["models"]}) == 2
    assert all(provider not in rendered for provider in providers)


def test_opaque_receipts_are_keyed_and_do_not_leak_raw_values_or_key_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe()
    rows = sys.modules["omniroute_proxy.observation_rows"]
    raw_provider = "tenant-secret-provider"
    raw_model = "tenant-secret-model"
    candidate_api_key = "test-api-key"
    row = {
        "id": raw_model,
        "root": raw_model,
        "owned_by": raw_provider,
        "supported_endpoints": ["tenant-secret-endpoint", "\ud800", "\ud801"],
    }

    first = _build(probe, None, {"data": [row]})
    same_test_key = probe.build_model_observation(
        None,
        {"data": [row]},
        observed_at=OBSERVED_AT,
        receipt_key=TEST_RECEIPT_KEY,
    )
    second = probe.build_model_observation(
        None,
        {"data": [row]},
        observed_at=OBSERVED_AT,
        receipt_key="a-different-test-key",
    )
    issued_receipt_keys = (b"a" * 32, b"b" * 32, b"c" * 32, b"d" * 32)
    receipt_key_iter = iter(issued_receipt_keys)
    requested_lengths: list[int] = []

    def token_bytes(length: int) -> bytes:
        requested_lengths.append(length)
        return next(receipt_key_iter)

    monkeypatch.setattr(rows.secrets, "token_bytes", token_bytes)

    api_first = probe.build_model_observation(
        None,
        {"data": [row]},
        observed_at=OBSERVED_AT,
        api_key=candidate_api_key,
    )
    api_second = probe.build_model_observation(
        None,
        {"data": [row]},
        observed_at=OBSERVED_AT,
        api_key=candidate_api_key,
    )
    assert requested_lengths == [32, 32]
    legacy_api_derived_receipt_key = hmac.new(
        candidate_api_key.encode("utf-8"),
        b"localsetup/omniroute/model-observation/receipt-key/v1",
        hashlib.sha256,
    ).digest()
    reproduced_from_candidate = probe.build_model_observation(
        None,
        {"data": [row]},
        observed_at=OBSERVED_AT,
        receipt_key=legacy_api_derived_receipt_key,
    )
    unauthenticated_first = probe.build_model_observation(
        None,
        {"data": [row]},
        observed_at=OBSERVED_AT,
    )
    unauthenticated_second = probe.build_model_observation(
        None,
        {"data": [row]},
        observed_at=OBSERVED_AT,
    )
    rendered = json.dumps(first, sort_keys=True, allow_nan=False)
    rendered_api = json.dumps(api_first, sort_keys=True, allow_nan=False)

    assert first == same_test_key
    assert first["models"][0]["model_id"] != second["models"][0]["model_id"]
    assert api_first["models"][0]["model_id"] != api_second["models"][0]["model_id"]
    assert (
        api_first["models"][0]["model_id"]
        != reproduced_from_candidate["models"][0]["model_id"]
    )
    assert (
        unauthenticated_first["models"][0]["model_id"]
        != unauthenticated_second["models"][0]["model_id"]
    )
    assert requested_lengths == [32, 32, 32, 32]
    assert len(first["models"][0]["endpoints"]) == 3
    assert all(
        value.startswith("opaque-endpoint:")
        for value in first["models"][0]["endpoints"]
    )
    for forbidden in (
        raw_provider,
        raw_model,
        "tenant-secret-endpoint",
        TEST_RECEIPT_KEY,
        "\ud800",
        "\ud801",
    ):
        assert forbidden not in rendered
    for forbidden in (
        candidate_api_key,
        legacy_api_derived_receipt_key.hex(),
        issued_receipt_keys[0].hex(),
    ):
        assert forbidden not in rendered_api


def test_blank_or_whitespace_direct_api_key_uses_fresh_unauthenticated_receipts() -> None:
    probe = _load_probe()
    payload = {
        "data": [
            {"id": "alias/model", "root": "model", "owned_by": "provider"}
        ]
    }

    no_key = probe.build_model_observation(
        None, payload, observed_at=OBSERVED_AT
    )
    blank_key = probe.build_model_observation(
        None, payload, observed_at=OBSERVED_AT, api_key=""
    )
    whitespace_key = probe.build_model_observation(
        None, payload, observed_at=OBSERVED_AT, api_key=" \t\n"
    )

    assert no_key["models"][0]["model_id"] != blank_key["models"][0]["model_id"]
    assert blank_key["models"][0]["model_id"] != whitespace_key["models"][0]["model_id"]


def test_schema_rejects_hostile_raw_note() -> None:
    probe = _load_probe()
    observation = _build(
        probe,
        None,
        {"data": [{"id": "alias/model", "root": "model", "owned_by": "provider"}]},
    )
    observation["notes"][0] = "tenant-secret payload prose"

    assert validate_json_schema(observation, SCHEMA, label="observation")
    with pytest.raises(
        probe.ObservationError,
        match="^observation_schema_validation_failed$",
    ):
        probe.validate_observation(observation)


def test_overlong_identity_components_are_invalid_and_receipted() -> None:
    probe = _load_probe()
    overlong = "tenant-" + ("x" * 512)

    observation = _build(
        probe,
        None,
        {"data": [{"id": overlong}, {"id": overlong + "-different"}]},
    )

    assert observation["models"] == []
    assert observation["truncation"]["models"] == {
        "input_rows": 2,
        "invalid_rows": 2,
        "duplicate_rows": 0,
        "unique_models": 0,
        "retained": 0,
        "dropped": 0,
        "truncated": False,
    }


@pytest.mark.parametrize("provider", (pytest.param(None, id="null"), pytest.param(7, id="number")))
def test_missing_null_or_nonstring_provider_is_an_invalid_row(provider: object) -> None:
    probe = _load_probe()
    row: dict[str, object] = {"id": "alias/model", "root": "model"}
    if provider != "missing":
        row["owned_by"] = provider

    observation = _build(probe, None, {"data": [row]})

    assert observation["models"] == []
    assert observation["truncation"]["models"] == {
        "input_rows": 1,
        "invalid_rows": 1,
        "duplicate_rows": 0,
        "unique_models": 0,
        "retained": 0,
        "dropped": 0,
        "truncated": False,
    }


def test_missing_provider_is_an_invalid_row() -> None:
    probe = _load_probe()
    observation = _build(
        probe,
        None,
        {"data": [{"id": "alias/model", "root": "model"}]},
    )

    assert observation["models"] == []
    assert observation["truncation"]["models"]["invalid_rows"] == 1


def test_canonical_root_reconciles_aliases_before_conflicts_and_provider_collisions() -> None:
    probe = _load_probe()
    catalog = {
        "catalog": {
            "provider-a": {
                "models": [
                    {"id": "alias-a/shared", "capabilities": {"tool_calling": True}},
                    {"id": "provider-a/shared", "capabilities": {"tool_calling": False}},
                    {"id": "shared"},
                ]
            },
            "provider-b": {"models": [{"id": "alias-b/shared"}]},
        }
    }
    openai = {
        "data": [
            {"id": "alias-a/shared", "root": "shared", "owned_by": "provider-a"},
            {"id": "provider-a/shared", "root": "shared", "owned_by": "provider-a"},
            {"id": "shared", "root": "shared", "owned_by": "provider-a"},
            {"id": "alias-b/shared", "root": "shared", "owned_by": "provider-b"},
        ]
    }

    first = _build(probe, catalog, openai)
    second = _build(
        probe,
        {"catalog": dict(reversed(list(catalog["catalog"].items())))},
        {"data": list(reversed(openai["data"]))},
    )

    assert first == second
    assert len(first["models"]) == 2
    assert len({row["provider_id"] for row in first["models"]}) == 2
    assert len(first["conflicts"]) == 1
    assert first["conflicts"][0]["field"] == "capabilities.tools"
    assert len(first["conflicts"][0]["value_fingerprints"]) == 2
    assert first["conflicts"][0]["value_fingerprint_truncation"] == {
        "available": 2,
        "retained": 2,
        "dropped": 0,
        "truncated": False,
    }


def test_conflict_value_fingerprints_are_bounded_and_permutation_stable() -> None:
    probe = _load_probe()
    rows = [
        {
            "id": f"alias/model-{index}",
            "root": "model",
            "owned_by": "provider",
            "supported_endpoints": [f"tenant-private-endpoint-{index}"],
        }
        for index in range(9)
    ]

    first = _build(probe, None, {"data": rows})
    second = _build(probe, None, {"data": list(reversed(rows))})
    conflict = next(item for item in first["conflicts"] if item["field"] == "endpoints")

    assert first == second
    assert len(conflict["value_fingerprints"]) == 8
    assert conflict["value_fingerprint_truncation"] == {
        "available": 9,
        "retained": 8,
        "dropped": 1,
        "truncated": True,
    }
    assert "tenant-private-endpoint" not in json.dumps(first, sort_keys=True)


def test_alias_flood_merges_before_cap_and_unique_models_sort_before_cap() -> None:
    probe = _load_probe()
    aliases = [{"id": f"alias-{index:03d}/shared"} for index in range(300)]
    anchor = {"id": "alias-000/shared", "root": "shared", "owned_by": "provider-a"}

    merged = _build(
        probe,
        {"catalog": {"provider-a": {"models": aliases}}},
        {"data": [anchor]},
    )
    assert len(merged["models"]) == 1
    assert merged["truncation"]["models"] == {
        "input_rows": 301,
        "invalid_rows": 0,
        "duplicate_rows": 300,
        "unique_models": 1,
        "retained": 1,
        "dropped": 0,
        "truncated": False,
    }

    unique = [
        {
            "id": f"alias/model-{index:03d}",
            "root": f"model-{index:03d}",
            "owned_by": "provider-a",
        }
        for index in range(300)
    ]
    forward = _build(probe, None, {"data": unique})
    reverse = _build(probe, None, {"data": list(reversed(unique))})
    assert forward == reverse
    assert len(forward["models"]) == 256
    assert forward["truncation"]["models"]["dropped"] == 44
    assert forward["truncation"]["models"]["truncated"] is True


def test_private_identity_arbitrary_enums_and_pricing_never_emit_verbatim() -> None:
    probe = _load_probe()
    row = {
        "id": "tenant-secret/private-combo",
        "root": "custom/private-model",
        "owned_by": "provider-secret",
        "input_modalities": ["text", "retina-private"],
        "output_modalities": ["private-output"],
        "supported_endpoints": ["chat", "/private/topology/route", "private-protocol"],
        "reasoning_efforts": ["high", "tenant-effort"],
        "pricing": {"currency": "USD", "input_per_million": 1.25},
    }

    observation = _build(probe, None, {"data": [row]})
    rendered = json.dumps(observation, sort_keys=True, allow_nan=False)
    model = observation["models"][0]

    assert "text" in model["modalities"]["input"]
    assert "chat" in model["endpoints"]
    assert "high" in model["reasoning_effort_options"]
    assert any(value.startswith("opaque-modality:") for value in model["modalities"]["input"])
    assert any(value.startswith("opaque-endpoint:") for value in model["endpoints"])
    assert any(value.startswith("opaque-effort:") for value in model["reasoning_effort_options"])
    assert model["cost_evidence"]["provenance_class"] == "unavailable"
    for forbidden in (
        "tenant-secret",
        "private-combo",
        "custom/private-model",
        "provider-secret",
        "retina-private",
        "private-output",
        "/private/topology/route",
        "private-protocol",
        "tenant-effort",
        "USD",
        "1.25",
    ):
        assert forbidden not in rendered


def test_arbitrary_enum_flood_is_bounded_and_permutation_stable() -> None:
    probe = _load_probe()
    arbitrary = [f"/private/route-{index:03d}" for index in range(300)]
    row = {
        "id": "alias/model",
        "root": "model",
        "owned_by": "provider",
        "supported_endpoints": arbitrary,
    }

    forward = _build(probe, None, {"data": [row]})
    row["supported_endpoints"] = list(reversed(arbitrary))
    reverse = _build(probe, None, {"data": [row]})

    assert forward == reverse
    assert len(forward["models"][0]["endpoints"]) == 32
    assert all(value.startswith("opaque-endpoint:") for value in forward["models"][0]["endpoints"])
    assert "/private/route" not in json.dumps(forward)


def test_schema_rejects_raw_topology_endpoint_observation_routes() -> None:
    probe = _load_probe()
    observation = _build(
        probe,
        None,
        {"data": [{"id": "alias/model", "root": "model", "owned_by": "provider"}]},
    )
    observation["endpoint_observations"] = [
        {"route": "/v1/models", "source_endpoint": "/v1/models"}
    ]
    assert validate_json_schema(observation, SCHEMA, label="observation") == []

    observation["endpoint_observations"] = [
        {"route": "/tenant/private/topology", "source_endpoint": "/v1/models"}
    ]
    assert validate_json_schema(observation, SCHEMA, label="observation")
    with pytest.raises(
        probe.ObservationError,
        match="^observation_schema_validation_failed$",
    ):
        probe.validate_observation(observation)


def test_observation_failures_are_typed_and_sanitized(tmp_path: Path) -> None:
    probe = _load_probe()

    with pytest.raises(probe.ObservationError, match="^observation_schema_missing$"):
        probe.build_model_observation(
            None,
            None,
            observed_at=OBSERVED_AT,
            schema_path=tmp_path / "missing-schema.json",
        )
    with pytest.raises(probe.ObservationError, match="^observation_time_invalid$"):
        probe.build_model_observation(None, None, observed_at="not-a-time")


@pytest.mark.parametrize(
    "catalog",
    [
        {"error": "tenant-secret-error"},
        {"metadata": {"version": "tenant-secret-metadata"}},
        {"provider": "tenant-secret-scalar"},
        {"provider": ["tenant-secret-list"]},
        {"provider": {}},
    ],
)
def test_catalog_payload_grammar_rejects_structurally_invalid_provider_buckets(
    catalog: object,
) -> None:
    probe = _load_probe()
    rows = sys.modules["omniroute_proxy.observation_rows"]
    payload = {"catalog": catalog}

    assert rows.source_payload_accepted(payload, "/api/models/catalog") is False
    assert rows.source_rows(payload, "/api/models/catalog") == ([], 1)
    assert rows.source_payload_accepted({"data": []}, "/api/models/catalog") is True
    assert rows.source_payload_accepted([], "/api/models/catalog") is True
    assert "tenant-secret" not in json.dumps(
        _build(probe, payload, None), sort_keys=True
    )


@pytest.mark.parametrize("provider_key", ["", "x" * 513])
def test_catalog_payload_grammar_rejects_unbounded_provider_keys(
    provider_key: str,
) -> None:
    _load_probe()
    rows = sys.modules["omniroute_proxy.observation_rows"]
    payload = {"catalog": {provider_key: {"models": []}}}

    assert rows.source_payload_accepted(payload, "/api/models/catalog") is False
    assert rows.source_rows(payload, "/api/models/catalog") == ([], 1)


@pytest.mark.parametrize(
    "payload",
    [
        {"catalog": {}},
        {"catalog": {"provider": {"models": []}}},
    ],
)
def test_zero_model_catalogs_are_valid_sources(payload: object) -> None:
    probe = _load_probe()
    rows = sys.modules["omniroute_proxy.observation_rows"]

    assert rows.source_payload_accepted(payload, "/api/models/catalog") is True
    assert rows.source_rows(payload, "/api/models/catalog") == ([], 0)
    observation = _build(probe, payload, None)

    assert observation["runtime"]["source_endpoints"] == ["/api/models/catalog"]
    assert observation["models"] == []
    assert observation["truncation"]["models"]["invalid_rows"] == 0


@pytest.mark.parametrize(
    ("models", "expected_invalid_rows"),
    [
        ([{"error": "tenant-secret-row-error"}], 1),
        (["tenant-secret-scalar-error"], 1),
    ],
)
def test_catalog_model_lists_require_usable_identity_provider_rows(
    models: list[object],
    expected_invalid_rows: int,
) -> None:
    _load_probe()
    rows = sys.modules["omniroute_proxy.observation_rows"]
    payload = {"catalog": {"provider-a": {"models": models}}}

    assert rows.source_payload_accepted(payload, "/api/models/catalog") is False
    assert rows.source_rows(payload, "/api/models/catalog") == (
        [],
        expected_invalid_rows,
    )


def test_model_observation_rejects_total_source_failure_without_success_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]
    cli = sys.modules["omniroute_proxy.cli"]
    requested_paths: list[str] = []

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    def unavailable_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        requested_paths.append(url.rsplit(".invalid", 1)[1])
        return {
            "ok": False,
            "status": 503,
            "error": "tenant-secret-source-error",
        }

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", unavailable_fetch)

    with pytest.raises(
        probe.ObservationError,
        match="^observation_sources_unavailable$",
    ):
        probe.run_model_observation(
            "https://proxy.invalid",
            None,
            5,
            observed_at=OBSERVED_AT,
        )
    assert requested_paths == ["/api/models/catalog", "/v1/models"]

    requested_paths.clear()
    assert cli.main(["--model-observation", "--base-url", "https://proxy.invalid", "--api-key-env", ""]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "omniroute_discover.py: invalid input: observation_sources_unavailable\n"
    )
    assert "tenant-secret" not in captured.err
    assert requested_paths == ["/api/models/catalog", "/v1/models"]


def test_model_observation_rejects_nonrow_list_sources_without_success_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]
    cli = sys.modules["omniroute_proxy.cli"]
    requested_paths: list[str] = []

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    def mixed_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        path = url.rsplit(".invalid", 1)[1]
        requested_paths.append(path)
        if path == "/api/models/catalog":
            return {"ok": False, "status": 503, "error": "tenant-secret-error"}
        return {"ok": True, "status": 200, "_payload": ["unauthorized"]}

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", mixed_fetch)

    with pytest.raises(
        probe.ObservationError,
        match="^observation_sources_unavailable$",
    ):
        probe.run_model_observation(
            "https://proxy.invalid",
            None,
            5,
            observed_at=OBSERVED_AT,
        )
    assert requested_paths == ["/api/models/catalog", "/v1/models"]

    requested_paths.clear()
    assert cli.main(["--model-observation", "--base-url", "https://proxy.invalid", "--api-key-env", ""]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "omniroute_discover.py: invalid input: observation_sources_unavailable\n"
    )
    assert "tenant-secret" not in captured.err
    assert requested_paths == ["/api/models/catalog", "/v1/models"]


def test_model_observation_rejects_mapping_shaped_error_list_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]
    cli = sys.modules["omniroute_proxy.cli"]
    requested_paths: list[str] = []

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    def mixed_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        path = url.rsplit(".invalid", 1)[1]
        requested_paths.append(path)
        if path == "/api/models/catalog":
            return {"ok": False, "status": 503, "error": "tenant-secret-error"}
        return {
            "ok": True,
            "status": 200,
            "_payload": [{"error": "tenant-secret-row-error"}],
        }

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", mixed_fetch)

    with pytest.raises(
        probe.ObservationError,
        match="^observation_sources_unavailable$",
    ):
        probe.run_model_observation(
            "https://proxy.invalid",
            None,
            5,
            observed_at=OBSERVED_AT,
        )
    assert requested_paths == ["/api/models/catalog", "/v1/models"]

    requested_paths.clear()
    assert cli.main(["--model-observation", "--base-url", "https://proxy.invalid", "--api-key-env", ""]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "omniroute_discover.py: invalid input: observation_sources_unavailable\n"
    )
    assert "tenant-secret" not in captured.err
    assert requested_paths == ["/api/models/catalog", "/v1/models"]


@pytest.mark.parametrize(
    "models",
    [
        [{"error": "tenant-secret-row-error"}],
        ["tenant-secret-scalar-error"],
    ],
)
def test_model_observation_rejects_unusable_catalog_model_lists(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: list[object],
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]
    cli = sys.modules["omniroute_proxy.cli"]
    requested_paths: list[str] = []

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    def mixed_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        path = url.rsplit(".invalid", 1)[1]
        requested_paths.append(path)
        if path == "/api/models/catalog":
            return {
                "ok": True,
                "status": 200,
                "_payload": {"catalog": {"provider-a": {"models": models}}},
            }
        return {"ok": False, "status": 503, "error": "tenant-secret-v1-error"}

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", mixed_fetch)

    with pytest.raises(
        probe.ObservationError,
        match="^observation_sources_unavailable$",
    ):
        probe.run_model_observation(
            "https://proxy.invalid",
            None,
            5,
            observed_at=OBSERVED_AT,
        )
    assert requested_paths == ["/api/models/catalog", "/v1/models"]

    requested_paths.clear()
    assert cli.main(["--model-observation", "--base-url", "https://proxy.invalid", "--api-key-env", ""]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "omniroute_discover.py: invalid input: observation_sources_unavailable\n"
    )
    assert "tenant-secret" not in captured.err
    assert requested_paths == ["/api/models/catalog", "/v1/models"]


def test_model_observation_accepts_partial_valid_catalog_model_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    payload = {
        "catalog": {
            "provider-a": {
                "models": [
                    {"error": "tenant-secret-row-error"},
                    "tenant-secret-scalar-error",
                    {"id": "provider-a/valid-model", "root": "valid-model"},
                ]
            }
        }
    }

    def mixed_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        path = url.rsplit(".invalid", 1)[1]
        if path == "/api/models/catalog":
            return {"ok": True, "status": 200, "_payload": payload}
        return {"ok": False, "status": 503, "error": "tenant-secret-v1-error"}

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", mixed_fetch)

    result = probe.run_model_observation(
        "https://proxy.invalid",
        None,
        5,
        observed_at=OBSERVED_AT,
    )

    assert result["runtime"]["source_endpoints"] == ["/api/models/catalog"]
    assert len(result["models"]) == 1
    assert result["truncation"]["models"] == {
        "input_rows": 3,
        "invalid_rows": 2,
        "duplicate_rows": 0,
        "unique_models": 1,
        "retained": 1,
        "dropped": 0,
        "truncated": False,
    }
    assert result["endpoint_status"] == [
        {"path": "/api/models/catalog", "available": True, "status": 200},
        {"path": "/v1/models", "available": False, "status": 503},
    ]
    assert "tenant-secret" not in json.dumps(result, sort_keys=True)


def test_model_observation_retains_valid_catalog_provider_when_another_has_error_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]
    rows = sys.modules["omniroute_proxy.observation_rows"]

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    payload = {
        "catalog": {
            "provider-good": {
                "models": [
                    {"id": "provider-good/model", "root": "model"},
                ]
            },
            "provider-error": {
                "models": [{"error": "tenant-secret-row-error"}]
            },
        }
    }

    assert rows.source_payload_accepted(payload, "/api/models/catalog") is True
    source_rows, invalid_rows = rows.source_rows(payload, "/api/models/catalog")
    assert source_rows == [
        (
            "provider-good",
            {"id": "provider-good/model", "root": "model"},
        ),
        ("provider-error", {"error": "tenant-secret-row-error"}),
    ]
    assert invalid_rows == 0

    def mixed_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        path = url.rsplit(".invalid", 1)[1]
        if path == "/api/models/catalog":
            return {"ok": True, "status": 200, "_payload": payload}
        return {"ok": False, "status": 503, "error": "tenant-secret-v1-error"}

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", mixed_fetch)

    result = probe.run_model_observation(
        "https://proxy.invalid",
        None,
        5,
        observed_at=OBSERVED_AT,
    )

    assert result["runtime"]["source_endpoints"] == ["/api/models/catalog"]
    assert len(result["models"]) == 1
    assert result["truncation"]["models"] == {
        "input_rows": 2,
        "invalid_rows": 1,
        "duplicate_rows": 0,
        "unique_models": 1,
        "retained": 1,
        "dropped": 0,
        "truncated": False,
    }
    assert result["endpoint_status"] == [
        {"path": "/api/models/catalog", "available": True, "status": 200},
        {"path": "/v1/models", "available": False, "status": 503},
    ]
    assert "tenant-secret" not in json.dumps(result, sort_keys=True)


def test_model_observation_accepts_partial_valid_wrapped_model_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    payloads: dict[str, object] = {
        "/api/models/catalog": None,
        "/v1/models": {
            "data": [
                {"error": "tenant-secret-row-error"},
                "tenant-secret-scalar-error",
                {
                    "id": "provider-a/valid-model",
                    "root": "valid-model",
                    "owned_by": "provider-a",
                },
            ]
        },
    }

    def mixed_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        path = url.rsplit(".invalid", 1)[1]
        if path == "/api/models/catalog":
            return {"ok": False, "status": 503, "error": "tenant-secret-error"}
        return {"ok": True, "status": 200, "_payload": payloads[path]}

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", mixed_fetch)

    result = probe.run_model_observation(
        "https://proxy.invalid",
        None,
        5,
        observed_at=OBSERVED_AT,
    )

    assert result["runtime"]["source_endpoints"] == ["/v1/models"]
    assert len(result["models"]) == 1
    assert result["truncation"]["models"] == {
        "input_rows": 3,
        "invalid_rows": 2,
        "duplicate_rows": 0,
        "unique_models": 1,
        "retained": 1,
        "dropped": 0,
        "truncated": False,
    }
    assert result["endpoint_status"] == [
        {"path": "/api/models/catalog", "available": False, "status": 503},
        {"path": "/v1/models", "available": True, "status": 200},
    ]
    assert "tenant-secret" not in json.dumps(result, sort_keys=True)


def test_model_observation_rejects_unusable_2xx_payloads_and_keeps_partial_valid_source(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _load_probe()
    observation = sys.modules["omniroute_proxy.observation"]
    cli = sys.modules["omniroute_proxy.cli"]

    class OfflineSession:
        def __enter__(self) -> "OfflineSession":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    payloads: dict[str, object] = {
        "/api/models/catalog": {
            "catalog": {"x" * 513: {"models": []}}
        },
        "/v1/models": {"error": "tenant-secret-models-error"},
    }

    def successful_but_unusable_fetch(
        session: object,
        url: str,
        api_key: str | None,
        timeout: float,
        *,
        include_payload: bool,
    ) -> dict[str, object]:
        assert isinstance(session, OfflineSession)
        assert api_key is None
        assert timeout == 5
        assert include_payload is True
        path = url.rsplit(".invalid", 1)[1]
        return {"ok": True, "status": 200, "_payload": payloads[path]}

    monkeypatch.setattr(observation.requests, "Session", OfflineSession)
    monkeypatch.setattr(observation, "fetch_json", successful_but_unusable_fetch)

    with pytest.raises(
        probe.ObservationError,
        match="^observation_sources_unavailable$",
    ):
        probe.run_model_observation(
            "https://proxy.invalid",
            None,
            5,
            observed_at=OBSERVED_AT,
        )
    assert cli.main(["--model-observation", "--base-url", "https://proxy.invalid", "--api-key-env", ""]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "omniroute_discover.py: invalid input: observation_sources_unavailable\n"
    )
    assert "tenant-secret" not in captured.err

    payloads["/v1/models"] = {
        "data": [
            {
                "id": "provider-a/valid-model",
                "root": "valid-model",
                "owned_by": "provider-a",
            }
        ]
    }
    partial = probe.run_model_observation(
        "https://proxy.invalid",
        None,
        5,
        observed_at=OBSERVED_AT,
    )

    assert partial["runtime"]["source_endpoints"] == ["/v1/models"]
    assert partial["truncation"]["models"]["invalid_rows"] == 1
    assert partial["endpoint_status"] == [
        {"path": "/api/models/catalog", "available": False, "status": 200},
        {"path": "/v1/models", "available": True, "status": 200},
    ]
    assert "tenant-secret" not in json.dumps(partial, sort_keys=True)


def test_catalog_type_supplies_only_approved_logical_endpoint_fallbacks() -> None:
    observation = _build(
        _load_probe(),
        {
            "catalog": {
                "provider": {
                    "models": [
                        {"id": "provider/chat", "type": "chat"},
                        {"id": "provider/embedding", "type": "embedding"},
                        {
                            "id": "provider/explicit",
                            "type": "embedding",
                            "supported_endpoints": ["responses"],
                        },
                        {"id": "provider/unknown", "type": "tenant-private-type"},
                        {"id": "provider/malformed", "type": ["chat"]},
                    ]
                }
            }
        },
        None,
    )
    endpoints = [row["endpoints"] for row in observation["models"]]
    rendered = json.dumps(observation, sort_keys=True, allow_nan=False)

    assert ["chat"] in endpoints
    assert ["embeddings"] in endpoints
    assert ["responses"] in endpoints
    assert endpoints.count(["embeddings"]) == 1
    assert ["embeddings", "responses"] not in endpoints
    assert endpoints.count("unknown") == 2
    assert observation["endpoint_observations"] == [
        {
            "route": "/api/models/catalog",
            "source_endpoint": "/api/models/catalog",
        }
    ]
    assert "tenant-private-type" not in rendered
