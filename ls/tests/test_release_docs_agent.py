from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from tools.qc_patrol.redaction import redact_text

from ls.core.release_docs.agent import CompletionBudget, _call, _escrow_urls, prepare_candidate
from ls.core.release_docs.proposals import normalize_source_material, validate_proposal, validate_record


def test_deadline_clients_preserve_one_run_session_without_sharing_prompts(monkeypatch):
    from dataclasses import replace
    from tools.qc_patrol.config import load_config
    from tools.qc_patrol.llm_client import LLMClient
    from ls.core.agent.completion_contract import envelope
    config=replace(load_config(Path.cwd()).llm,base_url='https://example.test/v1',api_key='fixture')
    observed=[]
    def complete(root,payload,authority):
        observed.append(json.loads(payload['request']))
        return envelope('succeeded',model=config.model,data={},attempts=1)
    monkeypatch.setattr('ls.core.agent.completion_run.run',complete)
    client=LLMClient(config)
    budget=CompletionBudget(3,time.monotonic()+30)
    for body in ({'source':'first'},{'source':'second'}):
        assert _call(client,budget,'analyst',{'type':'object'},body,'fixture')=={}
    assert _call(LLMClient(config),budget,'analyst',{'type':'object'},{'source':'third'},'fixture')=={}
    assert observed[0]['session_id']==observed[1]['session_id']==client.session_id
    assert observed[2]['session_id']!=client.session_id
    assert 'first' not in observed[1]['input']
    assert 'second' not in observed[2]['input']


def test_claim_mapping_can_decline_an_unrelated_fact_batch() -> None:
    from ls.core.release_docs.schemas import CLAIM_MAP_SCHEMA
    source = CLAIM_MAP_SCHEMA["properties"]["mappings"]["items"]["properties"]["source"]
    assert source["minItems"] == 0


def test_source_fact_schema_fixes_supplied_provenance(tmp_path):
    from ls.core.release_docs.agent import _source_facts
    from ls.core.release_docs.schemas import FACT_SCHEMA

    class Client:
        def complete(self, prompt, response_schema, **kwargs):
            properties = response_schema["properties"]["facts"]["items"]["properties"]
            assert properties["path"]["enum"] == ["source.py"]
            assert properties["chunk"]["enum"] == [2]
            assert properties["evidence"]["items"]["enum"] == ["source.py", "a" * 40]
            return json.dumps({"facts": [{"path": "source.py", "chunk": 2,
                                         "text": "A source fact.", "evidence": ["source.py"]}]})

    _source_facts(Client(), CompletionBudget(2, time.monotonic() + 30), {"source_commit": "a" * 40},
                  [{"path": "source.py", "chunk": 2, "kind": "change", "text": "source"}], {"source.py"})
    assert "enum" not in FACT_SCHEMA["properties"]["facts"]["items"]["properties"]["path"]


def _write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _plan(root: Path) -> dict[str, Any]:
    _write(root, "README.md", "# LocalSetup\n\nCurrent release notes.\n")
    _write(root, "ls/docs/RELEASE.md", "# Release\n")
    _write(root, "ls/core/example.py", "def new_feature():\n    return True\n")
    return {"source_commit": "a" * 40, "target_version": "5.0.0", "baseline_tag": "v4.22.9", "changed_paths": ["ls/core/example.py"], "source_material": [{"path": "ls/core/example.py", "content": "def new_feature():\n    return True\n"}], "documents": ["README.md", "ls/docs/RELEASE.md"]}


class _Client:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, prompt: str, **_kwargs: Any) -> str:
        self.calls.append(prompt)
        payload = json.loads(prompt)
        if payload["role"] == "release source analyst":
            source = payload["source_chunk"]
            return json.dumps({"facts": [{"path": source["path"], "chunk": source["chunk"], "text": "The source changes behavior.", "evidence": [source["path"]]}]})
        if payload["role"] == "release documentation evidence mapper":
            document = payload["document_chunks"][0]
            facts = payload["source_facts"]
            return json.dumps({"mappings": [{"path": document["path"], "chunk": document["chunk"], "source": ([{"path": facts[0]["path"], "chunk": facts[0]["chunk"]}] if facts else [])}]})
        if payload["role"] == "release record evidence mapper":
            claim = payload["record_claim"]
            facts = payload["source_facts"]
            return json.dumps({"mappings": [{"claim": claim["claim"], "source": [{"path": facts[0]["path"], "chunk": facts[0]["chunk"]}]}]})
        if payload["role"] == "release documentation auditor and editor":
            return json.dumps({"coverage": [{"path": item["path"], "chunk": item["chunk"], "disposition": "no_change", "evidence": ["ls/core/example.py"]} for item in payload["document_chunks"]], "edits": []})
        if payload["role"] == "release documentation release-note editor":
            release = payload["release"]
            return json.dumps({"record": {"schema_version": 1, "version": release["target_version"], "source_commit": release["source_commit"], "baseline_tag": release["baseline_tag"], "summary": "Adds the documented feature.", "highlights": [{"text": "Adds the feature.", "evidence": ["ls/core/example.py"]}], "compatibility": ["No compatibility changes are required."], "update": ["Use the normal install and update procedure."], "verification": ["Verify the archive with its `.sha256` checksum and `.cdx.json` SBOM sidecars."]}})
        return json.dumps({"accepted": True, "findings": [], "evidence": ["ls/core/example.py"]})


def test_prepare_candidate_is_read_only_hash_bound_and_reviews_every_document(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    client = _Client()
    proposal = prepare_candidate(tmp_path, plan, client=client)
    assert proposal["record"]["version"] == "5.0.0"
    assert {item["path"] for item in proposal["coverage"]} == {"README.md", "ls/docs/RELEASE.md"}
    assert proposal["binding"]["document_sha256"]["README.md"] == hashlib.sha256((tmp_path / "README.md").read_bytes()).hexdigest()
    assert proposal["binding"]["source_material_sha256"]["ls/core/example.py"]
    assert any('"role":"independent release documentation reviewer"' in call for call in client.calls)
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# LocalSetup\n\nCurrent release notes.\n"


def test_prepare_candidate_rejects_missing_source_material(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan["source_material"] = []
    with pytest.raises(ValueError, match="missing changed paths"):
        prepare_candidate(tmp_path, plan, client=_Client())


def test_small_edit_cannot_expand_review_to_all_sources_via_commit_evidence(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    class CommitOnlyEditor(_Client):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            payload = json.loads(prompt)
            if payload["role"] == "release documentation auditor and editor":
                document = payload["document_chunks"][0]
                return json.dumps({"coverage": [{"path": document["path"], "chunk": document["chunk"],
                    "disposition": "update_recommended", "evidence": [plan["source_commit"]]}],
                    "edits": [{"path": document["path"], "content": "# Updated guidance\n", "evidence": [plan["source_commit"]]}]})
            return super().complete(prompt, **kwargs)

    client = CommitOnlyEditor()
    with pytest.raises(ValueError, match="at least one source path"):
        prepare_candidate(tmp_path, plan, client=client)
    assert not any('"role":"independent release documentation reviewer"' in call for call in client.calls)


def test_unchanged_operational_references_are_bound_without_becoming_changed_paths(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    guidance = "UNCHANGED OPERATIONAL REFERENCE\nUse the documented updater and verify `.sha256` and `.cdx.json` sidecars."
    _write(tmp_path, "ls/docs/QUICKSTART.md", guidance)
    plan["reference_material"] = [{"path": "ls/docs/QUICKSTART.md", "content": guidance}]
    material = normalize_source_material(tmp_path, plan)
    assert {item["path"] for item in material} == {"ls/core/example.py", "ls/docs/QUICKSTART.md"}
    assert plan["changed_paths"] == ["ls/core/example.py"]

    class ReferenceClient(_Client):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            payload = json.loads(prompt)
            if payload["role"] == "release record evidence mapper":
                claim = payload["record_claim"]["claim"]
                path = "ls/core/example.py" if claim == "summary" or claim.startswith("highlight:") else "ls/docs/QUICKSTART.md"
                return json.dumps({"mappings": [{"claim": claim, "source": [{"path": path, "chunk": 0}]}]})
            return super().complete(prompt, **kwargs)

    candidate = prepare_candidate(tmp_path, plan, client=ReferenceClient())
    evidence = {item["claim"]: item["source"] for item in candidate["review"]["claim_evidence"]}
    assert evidence["update:0"] == [{"path": "ls/docs/QUICKSTART.md", "chunk": 0}]
    assert evidence["verification:0"] == evidence["update:0"]

    class StaleHighlightClient(ReferenceClient):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            payload = json.loads(prompt)
            if payload["role"] == "release record evidence mapper" and payload["record_claim"]["claim"].startswith("highlight:"):
                return json.dumps({"mappings": [{"claim": payload["record_claim"]["claim"], "source": [{"path": "ls/docs/QUICKSTART.md", "chunk": 0}]}]})
            return super().complete(prompt, **kwargs)

    with pytest.raises(ValueError, match="unchanged reference"):
        prepare_candidate(tmp_path, plan, client=StaleHighlightClient())


def test_preflight_accounts_for_mandatory_summary_and_mapping_batches() -> None:
    from ls.core.release_docs.agent import CompletionBudget, minimum_completion_calls
    import time
    required = minimum_completion_calls(220, 1)
    assert required == 319
    with pytest.raises(ValueError, match="reduce semantic scope or split"):
        CompletionBudget(240, time.monotonic() + 1800).preflight(required)


def test_validate_proposal_rejects_generated_target_and_stale_document(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    proposal = prepare_candidate(tmp_path, plan, client=_Client())
    proposal["edits"] = [{"path": "ls/docs/_generated/nope.md", "before_sha256": "x", "content": "# nope"}]
    with pytest.raises(ValueError, match="invalid shape|unauthorized"):
        validate_proposal(tmp_path, plan, proposal)
    proposal = prepare_candidate(tmp_path, plan, client=_Client())
    _write(tmp_path, "README.md", "changed after preparation\n")
    with pytest.raises(ValueError, match="binding changed"):
        validate_proposal(tmp_path, plan, proposal)


def test_validate_proposal_rejects_replayed_content_tampering(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    proposal = prepare_candidate(tmp_path, plan, client=_Client())
    original = (tmp_path / "README.md").read_text(encoding="utf-8")
    proposal["edits"] = [{"path": "README.md", "before_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(), "content": "# LocalSetup 5.0.0\n"}]
    with pytest.raises(ValueError, match="replay content changed"):
        validate_proposal(tmp_path, plan, proposal)


def test_prepare_candidate_chunks_source_larger_than_one_prompt(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan["source_material"][0]["content"] = "x" * 60_000
    proposal = prepare_candidate(tmp_path, plan, client=_Client())
    assert proposal["record"]["source_commit"] == "a" * 40


def test_prepare_candidate_can_complete_large_readme_after_chunk_audit(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _write(tmp_path, "README.md", "# LocalSetup\n" + ("details\n" * 2_000))
    class EditingClient(_Client):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            payload = json.loads(prompt)
            if payload["role"] == "release documentation auditor and editor":
                item = payload["document_chunks"][0]
                return json.dumps({"coverage": [{"path": item["path"], "chunk": item["chunk"], "disposition": "update_recommended" if item["path"] == "README.md" else "no_change", "evidence": ["ls/core/example.py"]}], "edits": []})
            if payload["role"] == "release documentation targeted editor":
                document = payload["document"]
                return json.dumps({"coverage": [], "edits": [{"path": document["path"], "content": document["content"] + "\nRelease 5.0.0\n", "evidence": ["ls/core/example.py"]}]})
            return super().complete(prompt, **kwargs)
    proposal = prepare_candidate(tmp_path, plan, client=EditingClient())
    assert proposal["edits"][0]["path"] == "README.md"


def test_record_matches_engine_schema_and_evidence_cannot_use_line_suffix(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    value = {
        "schema_version": 1, "version": plan["target_version"], "source_commit": plan["source_commit"],
        "baseline_tag": plan["baseline_tag"], "summary": "A valid source-backed record.",
        "highlights": [{"text": "Describes the changed source.", "evidence": ["ls/core/example.py:12"]}],
        "compatibility": ["No compatibility changes are required."], "update": ["Use the normal update procedure."],
        "verification": ["Run the documented verification."],
    }
    with pytest.raises(ValueError, match="record is invalid|evidence"):
        validate_record(tmp_path, plan, value, source_paths={"ls/core/example.py"})


def test_semantic_scope_requires_static_receipt_and_covers_static_documents(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan["semantic_documents"] = ["README.md"]
    with pytest.raises(ValueError, match="static audit"):
        prepare_candidate(tmp_path, plan, client=_Client())
    plan["static_audit"] = {"ok": True, "documents": plan["documents"], "findings": []}
    proposal = prepare_candidate(tmp_path, plan, client=_Client())
    static = [item for item in proposal["coverage"] if item["path"] == "ls/docs/RELEASE.md"]
    assert static and static[0]["disposition"] == "no_change"


def test_edit_rejects_redacted_or_removed_existing_url(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _write(tmp_path, "README.md", "# LocalSetup\n\nhttps://example.test/release\n")

    class RedactingClient(_Client):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            payload = json.loads(prompt)
            if payload["role"] == "release documentation auditor and editor":
                item = payload["document_chunks"][0]
                return json.dumps({"coverage": [{"path": item["path"], "chunk": item["chunk"], "disposition": "update_recommended", "evidence": ["ls/core/example.py"]}], "edits": [{"path": item["path"], "content": "# LocalSetup\n\n[REDACTED]\n", "evidence": ["ls/core/example.py"]}]})
            return super().complete(prompt, **kwargs)

    with pytest.raises(ValueError, match="preserve existing URLs"):
        prepare_candidate(tmp_path, plan, client=RedactingClient())


def test_public_url_escrow_survives_shared_redaction_without_restoring_private_url(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    public = "https://docs.example.test/releases"
    private = "http://10.0.0.8/internal"
    _write(tmp_path, "README.md", f"# LocalSetup\n\n{public}\n\n## Next heading\n")
    plan["source_material"][0]["content"] += f"# See {private}\n"

    class EscrowClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.redacted_prompts: list[str] = []

        def complete(self, prompt: str, **kwargs: Any) -> str:
            redacted = redact_text(prompt)
            self.redacted_prompts.append(redacted)
            payload = json.loads(redacted)
            if payload["role"] == "release documentation auditor and editor":
                item = payload["document_chunks"][0]
                if item["path"] == "README.md":
                    assert "## Next heading" in item["text"]
                    return json.dumps({"coverage": [{"path": item["path"], "chunk": item["chunk"], "disposition": "update_recommended", "evidence": ["ls/core/example.py"]}], "edits": [{"path": item["path"], "content": item["text"] + "\nRelease update.\n", "evidence": ["ls/core/example.py"]}]})
            return super().complete(redacted, **kwargs)

    client = EscrowClient()
    proposal = prepare_candidate(tmp_path, plan, client=client)
    assert public in proposal["edits"][0]["content"]
    assert private not in json.dumps(proposal)
    assert all(private not in prompt for prompt in client.redacted_prompts)


def test_url_escrow_leaves_source_regex_that_resembles_invalid_ipv6_unchanged() -> None:
    source = 'pattern = re.compile(r"https://[^\\s]+")'
    result, escrow = _escrow_urls(source)
    assert result == source
    assert escrow == {}


def test_completion_budget_preflight_stops_before_provider_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QC_LLM_MAX_CALLS", "1")
    client = _Client()
    with pytest.raises(ValueError, match="reduce semantic scope or split"):
        prepare_candidate(tmp_path, _plan(tmp_path), client=client)
    assert not client.calls


def test_completion_budget_enforces_actual_exhaustion() -> None:
    class EchoClient:
        def complete(self, _prompt: str, **_kwargs: Any) -> str:
            return "{}"

    budget = CompletionBudget(max_calls=1, deadline=time.monotonic() + 60)
    assert _call(EchoClient(), budget, "test", {}, {}, "test") == {}
    with pytest.raises(ValueError, match="budget exhausted"):
        _call(EchoClient(), budget, "test", {}, {}, "test")


def test_large_document_editor_uses_bounded_facts_not_full_source_chunks(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _write(tmp_path, "README.md", "# LocalSetup\n" + ("details\n" * 4_800))
    plan["source_material"][0]["content"] = "x" * 60_000

    class LargeEditingClient(_Client):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            payload = json.loads(prompt)
            if payload["role"] == "release documentation auditor and editor":
                item = payload["document_chunks"][0]
                return json.dumps({"coverage": [{"path": item["path"], "chunk": item["chunk"], "disposition": "update_recommended" if item["path"] == "README.md" else "no_change", "evidence": ["ls/core/example.py"]}], "edits": []})
            if payload["role"] == "release documentation targeted editor":
                assert "relevant_source_chunks" not in payload
                assert len(payload["relevant_source_facts"]) <= 3
                document = payload["document"]
                return json.dumps({"coverage": [], "edits": [{"path": document["path"], "content": document["content"] + "\nRelease 5.0.0\n", "evidence": ["ls/core/example.py"]}]})
            return super().complete(prompt, **kwargs)

    proposal = prepare_candidate(tmp_path, plan, client=LargeEditingClient())
    assert proposal["edits"][0]["path"] == "README.md"
