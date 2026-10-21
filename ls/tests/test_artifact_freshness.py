from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ls.core.artifact_freshness import (
    ArtifactFreshnessSchemaError,
    ArtifactFreshnessSemanticError,
    evaluate_review_state,
    validate_artifact_freshness,
)


ROOT = Path(__file__).resolve().parents[2]


def _metadata(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": "skill:ls-agent-capability-routing",
        "artifact_type": "skill",
        "content_sha256": "a" * 64,
        "owner": "ls-agent-capability-routing",
        "source": "ls/skills/ls-agent-capability-routing/SKILL.md",
        "target_state": "versioned",
        "target_kind": "application",
        "target_name": "Codex CLI",
        "target_version": "0.144.3",
        "verification_date": "2026-07-14",
        "verification_method": "official documentation review",
        "authoritative_sources": ["https://developers.openai.com/codex/"],
        "review_interval_days": 30,
        "risk_class": "high",
    }
    payload.update(overrides)
    return payload


def test_valid_versioned_metadata_evaluates_on_review_boundary() -> None:
    record = validate_artifact_freshness(_metadata(), ROOT)

    result = evaluate_review_state(record, "2026-08-13")

    assert result.state == "current"
    assert result.review_date == date(2026, 8, 13)
    assert result.evaluated_date == date(2026, 8, 13)


def test_review_date_becomes_stale_only_after_deadline() -> None:
    record = validate_artifact_freshness(_metadata(), ROOT)

    first = evaluate_review_state(record, "2026-08-14")
    second = evaluate_review_state(record, date(2026, 8, 14))

    assert first.state == "review_date_stale"
    assert first == second


@pytest.mark.parametrize(
    ("target_state", "reason"),
    [
        ("unversioned", "maintained internal policy without a versioned external target"),
        ("not_applicable", "binary asset has no semantic external target"),
    ],
)
def test_nonversioned_metadata_requires_reason_without_target_version(target_state: str, reason: str) -> None:
    payload = _metadata(
        target_state=target_state,
        target_reason=reason,
    )
    for key in ("target_kind", "target_name", "target_version"):
        payload.pop(key)

    record = validate_artifact_freshness(payload, ROOT)

    assert record.target_state == target_state
    assert record.target_reason == reason
    assert record.target_version is None


@pytest.mark.parametrize(
    "payload",
    [
        _metadata(owner=""),
        _metadata(target_state="versioned", target_version=""),
        _metadata(authoritative_sources=["http://example.test/source"]),
        _metadata(content_sha256="not-a-digest"),
        _metadata(target_state="not_applicable", target_reason="no target", target_version="1.0"),
        _metadata(review_interval_days=True),
    ],
)
def test_schema_rejects_malformed_metadata(payload: dict[str, object]) -> None:
    with pytest.raises(ArtifactFreshnessSchemaError):
        validate_artifact_freshness(payload, ROOT)


@pytest.mark.parametrize(
    "payload",
    [
        _metadata(verification_date="2026-02-30"),
        _metadata(authoritative_sources=["https://:"]),
        _metadata(authoritative_sources=["https://example.test:invalid"]),
        _metadata(authoritative_sources=["https://[::1"]),
        _metadata(authoritative_sources=["https://user@example.test/source"]),
        _metadata(authoritative_sources=["https://user:token@example.test/source"]),
        _metadata(verification_date="9999-12-31", review_interval_days=1),
    ],
)
def test_semantic_validation_rejects_invalid_calendar_or_interval(payload: dict[str, object]) -> None:
    with pytest.raises(ArtifactFreshnessSemanticError):
        validate_artifact_freshness(payload, ROOT)
