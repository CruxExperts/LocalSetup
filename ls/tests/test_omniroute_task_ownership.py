from __future__ import annotations

import json
from pathlib import Path

from ls.core.aliases import collect_skill_aliases
from ls.core.dependency_ledger import load_dependency_ledger
from ls.core.manifests import load_pack_config
from ls.core.skills import load_skill_catalog


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "ls" / "tests" / "fixtures" / "omniroute" / "task-owner-cases.json"
OWNERS = (
    "ls-omniroute-task-triage",
    "ls-omniroute-model-discovery",
    "ls-omniroute-admin-automation",
    "ls-omniroute-update",
)
OWNER_SET = set(OWNERS)
DIRECT_ROUTE_OWNERS = {
    "ambiguous_triage": "ls-omniroute-task-triage",
    "read_discovery": "ls-omniroute-model-discovery",
    "live_mutation": "ls-omniroute-admin-automation",
    "source_coverage_maintenance": "ls-omniroute-update",
}
LEGACY_SELECTORS = (
    "ls-omniroute-codex",
    "ls-omniroute-context",
    "ls-omniroute-integrations",
    "ls-omniroute-observability",
)
STATIC_TEMPLATES = (
    ROOT / "ls" / "templates" / "claude-code" / "CLAUDE.md",
    ROOT / "ls" / "templates" / "codex" / "AGENTS.md",
    ROOT / "ls" / "templates" / "cursor" / "ls-context-index.md",
    ROOT / "ls" / "templates" / "cursor" / "ls-context.mdc",
    ROOT / "ls" / "templates" / "kilo" / "AGENTS.md",
    ROOT / "ls" / "templates" / "kilo" / "instructions.md",
    ROOT / "ls" / "templates" / "openclaw" / "OPENCLAW_CONTEXT.md",
    ROOT / "ls" / "templates" / "opencode" / "AGENTS.md",
)
ROUTING_ARTIFACTS = (
    ROOT / "ls" / "skills" / "ls-omniroute-task-triage" / "SKILL.md",
    *STATIC_TEMPLATES,
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_omniroute_direct_cases_have_one_terminal_owner() -> None:
    payload = _load_fixture()

    assert set(payload) == {
        "schema_version",
        "direct_cases",
        "client_profile_cases",
        "ordered_sequences",
        "legacy_selector_requests",
    }
    assert payload["schema_version"] == 3

    direct_cases = payload["direct_cases"]
    assert len(direct_cases) == 4
    assert {row["terminal_owner"] for row in direct_cases} == OWNER_SET
    assert len({row["task"] for row in direct_cases}) == len(direct_cases)
    route_classes = [row["route_class"] for row in direct_cases]
    assert set(route_classes) == set(DIRECT_ROUTE_OWNERS)
    assert len(route_classes) == len(set(route_classes))
    for row in direct_cases:
        assert set(row) == {
            "route_class",
            "task",
            "task_state",
            "candidate_owners",
            "terminal_owner",
        }
        assert set(row["candidate_owners"]) == OWNER_SET
        assert len(row["candidate_owners"]) == len(OWNER_SET)
        assert row["route_class"] in DIRECT_ROUTE_OWNERS
        assert row["terminal_owner"] == DIRECT_ROUTE_OWNERS[row["route_class"]]
        if row["route_class"] != "ambiguous_triage":
            assert row["task_state"] == "classified"
            assert row["terminal_owner"] != "ls-omniroute-task-triage"
        else:
            assert row["task_state"] == "unclassified"
            assert row["terminal_owner"] == "ls-omniroute-task-triage"


def test_omniroute_client_profiles_and_ordered_sequence_have_explicit_owners() -> None:
    payload = _load_fixture()

    client_profiles = payload["client_profile_cases"]
    assert len(client_profiles) == 2
    assert {row["profile_access"] for row in client_profiles} == {"read", "write"}
    expected_profiles = {
        "read": "ls-omniroute-model-discovery",
        "write": "ls-omniroute-admin-automation",
    }
    for row in client_profiles:
        assert set(row) == {
            "task",
            "task_state",
            "profile_access",
            "candidate_owners",
            "terminal_owner",
        }
        assert row["task_state"] == "classified"
        assert set(row["candidate_owners"]) == OWNER_SET
        assert len(row["candidate_owners"]) == len(OWNER_SET)
        assert row["terminal_owner"] == expected_profiles[row["profile_access"]]
        assert row["terminal_owner"] != "ls-omniroute-task-triage"

    sequences = payload["ordered_sequences"]
    assert len(sequences) == 1
    sequence = sequences[0]
    assert set(sequence) == {"task", "candidate_owners", "ordered_owners", "terminal_owner"}
    assert sequence["candidate_owners"] == [
        "ls-omniroute-model-discovery",
        "ls-omniroute-admin-automation",
    ]
    assert sequence["ordered_owners"] == sequence["candidate_owners"]
    assert sequence["terminal_owner"] == "ls-omniroute-admin-automation"


def test_main_router_and_static_templates_exclude_classified_terminal_ownership() -> None:
    main = (ROOT / "ls" / "skills" / "ls-omniroute-task-triage" / "SKILL.md").read_text(encoding="utf-8")
    main_lower = main.casefold()

    assert "ambiguous-task/preflight router" in main_lower
    assert "not a universal omniroute terminal owner" in main_lower
    assert (
        "not the terminal owner for an already-classified proxy discovery, admin mutation, or update/coverage task"
        in main_lower
    )
    assert "ls-omniroute-model-discovery → ls-omniroute-admin-automation" in main
    assert "admin remains the sole mutation terminal owner" in main_lower
    assert "request get /v1/models" not in main_lower
    assert "request patch /api/settings" not in main_lower
    assert "generic deterministic api probes" not in main_lower

    required_terms = (
        "ambiguous-task/preflight router",
        "classified read-only discovery to ls-omniroute-model-discovery",
        "mutation to ls-omniroute-admin-automation",
        "source/coverage maintenance to ls-omniroute-update",
    )
    for template in STATIC_TEMPLATES:
        text = template.read_text(encoding="utf-8").casefold()
        assert all(term in text for term in required_terms), template
        assert "any omniroute task" not in text, template


def test_legacy_selectors_have_no_catalog_pack_ledger_or_routing_alias() -> None:
    payload = _load_fixture()

    legacy_requests = payload["legacy_selector_requests"]
    assert {row["selector"] for row in legacy_requests} == set(LEGACY_SELECTORS)
    assert all(set(row) == {"selector", "expected"} for row in legacy_requests)
    assert all(row["expected"] == "rejected_no_alias" for row in legacy_requests)

    catalog = load_skill_catalog(ROOT)
    catalog_names = {skill.name for skill in catalog}
    catalog_legacy_names = {skill.legacy_name for skill in catalog if skill.legacy_name}
    assert not set(LEGACY_SELECTORS) & catalog_names
    assert not set(LEGACY_SELECTORS) & catalog_legacy_names

    pack = load_pack_config(ROOT)
    pack_skill_names = {name for names in pack.packs.values() for name in names}
    assert not set(LEGACY_SELECTORS) & pack_skill_names
    assert not set(LEGACY_SELECTORS) & set(pack.skill_taxonomy)

    ledger = load_dependency_ledger(ROOT)
    ledger_names = {node.name for node in ledger.nodes} | {node.node_id for node in ledger.nodes}
    assert not set(LEGACY_SELECTORS) & ledger_names

    aliases = collect_skill_aliases(ROOT / "ls" / "skills")
    assert not set(LEGACY_SELECTORS) & (set(aliases) | set(aliases.values()))
    for artifact in ROUTING_ARTIFACTS:
        text = artifact.read_text(encoding="utf-8")
        assert all(selector not in text for selector in LEGACY_SELECTORS), artifact


def test_model_observation_and_equivalence_do_not_decide_task_routes() -> None:
    proxy = (ROOT / "ls" / "skills" / "ls-omniroute-model-discovery" / "SKILL.md").read_text(encoding="utf-8")

    assert "The observation makes no model-equivalence or task-routing decision" in proxy
    assert (
        "Upstream routing recommendation, explanation, and simulation outputs may only report "
        "current OmniRoute behavior and must never become a LocalSetup task-routing, "
        "model-equivalence, or recommendation decision."
    ) in proxy
