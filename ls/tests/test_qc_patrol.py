from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tools.qc_patrol.adjudication import adjudicate_packets, ai_findings_from_adjudications, build_packet_prompt
from tools.qc_patrol.chunking import chunk_text
from tools.qc_patrol.cli import build_pr_review_prompt
from tools.qc_patrol.cli import main as qc_main
from tools.qc_patrol.config import load_config
from tools.qc_patrol.diff_reader import read_diff
from tools.qc_patrol.drift import build_drift_packets, load_baseline
from tools.qc_patrol.deterministic_checks import QC_WORKFLOWS, check_release_exclusions, scan_workflow_permissions
from tools.qc_patrol.docs_drift import docs_alignment_findings
from tools.qc_patrol.issue_writer import extract_handoff, find_duplicate, fingerprint_for, issue_body, issue_policy
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
    assert issue_policy(ai_high, mode="conservative") == (True, "ai_high_confidence_actionable")


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


def test_release_artifact_exclusion_detects_qc_workflow_in_tar(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        member = tarfile.TarInfo(".github/workflows/qc-ci.yml")
        data = b"name: qc-ci\n"
        member.size = len(data)
        tar.addfile(member, fileobj=io.BytesIO(data))
    findings = check_release_exclusions(REPO, artifact)
    assert any("included in release artifact" in finding["title"] for finding in findings)


def test_new_qc_workflow_static_contracts() -> None:
    for workflow in sorted((REPO / ".github/workflows").glob("qc-*.yml")):
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        assert "permissions" in data, workflow
        text = workflow.read_text(encoding="utf-8")
        assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in text
        if workflow.name == "qc-patrol.yml":
            assert data["permissions"]["actions"] == "read"
            assert "qc/category/inventory" in text
            assert "--ai-mode \"$QC_PATROL_AI_MODE\"" in text
        if workflow.name == "qc-pr-review.yml":
            assert "github.event.pull_request.head.repo.full_name == github.repository" in text
            assert "repository: ${{ github.event.pull_request.head.repo.full_name || github.repository }}" in text
            assert "QC_BASE_REMOTE: https://github.com/${{ github.repository }}.git" in text
            job = data["jobs"]["pr-review"]
            assert "env" not in job
            checkouts = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
            assert len(checkouts) == 2
            assert all(step["with"]["persist-credentials"] is False for step in checkouts)
            route = next(step for step in job["steps"] if step.get("id") == "qc_route")
            assert "env" not in route
            assert 'accepted_relative = "ls/config/pack.yaml"' in route["run"]
            assert "except FileNotFoundError:" in route["run"]
            assert "declares_capability(subject_source)" in route["run"]
            trusted = next(step for step in job["steps"] if step.get("name") == "Run trusted-base PR QC review")
            assert trusted["if"] == "steps.qc_route.outputs.route == 'trusted-base'"
            assert "secrets.QC_LLM_API_KEY" in trusted["env"]["QC_LLM_API_KEY"]
            for key in ("LLM_MODE", "QC_LLM_BASE_URL", "QC_LLM_API_KEY", "QC_LLM_ORGANIZATION", "QC_LLM_PROJECT"):
                expression = trusted["env"][key]
                assert "vars.QC_PR_LLM_ENABLED == 'true' &&" in expression
                assert "github.event_name == 'pull_request' &&" in expression
                assert "github.event.pull_request.head.repo.full_name == github.repository &&" in expression
            assert trusted["env"]["LLM_MODE"].endswith("'auto' || 'off' }}")
            assert "python tools/qc_patrol/cli.py pr-review" in trusted["run"]
            assert "qc-subject/tools/qc_patrol/cli.py" not in trusted["run"]
            subject = next(step for step in job["steps"] if step.get("name") == "Run no-secret subject PR QC review")
            assert subject["if"] == "steps.qc_route.outputs.route == 'subject-no-secret'"
            assert set(subject["env"]) == {"QC_PR_BASE_SHA", "QC_PR_HEAD_SHA", "QC_BASE_REMOTE"}
            assert "secrets." not in route["run"]
            assert "secrets." not in subject["run"]
            assert "-u QC_LLM_API_KEY" in subject["run"]
            assert "-u GITHUB_TOKEN" in subject["run"]
            assert "--llm-mode off" in subject["run"]
            assert "github.event.pull_request.base.sha" not in "\n".join(
                str(step.get("run", "")) for step in job["steps"] if isinstance(step, dict)
            )
            assert "pull_request_target" not in text
        if workflow.name == "qc-autofix.yml":
            assert set(data[True].keys()) == {"workflow_dispatch"}
            assert "github.event.inputs.issue" not in "\n".join(
                str(step.get("run", "")) for step in data["jobs"]["autofix"]["steps"] if isinstance(step, dict)
            )


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
                    assert uses == "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"
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
