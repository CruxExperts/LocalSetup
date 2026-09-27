from __future__ import annotations

from copy import deepcopy

from ls.core.github_repo.controls import CONTROL_GROUPS, CONTROL_IDS, validate_control_observations


def _rows() -> list[dict[str, object]]:
    rows = []
    for group, names in CONTROL_GROUPS.items():
        for name in names:
            rows.append({
                "control_id": f"{group}.{name}",
                "group": group,
                "applicability": "applicable",
                "authority": "unknown",
                "capability": "unknown",
                "observation": "unknown",
                "reason": "fixture_evidence_not_collected",
            })
    return rows


def test_complete_control_registry_counts_explicit_unavailable_and_unknown_as_assessed() -> None:
    rows = _rows()
    rows[0]["observation"] = "unavailable"
    rows[0]["reason"] = "authentication_or_permission_unavailable"
    rows[1]["applicability"] = "not_applicable"
    rows[1]["observation"] = "not_applicable"
    rows[1]["reason"] = "feature_not_applicable_to_repository"

    coverage = validate_control_observations(rows)

    assert coverage["status"] == "complete"
    assert coverage["expected_count"] == len(CONTROL_IDS)
    assert coverage["observed_count"] == len(CONTROL_IDS)
    assert coverage["missing_control_ids"] == []


def test_missing_duplicate_and_unknown_ids_do_not_claim_complete_coverage() -> None:
    rows = _rows()
    rows.pop()
    rows.append(deepcopy(rows[0]))
    rows.append({
        "control_id": "repository_content.not_registered",
        "group": "repository_content",
        "applicability": "unknown",
        "authority": "unknown",
        "capability": "unknown",
        "observation": "unknown",
        "reason": "not_in_registry",
    })

    coverage = validate_control_observations(rows)

    assert coverage["status"] == "incomplete"
    assert len(coverage["missing_control_ids"]) == 1
    assert coverage["duplicate_control_ids"] == [rows[0]["control_id"]]
    assert coverage["invalid_control_ids"] == ["repository_content.not_registered"]


def test_incomplete_observation_or_pagination_is_not_complete_coverage() -> None:
    rows = _rows()
    rows[0]["observation"] = "incomplete"
    rows[0]["reason"] = "partial_response"
    rows[1]["pagination"] = "incomplete"

    coverage = validate_control_observations(rows)

    assert coverage["status"] == "incomplete"
    assert set(coverage["incomplete_control_ids"]) == {rows[0]["control_id"], rows[1]["control_id"]}
