"""Deterministic metadata-only secure-container catalog reconciliation."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from enum import StrEnum
import hashlib
import re
from typing import Any, Iterable, Mapping

from . import artifacts, trust_state
from .artifact_records import canonical_bytes, validate_opaque_id, validate_digest
from .artifacts import (
    ArtifactDiscoveryPolicy, ArtifactError, ArtifactErrorCode, ArtifactPolicy,
    VerifiedContainerMetadata,
)
from .container_format import ContainerHeader, ContainerLimits
from .container_sources import RangeSource
from .contracts import normalize_fingerprint
from .secrets import SecretReference, SecretResolver
from . import container_versions
from .container_catalog import (
    CatalogSnapshot, _SnapshotProof, _bounded_json_sha256,
    _snapshot_capture_sha256,
)


_SOURCE_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}\Z")
_TOKEN = object()
_MAX_OBSERVATIONS = 10_000
_MAX_COVERAGE_SOURCES = 256
_MAX_LOCATOR_BYTES = 2048
_MAX_TRANSITION_BYTES = 1024 * 1024
_MAX_CATALOG_BYTES = 8 * 1024 * 1024


class _CaptureProof:
    """Immutable in-process capture, issued only by local verification helpers."""

    __slots__ = ("_kind", "_digest")

    def __init__(self, issuer: object, kind: str, digest: str) -> None:
        if issuer is not _TOKEN:
            raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_digest", digest)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("metadata proof is immutable")

    def matches(self, kind: str, digest: str) -> bool:
        return self._kind == kind and self._digest == digest


class ReconcileErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    BOUNDED = "BOUNDED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    REJECTED = "REJECTED"


class ReconcileError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in {item.value for item in ReconcileErrorCode}:
            code = ReconcileErrorCode.INVALID_INPUT
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class CachedMetadata:
    """Prior authenticated catalog metadata; display cache only."""

    metadata: VerifiedContainerMetadata = field(repr=False)
    catalog_head: str
    identity: str
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.metadata, VerifiedContainerMetadata)
                or type(self.catalog_head) is not str or len(self.catalog_head) != 64
                or type(self._proof) is not _CaptureProof):
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        if not self._proof.matches("cache", _cache_capture_sha256(
                self.metadata, self.catalog_head, self.identity,
        )):
            raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)


@dataclass(frozen=True, slots=True, repr=False)
class ContainerCandidate:
    """One explicit carrier observation supplied by an authorized scanner."""

    source_id: str
    locator: str = field(repr=False)
    source: RangeSource = field(repr=False, compare=False)
    expected_key: str | None = None

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        _validate_locator(self.locator)
        if (not callable(getattr(self.source, "read_range", None))
                or not hasattr(self.source, "size")):
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        if self.expected_key is not None and type(self.expected_key) is not str:
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True, repr=False)
class CatalogObservation:
    """One current or safely cached authenticated metadata observation."""

    source_id: str
    locator: str = field(repr=False)
    identity: str
    metadata: VerifiedContainerMetadata = field(repr=False)
    observed_at: int
    cache_reused: bool = False
    _proof: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        _validate_locator(self.locator)
        if (type(self.identity) is not str or len(self.identity) != 64
                or any(ch not in "0123456789abcdef" for ch in self.identity)
                or not isinstance(self.metadata, VerifiedContainerMetadata)
                or type(self.observed_at) is not int or self.observed_at < 0
                or type(self.cache_reused) is not bool
                or type(self._proof) is not _CaptureProof):
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        if not self._proof.matches("observation", _bounded_json_sha256(
                _observation_body(self), _MAX_TRANSITION_BYTES,
        )):
            raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)


@dataclass(frozen=True, slots=True, repr=False)
class RejectedObservation:
    source_id: str
    locator: str = field(repr=False)
    error_code: str
    observed_at: int

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        _validate_locator(self.locator)
        if (self.error_code not in {item.value for item in ArtifactErrorCode}
                and self.error_code not in {"UNSUPPORTED", "UNAVAILABLE", "INVALID_ARTIFACT"}):
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        if type(self.observed_at) is not int or self.observed_at < 0:
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True)
class ScanCoverage:
    source_id: str
    status: str

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        if self.status not in {"complete", "incomplete", "unavailable"}:
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True, repr=False)
class ReconcileTransition:
    """Bounded deterministic scan result produced by the verifier helper."""

    scan_id: str
    evaluation_time: int
    policy_sha256: str
    source_config_sha256: str
    trust_store_id: str
    trust_revision: int
    governance_epoch: int
    scope: tuple[str, ...]
    coverage: tuple[ScanCoverage, ...]
    observations: tuple[CatalogObservation, ...]
    rejected: tuple[RejectedObservation, ...] = ()
    _proof: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        try:
            validate_opaque_id(self.scan_id)
            validate_digest(self.policy_sha256)
            validate_digest(self.source_config_sha256)
            validate_digest(self.trust_store_id)
            if (type(self.evaluation_time) is not int or self.evaluation_time < 0
                    or type(self.trust_revision) is not int or self.trust_revision < 1
                    or type(self.governance_epoch) is not int or self.governance_epoch < 1
                    or type(self.scope) is not tuple or not self.scope
                    or len(self.scope) > 32):
                raise ValueError
            if (type(self.coverage) is not tuple or len(self.coverage) > _MAX_COVERAGE_SOURCES
                    or type(self.observations) is not tuple
                    or len(self.observations) > _MAX_OBSERVATIONS
                    or type(self.rejected) is not tuple
                    or len(self.rejected) > _MAX_OBSERVATIONS
                    or (self._proof is not None and type(self._proof) is not _CaptureProof)):
                raise ValueError
            if any(not isinstance(item, ScanCoverage) for item in self.coverage):
                raise ValueError
            if any(not isinstance(item, CatalogObservation) for item in self.observations):
                raise ValueError
            if any(not isinstance(item, RejectedObservation) for item in self.rejected):
                raise ValueError
            source_ids = tuple(item.source_id for item in self.coverage)
            if source_ids != tuple(sorted(set(source_ids))):
                raise ValueError
        except Exception:
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT) from None
        if (self._proof is not None
                and not self._proof.matches("transition", _bounded_json_sha256(
                    self._body(), _MAX_TRANSITION_BYTES,
                ))):
            raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)

    def to_record(self) -> dict[str, Any]:
        if type(self._proof) is not _CaptureProof:
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        record = self._body()
        if not self._proof.matches("transition", _bounded_json_sha256(
                    record, _MAX_TRANSITION_BYTES,
                )):
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        return record

    def _body(self) -> dict[str, Any]:
        observation_records: list[dict[str, Any]] = []
        rejected_records: list[dict[str, Any]] = []
        used_bytes = 0
        for item in self.observations:
            record = _observation_record(item)
            used_bytes += len(canonical_bytes(record))
            if used_bytes > _MAX_TRANSITION_BYTES:
                raise ReconcileError(ReconcileErrorCode.BOUNDED)
            observation_records.append(record)
        for item in self.rejected:
            record = {
                "source_id": item.source_id,
                "locator": item.locator,
                "error_code": item.error_code,
                "observed_at": item.observed_at,
            }
            used_bytes += len(canonical_bytes(record))
            if used_bytes > _MAX_TRANSITION_BYTES:
                raise ReconcileError(ReconcileErrorCode.BOUNDED)
            rejected_records.append(record)
        return {
            "kind": "reconcile",
            "scan_id": self.scan_id,
            "evaluation_time": self.evaluation_time,
            "policy_sha256": self.policy_sha256,
            "source_config_sha256": self.source_config_sha256,
            "trust": {
                "store_id": self.trust_store_id,
                "revision": self.trust_revision,
                "epoch": self.governance_epoch,
                "scope": list(self.scope),
            },
            "coverage": [
                {"source_id": item.source_id, "status": item.status}
                for item in self.coverage
            ],
            "observations": observation_records,
            "rejected": rejected_records,
        }


def metadata_cache_for(snapshot: CatalogSnapshot, identity: str) -> CachedMetadata | None:
    """Reconstitute one private metadata cache from a loaded encrypted snapshot."""
    if type(identity) is not str or len(identity) != 64:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    if type(snapshot) is not CatalogSnapshot or type(snapshot._proof) is not _SnapshotProof:
        raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)
    try:
        if not snapshot._proof.matches(_snapshot_capture_sha256(
            snapshot.head, snapshot.sequence, snapshot.catalog, snapshot.chain_depth, snapshot.required_features,
        )):
            raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)
    except Exception:
        raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED) from None
    catalog = snapshot.catalog
    head = snapshot.head
    if type(catalog) is not dict or type(head) is not str:
        return None
    for item in catalog.get("containers", []):
        if type(item) is dict and item.get("identity") == identity:
            try:
                metadata = _metadata_from_record(item["authenticated_metadata"])
                return _capture_cached_metadata(metadata, head, identity)
            except Exception:
                return None
    return None


def inspect_candidate(
    candidate: ContainerCandidate,
    *,
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str,
    gnupg_home: str,
    passphrase_reference: SecretReference | None,
    secret_resolver: SecretResolver | None,
    evaluation_time: int,
    limits: ContainerLimits = ContainerLimits(),
    cached: CachedMetadata | None = None,
    _budget=None,
    _workspace=None,
) -> CatalogObservation | RejectedObservation:
    """Authenticate metadata under current policy; never read payload messages."""
    if (not isinstance(candidate, ContainerCandidate)
            or not isinstance(policy, (ArtifactPolicy, ArtifactDiscoveryPolicy))
            or type(evaluation_time) is not int or evaluation_time < 0):
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    try:
        if _budget is not None:
            _budget.checkpoint()
        metadata = None
        if cached is not None:
            if not isinstance(cached, CachedMetadata):
                raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
            if not cached._proof.matches("cache", _cache_capture_sha256(
                cached.metadata, cached.catalog_head, cached.identity,
            )):
                raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)
            if artifacts._revalidate_cached_container_metadata(
                (artifacts._BudgetedRangeSource(candidate.source, _budget)
                 if _budget is not None else candidate.source), cached.metadata, policy=policy,
                trust_state_path=trust_state_path, limits=limits,
                expected_key=candidate.expected_key,
            ):
                metadata = cached.metadata
                if _metadata_identity(metadata) != cached.identity:
                    raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
                cache_reused = True
            else:
                cache_reused = False
        else:
            cache_reused = False
        if metadata is None:
            metadata = artifacts.inspect_container_metadata(
                candidate.source, policy=policy,
                trust_state_path=trust_state_path, gnupg_home=gnupg_home,
                expected_key=candidate.expected_key,
                passphrase_reference=passphrase_reference,
                secret_resolver=secret_resolver, limits=limits,
                evaluation_time=evaluation_time,
                _budget=_budget, _workspace=_workspace,
            )
            _record_metadata_receipt(metadata, policy, trust_state_path)
        identity = _metadata_identity(metadata)
        if _budget is not None:
            _budget.checkpoint()
        observation = _capture_observation(
            source_id=candidate.source_id, locator=candidate.locator,
            identity=identity, metadata=metadata, observed_at=evaluation_time,
            cache_reused=cache_reused,
        )
        if len(canonical_bytes(_observation_record(observation))) > 1024 * 1024:
            raise ReconcileError(ReconcileErrorCode.BOUNDED)
        return observation
    except ReconcileError:
        raise
    except ArtifactError as exc:
        return RejectedObservation(
            candidate.source_id, candidate.locator, exc.code.value, evaluation_time,
        )
    except Exception:
        return RejectedObservation(
            candidate.source_id, candidate.locator, "UNAVAILABLE", evaluation_time,
        )


def build_transition(
    *,
    scan_id: str,
    evaluation_time: int,
    source_config_sha256: str,
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    coverage: Iterable[ScanCoverage],
    observations: Iterable[CatalogObservation | RejectedObservation],
) -> ReconcileTransition:
    """Freeze explicit scan coverage, policy, trust, and sorted observations."""
    if not isinstance(policy, (ArtifactPolicy, ArtifactDiscoveryPolicy)):
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    try:
        policy_digest = artifacts.container_admission_policy_sha256(policy)
        coverage_items: list[ScanCoverage] = []
        used_bytes = 0
        for item in coverage:
            if not isinstance(item, ScanCoverage):
                raise ValueError
            if len(coverage_items) >= _MAX_COVERAGE_SOURCES:
                raise ReconcileError(ReconcileErrorCode.BOUNDED)
            used_bytes += len(canonical_bytes({"source_id": item.source_id,
                                               "status": item.status}))
            if used_bytes > _MAX_TRANSITION_BYTES:
                raise ReconcileError(ReconcileErrorCode.BOUNDED)
            coverage_items.append(item)
        coverage_tuple = tuple(sorted(coverage_items, key=lambda item: item.source_id))
        verified: dict[tuple[str, str, str], tuple[CatalogObservation, bytes]] = {}
        rejected: list[RejectedObservation] = []
        examined = 0
        for item in observations:
            examined += 1
            if examined > _MAX_OBSERVATIONS:
                raise ReconcileError(ReconcileErrorCode.BOUNDED)
            if isinstance(item, CatalogObservation):
                if type(item._proof) is not _CaptureProof:
                    raise ValueError
                key = (item.identity, item.source_id, item.locator)
                record = _observation_record(item)
                if item.metadata.admission_policy_sha256 != policy_digest:
                    raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)
                used_bytes += len(canonical_bytes(record))
                if used_bytes > _MAX_TRANSITION_BYTES:
                    raise ReconcileError(ReconcileErrorCode.BOUNDED)
                # A repeated carrier observation may differ in observation
                # time or cache use, but never in its immutable content claim.
                # Preserve the latest observation, preferring fresh verification
                # on ties. Neither choice selects a winning content version.
                del record["observed_at"], record["cache_reused"]
                claim = canonical_bytes(record)
                prior = verified.get(key)
                if prior is not None and prior[1] != claim:
                    raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
                if prior is None or (item.observed_at, not item.cache_reused) > (
                        prior[0].observed_at, not prior[0].cache_reused):
                    verified[key] = (item, claim)
            elif isinstance(item, RejectedObservation):
                used_bytes += len(canonical_bytes({
                    "source_id": item.source_id, "locator": item.locator,
                    "error_code": item.error_code, "observed_at": item.observed_at,
                }))
                if used_bytes > _MAX_TRANSITION_BYTES:
                    raise ReconcileError(ReconcileErrorCode.BOUNDED)
                rejected.append(item)
            else:
                raise ValueError
        verified_tuple = tuple(sorted((item for item, _claim in verified.values()), key=lambda item: (
            item.identity, item.source_id, item.locator,
        )))
        rejected_tuple = tuple(sorted(rejected, key=lambda item: (
            item.source_id, item.locator, item.error_code, item.observed_at,
        )))
        if len(verified_tuple) + len(rejected_tuple) > _MAX_OBSERVATIONS:
            raise ReconcileError(ReconcileErrorCode.BOUNDED)
        unsigned = ReconcileTransition(
            scan_id=scan_id, evaluation_time=evaluation_time,
            policy_sha256=policy_digest,
            source_config_sha256=source_config_sha256,
            trust_store_id=policy.trust_store_id,
            trust_revision=policy.trust_revision,
            governance_epoch=policy.governance_epoch,
            scope=policy.scope, coverage=coverage_tuple,
            observations=verified_tuple, rejected=rejected_tuple,
        )
        return replace(unsigned, _proof=_CaptureProof(
            _TOKEN, "transition", _bounded_json_sha256(
                unsigned._body(), _MAX_TRANSITION_BYTES,
            ),
        ))
    except ReconcileError:
        raise
    except Exception:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT) from None


@dataclass(frozen=True, slots=True)
class ReconcileBatch:
    """A complete scan with independently verified policy captures."""
    transitions: tuple[ReconcileTransition, ...]

    def to_record(self):
        if type(self.transitions) is not tuple or not 2 <= len(self.transitions) <= 16:
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        transitions, _observations = _verified_batch(self.transitions)
        return _batch_record(transitions)


def _batch_record(transitions):
    return {'kind': 'reconcile-batch', 'version': 1,
            'transitions': [item.to_record() for item in transitions]}


def _verified_batch(transitions):
    if type(transitions) is not tuple or not 1 <= len(transitions) <= 16:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    used_bytes = examined = 0
    for item in transitions:
        if not isinstance(item, ReconcileTransition) or type(item._proof) is not _CaptureProof:
            raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
        record = item.to_record()
        used_bytes += len(canonical_bytes(record))
        examined += len(item.observations) + len(item.rejected)
        if used_bytes > _MAX_TRANSITION_BYTES or examined > _MAX_OBSERVATIONS:
            raise ReconcileError(ReconcileErrorCode.BOUNDED)
    transitions = tuple(sorted(transitions, key=lambda item: item.policy_sha256))
    transition = transitions[0]
    if len({item.policy_sha256 for item in transitions}) != len(transitions):
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    if any((item.scan_id, item.evaluation_time, item.source_config_sha256, item.coverage) !=
           (transition.scan_id, transition.evaluation_time, transition.source_config_sha256, transition.coverage)
           for item in transitions):
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    observations = tuple(sorted((row for item in transitions for row in item.observations),
                                key=lambda row: (row.identity, row.source_id, row.locator)))
    admitted = {}
    for item in transitions:
        for row in item.observations:
            key = row.source_id, row.locator
            prior = admitted.setdefault(key, item.policy_sha256)
            if prior != item.policy_sha256:
                # A carrier accepted under multiple contexts is ambiguous;
                # neither caller order nor ciphertext hints choose authority.
                raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)
    if len(transitions) > 1:
        if len(canonical_bytes(_batch_record(transitions))) > _MAX_TRANSITION_BYTES:
            raise ReconcileError(ReconcileErrorCode.BOUNDED)
    return transitions, observations


def apply_reconcile_transition(
    previous: dict[str, Any], transition: ReconcileTransition, *, max_entities: int,
) -> dict[str, Any]:
    """Pure deterministic reducer over one verified scan transition."""
    return apply_reconcile_batch(previous, (transition,), max_entities=max_entities)


def apply_reconcile_batch(
    previous: dict[str, Any], transitions: tuple[ReconcileTransition, ...], *, max_entities: int,
) -> dict[str, Any]:
    """Reduce one complete scan under independently captured admission policies.

    Each policy keeps its own verification proof and history decision. Availability
    is computed once across all admitted observations, never once per origin.
    The caller still owns current authority checks and atomic catalog publication.
    """
    if type(previous) is not dict or type(max_entities) is not int:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    transitions, observations = _verified_batch(transitions)
    transition = transitions[0]
    if set(previous) != {"containers", "versions", "sources", "reconciliation_history"} \
            or any(type(value) is not list for value in previous.values()) \
            or sum(len(value) for value in previous.values()) > max_entities:
        raise ReconcileError(ReconcileErrorCode.BOUNDED)
    prior_bytes = 0
    for rows in previous.values():
        for item in rows:
            prior_bytes += len(canonical_bytes(item))
            if prior_bytes > _MAX_CATALOG_BYTES:
                raise ReconcileError(ReconcileErrorCode.BOUNDED)
    containers = {item["identity"]: _json_copy(item)
                  for item in previous.get("containers", []) if type(item) is dict}
    coverage = {item.source_id: item.status for item in transition.coverage}
    seen_by_source: dict[str, set[str]] = {source_id: set() for source_id in coverage}
    for observation in observations:
        record = _observation_record(observation)
        identity = record["identity"]
        source_id = observation.source_id
        seen_by_source.setdefault(source_id, set()).add((identity, observation.locator))
        existing = containers.get(identity)
        if existing is None:
            containers[identity] = {
                "identity": identity,
                "artifact_id": record["artifact_id"],
                "version_id": record["version_id"],
                "commitment_sha256": record["commitment_sha256"],
                "volume_id": record["volume_id"],
                "origin_volume_id": record["origin_volume_id"],
                "parents": record["parents"],
                "expected_volumes": record["expected_volumes"],
                "file_inventory": record["file_inventory"],
                "authenticated_metadata": record["authenticated_metadata"],
                "metadata_status": "verified",
                "payload_status": "unverified",
                "segment_status": "unknown",
                "availability": "observed",
                "claim_conflict": False,
                "observations": [],
            }
            existing = containers[identity]
        elif _claim_signature(existing) != _claim_signature(record):
            existing["claim_conflict"] = True
        observation_record = {
            "source_id": source_id,
            "locator": observation.locator,
            "status": "metadata_verified",
            "scan_status": "observed",
            "observed_at": observation.observed_at,
            "header_sha256": hashlib.sha256(
                observation.metadata.header.canonical_public_header
            ).hexdigest(),
            "encrypted_metadata_sha256": hashlib.sha256(
                observation.metadata.header.encrypted_metadata
            ).hexdigest(),
            "cache_reused": observation.cache_reused,
        }
        _merge_observation(existing["observations"], observation_record)
        existing["availability"] = "observed"

    for source_id, status in coverage.items():
        seen = seen_by_source.get(source_id, set())
        for container in containers.values():
            for observation in container["observations"]:
                if observation["source_id"] != source_id:
                    continue
                if (container["identity"], observation["locator"]) in seen:
                    continue
                if status == "unavailable":
                    observation["scan_status"] = "source_unavailable"
                elif status == "incomplete":
                    observation["scan_status"] = "not_observed_in_incomplete_scan"
                else:
                    observation["scan_status"] = "not_observed"
    for container in containers.values():
        current_rows = [item for item in container["observations"]
                        if item["source_id"] in coverage]
        current_statuses = [item.get("scan_status") for item in current_rows]
        if "observed" in current_statuses:
            container["availability"] = "observed"
        elif "source_unavailable" in current_statuses:
            container["availability"] = "unavailable"
        elif "not_observed" in current_statuses:
            container["availability"] = "not_observed"
        elif "not_observed_in_incomplete_scan" in current_statuses:
            container["availability"] = "incomplete_scan"

    _update_segment_status(containers.values(), coverage)
    versions = _version_records(containers.values())
    collisions = {
        (item["artifact_id"], item["version_id"])
        for item in versions if item.get("status") == "collision"
    }
    for item in containers.values():
        if (item["artifact_id"], item["version_id"]) in collisions:
            item["claim_conflict"] = True
    sources = [
        {"source_id": source_id, "coverage_status": status,
         "evaluation_time": transition.evaluation_time}
        for source_id, status in sorted(coverage.items())
    ]
    history = list(previous.get("reconciliation_history", []))
    decisions = [_decision(item) for item in transitions]
    if len(transitions) > 1:
        batch_digest = _bounded_json_sha256(_batch_record(transitions), _MAX_TRANSITION_BYTES)
        decisions = [dict(item, admission_batch=batch_digest) for item in decisions]
    if history[-len(decisions):] != decisions:
        history.extend(decisions)
    history = history[-64:]
    result = {
        "containers": sorted(containers.values(), key=lambda item: item["identity"]),
        "versions": versions,
        "sources": sources,
        "reconciliation_history": history,
    }
    if sum(len(items) for items in result.values()) > max_entities:
        raise ReconcileError(ReconcileErrorCode.BOUNDED)
    if len(canonical_bytes(result)) > 8 * 1024 * 1024:
        raise ReconcileError(ReconcileErrorCode.BOUNDED)
    return result


def _decision(transition):
    return {
        "scan_id": transition.scan_id,
        "evaluation_time": transition.evaluation_time,
        "policy_sha256": transition.policy_sha256,
        "source_config_sha256": transition.source_config_sha256,
        "trust_store_id": transition.trust_store_id,
        "trust_revision": transition.trust_revision,
        "governance_epoch": transition.governance_epoch,
        "observed_count": len(transition.observations),
        "rejected_count": len(transition.rejected),
        "coverage": [{"source_id": row.source_id, "status": row.status}
                     for row in transition.coverage],
        "rejected": [{"source_id": row.source_id, "locator": row.locator,
                      "status": "rejected", "error_code": row.error_code,
                      "observed_at": row.observed_at} for row in transition.rejected],
    }


def _record_metadata_receipt(
    metadata: VerifiedContainerMetadata,
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str,
) -> None:
    try:
        trust_state._record_accepted_container(
            trust_state_path,
            identity_prefix=artifacts._metadata_prefix(metadata),
            metadata_identity=None, layers=metadata.metadata_layers,
            expected_store_id=policy.trust_store_id,
            expected_revision=policy.trust_revision,
            expected_epoch=policy.governance_epoch,
            expected_scope=policy.scope,
            created_at=metadata.logical["created_at"],
            expires_at=metadata.logical["expires_at"],
            **artifacts._discovery_bounds(policy),
            release_payload=lambda: None,
            rollback_payload=lambda: None,
        )
    except trust_state.TrustStateError:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None


def _metadata_identity(metadata: VerifiedContainerMetadata) -> str:
    value = artifacts._metadata_identity(metadata)
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _observation_record(observation: CatalogObservation) -> dict[str, Any]:
    if not isinstance(observation, CatalogObservation) or type(observation._proof) is not _CaptureProof:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    record = _observation_body(observation)
    try:
        if not observation._proof.matches(
            "observation", _bounded_json_sha256(record, _MAX_TRANSITION_BYTES),
        ):
            raise ReconcileError(ReconcileErrorCode.AUTHORIZATION_DENIED)
    except ReconcileError:
        raise
    except Exception:
        raise ReconcileError(ReconcileErrorCode.BOUNDED) from None
    return record


def _observation_body(observation: CatalogObservation) -> dict[str, Any]:
    return _observation_body_fields(
        observation.source_id, observation.locator, observation.identity,
        observation.metadata, observation.observed_at, observation.cache_reused,
    )


def _observation_body_fields(
    source_id: str, locator: str, identity: str,
    metadata: VerifiedContainerMetadata, observed_at: int, cache_reused: bool,
) -> dict[str, Any]:
    logical = metadata.logical
    inventory = metadata.inventory
    parents = logical["parent_versions"]
    if not isinstance(parents, list):
        raise ReconcileError(ReconcileErrorCode.REJECTED)
    try:
        parents_tuple = tuple(sorted({validate_opaque_id(item) for item in parents}))
        artifact_id = validate_opaque_id(logical["artifact_id"])
        version_id = validate_opaque_id(logical["version_id"])
        validate_digest(metadata.logical_commitment_sha256)
        expected_volumes = tuple(sorted(
            validate_opaque_id(item["volume_id"]) for item in inventory["volumes"]
        ))
        volume_id = validate_opaque_id(metadata.volume_id)
        origin_volume_id = validate_opaque_id(metadata.origin_volume_id)
        if origin_volume_id not in expected_volumes:
            raise ValueError
    except Exception:
        raise ReconcileError(ReconcileErrorCode.REJECTED) from None
    return {
        "identity": identity,
        "source_id": source_id,
        "locator": locator,
        "artifact_id": artifact_id,
        "version_id": version_id,
        "commitment_sha256": metadata.logical_commitment_sha256,
        "volume_id": volume_id,
        "origin_volume_id": origin_volume_id,
        "parents": list(parents_tuple),
        "expected_volumes": list(expected_volumes),
        "file_inventory": logical["files"],
        "authenticated_metadata": _metadata_to_record(metadata),
        "observed_at": observed_at,
        "cache_reused": cache_reused,
    }


def _capture_observation(
    *, source_id: str, locator: str, identity: str,
    metadata: VerifiedContainerMetadata, observed_at: int,
    cache_reused: bool = False,
) -> CatalogObservation:
    capture = _bounded_json_sha256(_observation_body_fields(
        source_id, locator, identity, metadata, observed_at, cache_reused,
    ), _MAX_TRANSITION_BYTES)
    return CatalogObservation(
        source_id, locator, identity, metadata, observed_at, cache_reused,
        _CaptureProof(_TOKEN, "observation", capture),
    )


def _metadata_to_record(metadata: VerifiedContainerMetadata) -> dict[str, Any]:
    header = metadata.header
    try:
        if (type(header.encrypted_metadata) is not bytes
                or type(header.canonical_public_header) is not bytes
                or type(header.recipient_hints) is not tuple
                or len(header.recipient_hints) > 256
                or type(metadata.metadata_layers) is not tuple
                or len(metadata.metadata_layers) > 8):
            raise ReconcileError(ReconcileErrorCode.BOUNDED)
        raw_bytes = len(header.encrypted_metadata) + len(header.canonical_public_header)
        for layer in metadata.metadata_layers:
            if type(layer) is not tuple or len(layer) != 3 or type(layer[0]) is not bytes:
                raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
            raw_bytes += len(layer[0])
            if raw_bytes > _MAX_TRANSITION_BYTES:
                raise ReconcileError(ReconcileErrorCode.BOUNDED)
    except ReconcileError:
        raise
    except Exception:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT) from None
    return {
        "header": {
            "format": header.format,
            "version": header.version,
            "recipient_hints": list(header.recipient_hints),
            "metadata_length": header.metadata_length,
            "encrypted_metadata_b64": base64.b64encode(header.encrypted_metadata).decode("ascii"),
            "messages_offset": header.messages_offset,
            "canonical_public_header_b64": base64.b64encode(
                header.canonical_public_header
            ).decode("ascii"),
        },
        "logical": metadata.logical,
        "inventory": metadata.inventory,
        "volume_record": metadata.volume_record,
        "key": metadata.key,
        "volume_id": metadata.volume_id,
        "origin_volume_id": metadata.origin_volume_id,
        "logical_commitment_sha256": metadata.logical_commitment_sha256,
        "admission_policy_sha256": metadata.admission_policy_sha256,
        "metadata_layers": [
            {
                "ciphertext_b64": base64.b64encode(layer[0]).decode("ascii"),
                "role": layer[1], "fingerprint": layer[2],
            }
            for layer in metadata.metadata_layers
        ],
        "payload_verified": False,
    }


def _bounded_metadata_sha256(metadata: VerifiedContainerMetadata) -> str:
    try:
        return _bounded_json_sha256(_metadata_to_record(metadata), _MAX_TRANSITION_BYTES)
    except ReconcileError:
        raise
    except Exception:
        raise ReconcileError(ReconcileErrorCode.BOUNDED) from None


def _cache_capture_sha256(
    metadata: VerifiedContainerMetadata, catalog_head: str, identity: str,
) -> str:
    return _bounded_json_sha256({
        "catalog_head": catalog_head, "identity": identity,
        "metadata_sha256": _bounded_metadata_sha256(metadata),
    }, 512)


def _capture_cached_metadata(
    metadata: VerifiedContainerMetadata, catalog_head: str, identity: str,
) -> CachedMetadata:
    return CachedMetadata(metadata, catalog_head, identity,
                          _CaptureProof(_TOKEN, "cache", _cache_capture_sha256(
                              metadata, catalog_head, identity,
                          )))


def _metadata_from_record(value: Any) -> VerifiedContainerMetadata:
    if type(value) is not dict or set(value) != {
        "header", "logical", "inventory", "volume_record", "key", "volume_id",
        "origin_volume_id", "logical_commitment_sha256", "admission_policy_sha256",
        "metadata_layers", "payload_verified",
    } or value.get("payload_verified") is not False:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    header_value = value["header"]
    if type(header_value) is not dict:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)
    header = ContainerHeader(
        format=header_value["format"], version=header_value["version"],
        recipient_hints=tuple(header_value["recipient_hints"]),
        metadata_length=header_value["metadata_length"],
        encrypted_metadata=base64.b64decode(header_value["encrypted_metadata_b64"], validate=True),
        messages_offset=header_value["messages_offset"],
        canonical_public_header=base64.b64decode(
            header_value["canonical_public_header_b64"], validate=True,
        ),
    )
    layers = tuple(
        (base64.b64decode(item["ciphertext_b64"], validate=True),
         item["role"], normalize_fingerprint(item["fingerprint"]))
        for item in value["metadata_layers"]
    )
    return VerifiedContainerMetadata(
        header=header, logical=value["logical"], inventory=value["inventory"],
        volume_record=value["volume_record"], key=value["key"],
        volume_id=value["volume_id"], origin_volume_id=value["origin_volume_id"],
        logical_commitment_sha256=value["logical_commitment_sha256"],
        admission_policy_sha256=value["admission_policy_sha256"],
        metadata_layers=layers, payload_verified=False,
    )


def _claim_signature(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("artifact_id"), value.get("version_id"),
        value.get("commitment_sha256"), value.get("volume_id"),
        value.get("origin_volume_id"),
        tuple(value.get("parents", [])), tuple(value.get("expected_volumes", [])),
    )


def _merge_observation(rows: list[dict[str, Any]], incoming: dict[str, Any]) -> None:
    identity = (incoming["source_id"], incoming["locator"])
    for index, item in enumerate(rows):
        if (item["source_id"], item["locator"]) == identity:
            # Stable exact observations are idempotent. Retain the latest
            # explicit evaluation time only when the authenticated bytes match.
            if (item["header_sha256"], item["encrypted_metadata_sha256"]) == (
                incoming["header_sha256"], incoming["encrypted_metadata_sha256"],
            ):
                rows[index] = incoming
            else:
                incoming = dict(incoming)
                incoming["status"] = "conflict"
                rows.append(incoming)
            return
    rows.append(incoming)
    rows.sort(key=lambda item: (item["source_id"], item["locator"],
                                item["header_sha256"], item["encrypted_metadata_sha256"]))


def _update_segment_status(
    containers: Iterable[dict[str, Any]], coverage: Mapping[str, str],
) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in containers:
        grouped.setdefault((item["artifact_id"], item["version_id"]), []).append(item)
    for group in grouped.values():
        expected = set().union(*(set(item["expected_volumes"]) for item in group))
        observed = {
            item["origin_volume_id"] for item in group
            if item["metadata_status"] == "verified"
            and any(observation.get("scan_status") == "observed"
                    and observation["source_id"] in coverage
                    for observation in item["observations"])
        }
        statuses = set(coverage.values())
        if statuses and statuses <= {"complete"}:
            result = "complete" if expected <= observed else "incomplete"
        elif expected <= observed:
            result = "complete_observed_with_partial_coverage"
        else:
            result = "unknown"
        for item in group:
            item["segment_status"] = result


def _version_records(containers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = [
        container_versions.VersionNode(
            artifact_id=item["artifact_id"], version_id=item["version_id"],
            parents=tuple(item["parents"]), commitment_sha256=item["commitment_sha256"],
        )
        for item in containers
    ]
    graph = container_versions.build_version_graph(nodes)
    return [
        {"artifact_id": node.artifact_id, "version_id": node.version_id,
         "parents": list(node.parents), "commitment_sha256": node.commitment_sha256}
        for node in graph.nodes
    ] + [
        {"artifact_id": artifact, "version_id": version,
         "status": "collision", "commitment_sha256": None}
        for artifact, version in graph.collisions
    ]


def _validate_source_id(value: str) -> None:
    if type(value) is not str or _SOURCE_ID_RE.fullmatch(value) is None:
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)


def _validate_locator(value: str) -> None:
    if (type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_LOCATOR_BYTES
            or "\x00" in value):
        raise ReconcileError(ReconcileErrorCode.INVALID_INPUT)


def _json_copy(value: Any) -> Any:
    return __import__("json").loads(canonical_bytes(value).decode("utf-8"))
