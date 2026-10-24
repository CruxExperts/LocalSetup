from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import itertools
import json
from pathlib import Path

import pytest

from ls.core.openpgp import artifacts
from ls.core.openpgp.artifact_records import canonical_bytes
from ls.core.openpgp.artifacts import ArtifactError, ArtifactErrorCode, ArtifactPolicy, VerifiedContainerMetadata
from ls.core.openpgp.container_format import ContainerHeader
from ls.core.openpgp.container_reconcile import (
    CachedMetadata, CatalogObservation, ContainerCandidate, ReconcileError,
    ReconcileErrorCode, RejectedObservation, ScanCoverage, apply_reconcile_transition,
    build_transition, inspect_candidate,
)
from ls.core.openpgp.container_sources import BytesRangeSource


SCOPE = ("agentq:reconcile-fixture",)
ARTIFACT_ID = "a" * 32
VERSION_ID = "b" * 32
VOLUME_ONE = "c" * 32
VOLUME_TWO = "d" * 32
SIGNER = "E" * 40
NOW = 1_800_000_000


def _policy(boundary: Path) -> ArtifactPolicy:
    return ArtifactPolicy(
        trust_store_id="1" * 64,
        trust_revision=4,
        governance_epoch=2,
        scope=SCOPE,
        origin_fingerprint=SIGNER,
        audience=(SIGNER,),
        purpose="reconcile.fixture",
        content_revision="fixture:1",
        created_at=NOW - 100,
        expires_at=NOW + 3600,
        boundary=boundary,
    )


def _metadata(volume_id: str, *, commitment: str = "f" * 64) -> VerifiedContainerMetadata:
    inventory = {
        "volumes": [
            {"volume_id": VOLUME_ONE, "key": f"lssc/v1/tenant/project/{'1'*32}/{ARTIFACT_ID}/{VERSION_ID}/{VOLUME_ONE}.lssc", "messages": []},
            {"volume_id": VOLUME_TWO, "key": f"lssc/v1/tenant/project/{'1'*32}/{ARTIFACT_ID}/{VERSION_ID}/{VOLUME_TWO}.lssc", "messages": []},
        ],
    }
    layer = b"authenticated encrypted metadata bytes"
    return VerifiedContainerMetadata(
        header=ContainerHeader(
            format="localsetup.secure-container",
            version=1,
            recipient_hints=(SIGNER,),
            metadata_length=len(layer),
            encrypted_metadata=layer,
            messages_offset=123,
            canonical_public_header=b'{"format":"localsetup.secure-container","version":1}',
        ),
        logical={
            "artifact_id": ARTIFACT_ID,
            "version_id": VERSION_ID,
            "parent_versions": [],
            "files": [{"file_id": "e" * 32, "length": 1, "sha256": "3" * 64,
                       "provenance": {}}],
            "created_at": NOW - 50,
            "expires_at": NOW + 500,
        },
        inventory=inventory,
        volume_record=next(item for item in inventory["volumes"]
                           if item["volume_id"] == volume_id),
        key=f"lssc/v1/tenant/project/{'1'*32}/{ARTIFACT_ID}/{VERSION_ID}/{volume_id}.lssc",
        volume_id=volume_id,
        origin_volume_id=volume_id,
        logical_commitment_sha256=commitment,
        admission_policy_sha256=artifacts.container_admission_policy_sha256(_policy(Path("/fixture"))),
        metadata_layers=((layer, "publisher", SIGNER),),
    )


def _observation(identity: str, source_id: str, locator: str, volume_id: str,
                 *, commitment: str = "f" * 64, observed_at: int = NOW) -> CatalogObservation:
    private = __import__("ls.core.openpgp.container_reconcile", fromlist=["_capture_observation"])
    return private._capture_observation(
        source_id=source_id,
        locator=locator,
        identity=identity,
        metadata=_metadata(volume_id, commitment=commitment),
        observed_at=observed_at,
    )


def _transition(policy: ArtifactPolicy, observations, coverage, *, scan_id: str,
                evaluation_time: int = NOW):
    return build_transition(
        scan_id=scan_id,
        evaluation_time=evaluation_time,
        source_config_sha256=hashlib.sha256(b"source configuration").hexdigest(),
        policy=policy,
        coverage=coverage,
        observations=observations,
    )


def test_reconciliation_is_order_independent_and_coalesces_duplicate_carriers(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    first = _observation("1" * 64, "nas", "nas:/opaque-one", VOLUME_ONE)
    duplicate = _observation("1" * 64, "drive", "drive:/opaque-two", VOLUME_ONE)
    second = _observation("2" * 64, "nas", "nas:/opaque-three", VOLUME_TWO)
    coverage = (ScanCoverage("drive", "complete"), ScanCoverage("nas", "complete"))
    transitions = [
        _transition(policy, order, coverage, scan_id="3" * 32)
        for order in itertools.permutations((first, duplicate, second))
    ]
    projections = [apply_reconcile_transition(
        {"containers": [], "versions": [], "sources": [], "reconciliation_history": []},
        item, max_entities=10_000,
    ) for item in transitions]

    assert all(canonical_bytes(value) == canonical_bytes(projections[0])
               for value in projections)
    catalog = projections[0]
    assert len(catalog["containers"]) == 2
    assert len(catalog["containers"][0]["observations"]) == 2
    assert {item["metadata_status"] for item in catalog["containers"]} == {"verified"}
    assert {item["payload_status"] for item in catalog["containers"]} == {"unverified"}
    assert {item["segment_status"] for item in catalog["containers"]} == {"complete"}
    assert len(catalog["versions"]) == 1


def test_duplicate_observation_and_rejection_order_is_deterministic(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    earlier = _observation("1" * 64, "nas", "opaque-one", VOLUME_ONE, observed_at=NOW - 1)
    latest = _observation("1" * 64, "nas", "opaque-one", VOLUME_ONE)
    rejected = [RejectedObservation("nas", "opaque-two", "UNAVAILABLE", timestamp)
                for timestamp in (NOW - 1, NOW)]
    records = [_transition(
        policy, order, (ScanCoverage("nas", "complete"),), scan_id="3" * 32,
    ).to_record() for order in itertools.permutations((earlier, latest, *rejected))]
    assert all(record == records[0] for record in records)
    assert records[0]["observations"][0]["observed_at"] == NOW
    conflicting = _observation("1" * 64, "nas", "opaque-one", VOLUME_ONE, commitment="4" * 64)
    for order in ((latest, conflicting), (conflicting, latest)):
        with pytest.raises(ReconcileError) as error:
            _transition(policy, order, (ScanCoverage("nas", "complete"),), scan_id="3" * 32)
        assert error.value.code is ReconcileErrorCode.INVALID_INPUT


def test_observation_cannot_be_relabelled_under_a_different_policy(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    observed = _observation("1" * 64, "nas", "opaque-one", VOLUME_ONE)
    changed = replace(policy, trust_revision=policy.trust_revision + 1)
    with pytest.raises(ReconcileError) as error:
        _transition(changed, (observed,), (ScanCoverage("nas", "complete"),), scan_id="3" * 32)
    assert error.value.code is ReconcileErrorCode.AUTHORIZATION_DENIED


def test_outage_and_complete_absence_remain_distinct_without_deleting_history(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path)
    initial = _transition(
        policy,
        (_observation("1" * 64, "nas", "nas:/opaque-one", VOLUME_ONE),),
        (ScanCoverage("nas", "complete"),), scan_id="4" * 32,
    )
    catalog = apply_reconcile_transition(
        {"containers": [], "versions": [], "sources": [], "reconciliation_history": []},
        initial, max_entities=10_000,
    )
    unavailable = _transition(
        policy, (), (ScanCoverage("nas", "unavailable"),), scan_id="5" * 32,
    )
    after_outage = apply_reconcile_transition(catalog, unavailable, max_entities=10_000)
    assert len(after_outage["containers"]) == 1
    assert after_outage["containers"][0]["availability"] == "unavailable"
    assert after_outage["containers"][0]["segment_status"] == "unknown"
    assert after_outage["containers"][0]["metadata_status"] == "verified"

    complete_absence = _transition(
        policy, (), (ScanCoverage("nas", "complete"),), scan_id="6" * 32,
    )
    after_absence = apply_reconcile_transition(
        after_outage, complete_absence, max_entities=10_000,
    )
    assert len(after_absence["containers"]) == 1
    assert after_absence["containers"][0]["availability"] == "not_observed"
    assert after_absence["containers"][0]["segment_status"] == "incomplete"
    assert after_absence["containers"][0]["payload_status"] == "unverified"


def test_conflicting_authenticated_version_claims_are_preserved_as_collision(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path)
    transition = _transition(
        policy,
        (
            _observation("1" * 64, "nas", "nas:/one", VOLUME_ONE,
                         commitment="f" * 64),
            _observation("2" * 64, "nas", "nas:/two", VOLUME_ONE,
                         commitment="9" * 64),
        ),
        (ScanCoverage("nas", "complete"),), scan_id="7" * 32,
    )
    catalog = apply_reconcile_transition(
        {"containers": [], "versions": [], "sources": [], "reconciliation_history": []},
        transition, max_entities=10_000,
    )

    assert len(catalog["containers"]) == 2
    assert len(catalog["versions"]) == 1
    assert catalog["versions"][0]["status"] == "collision"
    assert all(item["claim_conflict"] is True for item in catalog["containers"])


def test_reconcile_rejects_caller_supplied_unverified_dictionaries(tmp_path: Path) -> None:
    with pytest.raises(ReconcileError) as raised:
        _transition(
            _policy(tmp_path), ({"identity": "1" * 64, "metadata_status": "verified"},),
            (ScanCoverage("nas", "complete"),), scan_id="8" * 32,
        )
    assert raised.value.code is ReconcileErrorCode.INVALID_INPUT


def test_unchanged_cache_skips_redecryption_but_changed_header_reinspects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path)
    metadata = _metadata(VOLUME_ONE)
    private = __import__("ls.core.openpgp.container_reconcile", fromlist=["_metadata_identity"])
    identity = private._metadata_identity(metadata)
    cache = private._capture_cached_metadata(metadata, "a" * 64, identity)
    candidate = ContainerCandidate("nas", "nas:/opaque", BytesRangeSource(b"metadata"))
    monkeypatch.setattr(artifacts, "_revalidate_cached_container_metadata", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        artifacts, "inspect_container_metadata",
        lambda *args, **kwargs: pytest.fail("unchanged metadata was decrypted again"),
    )

    observation = inspect_candidate(
        candidate, policy=policy, trust_state_path="unused", gnupg_home="unused",
        passphrase_reference=None, secret_resolver=None,
        evaluation_time=NOW, cached=cache,
    )
    assert isinstance(observation, CatalogObservation)
    assert observation.cache_reused is True

    monkeypatch.setattr(artifacts, "_revalidate_cached_container_metadata", lambda *args, **kwargs: False)
    monkeypatch.setattr(artifacts, "inspect_container_metadata", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(
        "ls.core.openpgp.container_reconcile._record_metadata_receipt", lambda *args: None,
    )
    refreshed = inspect_candidate(
        candidate, policy=policy, trust_state_path="unused", gnupg_home="unused",
        passphrase_reference=None, secret_resolver=None,
        evaluation_time=NOW + 1, cached=cache,
    )
    assert isinstance(refreshed, CatalogObservation)
    assert refreshed.cache_reused is False
    assert refreshed.observed_at == NOW + 1


def test_failed_verification_is_recorded_separately_from_metadata_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path)
    candidate = ContainerCandidate("nas", "nas:/bad", BytesRangeSource(b"bad"))

    def reject(*_args, **_kwargs):
        raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT)

    monkeypatch.setattr(artifacts, "inspect_container_metadata", reject)
    result = inspect_candidate(
        candidate, policy=policy, trust_state_path="unused", gnupg_home="unused",
        passphrase_reference=None, secret_resolver=None, evaluation_time=NOW,
    )

    assert isinstance(result, RejectedObservation)
    transition = _transition(
        policy, (result,), (ScanCoverage("nas", "incomplete"),), scan_id="9" * 32,
    )
    catalog = apply_reconcile_transition(
        {"containers": [], "versions": [], "sources": [], "reconciliation_history": []},
        transition, max_entities=10_000,
    )
    assert catalog["containers"] == []
    assert catalog["reconciliation_history"][0]["rejected_count"] == 1
    assert catalog["sources"][0]["coverage_status"] == "incomplete"


def test_transition_ingest_stops_unbounded_generators_before_sorting(tmp_path: Path) -> None:
    consumed = 0
    def unbounded_coverage():
        nonlocal consumed
        for index in itertools.count():
            consumed += 1
            yield ScanCoverage(f"source-{index}", "complete")
    with pytest.raises(ReconcileError) as raised:
        _transition(_policy(tmp_path), (), unbounded_coverage(), scan_id="a" * 32)
    assert raised.value.code is ReconcileErrorCode.BOUNDED
    assert consumed <= 257


def test_transition_and_prior_catalog_byte_caps_precede_bulk_copy(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    consumed = 0
    def large_rejections():
        nonlocal consumed
        for index in itertools.count():
            consumed += 1
            yield RejectedObservation("nas", f"{index:04x}:" + "x" * 1600,
                                      "INVALID_ARTIFACT", NOW)
    with pytest.raises(ReconcileError) as transition_error:
        _transition(policy, large_rejections(), (ScanCoverage("nas", "complete"),),
                    scan_id="b" * 32)
    assert transition_error.value.code is ReconcileErrorCode.BOUNDED
    assert consumed < 1000

    empty = _transition(policy, (), (), scan_id="c" * 32)
    huge_prior = {
        "containers": [{"blob": "x" * (1024 * 1024)} for _ in range(9)],
        "versions": [], "sources": [], "reconciliation_history": [],
    }
    with pytest.raises(ReconcileError) as reducer_error:
        apply_reconcile_transition(huge_prior, empty, max_entities=10_000)
    assert reducer_error.value.code is ReconcileErrorCode.BOUNDED


def test_duplicate_observation_generator_has_independent_input_work_cap(tmp_path: Path) -> None:
    observation = _observation("1" * 64, "nas", "nas:/same", VOLUME_ONE)
    consumed = 0

    def duplicates():
        nonlocal consumed
        while True:
            consumed += 1
            yield observation

    with pytest.raises(ReconcileError) as raised:
        _transition(_policy(tmp_path), duplicates(),
                    (ScanCoverage("nas", "complete"),), scan_id="d" * 32)
    assert raised.value.code is ReconcileErrorCode.BOUNDED
    # Aggregate byte work may stop repeated observations before the independent
    # count ceiling. Either bound must stop an unbounded generator promptly.
    assert 1 < consumed <= 10_001


def test_observation_proof_rejects_nested_mutations_before_transition(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    mutations = (
        lambda metadata: metadata.logical["files"][0]["provenance"].update(
            {"original_name": "changed.bin"}),
        lambda metadata: metadata.logical["parent_versions"].append("9" * 32),
        lambda metadata: metadata.inventory["volumes"][0].update(
            {"key": "lssc/v1/changed"}),
        lambda metadata: object.__setattr__(metadata, "logical_commitment_sha256", "0" * 64),
    )
    for mutation in mutations:
        observation = _observation("1" * 64, "nas", "nas:/same", VOLUME_ONE)
        mutation(observation.metadata)
        with pytest.raises(ReconcileError) as raised:
            _transition(policy, (observation,),
                        (ScanCoverage("nas", "complete"),), scan_id="e" * 32)
        assert raised.value.code is ReconcileErrorCode.AUTHORIZATION_DENIED


def test_cached_metadata_proof_rejects_nested_mutation_before_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ls.core.openpgp.container_reconcile as private

    metadata = _metadata(VOLUME_ONE)
    cache = private._capture_cached_metadata(metadata, "a" * 64,
                                             private._metadata_identity(metadata))
    candidate = ContainerCandidate("nas", "nas:/opaque", BytesRangeSource(b"metadata"))
    monkeypatch.setattr(artifacts, "_revalidate_cached_container_metadata",
                        lambda *args, **kwargs: pytest.fail("mutated cache reached authority"))
    metadata.logical["files"][0]["provenance"]["original_name"] = "changed.bin"
    with pytest.raises(ReconcileError) as raised:
        inspect_candidate(
            candidate, policy=_policy(tmp_path), trust_state_path="unused", gnupg_home="unused",
            passphrase_reference=None, secret_resolver=None, evaluation_time=NOW,
            cached=cache,
        )
    assert raised.value.code is ReconcileErrorCode.AUTHORIZATION_DENIED


def test_transition_and_reducer_recheck_mutated_observation_proof(tmp_path: Path) -> None:
    observation = _observation("1" * 64, "nas", "nas:/same", VOLUME_ONE)
    transition = _transition(
        _policy(tmp_path), (observation,),
        (ScanCoverage("nas", "complete"),), scan_id="f" * 32,
    )
    observation.metadata.inventory["volumes"][0]["key"] = "lssc/v1/changed"
    with pytest.raises(ReconcileError) as serialized:
        transition.to_record()
    assert serialized.value.code is ReconcileErrorCode.AUTHORIZATION_DENIED
    with pytest.raises(ReconcileError) as reduced:
        apply_reconcile_transition(
            {"containers": [], "versions": [], "sources": [], "reconciliation_history": []},
            transition, max_entities=10_000,
        )
    assert reduced.value.code is ReconcileErrorCode.AUTHORIZATION_DENIED


def test_mutated_proof_size_is_rejected_before_json_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ls.core.openpgp.container_reconcile as private

    observation = _observation("1" * 64, "nas", "nas:/same", VOLUME_ONE)
    observation.metadata.logical["files"][0]["provenance"]["original_name"] = "x" * 1_100_000
    monkeypatch.setattr(json.JSONEncoder, "iterencode",
                        lambda *args, **kwargs: pytest.fail("oversize proof serialized"))
    with pytest.raises(ReconcileError) as raised:
        private._observation_record(observation)
    assert raised.value.code is ReconcileErrorCode.BOUNDED


def test_standard_replace_cannot_renew_cache_or_observation_proof(tmp_path: Path) -> None:
    import ls.core.openpgp.container_reconcile as private

    observation = _observation("1" * 64, "nas", "nas:/same", VOLUME_ONE)
    cache = private._capture_cached_metadata(
        observation.metadata, "a" * 64, observation.identity,
    )
    # An exact copy preserves the same capture; a changed copy cannot mint one.
    assert replace(observation) == observation
    assert replace(cache) == cache
    changed = copy.deepcopy(observation.metadata)
    changed.logical["files"][0]["provenance"]["original_name"] = "changed.bin"
    object.__setattr__(changed, "logical_commitment_sha256",
                       hashlib.sha256(canonical_bytes(changed.logical)).hexdigest())
    for value, changes in (
        (observation, {"metadata": changed}),
        (observation, {"locator": "nas:/different"}),
        (observation, {"observed_at": NOW + 1}),
        (cache, {"metadata": changed}),
        (cache, {"catalog_head": "b" * 64}),
        (cache, {"identity": "2" * 64}),
        (cache, {"_proof": hashlib.sha256(b"forged").hexdigest()}),
    ):
        with pytest.raises(ReconcileError):
            replace(value, **changes)
    transition = _transition(
        _policy(tmp_path), (observation,),
        (ScanCoverage("nas", "complete"),), scan_id="d" * 32,
    )
    assert transition.observations == (observation,)
    assert replace(transition) == transition
    for changes in ({"policy_sha256": "f" * 64},
                    {"trust_store_id": "2" * 64},
                    {"evaluation_time": NOW + 1},
                    {"_proof": hashlib.sha256(b"forged").hexdigest()}):
        with pytest.raises(ReconcileError):
            replace(transition, **changes)
    with pytest.raises(ReconcileError) as oversized_copy:
        replace(transition, observations=(observation,) * 3_000)
    assert oversized_copy.value.code is ReconcileErrorCode.BOUNDED
