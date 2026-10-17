from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tools.qc_patrol.adjudication import adjudicate_packets, ai_findings_from_adjudications, build_packet_prompt
from tools.qc_patrol.chunking import chunk_text
from tools.qc_patrol.cli import build_pr_review_prompt
from tools.qc_patrol.cli import main as qc_main
from tools.qc_patrol.config import load_config, patrol_llm_config
from tools.qc_patrol.diff_reader import read_diff
from tools.qc_patrol.drift import build_drift_packets, load_baseline
from tools.qc_patrol.deterministic_checks import QC_WORKFLOWS, check_release_exclusions, scan_workflow_permissions
from tools.qc_patrol.docs_drift import docs_alignment_findings
from tools.qc_patrol.issue_writer import (
    extract_handoff,
    find_duplicate,
    fingerprint_for,
    issue_body,
    issue_policy,
    write_issues,
)
from tools.qc_patrol.llm_client import LLMClient, LLMDisabled
from tools.qc_patrol.markdown_versions import markdown_version_packets
from tools.qc_patrol.pr_writer import plan_autofix
from tools.qc_patrol.redaction import redact_text
from tools.qc_patrol.repo_inventory import build_inventory
from tools.qc_patrol.review_contracts import parse_strict_json
from tools.qc_patrol.schemas import AI_ADJUDICATION_SCHEMA


REPO = Path(__file__).resolve().parents[2]


def test_qc_config_loads_defaults_and_blank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QC_LLM_MAX_TOKENS", "")
    config = load_config(REPO)
    assert "qc-patrol" in config.labels
    assert config.llm.max_tokens == 2000
    assert config.llm.api_style == "chat_completions"



def test_patrol_model_is_explicit_bounded_and_uses_only_declared_low_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QC_PATROL_MODEL", raising=False)
    with pytest.raises(ValueError, match="QC_PATROL_MODEL"):
        patrol_llm_config(load_config(REPO).llm)
    monkeypatch.setenv("QC_PATROL_MODEL", "Agent-QC")
    with pytest.raises(ValueError, match="concrete model"):
        patrol_llm_config(load_config(REPO).llm)
    monkeypatch.setenv("QC_PATROL_MODEL", "small-model")
    monkeypatch.setenv("QC_LLM_MAX_TOKENS", "2500")
    monkeypatch.setenv("QC_LLM_REASONING_EFFORTS", "high,low")
    configured = patrol_llm_config(load_config(REPO).llm)
    assert configured.model == "small-model"
    assert configured.max_tokens == 1000
    assert configured.reasoning_effort == "low"
    monkeypatch.setenv("QC_LLM_REASONING_EFFORTS", "high")
    assert patrol_llm_config(load_config(REPO).llm).reasoning_effort == ""

def test_redaction_removes_secret_shapes() -> None:
    text = """API_TOKEN=fake-secret-token-value
password: supersecret
https://user:token@example.com/path
-----BEGIN PRIVATE KEY-----
abc
-----END PRIVATE KEY-----
"""
    redacted = redact_text(text)
    assert "fake-secret-token-value" not in redacted
    assert "supersecret" not in redacted
    assert "user:token" not in redacted
    assert "example.com" not in redacted
    assert "PRIVATE KEY-----\nabc" not in redacted


def test_chunk_text_respects_byte_boundaries() -> None:
    chunks = chunk_text("sample", "aé" * 10, 5)
    assert len(chunks) > 1
    assert all(int(chunk["bytes"]) <= 5 for chunk in chunks)
    assert "".join(str(chunk["text"]) for chunk in chunks) == "aé" * 10


def test_strict_json_schema_failure_writes_raw(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    with pytest.raises(ValueError, match="schema"):
        parse_strict_json('{"findings":[{"category":"bad"}]}', raw)
    payload = json.loads(raw.read_text(encoding="utf-8"))
    assert payload["schema_errors"]


def _finding(title: str = "Example finding") -> dict[str, object]:
    return {
        "category": "workflow_security",
        "severity": "high",
        "title": title,
        "body": "Body",
        "affected_paths": [".github/workflows/example.yml"],
        "region": "permissions",
        "check_type": "deterministic",
    }


def test_fingerprint_stability_and_duplicate_matching() -> None:
    finding = _finding("  Example   Finding ")
    same = _finding("example finding")
    assert fingerprint_for(finding) == fingerprint_for(same)
    body = issue_body(finding)
    handoff = extract_handoff(body)
    assert handoff is not None
    duplicate = find_duplicate([{"number": 7, "body": body}], same)
    assert duplicate and duplicate["number"] == 7


def test_inventory_v2_classifies_core_surfaces() -> None:
    inventory = build_inventory(REPO)
    assert inventory["schema_version"] == "qc.inventory.v2"
    assert inventory["tracked_file_count"] > 0
    surfaces = inventory["surfaces"]
    assert any(row["path"] == ".github/workflows/qc-patrol.yml" for row in surfaces["workflows"])
    assert any(row["path"] == "pyproject.toml" and row["version"] for row in surfaces["packages"])
    assert surfaces["registry_catalog_metadata"]["pack_config"] is True
    assert inventory["version_truth"]["value"] == (REPO / "VERSION").read_text(encoding="utf-8").strip()


def test_drift_packets_detect_changed_manifests() -> None:
    current = {
        "schema_version": "qc.inventory.v2",
        "tracked_file_count": 2,
        "files": [{"path": "pyproject.toml", "hash": "new"}, {"path": ".github/workflows/qc-patrol.yml", "hash": "wf"}],
        "surfaces": {"workflows": [{"path": ".github/workflows/qc-patrol.yml"}], "generated_artifacts": []},
    }
    baseline = {
        "schema_version": "qc.inventory.v2",
        "files": [{"path": "pyproject.toml", "hash": "old"}],
        "surfaces": {"workflows": [], "generated_artifacts": []},
    }
    packets = build_drift_packets(current, baseline)["packets"]
    kinds = {packet["kind"] for packet in packets}
    assert "shape.manifests_changed" in kinds
    assert "shape.workflows_added" in kinds



def test_drift_packets_include_changed_skill_workflow_and_support_excerpts(tmp_path: Path) -> None:
    paths = [
        "ls/skills/example/SKILL.md",
        "ls/workflows/example/SKILL.md",
        "ls/skills/example/references/provenance.json",
        ".github/workflows/qc-patrol.yml",
    ]
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"Current content for {path}.\n", encoding="utf-8")
    current = {
        "schema_version": "qc.inventory.v2",
        "tracked_file_count": len(paths),
        "files": [{"path": path, "hash": "b" * 64} for path in paths],
        "surfaces": {"workflows": [], "generated_artifacts": []},
    }
    baseline = {
        "schema_version": "qc.inventory.v2",
        "files": [{"path": path, "hash": "a" * 64} for path in paths],
        "surfaces": {"workflows": [], "generated_artifacts": []},
    }
    packets = build_drift_packets(current, baseline, repo_root=tmp_path)["packets"]
    assert len(packets) == 1
    packet = packets[0]
    assert packet["kind"] == "source.file_hashes_changed"
    assert set(packet["affected_paths"]) == set(paths)
    assert {row["expected_sha256"] for row in packet["facts"]["hash_changes"]} == {"a" * 64}
    assert {row["observed_sha256"] for row in packet["facts"]["hash_changes"]} == {"b" * 64}
    assert {row["path"] for row in packet["snippets"]} == set(paths)
    assert all(f"Current content for {row['path']}." in row["text"] for row in packet["snippets"])


def test_drift_packets_do_not_report_unchanged_source_hashes() -> None:
    paths = ["ls/skills/example/SKILL.md", "ls/workflows/example/SKILL.md", ".github/workflows/qc-patrol.yml"]
    current = {
        "schema_version": "qc.inventory.v2",
        "files": [{"path": path, "hash": "same"} for path in paths],
        "surfaces": {"workflows": [], "generated_artifacts": []},
    }
    packets = build_drift_packets(current, current)["packets"]
    assert not any(packet["kind"] == "source.file_hashes_changed" for packet in packets)

def test_markdown_version_packets_ignore_historical_contexts(tmp_path: Path) -> None:
    current = tmp_path / "README.md"
    current.write_text("Install LocalSetup 1.2.3 today.\n", encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("Released 1.2.3 historically.\n", encoding="utf-8")
    inventory = {
        "version_truth": {"value": "4.0.11"},
        "surfaces": {
            "version_references": [
                {"path": "README.md", "line": 1, "value": "1.2.3", "doc_class": "public"},
                {"path": "CHANGELOG.md", "line": 1, "value": "1.2.3", "doc_class": "public"},
            ]
        },
    }
    packets = markdown_version_packets(tmp_path, inventory)["packets"]
    assert len(packets) == 1
    assert packets[0]["affected_paths"] == ["README.md"]


def test_ai_adjudication_schema_accepts_packet_result() -> None:
    payload = {
        "packet_id": "markdown.version_reference_drift:abc",
        "finding": "README has stale LocalSetup version",
        "confidence": 0.9,
        "category": "docs",
        "severity": "high",
        "affected_paths": ["README.md"],
        "evidence": ["README.md:1"],
        "is_actionable": True,
        "recommended_action": "Update the current-facing version reference.",
        "suggested_rule": None,
        "should_create_issue": True,
        "why_deterministic_checks_could_not_decide": "The line may be historical.",
    }
    assert parse_strict_json(json.dumps(payload), schema=AI_ADJUDICATION_SCHEMA) == payload


def test_checked_in_ai_adjudication_schema_matches_runtime() -> None:
    checked_in = json.loads((REPO / ".ai/qc/schemas/ai-adjudication.schema.json").read_text(encoding="utf-8"))
    assert checked_in == AI_ADJUDICATION_SCHEMA
    prompt = (REPO / ".ai/qc/prompts/patrol.md").read_text(encoding="utf-8")
    assert "single-object schema in `ai-adjudication.schema.json`" in prompt


def test_packet_prompt_redacts_urls() -> None:
    prompt = build_packet_prompt(
        {
            "packet_id": "p1",
            "affected_paths": ["README.md"],
            "snippets": [{"text": "See https://example.test/private"}],
        }
    )
    assert "example.test" not in prompt
    assert "[REDACTED_URL]" in prompt



def test_packet_adjudication_caps_calls_and_preserves_unprocessed_packets(tmp_path: Path) -> None:
    class FakeClient:
        calls = 0

        def complete(self, prompt: str, **_kwargs: object) -> str:
            self.calls += 1
            packet_id = json.loads(prompt)["packet"]["packet_id"]
            return json.dumps(
                {
                    "packet_id": packet_id,
                    "finding": "",
                    "confidence": 0,
                    "category": "inventory",
                    "severity": "low",
                    "affected_paths": [],
                    "evidence": [],
                    "is_actionable": False,
                    "recommended_action": "",
                    "suggested_rule": None,
                    "should_create_issue": False,
                    "why_deterministic_checks_could_not_decide": "",
                }
            )

    client = FakeClient()
    packets = {
        "schema_version": "qc.drift-packets.v1",
        "packets": [{"packet_id": f"p{index}"} for index in range(5)],
    }
    result = adjudicate_packets(packets, client, tmp_path)
    assert client.calls == 4
    assert result["calls_made"] == 4
    assert [packet["packet_id"] for packet in result["pending_packets"]] == ["p4"]

def test_adjudicate_packets_uses_strict_schema_with_fake_client(tmp_path: Path) -> None:
    class FakeClient:
        def complete(self, _prompt: str, response_schema: dict[str, Any] | None = None, schema_name: str = "qc") -> str:
            assert response_schema == AI_ADJUDICATION_SCHEMA
            assert schema_name == "qc_ai_adjudication"
            return json.dumps(
                {
                    "packet_id": "p1",
                    "finding": "README has stale version",
                    "confidence": 0.95,
                    "category": "docs",
                    "severity": "high",
                    "affected_paths": ["README.md"],
                    "evidence": ["packet evidence"],
                    "is_actionable": True,
                    "recommended_action": "Update README.",
                    "suggested_rule": None,
                    "should_create_issue": True,
                    "why_deterministic_checks_could_not_decide": "Historical context was ambiguous.",
                }
            )

    packets = {"schema_version": "qc.drift-packets.v1", "packets": [{"packet_id": "p1", "affected_paths": ["README.md"]}]}
    result = adjudicate_packets(packets, FakeClient(), tmp_path)
    assert result["adjudications"][0]["packet_id"] == "p1"
    findings = ai_findings_from_adjudications(result)
    assert findings[0]["check_type"] == "ai_packet_adjudication"
    assert findings[0]["should_create_issue"] is True


def test_adjudicate_packets_records_invalid_json_errors(tmp_path: Path) -> None:
    class FakeClient:
        def complete(self, *_args: object, **_kwargs: object) -> str:
            return "not json"

    packets = {"schema_version": "qc.drift-packets.v1", "packets": [{"packet_id": "p1"}]}
    result = adjudicate_packets(packets, FakeClient(), tmp_path)
    assert result["adjudications"] == []
    assert result["errors"][0]["packet_id"] == "p1"
    assert (tmp_path / "llm-error.json").exists()


def test_issue_policy_is_conservative() -> None:
    assert issue_policy(_finding(), mode="conservative")[0] is True
    medium = {**_finding(), "severity": "medium"}
    assert issue_policy(medium, mode="conservative") == (False, "deterministic_below_issue_threshold")
    assert issue_policy(medium) == (True, "legacy_issue_behavior")
    ai_medium = {**_finding(), "check_type": "ai_packet_adjudication", "confidence": 0.7, "is_actionable": True, "should_create_issue": True, "evidence": ["packet"]}
    assert issue_policy(ai_medium, mode="conservative") == (False, "ai_artifact_only")
    ai_high = {**ai_medium, "severity": "high", "confidence": 0.95}
    assert issue_policy(ai_high, mode="conservative") == (False, "ai_artifact_only")


def test_load_baseline_ignores_malformed_json(tmp_path: Path) -> None:
    malformed = tmp_path / "inventory.json"
    malformed.write_text("{", encoding="utf-8")
    assert load_baseline(malformed) is None


def test_drift_packets_ignore_malformed_file_rows() -> None:
    current = {"schema_version": "qc.inventory.v2", "files": [{"path": "README.md"}], "surfaces": {"workflows": [], "generated_artifacts": []}}
    packets = build_drift_packets(current, {"schema_version": "qc.inventory.v2", "files": [], "surfaces": {}})
    assert packets["packets"] == []


def test_workflow_permission_scan_accepts_qc_workflows() -> None:
    findings = scan_workflow_permissions(REPO)
    high_titles = {finding["title"] for finding in findings if finding["severity"] in {"high", "critical"}}
    assert not high_titles


def test_qc_workflows_are_exact_private_paths() -> None:
    data = yaml.safe_load((REPO / "ls/config/pack.yaml").read_text(encoding="utf-8"))
    private = set(data["public_private"]["private_paths"])
    assert QC_WORKFLOWS <= private
    assert check_release_exclusions(REPO) == []


def _run_triage_step(
    tmp_path: Path,
    step_name: str,
    *,
    event: str = "issues",
    issue_number: str = "42",
    title: str = "",
    body: str = "",
    item_labels: list[str] | None = None,
    catalog_metadata: dict[str, dict[str, str]] | None = None,
    read_failures: list[str] | None = None,
    race_label: str | None = None,
    race_present_label: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], str]:
    workflow = yaml.safe_load((REPO / ".github/workflows/triage.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    step = next(
        candidate
        for job in jobs.values()
        for candidate in job.get("steps", [])
        if candidate.get("name") == step_name
    )

    state_path = tmp_path / "fake-gh-state.json"
    state = {
        "catalog_metadata": catalog_metadata or {},
        "item_labels": item_labels or [],
        "item_number": issue_number,
        "read_failures": read_failures or [],
        "race_label": race_label,
        "race_present_label": race_present_label,
        "race_used": False,
        "calls": [],
        "api_reads": [],
        "create_calls": [],
        "writes": [],
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            state_path = Path(os.environ["FAKE_GH_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            args = sys.argv[1:]
            state.setdefault("calls", []).append(args)

            def save():
                state_path.write_text(json.dumps(state), encoding="utf-8")

            def option_value(option):
                try:
                    return args[args.index(option) + 1]
                except (ValueError, IndexError):
                    return ""

            repository = os.environ["REPOSITORY"]
            if args[:1] == ["api"]:
                route = args[-1] if "--paginate" in args else args[1]
                if "--paginate" in args:
                    state.setdefault("api_reads", []).append(route)
                    save()
                    if route in state.get("read_failures", []):
                        print("simulated API read failure", file=sys.stderr)
                        raise SystemExit(1)
                    if route == f"repos/{repository}/labels":
                        names = list(state.get("catalog_metadata", {}))
                    elif route == f"repos/{repository}/issues/{state['item_number']}/labels":
                        names = list(state.get("item_labels", []))
                    else:
                        print(f"unexpected paginated route: {route}", file=sys.stderr)
                        raise SystemExit(2)
                    print("\\n".join(names))
                    raise SystemExit(0)

                if "-f" in args:
                    labels = []
                    for index, argument in enumerate(args[:-1]):
                        if argument == "-f" and args[index + 1].startswith("labels[]="):
                            labels.append(args[index + 1].split("=", 1)[1])
                    state.setdefault("writes", []).append({"route": route, "labels": labels})
                    state["item_labels"] = list(dict.fromkeys(state.get("item_labels", []) + labels))
                    save()
                    print("[]")
                    raise SystemExit(0)

                print(f"unexpected API call: {args}", file=sys.stderr)
                save()
                raise SystemExit(2)

            if args[:2] == ["label", "create"]:
                label = args[2]
                state.setdefault("create_calls", []).append(args)
                catalog = state.setdefault("catalog_metadata", {})
                if label == state.get("race_label") and not state.get("race_used"):
                    state["race_used"] = True
                    present = state.get("race_present_label")
                    if present:
                        catalog[present] = {"color": "abcdef", "description": "Concurrent label"}
                    save()
                    print("simulated create race", file=sys.stderr)
                    raise SystemExit(1)
                if label in catalog and "--force" not in args:
                    save()
                    print("label already exists", file=sys.stderr)
                    raise SystemExit(1)
                catalog[label] = {
                    "color": option_value("--color"),
                    "description": option_value("--description"),
                }
                save()
                print(label)
                raise SystemExit(0)

            save()
            print(f"unexpected gh call: {args}", file=sys.stderr)
            raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    summary_path = tmp_path / "step-summary.md"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}",
            "FAKE_GH_STATE": str(state_path),
            "GH_TOKEN": "test-token",
            "REPOSITORY": "CruxExperts/LocalSetup",
            "EVENT_NAME": event,
            "ISSUE_NUMBER": issue_number,
            "TITLE": title,
            "BODY": body,
            "GITHUB_STEP_SUMMARY": str(summary_path),
        }
    )
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result, json.loads(state_path.read_text(encoding="utf-8")), (
        summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    )


def test_triage_reopen_preserves_accepted_bug_labels(tmp_path: Path) -> None:
    original_labels = ["status/accepted", "type/bug", "maintainer/keep"]
    result, state, _ = _run_triage_step(
        tmp_path,
        "Apply triage labels",
        event="issues",
        title="Bug report regression",
        body="The accepted bug has no additional area hint.",
        item_labels=original_labels,
    )

    assert result.returncode == 0, result.stderr
    assert state["item_labels"] == original_labels
    assert state["writes"] == []
    assert state["create_calls"] == []


def test_triage_preserves_existing_categories_and_adds_only_missing_hints(tmp_path: Path) -> None:
    original_labels = [
        "status/blocked",
        "type/feature",
        "area/docs",
        "security",
        "maintainer/keep",
    ]
    result, state, _ = _run_triage_step(
        tmp_path,
        "Apply triage labels",
        title="Bug in docs and skills; security review needed",
        item_labels=original_labels,
    )

    assert result.returncode == 0, result.stderr
    assert state["writes"] == [
        {
            "route": "repos/CruxExperts/LocalSetup/issues/42/labels",
            "labels": ["area/skills"],
        }
    ]
    assert state["item_labels"] == original_labels + ["area/skills"]
    assert state["create_calls"] == []


def test_triage_bootstrap_does_not_overwrite_existing_label_metadata(tmp_path: Path) -> None:
    original = {
        "status/needs-triage": {"color": "123abc", "description": "Maintainer-owned wording"},
        "maintainer/custom": {"color": "456def", "description": "Custom label"},
    }
    result, state, _ = _run_triage_step(
        tmp_path,
        "Ensure maintainer labels exist",
        event="issues",
        catalog_metadata=original.copy(),
    )

    assert result.returncode == 0, result.stderr
    assert state["catalog_metadata"]["status/needs-triage"] == original["status/needs-triage"]
    assert state["catalog_metadata"]["maintainer/custom"] == original["maintainer/custom"]
    assert all(args[2] != "status/needs-triage" for args in state["create_calls"])
    assert all("--force" not in args for args in state["create_calls"])


def test_triage_adds_only_missing_labels_to_new_issue(tmp_path: Path) -> None:
    result, state, _ = _run_triage_step(
        tmp_path,
        "Apply triage labels",
        title="Bug in skill workflow docs",
        body="This is also security-sensitive.",
    )

    expected = [
        "status/needs-triage",
        "type/bug",
        "area/docs",
        "area/skills",
        "security",
    ]
    assert result.returncode == 0, result.stderr
    assert state["writes"] == [
        {"route": "repos/CruxExperts/LocalSetup/issues/42/labels", "labels": expected}
    ]
    assert state["item_labels"] == expected
    assert state["create_calls"] == []


@pytest.mark.parametrize(
    ("step_name", "route"),
    [
        ("Ensure maintainer labels exist", "repos/CruxExperts/LocalSetup/labels"),
        ("Apply triage labels", "repos/CruxExperts/LocalSetup/issues/42/labels"),
    ],
)
def test_triage_api_read_failure_prevents_writes(
    tmp_path: Path, step_name: str, route: str
) -> None:
    result, state, _ = _run_triage_step(
        tmp_path,
        step_name,
        read_failures=[route],
    )

    assert result.returncode != 0
    assert state["writes"] == []
    assert state["create_calls"] == []
    if step_name == "Ensure maintainer labels exist":
        assert "no labels were changed" in result.stderr
    else:
        assert "Unable to read current item labels" in result.stderr


@pytest.mark.parametrize(
    ("race_present_label", "expected_success"),
    [
        ("status/needs-triage", True),
        ("status/blocked", False),
    ],
)
def test_triage_catalog_creation_race_requires_exact_readback(
    tmp_path: Path, race_present_label: str, expected_success: bool
) -> None:
    result, state, _ = _run_triage_step(
        tmp_path,
        "Ensure maintainer labels exist",
        event="issues",
        race_label="status/needs-triage",
        race_present_label=race_present_label,
    )

    assert (result.returncode == 0) is expected_success
    assert state["api_reads"].count("repos/CruxExperts/LocalSetup/labels") == 2
    if expected_success:
        assert "status/needs-triage" in state["catalog_metadata"]
    else:
        assert "Could not create label" in result.stderr
        assert "status/needs-triage" not in state["catalog_metadata"]


def test_triage_treats_malicious_issue_text_as_data(tmp_path: Path) -> None:
    marker = tmp_path / "command-executed"
    payload = f"$(touch {marker}); `touch {marker}`; ' ; touch {marker}; bug"
    result, state, _ = _run_triage_step(
        tmp_path,
        "Apply triage labels",
        title=payload,
        body=payload,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert "type/bug" in state["item_labels"]


@pytest.mark.parametrize(
    ("step_name", "event", "issue_number"),
    [
        ("Ensure maintainer labels exist", "schedule", "42"),
        ("Ensure maintainer labels exist", "issues", "42; touch __MARKER__"),
        ("Apply triage labels", "schedule", "42"),
        ("Apply triage labels", "issues", "42; touch __MARKER__"),
    ],
)
def test_triage_rejects_invalid_event_or_item_before_mutation(
    tmp_path: Path, step_name: str, event: str, issue_number: str
) -> None:
    invalid_marker = tmp_path / "invalid-item-executed"
    issue_number = issue_number.replace("__MARKER__", str(invalid_marker))
    result, state, _ = _run_triage_step(
        tmp_path,
        step_name,
        event=event,
        issue_number=issue_number,
    )

    assert result.returncode != 0
    assert state["calls"] == []
    assert state["create_calls"] == []
    assert state["writes"] == []
    assert not invalid_marker.exists()


@pytest.mark.parametrize(
    ("event", "issue_number"),
    [
        ("workflow_dispatch", "42"),
        ("issues", "42; touch __MARKER__"),
    ],
)
def test_triage_handoff_rejects_invalid_event_or_item(
    tmp_path: Path, event: str, issue_number: str
) -> None:
    invalid_marker = tmp_path / "invalid-handoff-executed"
    issue_number = issue_number.replace("__MARKER__", str(invalid_marker))
    result, state, summary = _run_triage_step(
        tmp_path,
        "Write workflow handoff",
        event=event,
        issue_number=issue_number,
        title="untrusted title",
        body="untrusted body",
    )

    assert result.returncode != 0
    assert state["calls"] == []
    assert summary == ""
    assert not invalid_marker.exists()


def test_triage_handoff_uses_only_fixed_text_and_validated_id(tmp_path: Path) -> None:
    marker = tmp_path / "handoff-command-executed"
    payload = f"untrusted title $(touch {marker}) and body"
    result, state, summary = _run_triage_step(
        tmp_path,
        "Write workflow handoff",
        event="issues",
        issue_number="17",
        title=payload,
        body=payload,
    )

    assert result.returncode == 0, result.stderr
    assert state["calls"] == []
    assert summary == (
        "# Triage handoff\n\n"
        "Intake labels were applied to issue #17. They are metadata hints, not a validity or acceptance decision.\n\n"
        "[Continue with the Codex GitHub Issue Goal Loop]"
        "(https://github.com/CruxExperts/LocalSetup/blob/main/ls/docs/CODEX_GITHUB_ISSUE_GOAL_LOOP.md).\n"
    )
    assert payload not in summary
    assert not marker.exists()


def test_release_artifact_exclusion_detects_qc_workflow_in_tar(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        member = tarfile.TarInfo(".github/workflows/qc-ci.yml")
        data = b"name: qc-ci\n"
        member.size = len(data)
        tar.addfile(member, fileobj=io.BytesIO(data))
    findings = check_release_exclusions(REPO, artifact)
    assert any("included in release artifact" in finding["title"] for finding in findings)




def test_workflow_dependency_actions_are_pinned_and_cache_bounded() -> None:
    setup_uv_steps = 0
    for workflow in sorted((REPO / ".github/workflows").glob("*.yml")):
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for job in data["jobs"].values():
            if "uses" in job:
                # Local reusable workflows are scanned by this same outer loop.
                assert job["uses"].startswith("./.github/workflows/")
                assert (REPO / job["uses"]).is_file()
                continue
            for step in job["steps"]:
                uses = str(step.get("uses", ""))
                if uses.startswith("actions/checkout@"):
                    assert uses == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
                if uses.startswith("astral-sh/setup-uv@"):
                    setup_uv_steps += 1
                    assert uses == "astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884"
                    assert step["with"]["prune-cache"] is True
    assert setup_uv_steps > 0


def test_llm_client_disabled_without_secret(monkeypatch) -> None:
    monkeypatch.delenv("QC_LLM_API_KEY", raising=False)
    config = load_config(REPO).llm
    with pytest.raises(LLMDisabled):
        LLMClient(config).complete("prompt")


def test_pr_review_prompt_requires_strict_json() -> None:
    prompt = build_pr_review_prompt([{"name": "chunk", "text": "diff"}], [])
    payload = json.loads(prompt)
    assert payload["output_contract"]["empty_result"] == {"findings": []}
    rules = "\n".join(payload["output_contract"]["rules"])
    assert "Return only one JSON object" in rules
    assert "Do not wrap the JSON in markdown fences" in rules
    assert "{\"findings\":[]}" in rules


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_llm_client_shared_completion_preserves_redaction(monkeypatch, api):
    from dataclasses import replace
    from ls.core.agent.completion_contract import envelope
    config=replace(load_config(REPO).llm,base_url="https://example.test/v1",api_key="secret",api_style=api)
    calls=[]
    def complete(root,payload,authority):
        calls.append(payload)
        request=json.loads(payload['request'])
        assert 'secret-value' not in request['input']
        assert request['max_attempts']==1 and 'temperature' not in request
        assert payload['profile']['api']==api
        return envelope('succeeded',model=config.model,data={'findings':[]},attempts=1)
    monkeypatch.setattr('ls.core.agent.completion_run.run',complete)
    assert LLMClient(config).complete('api_key=secret-value')=='{"findings":[]}'
    assert len(calls)==1


def test_llm_client_uncertain_not_retried(monkeypatch):
    from dataclasses import replace
    from ls.core.agent.completion_contract import envelope
    config=replace(load_config(REPO).llm,base_url="https://example.test",api_key="secret",retry_count=4)
    calls=[]
    def complete(*args):
        calls.append(1)
        return envelope('uncertain',model=config.model,attempts=1)
    monkeypatch.setattr('ls.core.agent.completion_run.run',complete)
    with pytest.raises(RuntimeError,match='uncertain'):LLMClient(config).complete('prompt')
    assert calls==[1]


def test_llm_strict_json_invalid_response(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    with pytest.raises(ValueError, match="valid JSON"):
        parse_strict_json("not json", raw)
    assert "not json" in raw.read_text(encoding="utf-8")


def test_llm_strict_json_accepts_repeated_identical_objects(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    assert parse_strict_json('{"findings":[]}{"findings":[]}', raw) == {"findings": []}
    assert not raw.exists()


def test_llm_strict_json_rejects_different_concatenated_objects(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    with pytest.raises(ValueError, match="valid JSON"):
        parse_strict_json('{"findings":[]}{"findings":[{"category":"bad"}]}', raw)
    assert raw.exists()


def test_read_diff_uses_hunks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "@@ -1 +1 @@\n-old\n+new\n"

    def run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr("tools.qc_patrol.diff_reader.subprocess.run", run)
    assert "+new" in read_diff(REPO, "base", "head")
    assert "--stat" not in calls[0]
    assert "--unified=80" in calls[0]


def test_autofix_only_allows_explicit_ids(tmp_path: Path) -> None:
    assert plan_autofix("123", dry_run=False, create_pr=True, out=tmp_path)["create_pr"] is False
    assert plan_autofix("generated-docs-refresh", dry_run=False, create_pr=True, out=tmp_path)["create_pr"] is True


def test_llm_error_artifact_redacts_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def complete(_self: object, _prompt: str) -> str:
        raise RuntimeError("failed calling https://llm.example.test/chat/completions")

    monkeypatch.setattr("tools.qc_patrol.cli.LLMClient.complete", complete)
    monkeypatch.setenv("QC_LLM_BASE_URL", "https://llm.example.test")
    monkeypatch.setenv("QC_LLM_API_KEY", "secret")
    result = qc_main(
        [
            "pr-review",
            "--subject-repo",
            str(REPO),
            "--base",
            "HEAD",
            "--head",
            "HEAD",
            "--out",
            str(tmp_path),
            "--llm-mode",
            "auto",
        ]
    )
    assert result == 0
    payload = json.loads((tmp_path / "llm-error.json").read_text(encoding="utf-8"))
    assert "llm.example.test" not in payload["error"]
    assert payload["endpoint_alias"]


def test_docs_alignment_failure_becomes_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 1
        stdout = '{"ok": false, "findings": [{"id": "drift"}]}'
        stderr = ""

    monkeypatch.setattr("tools.qc_patrol.docs_drift.subprocess.run", lambda *args, **kwargs: Result())
    findings = docs_alignment_findings(REPO)
    assert findings[0]["category"] == "docs"
    assert findings[0]["check_type"] == "docs_alignment"


def test_unavailable_documentation_check_is_artifact_only_and_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        returncode = 1
        stdout = "runner could not execute docs alignment"
        stderr = "tool unavailable"

    monkeypatch.setattr("tools.qc_patrol.docs_drift.subprocess.run", lambda *args, **kwargs: Result())
    out = tmp_path / "docs-drift"
    result = qc_main(["docs-drift", "--repo", str(REPO), "--out", str(out), "--write-issues"])
    ledger = json.loads((out / "ledger.json").read_text(encoding="utf-8"))
    assert result == 1
    assert ledger["findings"][0]["check_type"] == "docs_alignment_unavailable"
    assert ledger["issue_results"] == []


@pytest.mark.parametrize(("runtime_status", "expected_calls"), [("missing", 0), ("verified", 1)])
def test_unavailable_protected_completion_fails_but_keeps_deterministic_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime_status: str, expected_calls: int
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "qc.inventory.v2",
                "files": [{"path": ".github/workflows/qc-patrol.yml", "hash": "older"}],
                "surfaces": {"workflows": [], "generated_artifacts": []},
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def unavailable(_self: object, *_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        raise LLMDisabled("QC completion unavailable")

    packets = [_lifecycle_packet("pending-1"), _lifecycle_packet("pending-2")]
    monkeypatch.setattr("tools.qc_patrol.cli.build_inventory", lambda _repo: _lifecycle_inventory())
    monkeypatch.setattr(
        "tools.qc_patrol.cli.build_drift_packets",
        lambda *_args, **_kwargs: {"schema_version": "qc.drift-packets.v1", "packets": packets},
    )
    monkeypatch.setattr("tools.qc_patrol.cli.markdown_version_packets", lambda *_args, **_kwargs: {"packets": []})
    monkeypatch.setattr(LLMClient, "complete", unavailable)
    monkeypatch.setattr("tools.qc_patrol.cli._patrol_runtime_status", lambda _config: runtime_status)
    monkeypatch.setattr("tools.qc_patrol.cli.run_deterministic", lambda *_args: [_finding()])
    monkeypatch.setenv("QC_PATROL_MODEL", "small-model")
    monkeypatch.setenv("QC_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("QC_LLM_API_KEY", "fixture-credential")
    dispositions = tmp_path / "packet-dispositions.json"
    dispositions.write_text(
        json.dumps({"schema_version": "qc.packet-dispositions.v1", "dispositions": {}}),
        encoding="utf-8",
    )
    out = tmp_path / "patrol-out"
    result = qc_main(
        [
            "patrol",
            "--repo",
            str(REPO),
            "--out",
            str(out),
            "--baseline",
            str(baseline),
            "--dispositions",
            str(dispositions),
            "--ai-mode",
            "packets",
        ]
    )
    assert result == 1
    adjudications = json.loads((out / "ai-adjudications.json").read_text(encoding="utf-8"))
    assert adjudications["errors"]
    assert [packet["packet_id"] for packet in adjudications["pending_packets"]] == (
        ["pending-1", "pending-2"] if runtime_status == "missing" else ["pending-2"]
    )
    ledger = json.loads((out / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["deterministic_findings"] == [_finding()]
    assert ledger["ai_findings"] == []
    assert ledger["issue_results"][0]["action"] == "create"
    assert calls == expected_calls

def _lifecycle_packet(packet_id: str) -> dict[str, Any]:
    return {
        "packet_id": packet_id,
        "fingerprint": packet_id,
        "kind": "test.packet",
        "reason": "A synthetic patrol packet.",
        "severity_hint": "low",
        "affected_paths": ["README.md"],
        "facts": {},
        "deterministic_evidence": [],
        "snippets": [],
        "question": "Record only a factual observation.",
        "redaction_applied": True,
    }


def _lifecycle_inventory() -> dict[str, Any]:
    return {
        "schema_version": "qc.inventory.v2",
        "head_sha": "lifecycle-fixture",
        "tracked_file_count": 0,
        "files": [],
        "surfaces": {"docs": [], "workflows": [], "generated_artifacts": []},
    }


def _prepare_packet_lifecycle(monkeypatch: pytest.MonkeyPatch, packets: list[dict[str, Any]]) -> None:
    monkeypatch.setattr("tools.qc_patrol.cli.build_inventory", lambda _repo: _lifecycle_inventory())
    monkeypatch.setattr(
        "tools.qc_patrol.cli.build_drift_packets",
        lambda *_args, **_kwargs: {"schema_version": "qc.drift-packets.v1", "packets": packets},
    )
    monkeypatch.setattr("tools.qc_patrol.cli.markdown_version_packets", lambda *_args, **_kwargs: {"packets": []})
    monkeypatch.setattr("tools.qc_patrol.cli._patrol_runtime_status", lambda _config: "verified")
    monkeypatch.setattr("tools.qc_patrol.cli.run_deterministic", lambda *_args: [])
    monkeypatch.setenv("QC_PATROL_MODEL", "small-model")
    monkeypatch.setenv("QC_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("QC_LLM_API_KEY", "fixture-credential")


def _write_packet_lifecycle_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"schema_version": "qc.inventory.v2", "files": [], "surfaces": {}}),
        encoding="utf-8",
    )
    dispositions = tmp_path / "prior-dispositions.json"
    dispositions.write_text(
        json.dumps({"schema_version": "qc.packet-dispositions.v1", "dispositions": {}}),
        encoding="utf-8",
    )
    pending = tmp_path / "prior-pending.json"
    pending.write_text(
        json.dumps({"schema_version": "qc.pending-packets.v1", "packets": []}),
        encoding="utf-8",
    )
    return baseline, dispositions, pending


def _run_packet_lifecycle(
    out: Path, baseline: Path, dispositions: Path, pending: Path
) -> int:
    return qc_main(
        [
            "patrol",
            "--repo",
            str(REPO),
            "--out",
            str(out),
            "--baseline",
            str(baseline),
            "--dispositions",
            str(dispositions),
            "--pending-packets",
            str(pending),
            "--ai-mode",
            "packets",
        ]
    )


def _factual_packet_adjudication(packet_id: str) -> str:
    return json.dumps(
        {
            "packet_id": packet_id,
            "finding": "",
            "confidence": 0,
            "category": "inventory",
            "severity": "low",
            "affected_paths": [],
            "evidence": [],
            "is_actionable": False,
            "recommended_action": "",
            "suggested_rule": None,
            "should_create_issue": False,
            "why_deterministic_checks_could_not_decide": "",
        }
    )


def test_patrol_does_not_replay_successfully_adjudicated_packet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_packet_lifecycle(monkeypatch, [_lifecycle_packet("same-packet")])
    baseline, dispositions, pending = _write_packet_lifecycle_inputs(tmp_path)
    calls: list[str] = []

    def complete(_self: object, prompt: str, **_kwargs: object) -> str:
        packet_id = json.loads(prompt)["packet"]["packet_id"]
        calls.append(packet_id)
        return _factual_packet_adjudication(packet_id)

    monkeypatch.setattr(LLMClient, "complete", complete)
    first_out = tmp_path / "first"
    assert _run_packet_lifecycle(first_out, baseline, dispositions, pending) == 0
    second_out = tmp_path / "second"
    assert _run_packet_lifecycle(
        second_out,
        baseline,
        first_out / "packet-dispositions.json",
        first_out / "pending-packets.json",
    ) == 0

    state = json.loads((second_out / "packet-dispositions.json").read_text(encoding="utf-8"))
    result = json.loads((second_out / "ai-adjudications.json").read_text(encoding="utf-8"))
    drift = json.loads((second_out / "drift-packets.json").read_text(encoding="utf-8"))
    assert calls == ["same-packet"]
    assert state == {"schema_version": "qc.packet-dispositions.v1", "dispositions": {"same-packet": "completed"}}
    assert result["calls_made"] == 0
    assert result["pending_packets"] == []
    assert drift["packets"] == []


def test_first_patrol_bootstraps_empty_state_before_packet_mode_can_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    packet = _lifecycle_packet("bootstrap-packet")
    _prepare_packet_lifecycle(monkeypatch, [packet])
    calls: list[str] = []

    def complete(_self: object, prompt: str, **_kwargs: object) -> str:
        packet_id = json.loads(prompt)["packet"]["packet_id"]
        calls.append(packet_id)
        return _factual_packet_adjudication(packet_id)

    monkeypatch.setattr(LLMClient, "complete", complete)
    bootstrap_out = tmp_path / "bootstrap"
    assert qc_main(
        [
            "patrol",
            "--repo",
            str(REPO),
            "--out",
            str(bootstrap_out),
            "--initialize-dispositions",
            "--ai-mode",
            "off",
        ]
    ) == 0
    bootstrap_state = json.loads(
        (bootstrap_out / "packet-dispositions.json").read_text(encoding="utf-8")
    )
    assert bootstrap_state == {"schema_version": "qc.packet-dispositions.v1", "dispositions": {}}
    assert calls == []

    run_out = tmp_path / "after-bootstrap"
    assert _run_packet_lifecycle(
        run_out,
        bootstrap_out / "inventory.json",
        bootstrap_out / "packet-dispositions.json",
        bootstrap_out / "pending-packets.json",
    ) == 0
    assert calls == ["bootstrap-packet"]


def test_patrol_failed_attempt_is_not_replayed_and_deferred_packets_continue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    packets = [_lifecycle_packet(packet_id) for packet_id in ("uncertain", "deferred-1", "deferred-2")]
    _prepare_packet_lifecycle(monkeypatch, packets)
    baseline, dispositions, pending = _write_packet_lifecycle_inputs(tmp_path)
    calls: list[str] = []
    first_out = tmp_path / "failed-run"
    active_out = first_out

    def complete(_self: object, prompt: str, **_kwargs: object) -> str:
        packet_id = json.loads(prompt)["packet"]["packet_id"]
        calls.append(packet_id)
        state = json.loads((active_out / "packet-dispositions.json").read_text(encoding="utf-8"))
        assert state["dispositions"][packet_id] == "attempted"
        queue = json.loads((active_out / "pending-packets.json").read_text(encoding="utf-8"))
        expected_pending = {
            "uncertain": ["deferred-1", "deferred-2"],
            "deferred-1": ["deferred-2"],
            "deferred-2": [],
        }[packet_id]
        assert [packet["packet_id"] for packet in queue["packets"]] == expected_pending
        if packet_id == "uncertain":
            raise RuntimeError("provider response was uncertain")
        return _factual_packet_adjudication(packet_id)

    monkeypatch.setattr(LLMClient, "complete", complete)
    assert _run_packet_lifecycle(first_out, baseline, dispositions, pending) == 1
    failed_state = json.loads((first_out / "packet-dispositions.json").read_text(encoding="utf-8"))
    failed_queue = json.loads((first_out / "pending-packets.json").read_text(encoding="utf-8"))
    assert failed_state["dispositions"] == {"uncertain": "attempted"}
    assert [packet["packet_id"] for packet in failed_queue["packets"]] == ["deferred-1", "deferred-2"]

    second_out = tmp_path / "recovered-run"
    active_out = second_out
    assert _run_packet_lifecycle(
        second_out,
        baseline,
        first_out / "packet-dispositions.json",
        first_out / "pending-packets.json",
    ) == 0
    recovered_state = json.loads((second_out / "packet-dispositions.json").read_text(encoding="utf-8"))
    recovered_queue = json.loads((second_out / "pending-packets.json").read_text(encoding="utf-8"))
    result = json.loads((second_out / "ai-adjudications.json").read_text(encoding="utf-8"))
    assert calls == ["uncertain", "deferred-1", "deferred-2"]
    assert recovered_state["dispositions"] == {
        "uncertain": "attempted",
        "deferred-1": "completed",
        "deferred-2": "completed",
    }
    assert recovered_queue["packets"] == []
    assert result["calls_made"] == 2


@pytest.mark.parametrize("receipt_kind", ["missing", "corrupt"])
def test_patrol_missing_or_corrupt_disposition_receipt_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, receipt_kind: str
) -> None:
    _prepare_packet_lifecycle(monkeypatch, [_lifecycle_packet("must-not-dispatch")])
    baseline, _dispositions, pending = _write_packet_lifecycle_inputs(tmp_path)
    receipt = tmp_path / "unavailable-dispositions.json"
    if receipt_kind == "corrupt":
        receipt.write_text("{", encoding="utf-8")
    calls: list[str] = []

    def complete(_self: object, prompt: str, **_kwargs: object) -> str:
        calls.append(json.loads(prompt)["packet"]["packet_id"])
        return _factual_packet_adjudication(calls[-1])

    monkeypatch.setattr(LLMClient, "complete", complete)
    out = tmp_path / receipt_kind
    assert _run_packet_lifecycle(out, baseline, receipt, pending) == 1
    result = json.loads((out / "ai-adjudications.json").read_text(encoding="utf-8"))
    queue = json.loads((out / "pending-packets.json").read_text(encoding="utf-8"))
    assert calls == []
    assert result["errors"][0]["packet_id"] == "packet-dispositions"
    assert [packet["packet_id"] for packet in queue["packets"]] == ["must-not-dispatch"]
    assert not (out / "packet-dispositions.json").exists()


def test_docs_alignment_issue_is_factual_and_unchanged_duplicate_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_value = "observed-1"

    class Result:
        returncode = 1
        stderr = ""

        @property
        def stdout(self) -> str:
            return json.dumps(
                {
                    "ok": False,
                    "findings": [
                        {
                            "id": "generated-version",
                            "category": "generated",
                            "path": "README.md",
                            "message": "Generated version differs.",
                            "expected": "expected-1",
                            "actual": actual_value,
                            "severity": "major",
                        }
                    ],
                }
            )

    class FakeAPI:
        issues: list[dict[str, Any]] = []
        comments: list[str] = []

        def enabled(self) -> bool:
            return True

        def list_open_issues(self, _label: str) -> list[dict[str, Any]]:
            return list(self.issues)

        def create_issue(self, title: str, body: str, _labels: list[str]) -> dict[str, Any]:
            issue = {"number": 1, "title": title, "body": body}
            self.issues.append(issue)
            return issue

        def comment_issue(self, _number: int, body: str) -> None:
            self.comments.append(body)

        def list_issue_comments(self, _number: int) -> list[dict[str, str]]:
            return [{"body": body} for body in self.comments]

    monkeypatch.setattr("tools.qc_patrol.docs_drift.subprocess.run", lambda *args, **kwargs: Result())
    api = FakeAPI()
    finding = docs_alignment_findings(REPO)[0]
    first = write_issues([finding], ["qc-patrol"], api=api)
    second = write_issues([finding], ["qc-patrol"], api=api)
    assert first[0]["action"] == "create"
    assert second[0]["action"] == "unchanged"
    assert api.comments == []
    assert "expected-1" in api.issues[0]["body"]
    assert "observed-1" in api.issues[0]["body"]
    assert "Recommended action" not in api.issues[0]["body"]

    actual_value = "observed-2"
    changed = docs_alignment_findings(REPO)[0]
    assert fingerprint_for(changed) == fingerprint_for(finding)
    # A maintainer can edit the issue after it was listed; append-only updates
    # must leave that text intact.
    api.issues[0]["body"] += "\nMaintainer note: retain this context."
    body_before_update = api.issues[0]["body"]
    updated = write_issues([changed], ["qc-patrol"], api=api)
    assert updated[0]["action"] == "comment"
    assert api.issues[0]["body"] == body_before_update
    assert "observed-2" in api.comments[0]
    repeated = write_issues([changed], ["qc-patrol"], api=api)
    assert repeated[0]["action"] == "unchanged"
    assert len(api.issues) == 1
    assert len(api.comments) == 1


def _run_patrol_baseline_step(
    tmp_path: Path, mode: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], dict[str, Any]]:
    workflow = yaml.safe_load((REPO / ".github/workflows/qc-patrol.yml").read_text(encoding="utf-8"))
    step = next(
        item
        for item in workflow["jobs"]["patrol"]["steps"]
        if item.get("name") == "Download previous patrol baseline"
    )
    state_path = tmp_path / "baseline-gh-state.json"
    state_path.write_text(json.dumps({"mode": mode, "calls": []}), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            state_path = Path(os.environ["BASELINE_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            args = sys.argv[1:]
            state["calls"].append(args)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            mode = state["mode"]
            if args[:2] == ["run", "list"]:
                status = args[args.index("--status") + 1]
                if status == "success":
                    if mode in {"lookup-fail", "success-lookup-fail"}:
                        raise SystemExit(1)
                    if mode in {"invalid-json", "success-invalid-json"}:
                        print("{")
                    else:
                        print(json.dumps([] if mode == "no-baseline" else [{"databaseId": 12345}]))
                    raise SystemExit(0)
                if status == "completed":
                    if mode == "completed-lookup-fail":
                        raise SystemExit(1)
                    if mode == "completed-invalid-json":
                        print("{")
                    else:
                        print(json.dumps([] if mode == "no-baseline" else [{"databaseId": 23456}]))
                    raise SystemExit(0)
                raise SystemExit(2)
            if args[:2] == ["run", "download"]:
                run_id = args[2]
                if run_id == "12345" and mode in {"download-fail", "success-download-fail"}:
                    raise SystemExit(1)
                if run_id == "23456" and mode == "completed-download-fail":
                    raise SystemExit(1)
                output = Path(args[args.index("--dir") + 1])
                artifact = output / f"qc-patrol-{run_id}"
                artifact.mkdir(parents=True, exist_ok=True)
                if run_id == "12345":
                    (artifact / "inventory.json").write_text(
                        json.dumps({"schema_version": "qc.inventory.v2", "files": [], "surfaces": {}}),
                        encoding="utf-8",
                    )
                elif run_id == "23456":
                    if mode != "missing-state":
                        receipt = (
                            "{"
                            if mode == "corrupt-state"
                            else json.dumps(
                                {
                                    "schema_version": "qc.packet-dispositions.v1",
                                    "dispositions": {"finished": "completed", "uncertain": "attempted"},
                                }
                            )
                        )
                        (artifact / "packet-dispositions.json").write_text(receipt, encoding="utf-8")
                    if mode != "missing-queue":
                        (artifact / "pending-packets.json").write_text(
                            json.dumps({"schema_version": "qc.pending-packets.v1", "packets": []}),
                            encoding="utf-8",
                        )
                else:
                    raise SystemExit(2)
                raise SystemExit(0)
            raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    fake_jq = fake_bin / "jq"
    fake_jq.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            if args[0] == "-r":
                payload = json.load(sys.stdin)
                print(payload[0].get("databaseId", "") if payload else "")
                raise SystemExit(0)
            if args[0] == "-e":
                try:
                    payload = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raise SystemExit(1)
                valid = (
                    isinstance(payload, dict)
                    and payload.get("schema_version") == "qc.packet-dispositions.v1"
                    and isinstance(payload.get("dispositions"), dict)
                    and all(
                        isinstance(packet_id, str)
                        and packet_id
                        and status in {"attempted", "completed"}
                        for packet_id, status in payload["dispositions"].items()
                    )
                )
                if valid:
                    print("true")
                    raise SystemExit(0)
                raise SystemExit(1)
            raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    fake_jq.chmod(0o755)
    output_path = tmp_path / "step-output"
    summary_path = tmp_path / "step-summary"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "BASELINE_STATE": str(state_path),
            "GITHUB_OUTPUT": str(output_path),
            "GITHUB_STEP_SUMMARY": str(summary_path),
            "GH_TOKEN": "fixture",
            "REPOSITORY": "CruxExperts/LocalSetup",
            "BRANCH_NAME": "main",
        }
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    outputs = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    status = json.loads((tmp_path / "qc-out/baseline-status.json").read_text(encoding="utf-8"))
    return result, outputs, status


@pytest.mark.parametrize(
    ("mode", "expected_baseline", "expected_disposition"),
    [
        ("available", "available", "available"),
        ("no-baseline", "no-baseline", "no-prior-run"),
        ("lookup-fail", "unavailable", "available"),
        ("download-fail", "unavailable", "available"),
        ("invalid-json", "unavailable", "available"),
        ("completed-lookup-fail", "available", "unavailable"),
        ("completed-invalid-json", "available", "unavailable"),
        ("completed-download-fail", "available", "unavailable"),
        ("missing-state", "available", "unavailable"),
        ("corrupt-state", "available", "unavailable"),
        ("missing-queue", "available", "unavailable"),
    ],
)
def test_patrol_artifact_lookup_separates_inventory_baseline_and_latest_completed_state(
    tmp_path: Path, mode: str, expected_baseline: str, expected_disposition: str
) -> None:
    result, outputs, status = _run_patrol_baseline_step(tmp_path, mode)
    assert result.returncode == 0
    assert outputs["status"] == expected_baseline
    assert outputs["disposition_status"] == expected_disposition
    assert status["status"] == expected_baseline
    assert status["disposition_status"] == expected_disposition
    state = json.loads((tmp_path / "baseline-gh-state.json").read_text(encoding="utf-8"))
    list_calls = [call for call in state["calls"] if call[:2] == ["run", "list"]]
    assert [call[call.index("--status") + 1] for call in list_calls] == ["success", "completed"]
    if mode == "available":
        assert status["run_id"] == 12345
        assert status["disposition_run_id"] == 23456
        assert (tmp_path / outputs["baseline_path"]).is_file()
        assert (tmp_path / outputs["disposition_path"]).is_file()
        assert (tmp_path / outputs["pending_path"]).is_file()
        assert json.loads(
            (tmp_path / outputs["disposition_path"]).read_text(encoding="utf-8")
        )["dispositions"] == {"finished": "completed", "uncertain": "attempted"}
        assert "qc-completed" in outputs["pending_path"]
    elif mode == "no-baseline":
        assert outputs["baseline_path"] == ""
        assert outputs["disposition_path"] == ""
        assert outputs["pending_path"] == ""
    elif mode in {"missing-state", "corrupt-state"}:
        assert outputs["disposition_path"] == ""
        assert outputs["pending_path"]
        assert (tmp_path / outputs["pending_path"]).is_file()
    elif mode == "missing-queue":
        assert outputs["disposition_status"] == "unavailable"
        assert outputs["pending_path"] == ""
    elif mode in {"lookup-fail", "download-fail", "invalid-json"}:
        assert outputs["disposition_status"] == "available"
        assert (tmp_path / outputs["disposition_path"]).is_file()



def test_packet_mode_reports_missing_runtime_even_with_no_packets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "tools.qc_patrol.cli.build_inventory",
        lambda _repo: {
            "schema_version": "qc.inventory.v2",
            "head_sha": "fixture",
            "tracked_file_count": 0,
            "surfaces": {"docs": [], "workflows": []},
        },
    )
    monkeypatch.setattr(
        "tools.qc_patrol.cli.build_drift_packets",
        lambda *_args, **_kwargs: {"schema_version": "qc.drift-packets.v1", "packets": []},
    )
    monkeypatch.setattr("tools.qc_patrol.cli.markdown_version_packets", lambda *_args, **_kwargs: {"packets": []})
    monkeypatch.setattr("tools.qc_patrol.cli._patrol_runtime_status", lambda _config: "missing")
    monkeypatch.setattr("tools.qc_patrol.cli.run_deterministic", lambda *_args: [])
    monkeypatch.setenv("QC_PATROL_MODEL", "small-model")
    monkeypatch.setenv("QC_LLM_BASE_URL", "https://fixture.invalid")
    baseline, dispositions, _pending = _write_packet_lifecycle_inputs(tmp_path)
    monkeypatch.setenv("QC_LLM_API_KEY", "fixture-credential")
    out = tmp_path / "empty-packet-patrol"
    result = qc_main(
        [
            "patrol",
            "--repo",
            str(REPO),
            "--out",
            str(out),
            "--baseline",
            str(baseline),
            "--dispositions",
            str(dispositions),
            "--ai-mode",
            "packets",
        ]
    )
    assert result == 1
    adjudications = json.loads((out / "ai-adjudications.json").read_text(encoding="utf-8"))
    assert adjudications["errors"]
    assert adjudications["pending_packets"] == []
