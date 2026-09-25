"""Tool-free, fail-closed model boundary for release-documentation candidates."""
from __future__ import annotations

import json
import ipaddress
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qsl, urlsplit

from tools.qc_patrol.chunking import chunk_text
from tools.qc_patrol.config import load_config
from tools.qc_patrol.llm_client import LLMClient

from .proposals import (
    normalize_source_material,
    plan_documents,
    require_plan,
    sha256_text,
    validate_audit_response,
    validate_edits,
    validate_proposal,
    validate_record,
)
from .schemas import AUDIT_SCHEMA, CHUNK_BYTES, CLAIM_MAP_SCHEMA, FACT_SCHEMA, SUMMARY_SCHEMA, MAP_SCHEMA, MAX_FACTS_PER_CALL, MAX_MAPPING_FACTS, MAX_RESPONSE_BYTES, RECORD_SCHEMA, REVIEW_SCHEMA


PROMPT_BYTES = 48_000
MAX_EDITOR_FACTS = 3
_URL = re.compile(r"https?://[^\s'\")>\\]+")
_ESCROW_TOKEN = re.compile(r"__RELEASE_DOCS_PUBLIC_LINK_[0-9]{6}__")
_SENSITIVE_QUERY = re.compile(r"(?:api[_-]?key|token|secret|password|credential|auth)", re.IGNORECASE)


class CompletionClient(Protocol):
    def complete(self, prompt: str, response_schema: dict[str, Any] | None = None,
                 schema_name: str = "release_docs") -> str: ...


def minimum_completion_calls(source_chunks: int, semantic_chunks: int, change_chunks: int | None = None,
                             semantic_mapping_calls: int | None = None) -> int:
    """Count mandatory calls assuming the minimum one fact per source chunk."""
    summaries = 0
    changes = source_chunks if change_chunks is None else change_chunks
    for minimum_facts in (changes, source_chunks - changes):
        while minimum_facts > MAX_FACTS_PER_CALL:
            minimum_facts = (minimum_facts + MAX_FACTS_PER_CALL - 1) // MAX_FACTS_PER_CALL
            summaries += minimum_facts
    mapping_batches = (source_chunks + MAX_MAPPING_FACTS - 1) // MAX_MAPPING_FACTS
    change_batches = (changes + MAX_MAPPING_FACTS - 1) // MAX_MAPPING_FACTS
    mappings = semantic_chunks * mapping_batches if semantic_mapping_calls is None else semantic_mapping_calls
    return source_chunks + summaries + mappings + semantic_chunks + 1 + 2 * (change_batches + 1) + 3 * (mapping_batches + 1)


def _mapped_source_paths(plan: Mapping[str, Any], document: str) -> set[str] | None:
    if document in {"README.md", "ls/README.md", "ls/docs/README.md"}:
        return None
    paths = {path for path, docs in plan.get("affected_documents", {}).items() if document in docs}
    return paths or None


@dataclass
class CompletionBudget:
    """Bound the whole release-docs preparation session; calls also have a per-request timeout."""
    max_calls: int
    deadline: float
    calls: int = 0

    @classmethod
    def from_environment(cls) -> "CompletionBudget":
        max_calls = int(os.environ.get("QC_LLM_MAX_CALLS", "800"))
        seconds = int(os.environ.get("QC_LLM_TOTAL_DEADLINE_SECONDS", "9000"))
        if max_calls < 1 or seconds < 1:
            raise ValueError("QC_LLM_MAX_CALLS and QC_LLM_TOTAL_DEADLINE_SECONDS must be positive")
        return cls(max_calls=max_calls, deadline=time.monotonic() + seconds)

    def preflight(self, lower_bound: int) -> None:
        if lower_bound > self.max_calls:
            raise ValueError("release documentation completion budget is too small; reduce semantic scope or split the release")
        if time.monotonic() >= self.deadline:
            raise ValueError("release documentation completion deadline elapsed; reduce semantic scope or split the release")

    def reserve(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.calls >= self.max_calls or remaining < 1:
            raise ValueError("release documentation completion budget exhausted; reduce semantic scope or split the release")
        self.calls += 1
        return remaining


def _decode(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError(f"release documentation {label} response is too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"release documentation {label} returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"release documentation {label} response must be an object")
    return dict(value)


def _prompt(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PROMPT_BYTES:
        raise ValueError("release documentation prompt exceeds its bounded context")
    return encoded


def _public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        return False
    host = hostname.lower()
    if "." not in host or host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan", ".home")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return False
    return not any(_SENSITIVE_QUERY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True))


def _escrow_urls(prompt: str) -> tuple[str, dict[str, str]]:
    escrow: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        if not _public_url(value):
            return value
        token = f"__RELEASE_DOCS_PUBLIC_LINK_{len(escrow):06d}__"
        escrow[token] = value
        return token

    return _URL.sub(replace, prompt), escrow


def _restore_urls(response: Any, escrow: Mapping[str, str]) -> Any:
    if not isinstance(response, str):
        return response
    observed = set(_ESCROW_TOKEN.findall(response))
    if not observed <= set(escrow):
        raise ValueError("release documentation completion returned an unknown URL escrow token")
    for token, value in escrow.items():
        response = response.replace(token, value)
    return response


def _call(client: CompletionClient, budget: CompletionBudget, role: str, schema: dict[str, Any], body: dict[str, Any], name: str) -> dict[str, Any]:
    prompt, escrow = _escrow_urls(_prompt({"role": role, **body}))
    remaining = budget.reserve()
    call_client = client
    if isinstance(client, LLMClient):
        timeout = min(client.config.timeout_seconds, max(1, int(remaining)))
        call_client = LLMClient(replace(client.config, timeout_seconds=timeout), session_id=client.session_id)
    response = call_client.complete(prompt, response_schema=schema, schema_name=name)
    response = _restore_urls(response, escrow)
    return _decode(response, role)


def _chunks(items: list[dict[str, str]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for item in items:
        for part in chunk_text(item["path"], item["content"], CHUNK_BYTES):
            chunks.append({
                "path": item["path"], "chunk": int(part["index"]), "text": part["text"],
                "sha256": item.get("sha256", item.get("before_sha256", "")),
                "kind": item.get("kind", "change"),
            })
    return chunks


def _semantic_paths(plan: Mapping[str, Any], documents: list[dict[str, Any]]) -> set[str]:
    known = {item["path"] for item in documents}
    requested = plan.get("semantic_documents")
    if requested is None:
        # Small ad hoc callers have no verified static-audit receipt, so every
        # supplied document remains semantic.  The production planner supplies
        # an explicit, targeted list.
        return known
    if not isinstance(requested, list) or not all(isinstance(path, str) for path in requested):
        raise ValueError("release documentation semantic_documents must be a list of paths")
    selected = set(requested)
    if not selected <= known:
        raise ValueError("release documentation semantic scope contains unknown documents")
    static = plan.get("static_audit", plan.get("static_coverage"))
    if not isinstance(static, Mapping) or static.get("ok") is not True:
        raise ValueError("release documentation semantic scoping requires a passing static audit receipt")
    return selected


def _source_facts(client: CompletionClient, budget: CompletionBudget, plan: Mapping[str, Any], source_chunks: list[dict[str, Any]],
                  source_paths: set[str]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for chunk in source_chunks:
        # Provenance identifiers are supplied facts, not model-authored fields.
        schema = json.loads(json.dumps(FACT_SCHEMA))
        properties = schema["properties"]["facts"]["items"]["properties"]
        properties["path"]["enum"] = [chunk["path"]]
        properties["chunk"]["enum"] = [chunk["chunk"]]
        properties["evidence"]["items"]["enum"] = [chunk["path"], plan["source_commit"]]
        response = _call(
            client, budget, "release source analyst", schema,
            {"instruction": "Extract at least one concrete fact from this exact source chunk, stating no public impact when appropriate. Copy source_chunk.path and source_chunk.chunk exactly into every fact; do not use file paths mentioned inside its text. Unchanged references supply existing operational guidance, not new features. JSON only; every evidence item must name this source path or the source commit.",
             "release": {"source_commit": plan["source_commit"]}, "source_chunk": chunk},
            "release_docs_source_facts",
        )
        rows = response.get("facts")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FACTS_PER_CALL:
            raise ValueError("release documentation source analysis is incomplete")
        for fact in rows:
            if not isinstance(fact, Mapping) or set(fact) != {"path", "chunk", "text", "evidence"}:
                raise ValueError("release documentation source analysis returned an invalid fact")
            if fact.get("path") != chunk["path"] or fact.get("chunk") != chunk["chunk"]:
                raise ValueError("release documentation source analysis misbound a fact")
            if not isinstance(fact.get("text"), str) or not fact["text"].strip() or len(fact["text"]) > 800:
                raise ValueError("release documentation source analysis returned an invalid fact text")
            evidence = fact.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item in {chunk["path"], plan["source_commit"]}
                for item in evidence
            ):
                raise ValueError("release documentation source fact lacks chunk-backed evidence")
            facts.append({**fact, "kind": chunk["kind"]})
    if not facts:
        raise ValueError("release documentation source analysis produced no facts")
    return facts


def _fact_context(client: CompletionClient, budget: CompletionBudget, plan: Mapping[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep record prompts bounded while retaining an explicit source-chunk ledger."""
    if len(facts) <= MAX_FACTS_PER_CALL:
        return facts
    summaries: list[dict[str, Any]] = []
    for index in range(0, len(facts), MAX_FACTS_PER_CALL):
        batch = facts[index:index + MAX_FACTS_PER_CALL]
        response = _call(
            client, budget, "release source fact summarizer", SUMMARY_SCHEMA,
            {"instruction": "Return exactly one summary fact for this batch without adding claims. JSON only; preserve the cited source path and chunk.", "facts": batch},
            "release_docs_fact_summary",
        )
        rows = response.get("facts")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("release documentation fact summary is incomplete")
        row = rows[0]
        known = {(item["path"], item["chunk"]) for item in batch}
        if (not isinstance(row, Mapping) or set(row) != {"path", "chunk", "text", "evidence"}
                or (row.get("path"), row.get("chunk")) not in known
                or not isinstance(row.get("text"), str) or not row["text"].strip() or len(row["text"]) > 800
                or not isinstance(row.get("evidence"), list)
                or not all(item in {row["path"], plan["source_commit"]} for item in row["evidence"])):
            raise ValueError("release documentation fact summary is invalid")
        summaries.append({**row, "kind": batch[0]["kind"]})
    if len(summaries) >= len(facts):
        raise ValueError("release documentation fact summary did not reduce context")
    return _fact_context(client, budget, plan, summaries)


def _review_or_reject(client: CompletionClient, budget: CompletionBudget, body: dict[str, Any], name: str) -> dict[str, Any]:
    verdict = _call(client, budget, "independent release documentation reviewer", REVIEW_SCHEMA, body, name)
    if verdict.get("accepted") is not True or verdict.get("findings"):
        raise ValueError("release documentation independent review rejected candidate")
    return verdict


def _record_claims(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims = [{"claim": "summary", "text": record["summary"], "evidence": []}]
    claims.extend({"claim": f"highlight:{index}", "text": item["text"], "evidence": item["evidence"]}
                  for index, item in enumerate(record["highlights"]))
    for section in ("compatibility", "update", "verification"):
        claims.extend({"claim": f"{section}:{index}", "text": text, "evidence": []}
                      for index, text in enumerate(record[section]))
    return claims


def _claim_sources(client: CompletionClient, budget: CompletionBudget, claims: list[dict[str, Any]], facts: list[dict[str, Any]],
                   source_by_key: Mapping[tuple[str, int], dict[str, Any]]) -> dict[str, set[tuple[str, int]]]:
    """Map every outward-facing record claim to a small, verified source receipt."""
    mapped: dict[str, set[tuple[str, int]]] = {}
    for claim in claims:
        selected: set[tuple[str, int]] = set()
        new_claim = claim["claim"] == "summary" or claim["claim"].startswith("highlight:")
        eligible = [fact for fact in facts if not new_claim or fact["kind"] == "change"]
        for index in range(0, len(eligible), MAX_MAPPING_FACTS):
            response = _call(
                client, budget, "release record evidence mapper", CLAIM_MAP_SCHEMA,
                {"instruction": "Map this release-record claim to actual source chunks. JSON only; choose only supporting chunks.",
                 "record_claim": claim, "source_facts": eligible[index:index + MAX_MAPPING_FACTS]},
                "release_docs_record_evidence_mapping",
            )
            rows = response.get("mappings")
            if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("claim") != claim["claim"]:
                raise ValueError("release documentation record evidence mapping is incomplete")
            for row in rows[0].get("source", []):
                key = (row.get("path"), row.get("chunk")) if isinstance(row, Mapping) else (None, None)
                if key not in source_by_key:
                    raise ValueError("release documentation record evidence mapping selected unknown source")
                if new_claim and source_by_key[key]["kind"] == "reference":
                    raise ValueError("unchanged reference cannot support a new release summary or highlight")
                selected.add(key)
        if not selected:
            raise ValueError("release documentation record claim lacks source evidence")
        mapped[claim["claim"]] = selected
    return mapped


def prepare_candidate(root: Path, plan: Mapping[str, Any], *, client: CompletionClient | None = None) -> dict[str, Any]:
    """Prepare a hash-bound, independently reviewed candidate without writing files."""
    root = Path(root).resolve(strict=True)
    require_plan(plan)
    documents = plan_documents(root, plan)
    source = normalize_source_material(root, plan)
    source_paths = {item["path"] for item in source}
    completion = client or LLMClient(load_config(root).llm)
    source_chunks = _chunks(source)
    document_chunks = _chunks(documents)
    source_by_key = {(item["path"], item["chunk"]): item for item in source_chunks}
    semantic_paths = _semantic_paths(plan, documents)
    budget = CompletionBudget.from_environment()
    semantic_chunks = sum(1 for item in document_chunks if item["path"] in semantic_paths)
    mapping_calls = 0
    for document in (item for item in document_chunks if item["path"] in semantic_paths):
        mapped = _mapped_source_paths(plan, document["path"])
        selected_count = sum(mapped is None or item["path"] in mapped for item in source_chunks)
        mapping_calls += (selected_count + MAX_MAPPING_FACTS - 1) // MAX_MAPPING_FACTS
    budget.preflight(minimum_completion_calls(len(source_chunks), semantic_chunks,
                                             sum(item["kind"] == "change" for item in source_chunks), mapping_calls))

    facts = _source_facts(completion, budget, plan, source_chunks, source_paths)
    compact_facts = [fact for kind in ("change", "reference") for fact in
                     _fact_context(completion, budget, plan, [item for item in facts if item["kind"] == kind])]
    coverage: list[dict[str, Any]] = [
        {"path": chunk["path"], "chunk": chunk["chunk"], "disposition": "no_change", "evidence": [plan["source_commit"]]}
        for chunk in document_chunks if chunk["path"] not in semantic_paths
    ]
    relevant: dict[tuple[str, int], set[tuple[str, int]]] = {}
    raw_edits: list[dict[str, Any]] = []
    for document in (item for item in document_chunks if item["path"] in semantic_paths):
        selected: set[tuple[str, int]] = set()
        mapped = _mapped_source_paths(plan, document["path"])
        eligible_facts = [fact for fact in facts if mapped is None or fact["path"] in mapped]
        for index in range(0, len(eligible_facts), MAX_MAPPING_FACTS):
            mapping = _call(
                completion, budget, "release documentation evidence mapper", MAP_SCHEMA,
                {"instruction": "Map this one document chunk to relevant source chunks. JSON only.",
                 "document_chunks": [document], "source_facts": eligible_facts[index:index + MAX_MAPPING_FACTS]},
                "release_docs_evidence_mapping",
            )
            rows = mapping.get("mappings")
            if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("path") != document["path"] or rows[0].get("chunk") != document["chunk"]:
                raise ValueError("release documentation evidence mapping is incomplete")
            for row in rows[0].get("source", []):
                key = (row.get("path"), row.get("chunk")) if isinstance(row, Mapping) else (None, None)
                if key not in source_by_key:
                    raise ValueError("release documentation mapping selected unknown source")
                selected.add(key)
        relevant[(document["path"], document["chunk"])] = selected
        audit = _call(
            completion, budget, "release documentation auditor and editor", AUDIT_SCHEMA,
            {"instruction": "Audit this document chunk using the actual relevant source chunks. JSON only. Every edit must cite at least one source path, not only a commit SHA. Do not edit generated, policy, skill, workflow, template, private, or executable files.",
             "release": {key: plan[key] for key in ("source_commit", "target_version", "baseline_tag", "changed_paths")},
             "document_chunks": [document], "source_chunks": [source_by_key[key] for key in sorted(selected)]},
            "release_docs_audit_batch",
        )
        checked = validate_audit_response(root, [document], audit, source_paths=source_paths, source_commit=plan["source_commit"])
        coverage.extend(checked["coverage"])
        raw_edits.extend(checked["edits"])

    chunk_counts = {item["path"]: sum(1 for chunk in document_chunks if chunk["path"] == item["path"]) for item in documents}
    raw_edits = [item for item in raw_edits if chunk_counts.get(item.get("path"), 0) == 1]
    updates = {item["path"] for item in coverage if item["disposition"] == "update_recommended"}
    by_document = {item["path"]: item for item in documents}
    for path in sorted(updates):
        if chunk_counts[path] <= 1:
            continue
        keys = set().union(*(relevant[(path, index)] for index in range(chunk_counts[path])))
        relevant_facts = [fact for fact in facts if (fact["path"], fact["chunk"]) in keys][:MAX_EDITOR_FACTS]
        if len(by_document[path]["content"].encode("utf-8")) > 40_000:
            raise ValueError("release documentation full-document replacement exceeds its 40 KiB bounded editor limit")
        replacement = _call(
            completion, budget, "release documentation targeted editor", AUDIT_SCHEMA,
            {"instruction": "Return one complete replacement for this affected Markdown document. Cite at least one supporting source path, not only a commit SHA. JSON only; retain unrelated content exactly.",
             "release": {key: plan[key] for key in ("source_commit", "target_version", "baseline_tag")},
             "document": by_document[path], "relevant_source_facts": relevant_facts},
            "release_docs_targeted_edit",
        )
        if replacement.get("coverage") not in ([], None):
            raise ValueError("release documentation targeted edit returned unexpected coverage")
        raw_edits.extend(replacement.get("edits", []))
    raw_edit_evidence = {
        item["path"]: item["evidence"]
        for item in raw_edits
        if isinstance(item, Mapping) and isinstance(item.get("path"), str) and isinstance(item.get("evidence"), list)
    }
    edits = validate_edits(root, documents, raw_edits, source_paths=source_paths, source_commit=plan["source_commit"])

    record_response = _call(
        completion, budget, "release documentation release-note editor", RECORD_SCHEMA,
        {"instruction": "Build the exact release record object required by the supplied schema from the bounded fact hierarchy and semantic audit coverage. JSON only. Every highlight needs source-backed evidence. Include non-empty compatibility, update, and verification lists. Update guidance must state how to install or update; verification guidance must explicitly mention both `.sha256` and `.cdx.json` sidecars and verification. Framework archive filenames must use target_version, not an older reference version.",
         "release": {key: plan[key] for key in ("source_commit", "target_version", "baseline_tag", "changed_paths")},
         "source_fact_summary": compact_facts,
         "source_chunk_coverage": [{"path": item["path"], "chunk": item["chunk"], "sha256": item["sha256"]} for item in source_chunks],
         "audit_coverage": [item for item in coverage if item["path"] in semantic_paths]},
        "release_docs_record",
    )
    if set(record_response) != {"record"}:
        raise ValueError("release documentation record response has invalid shape")
    record = validate_record(root, plan, record_response["record"], source_paths=source_paths)

    claims = _record_claims(record)
    claim_sources = _claim_sources(completion, budget, claims, facts, source_by_key)
    record_reviews: list[dict[str, Any]] = []
    for claim in claims:
        for key in sorted(claim_sources[claim["claim"]]):
            verdict = _review_or_reject(
                completion, budget,
                {"instruction": "Independently review this user-facing release-record claim against this source chunk. For unchanged operational references, adapting archive/version placeholders to the target version is expected. JSON only.",
                 "release": {"target_version": plan["target_version"], "source_commit": plan["source_commit"]},
                 "record_claim": claim, "source_chunk": source_by_key[key]},
                "release_docs_independent_record_review",
            )
            record_reviews.append({"claim": claim["claim"], "path": key[0], "chunk": key[1], **verdict})
    edit_reviews: list[dict[str, Any]] = []
    edit_sources: dict[str, set[tuple[str, int]]] = {}
    for edit in edits:
        evidence = raw_edit_evidence.get(edit["path"], [])
        cited_paths = {item for item in evidence if item in source_paths}
        keys = {key for key in source_by_key if key[0] in cited_paths}
        if not keys:
            raise ValueError("release documentation edit lacks source evidence")
        edit_sources[edit["path"]] = keys
        for edit_chunk in chunk_text(edit["path"], edit["content"], CHUNK_BYTES):
            candidate_chunk = {"path": edit["path"], "chunk": int(edit_chunk["index"]), "text": edit_chunk["text"],
                               "sha256": sha256_text(edit_chunk["text"])}
            for key in sorted(keys):
                verdict = _review_or_reject(
                    completion, budget,
                    {"instruction": "Independently review this proposed edit chunk against this actual source chunk. JSON only.",
                     "release": {"target_version": plan["target_version"], "source_commit": plan["source_commit"]},
                     "candidate_edit_chunk": candidate_chunk, "source_chunk": source_by_key[key]},
                    "release_docs_independent_edit_review",
                )
                edit_reviews.append({"path": edit["path"], "edit_chunk": candidate_chunk["chunk"],
                                     "source_path": key[0], "source_chunk": key[1], **verdict})

    review = {"accepted": True, "findings": [], "evidence": [plan["source_commit"]],
              "claim_evidence": [{"claim": claim, "source": [{"path": path, "chunk": chunk} for path, chunk in sorted(keys)]}
                                 for claim, keys in sorted(claim_sources.items())],
              "edit_evidence": [{"path": path, "source": [{"path": source_path, "chunk": source_chunk} for source_path, source_chunk in sorted(keys)]}
                                for path, keys in sorted(edit_sources.items())],
              "edit_reviews": edit_reviews, "record_reviews": record_reviews}
    binding: dict[str, Any] = {
        "source_commit": plan["source_commit"], "target_version": plan["target_version"], "baseline_tag": plan["baseline_tag"],
        "document_sha256": {item["path"]: item["before_sha256"] for item in documents},
        "source_material_sha256": {item["path"]: item["sha256"] for item in source},
        "source_material_kind": {item["path"]: item["kind"] for item in source},
    }
    protected = {"record": record, "edits": edits, "coverage": coverage, "review": review}
    binding["checksums"] = {key: sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)) for key, value in protected.items()}
    return validate_proposal(root, plan, {**protected, "binding": binding})
