"""Trusted secure-container preparation, metadata, selection, and history."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import secrets
import stat
import struct
import tempfile
import time
from typing import Any, Callable, TypeAlias

from .artifact_records import (
    ArtifactProvenance, ArtifactRecordError, canonical_bytes, parse_canonical,
    parse_descriptor, validate_schema, validate_inventory, logical_commitment,
    decode_message_prefix, validate_opaque_id, validate_digest, schema_envelope,
    parse_container_key,
)
from .container_format import (
    CONTAINER_VERSION, PRIVATE_CONTAINER_VERSION,
    ContainerHeader, ContainerLimits, read_metadata,
    copy_message, scan_message_descriptors, write_container,
    UnsupportedContainerVersion,
)
from .container_schema import UnsupportedProtectedSchemaVersion, UnsupportedRequiredFeature
from .container_sources import RangeSource
from .container_snapshot_records import SNAPSHOT_FEATURE, SnapshotClaim
from .container_worker_records import validate_worker_layers
from .container_preparation import (
    ContainerInput, ContainerNamespace, ContainerPreparationLimits,
    ContainerPreparationError, PreparedVolume, PreparedContainerSet, prepare_volumes,
    _Budget, _CheckedWriter, _DiscardWriter, _pad_metadata, _seal, _write_stream,
)
from .envelope_io import ExclusiveOutput
from .envelope_io import AnchoredFile, FileEnvelopeLimits, OperationBudget
from .contracts import EnvelopePolicy, LocalTrust, normalize_fingerprint
from .historical import open_historical_envelope, open_historical_envelope_file
from .opening import open_envelope, open_envelope_file
from .secrets import SecretReference, SecretResolver
from . import trust_state

_ROLE: TypeAlias = str
_MAX_ARTIFACT_LIFETIME = 30 * 24 * 60 * 60
_CONTENT_REVISION = 128
_PURPOSE_MAX = 96

class ArtifactErrorCode(StrEnum):
    """Stable failures that never include artifact or filesystem metadata."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_ARTIFACT = "INVALID_ARTIFACT"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    UNSAFE_BOUNDARY = "UNSAFE_BOUNDARY"
    REPLAY = "REPLAY"
    UNSUPPORTED = "UNSUPPORTED"


class ArtifactError(RuntimeError):
    """Redacted artifact failure with a stable code."""

    def __init__(self, code: ArtifactErrorCode) -> None:
        self.code = ArtifactErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactPolicy:
    """Pin local authority and independently expected signed origin evidence."""

    trust_store_id: str
    trust_revision: int
    governance_epoch: int
    scope: tuple[str, ...]
    origin_fingerprint: str
    audience: tuple[str, ...]
    purpose: str
    content_revision: str
    created_at: int
    expires_at: int
    boundary: str | os.PathLike[str] = field(repr=False)
    controller_fingerprint: str | None = None
    origin_trust_store_id: str | None = None
    origin_trust_revision: int | None = None
    origin_governance_epoch: int | None = None
    require_worker_snapshots: bool = False

    def __post_init__(self) -> None:
        try:
            if (type(self.require_worker_snapshots) is not bool
                    or self.require_worker_snapshots and self.controller_fingerprint is None):
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            if (
                type(self.trust_store_id) is not str
                or len(self.trust_store_id) != 64
                or any(character not in "0123456789abcdef" for character in self.trust_store_id)
                or type(self.trust_revision) is not int
                or self.trust_revision < 0
                or type(self.governance_epoch) is not int
                or self.governance_epoch < 1
                or type(self.scope) is not tuple
                or not self.scope
                or len(self.scope) > 32
                or any(
                    type(item) is not str
                    or not item
                    or len(item.encode("utf-8")) > 128
                    or not item[0].isascii()
                    or not item[0].isalnum()
                    or any(not (character.isascii() and (
                        character.isalnum() or character in "._:/-"
                    )) for character in item)
                    for item in self.scope
                )
                or self.scope != tuple(sorted(set(self.scope)))
                or type(self.audience) is not tuple
                or not self.audience
                or len(self.audience) > 64
                or type(self.purpose) is not str
                or len(self.purpose) > _PURPOSE_MAX
                or not self.purpose
                or not self.purpose[0].isascii()
                or not self.purpose[0].isalnum()
                or any(not (character.isascii() and (
                    character.isalnum() or character in "._:/-"
                )) for character in self.purpose)
                or type(self.content_revision) is not str
                or not self.content_revision
                or len(self.content_revision) > _CONTENT_REVISION
                or not self.content_revision[0].isascii()
                or not self.content_revision[0].isalnum()
                or any(not (character.isascii() and (
                    character.isalnum() or character in "._:+/-"
                )) for character in self.content_revision)
                or type(self.created_at) is not int
                or type(self.expires_at) is not int
                or self.created_at < 0
                or self.expires_at <= self.created_at
                or self.expires_at - self.created_at > _MAX_ARTIFACT_LIFETIME
                or not isinstance(self.boundary, (str, os.PathLike))
            ):
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            origin = normalize_fingerprint(self.origin_fingerprint)
            audience = tuple(sorted(normalize_fingerprint(item) for item in self.audience))
            if len(set(audience)) != len(audience):
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            controller = (
                normalize_fingerprint(self.controller_fingerprint)
                if self.controller_fingerprint is not None
                else None
            )
            if controller is not None and controller not in audience:
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            origin_fields = (
                self.origin_trust_store_id,
                self.origin_trust_revision,
                self.origin_governance_epoch,
            )
            if any(value is None for value in origin_fields):
                if any(value is not None for value in origin_fields):
                    raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
                object.__setattr__(self, "origin_trust_store_id", self.trust_store_id)
                object.__setattr__(self, "origin_trust_revision", self.trust_revision)
                object.__setattr__(self, "origin_governance_epoch", self.governance_epoch)
            elif (
                type(self.origin_trust_store_id) is not str
                or len(self.origin_trust_store_id) != 64
                or any(character not in "0123456789abcdef" for character in self.origin_trust_store_id)
                or type(self.origin_trust_revision) is not int
                or self.origin_trust_revision < 0
                or type(self.origin_governance_epoch) is not int
                or self.origin_governance_epoch < 1
            ):
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            object.__setattr__(self, "origin_fingerprint", origin)
            object.__setattr__(self, "audience", audience)
            object.__setattr__(self, "controller_fingerprint", controller)
            boundary = os.fspath(self.boundary)
            if type(boundary) is not str or "\x00" in boundary:
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            object.__setattr__(self, "boundary", boundary)
        except ArtifactError:
            raise
        except Exception:
            raise ArtifactError(ArtifactErrorCode.INVALID_INPUT) from None


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactDiscoveryPolicy:
    """Preconfigured admission bounds for versions not known before decryption."""

    trust_store_id: str
    trust_revision: int
    governance_epoch: int
    scope: tuple[str, ...]
    origin_fingerprint: str
    audience: tuple[str, ...]
    purpose: str
    boundary: str | os.PathLike[str] = field(repr=False)
    admit_from: int
    admit_until: int
    max_content_age_seconds: int
    max_content_lifetime_seconds: int
    controller_fingerprint: str | None = None
    origin_trust_store_id: str | None = None
    origin_trust_revision: int | None = None
    origin_governance_epoch: int | None = None
    require_worker_snapshots: bool = False

    def __post_init__(self) -> None:
        if (type(self.admit_from) is not int or type(self.admit_until) is not int
                or self.admit_from < 0 or self.admit_until <= self.admit_from
                or type(self.max_content_age_seconds) is not int
                or type(self.max_content_lifetime_seconds) is not int
                or not 0 < self.max_content_age_seconds <= 30 * 24 * 3600
                or not 0 < self.max_content_lifetime_seconds <= 30 * 24 * 3600):
            raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
        checked = ArtifactPolicy(
            trust_store_id=self.trust_store_id, trust_revision=self.trust_revision,
            governance_epoch=self.governance_epoch, scope=self.scope,
            origin_fingerprint=self.origin_fingerprint, audience=self.audience,
            purpose=self.purpose, content_revision="discovery", created_at=0,
            expires_at=1, boundary=self.boundary,
            controller_fingerprint=self.controller_fingerprint,
            origin_trust_store_id=self.origin_trust_store_id,
            origin_trust_revision=self.origin_trust_revision,
            origin_governance_epoch=self.origin_governance_epoch,
            require_worker_snapshots=self.require_worker_snapshots,
        )
        for name in ("origin_fingerprint", "audience", "controller_fingerprint", "boundary",
                     "origin_trust_store_id", "origin_trust_revision", "origin_governance_epoch"):
            object.__setattr__(self, name, getattr(checked, name))


# The canonical container APIs are defined below.


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedContainerMetadata:
    """Verified claims from one observation, never a bearer authorization token."""

    header: ContainerHeader
    logical: dict[str, Any]
    inventory: dict[str, Any]
    volume_record: dict[str, Any]
    key: str
    volume_id: str
    origin_volume_id: str
    logical_commitment_sha256: str
    admission_policy_sha256: str
    metadata_layers: tuple[tuple[bytes, str, str], ...] = field(repr=False)
    payload_verified: bool = False


@dataclass(frozen=True, slots=True)
class ContainerReceipt:
    operation: str
    status: str
    logical_commitment_sha256: str
    volume_id: str
    message_index: int | None = None


class _BudgetedRangeSource:
    """Apply the preparation deadline and cancellation to metadata ranges."""

    def __init__(self, source: RangeSource, budget: _Budget) -> None:
        self.source = source
        self.budget = budget

    @property
    def size(self) -> int:
        self.budget.checkpoint()
        value = self.source.size
        self.budget.checkpoint()
        return value

    def read_range(self, offset: int, length: int) -> bytes:
        self.budget.checkpoint()
        value = self.source.read_range(offset, length)
        self.budget.checkpoint()
        return value


def _logical_policy(policy):
    return {
        "origin": {"store_id": policy.trust_store_id, "revision": policy.trust_revision,
                   "epoch": policy.governance_epoch, "fingerprint": policy.origin_fingerprint},
        "audience": list(policy.audience), "scope": list(policy.scope),
        "purpose": policy.purpose, "content_revision": policy.content_revision,
        "created_at": policy.created_at, "expires_at": policy.expires_at,
    }


def prepare_container(
    files: tuple[ContainerInput, ...], *, output_dir: str | os.PathLike[str],
    namespace: ContainerNamespace, policy: ArtifactPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    passphrase_reference: SecretReference, secret_resolver: SecretResolver,
    limits: ContainerPreparationLimits = ContainerPreparationLimits(),
    artifact_id: str | None = None, parent_versions: tuple[str, ...] = (),
    snapshot: SnapshotClaim | None = None, _budget: _Budget | None = None,
    _version_id: str | None = None,
    publication_check: Callable[[], None] | None = None,
) -> PreparedContainerSet:
    """Seal every file segment and publish only a fully checked volume set."""
    _require_policy(policy)
    _require_boundary(policy.boundary)
    _require_boundary(output_dir)
    _require_fresh_policy(policy)
    if publication_check is not None and not callable(publication_check):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    def final_check():
        _current_authorities(trust_state_path, policy, include_controller=False)
        _require_fresh_policy(policy)
        if publication_check is not None:
            publication_check()
    try:
        final_check()
        if (policy.origin_trust_store_id, policy.origin_trust_revision,
                policy.origin_governance_epoch) != (
                    policy.trust_store_id, policy.trust_revision, policy.governance_epoch):
            raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
        logical_policy = _logical_policy(policy)
        result = prepare_volumes(
            files, namespace=namespace, logical_policy=logical_policy,
            output_dir=output_dir, gnupg_home=gnupg_home,
            passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
            limits=limits, artifact_id=artifact_id, parent_versions=parent_versions,
            snapshot=snapshot, _budget=_budget, final_check=final_check, _version_id=_version_id,
        )
        return result
    except ArtifactError:
        raise
    except (UnsupportedContainerVersion, UnsupportedProtectedSchemaVersion,
            UnsupportedRequiredFeature):
        raise ArtifactError(ArtifactErrorCode.UNSUPPORTED) from None
    except trust_state.TrustStateError:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None
    except Exception:
        raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT) from None


def _policy_logical(logical: dict[str, Any], policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
                    *, historical: bool, evaluation_time: int | None = None,
                    limits: ContainerLimits = ContainerLimits()) -> str:
    commitment = logical_commitment(logical, limits=limits)
    if (not historical and policy.require_worker_snapshots and 'snapshot' in logical
            and 'worker' not in logical['snapshot']):
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
    origin = logical["origin"]
    if (origin != {"store_id": policy.origin_trust_store_id,
                   "revision": policy.origin_trust_revision,
                   "epoch": policy.origin_governance_epoch,
                   "fingerprint": policy.origin_fingerprint}
            or logical["audience"] != list(policy.audience)
            or logical["scope"] != list(policy.scope)
            or logical["purpose"] != policy.purpose):
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
    if isinstance(policy, ArtifactPolicy) and (
        logical["content_revision"] != policy.content_revision
        or logical["created_at"] != policy.created_at
        or logical["expires_at"] != policy.expires_at
    ):
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
    if isinstance(policy, ArtifactDiscoveryPolicy):
        if historical:
            raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
        now = _clock() if evaluation_time is None else evaluation_time
        if not (policy.admit_from <= logical["created_at"] <= now < policy.admit_until
                and now - logical["created_at"] <= policy.max_content_age_seconds
                and logical["expires_at"] - logical["created_at"]
                    <= policy.max_content_lifetime_seconds):
            raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
    now = _clock() if evaluation_time is None else evaluation_time
    if not historical and not logical["created_at"] <= now < logical["expires_at"]:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
    return commitment


def container_admission_policy_sha256(
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
) -> str:
    """Stable digest for local cache binding; it never grants admission."""
    _require_policy(policy)
    value: dict[str, Any] = {
        "mode": "discovery" if isinstance(policy, ArtifactDiscoveryPolicy) else "exact",
        "store_id": policy.trust_store_id, "revision": policy.trust_revision,
        "epoch": policy.governance_epoch, "scope": list(policy.scope),
        "origin": [policy.origin_trust_store_id, policy.origin_trust_revision,
                   policy.origin_governance_epoch, policy.origin_fingerprint],
        "audience": list(policy.audience), "purpose": policy.purpose,
        "controller": policy.controller_fingerprint,
    }
    if policy.require_worker_snapshots:
        value['require_worker_snapshots'] = True
    if isinstance(policy, ArtifactDiscoveryPolicy):
        value.update(_discovery_bounds(policy))
    else:
        value.update({"content_revision": policy.content_revision,
                      "created_at": policy.created_at, "expires_at": policy.expires_at})
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _container_key(key: Any, logical: dict[str, Any], volume_id: str) -> str:
    parse_container_key(key, logical["artifact_id"], logical["version_id"], volume_id)
    return key


def _require_header_audience(header: ContainerHeader, audience: tuple[str, ...]) -> None:
    """Check legacy public hints without exposing identities in newer framing."""
    if type(header.version) is not int or header.version not in (CONTAINER_VERSION, PRIVATE_CONTAINER_VERSION):
        raise ArtifactError(ArtifactErrorCode.UNSUPPORTED)
    expected = audience if header.version == CONTAINER_VERSION else ()
    if header.recipient_hints != expected:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)


def _metadata_layers(
    header: ContainerHeader, *, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    passphrase_reference: SecretReference | None, secret_resolver: SecretResolver | None,
    historical: bool, allow_origin_unapproved: bool,
    evaluation_time: int | None,
    limits: ContainerLimits,
    budget: _Budget | None = None,
    workspace: Path | None = None,
) -> tuple[dict[str, Any], str, str, tuple[tuple[bytes, str, str], ...]]:
    """Open owner wrapper when present, then verify immutable origin claims."""
    _require_header_audience(header, policy.audience)

    def open_layer(ciphertext: bytes, signer: str, role: str) -> bytes:
        if budget is not None:
            if workspace is None:
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            budget.use_crypto()
            cipher_path = workspace / (secrets.token_hex(16) + ".inspect-cipher")
            plain_path = workspace / (secrets.token_hex(16) + ".inspect-plain")
            budget.reserve_temp(len(ciphertext))
            try:
                with cipher_path.open("xb") as handle:
                    os.chmod(cipher_path, 0o600)
                    handle.write(ciphertext)
                arguments = dict(
                    limits=budget.file_limits(plaintext_output=True),
                    authorized=True, gnupg_home=gnupg_home,
                    policy=_envelope_policy(signer, policy.audience),
                    passphrase_reference=passphrase_reference,
                    secret_resolver=secret_resolver, cancel_check=budget.cancelled,
                )
                if historical:
                    open_historical_envelope_file(
                        cipher_path, plain_path, trust_state_path=trust_state_path,
                        expected_scope=policy.scope, role=role,
                        signer_fingerprint=signer, **arguments,
                    )
                else:
                    open_envelope_file(
                        cipher_path, plain_path, local_trust=LocalTrust({signer}),
                        **arguments,
                    )
                size = plain_path.stat().st_size
                budget.reserve_temp(size)
                try:
                    budget.retain(size)
                    result = plain_path.read_bytes()
                    budget.checkpoint()
                    return result
                finally:
                    plain_path.unlink(missing_ok=True)
                    budget.release_temp(size)
            finally:
                cipher_path.unlink(missing_ok=True)
                budget.release_temp(len(ciphertext))
        if historical:
            return open_historical_envelope(
                ciphertext, trust_state_path=trust_state_path,
                expected_scope=policy.scope, role=role, signer_fingerprint=signer,
                gnupg_home=gnupg_home, policy=_envelope_policy(signer, policy.audience),
                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
            )
        return open_envelope(
            ciphertext, gnupg_home=gnupg_home,
            policy=_envelope_policy(signer, policy.audience),
            local_trust=LocalTrust({signer}),
            passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
        )

    outer: dict[str, Any] | None = None
    if policy.controller_fingerprint is not None:
        try:
            owner_plaintext = open_layer(header.encrypted_metadata,
                                         policy.controller_fingerprint, "owner")
        except Exception:
            if budget is not None:
                budget.checkpoint()
            if not allow_origin_unapproved:
                raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None
        else:
            outer = parse_canonical(owner_plaintext, maximum=16 * 1024 * 1024)
    if outer is not None:
        outer_fields = {
            "schema", "type", "origin_metadata_b64", "origin_public_header_b64",
            "origin_volume_id", "approved_volume_id", "approved_key",
            "logical_sha256", "outer_header_sha256", "padding"
        }
        if set(outer) not in (outer_fields, outer_fields | {'worker_admission'}) or outer["type"] != "approved":
            raise ArtifactRecordError()
        validate_schema(outer["schema"])
        if outer["outer_header_sha256"] != header.public_header_sha256:
            raise ArtifactRecordError()
        origin_cipher = base64.b64decode(outer["origin_metadata_b64"], validate=True)
        origin_header = base64.b64decode(outer["origin_public_header_b64"], validate=True)
        origin_volume_id = validate_opaque_id(outer["origin_volume_id"])
        volume_id = validate_opaque_id(outer["approved_volume_id"])
        origin_plaintext = open_layer(origin_cipher, policy.origin_fingerprint, "publisher")
        origin_header_hash = hashlib.sha256(origin_header).hexdigest()
        layers = ((header.encrypted_metadata, "owner", policy.controller_fingerprint),
                  (origin_cipher, "publisher", policy.origin_fingerprint))
    else:
        origin_cipher = header.encrypted_metadata
        origin_plaintext = open_layer(origin_cipher, policy.origin_fingerprint, "publisher")
        origin_header_hash = header.public_header_sha256
        volume_id = origin_volume_id = ""
        layers = ((origin_cipher, "publisher", policy.origin_fingerprint),)
    record = parse_canonical(origin_plaintext, maximum=16 * 1024 * 1024)
    if set(record) != {"schema", "type", "logical", "logical_sha256", "inventory",
                       "this_volume", "header_sha256", "padding"} or record["type"] != "origin":
        raise ArtifactRecordError()
    validate_schema(record["schema"])
    logical = record["logical"]
    if isinstance(logical, dict):
        has_snapshot = 'snapshot' in logical
        if (has_snapshot != (SNAPSHOT_FEATURE in record['schema'].get('required_features', []))
                or has_snapshot and record['schema']['minor'] < 2):
            raise ArtifactRecordError()
    from .container_delta_records import DELTA_FEATURE
    if isinstance(logical, dict) and isinstance(logical.get('files'), list):
        has_delta = any(isinstance(item, dict) and 'delta' in item for item in logical['files'])
        if has_delta != (DELTA_FEATURE in record['schema'].get('required_features', [])):
            raise ArtifactRecordError()
        if has_delta and record['schema']['minor'] < 1:
            raise ArtifactRecordError()
    commitment = _policy_logical(logical, policy, historical=historical,
                                 evaluation_time=evaluation_time, limits=limits)
    validate_worker_layers(record, outer, policy.controller_fingerprint,
                           allow_origin_unapproved=allow_origin_unapproved)
    if record["logical_sha256"] != commitment or record["header_sha256"] != origin_header_hash:
        raise ArtifactRecordError()
    validate_inventory(record["inventory"], logical, limits=limits)
    origin_volume_id = validate_opaque_id(record["this_volume"])
    if outer is None:
        volume_id = origin_volume_id
        key = next((volume["key"] for volume in record["inventory"]["volumes"]
                    if volume["volume_id"] == origin_volume_id), None)
    else:
        if (outer["origin_volume_id"] != origin_volume_id
                or outer["logical_sha256"] != commitment):
            raise ArtifactRecordError()
        key = outer["approved_key"]
    _container_key(key, logical, volume_id)
    return record, key, volume_id, layers


def inspect_container_metadata(
    source: RangeSource, *, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    expected_key: str | None = None,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    limits: ContainerLimits = ContainerLimits(),
    allow_origin_unapproved: bool = False,
    evaluation_time: int | None = None,
    _historical: bool = False,
    _budget: _Budget | None = None,
    _workspace: Path | None = None,
) -> VerifiedContainerMetadata:
    """Read metadata only; trusted runtimes may set deterministic inspection time.

    The result is observational evidence. Acceptance and release always use the
    current trusted clock, including the writer transaction's final check.
    """
    _require_policy(policy)
    if evaluation_time is not None and (type(evaluation_time) is not int or evaluation_time < 0):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    if (_budget is None) != (_workspace is None):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    try:
        if not _historical:
            _current_authorities(trust_state_path, policy,
                                 include_controller=policy.controller_fingerprint is not None)
            _require_fresh_policy(policy, evaluation_time=evaluation_time)
        else:
            _require_historical_store(trust_state_path, policy)
        selected_source = _BudgetedRangeSource(source, _budget) if _budget else source
        header = read_metadata(selected_source, limits=limits)
        record, key, volume_id, layers = _metadata_layers(
            header, policy=policy, trust_state_path=trust_state_path,
            gnupg_home=gnupg_home, passphrase_reference=passphrase_reference,
            secret_resolver=secret_resolver, historical=_historical,
            allow_origin_unapproved=allow_origin_unapproved,
            evaluation_time=evaluation_time,
            limits=limits,
            budget=_budget, workspace=_workspace,
        )
        if expected_key is not None and expected_key != key:
            raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
        origin_volume_id = record["this_volume"]
        volume_record = next((item for item in record["inventory"]["volumes"]
                              if item["volume_id"] == origin_volume_id), None)
        if volume_record is None:
            raise ArtifactRecordError()
        _container_key(volume_record["key"], record["logical"], origin_volume_id)
        if not _historical:
            _require_current_snapshot(trust_state_path, policy)
            _require_fresh_policy(policy, evaluation_time=evaluation_time)
        return VerifiedContainerMetadata(
            header=header, logical=record["logical"], inventory=record["inventory"],
            volume_record=volume_record, key=key, volume_id=volume_id,
            origin_volume_id=origin_volume_id,
            logical_commitment_sha256=record["logical_sha256"],
            admission_policy_sha256=container_admission_policy_sha256(policy),
            metadata_layers=layers,
        )
    except ArtifactError:
        raise
    except (UnsupportedContainerVersion, UnsupportedProtectedSchemaVersion,
            UnsupportedRequiredFeature):
        raise ArtifactError(ArtifactErrorCode.UNSUPPORTED) from None
    except trust_state.TrustStateError:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None
    except Exception:
        raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT) from None


def metadata_observation_unchanged(
    source: RangeSource, observed: VerifiedContainerMetadata, *,
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str],
    limits: ContainerLimits = ContainerLimits(),
    evaluation_time: int | None = None,
) -> bool:
    """Cheap cache hint after fresh authority; never authorizes payload release.

    A caller may reuse its own prior signed metadata for catalog display only.
    Acceptance and selection always reverify signatures and receipts.
    """
    _require_policy(policy)
    if not isinstance(observed, VerifiedContainerMetadata):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    if evaluation_time is not None and (type(evaluation_time) is not int or evaluation_time < 0):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    try:
        _current_authorities(trust_state_path, policy,
                             include_controller=policy.controller_fingerprint is not None)
        _require_fresh_policy(policy, evaluation_time=evaluation_time)
        if observed.admission_policy_sha256 != container_admission_policy_sha256(policy):
            return False
        _policy_logical(observed.logical, policy, historical=False,
                        evaluation_time=evaluation_time, limits=limits)
        header = read_metadata(source, limits=limits)
        return (
            header.canonical_public_header == observed.header.canonical_public_header
            and header.encrypted_metadata == observed.header.encrypted_metadata
            and header.messages_offset == observed.header.messages_offset
        )
    except ArtifactError:
        raise
    except UnsupportedContainerVersion:
        raise ArtifactError(ArtifactErrorCode.UNSUPPORTED) from None
    except trust_state.TrustStateError:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None
    except Exception:
        raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT) from None


def _revalidate_cached_container_metadata(
    source: RangeSource, observed: VerifiedContainerMetadata, *,
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str],
    expected_key: str | None, limits: ContainerLimits,
) -> bool:
    """Recheck a receipted observation under current authority without decrypting."""
    _require_policy(policy)
    if not isinstance(observed, VerifiedContainerMetadata):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    try:
        if expected_key is not None and observed.key != expected_key:
            raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
        _require_header_audience(observed.header, policy.audience)
        if (not observed.metadata_layers
                or observed.metadata_layers[0][0] != observed.header.encrypted_metadata
                or observed.payload_verified):
            raise ArtifactRecordError()
        commitment = _policy_logical(observed.logical, policy, historical=False, limits=limits)
        if commitment != observed.logical_commitment_sha256:
            raise ArtifactRecordError()
        validate_inventory(observed.inventory, observed.logical, limits=limits)
        _container_key(observed.key, observed.logical, observed.volume_id)
        volume = next((item for item in observed.inventory["volumes"]
                       if item["volume_id"] == observed.origin_volume_id), None)
        if volume is None or volume != observed.volume_record:
            raise ArtifactRecordError()
        if not metadata_observation_unchanged(
            source, observed, policy=policy,
            trust_state_path=trust_state_path, limits=limits,
        ):
            return False
        trust_state._authorize_cached_container_metadata(
            trust_state_path,
            identity_prefix=_metadata_prefix(observed),
            layers=observed.metadata_layers,
            expected_store_id=policy.trust_store_id,
            expected_revision=policy.trust_revision,
            expected_epoch=policy.governance_epoch,
            expected_scope=policy.scope,
            created_at=observed.logical["created_at"],
            expires_at=observed.logical["expires_at"],
            **_discovery_bounds(policy),
        )
        _current_authorities(trust_state_path, policy,
                             include_controller=policy.controller_fingerprint is not None)
        _require_fresh_policy(policy)
        return True
    except ArtifactError:
        raise
    except trust_state.TrustStateError:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None
    except Exception:
        raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT) from None


def _metadata_prefix(metadata: VerifiedContainerMetadata) -> str:
    logical = metadata.logical
    return ":".join((logical["artifact_id"], logical["version_id"], metadata.volume_id))


def _metadata_identity(metadata: VerifiedContainerMetadata) -> str:
    return _metadata_prefix(metadata) + ":" + ":".join(
        hashlib.sha256(layer[0]).hexdigest() for layer in metadata.metadata_layers
    )


def _selected_prefix(metadata: VerifiedContainerMetadata, message_index: int) -> str:
    return _metadata_prefix(metadata) + f":{message_index:08x}:" + metadata.logical_commitment_sha256


def _selected_identity(metadata: VerifiedContainerMetadata, message_index: int,
                       ciphertext_sha256: str) -> str:
    validate_digest(ciphertext_sha256)
    return _selected_prefix(metadata, message_index) + ":" + ":".join(
        [*(hashlib.sha256(layer[0]).hexdigest() for layer in metadata.metadata_layers),
         ciphertext_sha256]
    )


def _discovery_bounds(policy: ArtifactPolicy | ArtifactDiscoveryPolicy) -> dict[str, int]:
    if not isinstance(policy, ArtifactDiscoveryPolicy):
        return {}
    return {
        "admit_from": policy.admit_from,
        "admit_until": policy.admit_until,
        "max_content_age_seconds": policy.max_content_age_seconds,
        "max_content_lifetime_seconds": policy.max_content_lifetime_seconds,
    }


def accept_container_metadata(
    source: RangeSource, *, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    expected_key: str | None = None,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    limits: ContainerLimits = ContainerLimits(),
) -> ContainerReceipt:
    """Persist exact metadata evidence; no payload is read or released."""
    metadata = inspect_container_metadata(
        source, policy=policy, trust_state_path=trust_state_path,
        gnupg_home=gnupg_home, expected_key=expected_key,
        passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
        limits=limits,
    )
    return _accept_verified_metadata(metadata, policy=policy, trust_state_path=trust_state_path)


def _accept_verified_metadata(
    metadata: VerifiedContainerMetadata, *, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str],
) -> ContainerReceipt:
    """Record freshly inspected bytes, never a caller-provided metadata claim."""
    try:
        status, _identity = trust_state._record_accepted_container(
            trust_state_path, identity_prefix=_metadata_prefix(metadata),
            metadata_identity=None, layers=metadata.metadata_layers,
            expected_store_id=policy.trust_store_id,
            expected_revision=policy.trust_revision,
            expected_epoch=policy.governance_epoch, expected_scope=policy.scope,
            created_at=metadata.logical["created_at"], expires_at=metadata.logical["expires_at"],
            **_discovery_bounds(policy),
            release_payload=lambda: None, rollback_payload=lambda: None,
        )
        return ContainerReceipt("accept-metadata", status,
                                metadata.logical_commitment_sha256, metadata.volume_id)
    except trust_state.TrustStateError as exc:
        if "replay" in str(exc) or "receipt collision" in str(exc):
            raise ArtifactError(ArtifactErrorCode.REPLAY) from None
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None


def _copy_selected_file(source: RangeSource, metadata: VerifiedContainerMetadata,
                        message_index: int, destination: Path,
                        limits: ContainerLimits, budget: _Budget | None = None) -> str:
    if type(message_index) is not int or message_index < 0:
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    messages = metadata.volume_record["messages"]
    if message_index >= len(messages):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    descriptor = parse_descriptor(messages[message_index]["descriptor"])
    with destination.open("xb") as handle:
        os.chmod(destination, 0o600)
        writer = _CheckedWriter(handle, budget, descriptor.ciphertext_length) if budget else handle
        copied = copy_message(source, metadata.header, descriptor, writer,
                              expected_index=message_index, limits=limits)
        handle.flush()
        os.fsync(handle.fileno())
    if copied != descriptor.ciphertext_length:
        raise ArtifactRecordError()
    return descriptor.sha256


def _open_selected_file(ciphertext_path: Path, plaintext_path: Path,
                        metadata: VerifiedContainerMetadata,
                        message_index: int, *, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
                        trust_state_path: str | os.PathLike[str],
                        gnupg_home: str | os.PathLike[str],
                        passphrase_reference: SecretReference | None,
                        secret_resolver: SecretResolver | None,
                        historical: bool, file_limits: FileEnvelopeLimits,
                        budget: _Budget) -> tuple[int, int]:
    envelope_policy = _envelope_policy(policy.origin_fingerprint, policy.audience)
    if historical:
        open_historical_envelope_file(
            ciphertext_path, plaintext_path, limits=file_limits,
            authorized=True, trust_state_path=trust_state_path,
            expected_scope=policy.scope, role="publisher",
            signer_fingerprint=policy.origin_fingerprint,
            gnupg_home=gnupg_home, policy=envelope_policy,
            passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
            cancel_check=budget.cancelled,
        )
    else:
        open_envelope_file(
            ciphertext_path, plaintext_path, limits=file_limits,
            authorized=True, gnupg_home=gnupg_home, policy=envelope_policy,
            local_trust=LocalTrust({policy.origin_fingerprint}),
            passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
            cancel_check=budget.cancelled,
        )
    with AnchoredFile(plaintext_path, private=True) as opened:
        header = os.pread(opened.file, 11, 0)
        if len(header) != 11:
            raise ArtifactRecordError()
        prefix_length = struct.unpack(">I", header[7:11])[0]
        if prefix_length < 1 or prefix_length > 4096:
            raise ArtifactRecordError()
        prefix_bytes = os.pread(opened.file, prefix_length, 11)
        prefix = decode_message_prefix(header + prefix_bytes)
        start = 11 + prefix_length
        if opened.size - start != prefix["length"]:
            raise ArtifactRecordError()
        sha = hashlib.sha256()
        offset = start
        while offset < opened.size:
            budget.checkpoint()
            chunk = os.pread(opened.file, min(64 * 1024, opened.size - offset), offset)
            if not chunk:
                raise ArtifactRecordError()
            sha.update(chunk)
            offset += len(chunk)
        if not opened.unchanged() or sha.hexdigest() != prefix["plaintext_sha256"]:
            raise ArtifactRecordError()
        budget.checkpoint()
    expected = metadata.volume_record["messages"][message_index]
    if (prefix["logical_sha256"] != metadata.logical_commitment_sha256
            or any(prefix[name] != expected[name] for name in
                   ("file_id", "offset", "length", "segment_index", "segment_count",
                    "plaintext_sha256"))):
        raise ArtifactRecordError()
    return start, prefix["length"]


def _copy_verified_payload(source: AnchoredFile, *, offset: int, length: int,
                           output: ExclusiveOutput, budget: OperationBudget) -> None:
    name, descriptor = output.stage()
    try:
        position = offset
        end = offset + length
        while position < end:
            budget.checkpoint()
            chunk = os.pread(source.file, min(64 * 1024, end - position), position)
            if not chunk:
                raise ArtifactRecordError()
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short private output write")
                view = view[written:]
            position += len(chunk)
        if not source.unchanged():
            raise ArtifactRecordError()
        budget.checkpoint()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    budget.checkpoint()
    if not source.unchanged():
        raise ArtifactRecordError()
    output.publish(name)
    budget.checkpoint()
    if not source.unchanged():
        raise ArtifactRecordError()


def extract_container_message(
    source: RangeSource, message_index: int, destination_name: str, *,
    authorized: bool, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    expected_key: str | None = None,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    limits: ContainerLimits = ContainerLimits(),
    file_limits: FileEnvelopeLimits | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ContainerReceipt:
    """Reverify one selected ciphertext with a shared bounded operation."""
    return _release_container_message(
        source, message_index, destination_name, authorized=authorized, policy=policy,
        trust_state_path=trust_state_path, gnupg_home=gnupg_home, expected_key=expected_key,
        passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
        limits=limits, file_limits=file_limits, cancel_check=cancel_check, historical=False,
    )


def open_historical_container_message(
    source: RangeSource, message_index: int, destination_name: str, *,
    authorized: bool, policy: ArtifactPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    expected_key: str | None = None,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    limits: ContainerLimits = ContainerLimits(),
    file_limits: FileEnvelopeLimits | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ContainerReceipt:
    """Open only exact metadata and selected ciphertext receipted earlier."""
    if not isinstance(policy, ArtifactPolicy):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    return _release_container_message(
        source, message_index, destination_name, authorized=authorized, policy=policy,
        trust_state_path=trust_state_path, gnupg_home=gnupg_home, expected_key=expected_key,
        passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
        limits=limits, file_limits=file_limits, cancel_check=cancel_check, historical=True,
    )


def _release_container_message(
    source: RangeSource, message_index: int, destination_name: str, *,
    authorized: bool, policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str], gnupg_home: str | os.PathLike[str],
    expected_key: str | None, passphrase_reference: SecretReference | None,
    secret_resolver: SecretResolver | None, limits: ContainerLimits,
    file_limits: FileEnvelopeLimits | None, cancel_check: Callable[[], bool] | None,
    historical: bool,
) -> ContainerReceipt:
    if type(authorized) is not bool or not authorized:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
    _require_policy(policy)
    boundary = _require_boundary(policy.boundary)
    selected_name = _validate_destination_name(destination_name)
    selected_limits = file_limits if file_limits is not None else ContainerPreparationLimits().file_limits
    if (not isinstance(selected_limits, FileEnvelopeLimits)
            or not isinstance(limits, ContainerLimits)
            or type(message_index) is not int or message_index < 0
            or (cancel_check is not None and not callable(cancel_check))):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    budget = _Budget(ContainerPreparationLimits(
        file_limits=selected_limits, container_limits=limits,
        max_temporary_bytes=selected_limits.max_temporary_bytes,
        timeout_seconds=selected_limits.timeout_seconds, cancel_check=cancel_check,
        # Only metadata plaintext is retained by this counter; all live files
        # share the independently enforced temporary-storage allowance.
        max_generated_bytes=selected_limits.max_plaintext_bytes * 2,
    ))
    try:
        budget.checkpoint()
        source = _BudgetedRangeSource(source, budget)
        with tempfile.TemporaryDirectory(prefix=".lssc-selected-", dir=boundary) as temporary:
            workspace = Path(temporary)
            metadata = inspect_container_metadata(
                source, policy=policy, trust_state_path=trust_state_path,
                gnupg_home=gnupg_home, expected_key=expected_key,
                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                limits=limits, _historical=historical, _budget=budget, _workspace=workspace,
            )
            if message_index >= len(metadata.volume_record["messages"]):
                raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
            if not historical:
                _accept_verified_metadata(metadata, policy=policy, trust_state_path=trust_state_path)
            descriptor = parse_descriptor(metadata.volume_record["messages"][message_index]["descriptor"])
            if descriptor.ciphertext_length > selected_limits.max_envelope_bytes:
                raise ContainerPreparationError("selected ciphertext exceeds file limit")
            budget.reserve_temp(descriptor.ciphertext_length)
            cipher_path = workspace / "selected.cipher"
            plain_path = workspace / "verified.plain"
            digest = _copy_selected_file(source, metadata, message_index, cipher_path, limits, budget)
            if historical:
                trust_state._has_container_receipts(
                    trust_state_path, expected_scope=policy.scope,
                    metadata_identity=_metadata_identity(metadata),
                    selected_identity=_selected_identity(metadata, message_index, digest),
                    layers=(*metadata.metadata_layers,
                            (digest, "publisher", policy.origin_fingerprint)),
                )
            budget.use_crypto()
            offset, length = _open_selected_file(
                cipher_path, plain_path, metadata, message_index, policy=policy,
                trust_state_path=trust_state_path, gnupg_home=gnupg_home,
                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                historical=historical, file_limits=budget.file_limits(plaintext_output=True),
                budget=budget,
            )
            with AnchoredFile(cipher_path, private=True) as cipher_source, \
                 AnchoredFile(plain_path, private=True) as plain_source, \
                 ExclusiveOutput(boundary / selected_name) as output:
                budget.reserve_temp(plain_source.size)
                budget.reserve_temp(length)
                release_budget = OperationBudget(selected_limits, budget.cancelled)
                release_budget.deadline = min(release_budget.deadline, budget.deadline)
                def release() -> None:
                    _copy_verified_payload(plain_source, offset=offset, length=length,
                                           output=output, budget=release_budget)
                if historical:
                    release()
                    status = "opened"
                else:
                    status, _identity = trust_state._record_accepted_container(
                        trust_state_path, identity_prefix=_selected_prefix(metadata, message_index),
                        metadata_identity=_metadata_identity(metadata),
                        layers=(*metadata.metadata_layers,
                                (trust_state._ContainerCipherFile(
                                    cipher_source, digest, selected_limits.max_envelope_bytes,
                                    release_budget,
                                ), "publisher", policy.origin_fingerprint)),
                        expected_store_id=policy.trust_store_id,
                        expected_revision=policy.trust_revision,
                        expected_epoch=policy.governance_epoch, expected_scope=policy.scope,
                        created_at=metadata.logical["created_at"],
                        expires_at=metadata.logical["expires_at"], **_discovery_bounds(policy),
                        release_payload=release, rollback_payload=output.rollback_published,
                    )
                output.published_identity = None
        operation = "historical-message" if historical else "extract-message"
        return ContainerReceipt(operation, status, metadata.logical_commitment_sha256,
                                metadata.volume_id, message_index)
    except ArtifactError:
        raise
    except trust_state.TrustStateError as exc:
        if "replay" in str(exc) or "receipt collision" in str(exc):
            raise ArtifactError(ArtifactErrorCode.REPLAY) from None
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None
    except Exception:
        raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT) from None


def approve_container_distribution(
    source: RangeSource, *, output_dir: str | os.PathLike[str],
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    trust_state_path: str | os.PathLike[str],
    gnupg_home: str | os.PathLike[str], passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    limits: ContainerPreparationLimits = ContainerPreparationLimits(),
    publication_check: Callable[[], None] | None = None,
    metadata_check: Callable[[VerifiedContainerMetadata], None] | None = None,
    _budget: _Budget | None = None,
    _worker_admission=None,
) -> PreparedVolume:
    """Owner-sign new metadata while copying exact origin ciphertext streams."""
    _require_policy(policy)
    if policy.controller_fingerprint is None:
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    if (publication_check is not None and not callable(publication_check)
            or metadata_check is not None and not callable(metadata_check)):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    budget = _budget if _budget is not None else _Budget(limits)
    if not isinstance(budget, _Budget) or budget.limits != limits:
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
    initial_temporary = budget.temporary

    def current():
        budget.checkpoint()
        if _worker_admission is not None:
            from .container_worker_admission import WorkerAdmission
            if not isinstance(_worker_admission, WorkerAdmission):
                raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
            _worker_admission.check()
        _current_authorities(trust_state_path, policy, include_controller=True)
        _require_fresh_policy(policy)
        if publication_check is not None:
            publication_check()
        budget.checkpoint()
    destination = Path(output_dir)
    if not destination.is_absolute() or ".." in destination.parts:
        raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
    _require_boundary(destination)
    try:
        current()
        source = _BudgetedRangeSource(source, budget)
        with tempfile.TemporaryDirectory(prefix=".lssc-inspect-", dir=destination) as inspection:
            metadata = inspect_container_metadata(
                source, policy=policy, trust_state_path=trust_state_path,
                gnupg_home=gnupg_home, limits=limits.container_limits,
                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                allow_origin_unapproved=True, _budget=budget,
                _workspace=Path(inspection),
            )
        if len(metadata.metadata_layers) != 1:
            raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)
        if metadata_check is not None:
            metadata_check(metadata)
        has_worker = 'worker' in metadata.logical.get('snapshot', {})
        if has_worker != (_worker_admission is not None):
            raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
        admission = _worker_admission.receipt(metadata, policy, budget) if has_worker else None
        current()
        descriptors = scan_message_descriptors(
            source, header=metadata.header, limits=limits.container_limits,
        )
        if descriptors != tuple(parse_descriptor(item["descriptor"])
                                for item in metadata.volume_record["messages"]):
            raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT)
        with tempfile.TemporaryDirectory(prefix=".lssc-approve-", dir=destination) as temporary:
            workspace = Path(temporary)
            messages: list[Path] = []
            for index, descriptor in enumerate(descriptors):
                budget.checkpoint()
                message_path = workspace / (f"{index:08x}.cipher")
                budget.reserve_temp(descriptor.ciphertext_length)
                _copy_selected_file(source, metadata, index, message_path,
                                    limits.container_limits, budget)
                messages.append(message_path)
            fields = metadata.key.split("/")
            namespace = ContainerNamespace(fields[2], fields[3], fields[4])
            approved_id = secrets.token_hex(16)
            approved_key = namespace.key(metadata.logical["artifact_id"],
                                         metadata.logical["version_id"], approved_id)
            wrapper = {
                "schema": schema_envelope(worker_snapshot=has_worker), "type": "approved",
                **({'worker_admission': admission} if has_worker else {}),
                "origin_metadata_b64": base64.b64encode(metadata.header.encrypted_metadata).decode("ascii"),
                "origin_public_header_b64": base64.b64encode(
                    metadata.header.canonical_public_header).decode("ascii"),
                "origin_volume_id": metadata.origin_volume_id,
                "approved_volume_id": approved_id, "approved_key": approved_key,
                "logical_sha256": metadata.logical_commitment_sha256,
                "outer_header_sha256": "0" * 64, "padding": "",
            }
            target = len(canonical_bytes(wrapper))
            if target > limits.container_limits.max_metadata_bytes:
                raise ContainerPreparationError("approved metadata exceeds configured limit")

            def seal_wrapper() -> bytes:
                current()
                return _seal(
                    _pad_metadata(wrapper, target), home=gnupg_home,
                    signer=policy.controller_fingerprint, audience=policy.audience,
                    passphrase_reference=passphrase_reference,
                    secret_resolver=secret_resolver, budget=budget, workspace=workspace,
                )

            final_cipher: bytes | None = None
            for _attempt in range(limits.max_plan_passes):
                budget.checkpoint()
                wrapper["outer_header_sha256"] = "0" * 64
                provisional = seal_wrapper()
                first = _write_stream(_DiscardWriter(), provisional, messages,
                                      limits.container_limits, budget)
                wrapper["outer_header_sha256"] = first.public_header_sha256
                candidate = seal_wrapper()
                if len(candidate) == len(provisional):
                    final_cipher = candidate
                    break
            if final_cipher is None:
                raise ContainerPreparationError(
                    "approved metadata length did not converge within plan budget"
                )
            measured = _DiscardWriter()
            expected = _write_stream(measured, final_cipher, messages,
                                     limits.container_limits, budget)
            if (expected.public_header_sha256 != wrapper["outer_header_sha256"]
                    or expected.message_descriptors != descriptors):
                raise ContainerPreparationError("approved framing differs from origin descriptors")
            ceiling = min(limits.packing_limits.target_bytes * (1 + limits.packing_limits.tolerance),
                          limits.packing_limits.hard_ceiling_bytes or float("inf"))
            if measured.count > ceiling:
                raise ContainerPreparationError("approved container exceeds actual-size ceiling")
            budget.reserve_temp(expected.serialized_length)
            budget.retain(expected.serialized_length)
            with ExclusiveOutput(destination / (approved_id + ".lssc")) as output:
                name, descriptor = output.stage()
                with os.fdopen(descriptor, "w+b", closefd=True) as handle:
                    result = _write_stream(handle, final_cipher, messages,
                                           limits.container_limits,
                                           budget, expected.serialized_length)
                    handle.flush()
                    os.fsync(handle.fileno())
                    if result != expected or handle.tell() > ceiling:
                        raise ContainerPreparationError("approved final size differs from measurement")
                    handle.seek(0)
                    digest = hashlib.sha256()
                    while chunk := handle.read(64 * 1024):
                        budget.checkpoint()
                        digest.update(chunk)
                current()
                output.publish(name)
                current()
                output.published_identity = None
                budget.release_temp(expected.serialized_length)
                return PreparedVolume(
                    key=approved_key, path=output.path,
                    serialized_length=result.serialized_length,
                    transport_sha256=digest.hexdigest(),
                    volume_id=approved_id, message_count=len(messages),
                )
    except ArtifactError:
        raise
    except trust_state.TrustStateError:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED) from None
    except Exception:
        raise ArtifactError(ArtifactErrorCode.INVALID_ARTIFACT) from None
    finally:
        budget.release_temp(budget.temporary - initial_temporary)


def _current_authorities(
    path: str | os.PathLike[str],
    policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
    *,
    include_controller: bool,
) -> tuple[trust_state.TrustSnapshot, trust_state.Authority, trust_state.Authority | None]:
    snapshot = trust_state.load_trust_state(path, expected_scope=policy.scope)
    _match_snapshot(snapshot, policy)
    if snapshot.publisher is None or snapshot.publisher.fingerprint != policy.origin_fingerprint:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
    publisher = trust_state.authorize_inbound_peer(
        path, role="publisher", fingerprint=policy.origin_fingerprint,
        expected_scope=policy.scope,
    )
    _match_authority(publisher, policy)
    owner: trust_state.Authority | None = None
    if include_controller:
        if policy.controller_fingerprint is None or snapshot.owner is None:
            raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
        if snapshot.owner.fingerprint != policy.controller_fingerprint:
            raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
        owner = trust_state.authorize_inbound_peer(
            path, role="owner", fingerprint=policy.controller_fingerprint,
            expected_scope=policy.scope,
        )
        _match_authority(owner, policy)
    return snapshot, publisher, owner


def _require_current_snapshot(path: str | os.PathLike[str],
                              policy: ArtifactPolicy | ArtifactDiscoveryPolicy) -> None:
    snapshot = trust_state.load_trust_state(path, expected_scope=policy.scope)
    _match_snapshot(snapshot, policy)
    if (
        snapshot.publisher is None
        or snapshot.publisher.fingerprint != policy.origin_fingerprint
        or (
            policy.controller_fingerprint is not None
            and (snapshot.owner is None or snapshot.owner.fingerprint != policy.controller_fingerprint)
        )
    ):
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)


def _require_historical_store(path: str | os.PathLike[str],
                              policy: ArtifactPolicy | ArtifactDiscoveryPolicy) -> None:
    snapshot = trust_state.load_trust_state(path, expected_scope=policy.scope)
    if snapshot.store_id != policy.trust_store_id or snapshot.scope != policy.scope:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)


def _match_snapshot(snapshot: trust_state.TrustSnapshot,
                    policy: ArtifactPolicy | ArtifactDiscoveryPolicy) -> None:
    if (
        snapshot.store_id != policy.trust_store_id
        or snapshot.revision != policy.trust_revision
        or snapshot.epoch != policy.governance_epoch
        or snapshot.scope != policy.scope
    ):
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)


def _match_authority(authority: trust_state.Authority,
                     policy: ArtifactPolicy | ArtifactDiscoveryPolicy) -> None:
    if (
        authority.store_id != policy.trust_store_id
        or authority.revision != policy.trust_revision
        or authority.epoch != policy.governance_epoch
        or authority.scope != policy.scope
    ):
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)


def _envelope_policy(signer: str, audience: tuple[str, ...]) -> EnvelopePolicy:
    return EnvelopePolicy(expected_signers=(signer,), expected_recipients=audience)


def _require_policy(policy: ArtifactPolicy | ArtifactDiscoveryPolicy) -> None:
    if not isinstance(policy, (ArtifactPolicy, ArtifactDiscoveryPolicy)):
        raise ArtifactError(ArtifactErrorCode.INVALID_INPUT)


def _require_boundary(value: str | os.PathLike[str]) -> Path:
    try:
        if not isinstance(value, (str, os.PathLike)):
            raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
        raw = os.fspath(value)
        if type(raw) is not str or "\x00" in raw:
            raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
        path = Path(raw)
        if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
            raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
        current = Path(path.anchor)
        parts = path.parts[1:]
        if not parts:
            raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
        for index, part in enumerate(parts):
            current = current / part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
                raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
            mode = stat.S_IMODE(info.st_mode)
            is_final = index == len(parts) - 1
            if is_final:
                if info.st_uid != os.geteuid() or mode & 0o077:
                    raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
            elif mode & 0o022 and not (
                info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            ):
                raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
        return path
    except ArtifactError:
        raise
    except Exception:
        raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY) from None


def _validate_destination_name(name: str) -> str:
    try:
        if type(name) is not str:
            raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
        encoded = name.encode("utf-8")
    except ArtifactError:
        raise
    except Exception:
        raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY) from None
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or len(encoded) > 255
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ArtifactError(ArtifactErrorCode.UNSAFE_BOUNDARY)
    return name


def _clock() -> int:
    return int(time.time())


def _require_fresh_policy(policy: ArtifactPolicy | ArtifactDiscoveryPolicy,
                          *, evaluation_time: int | None = None) -> None:
    now = _clock() if evaluation_time is None else evaluation_time
    if isinstance(policy, ArtifactDiscoveryPolicy):
        fresh = policy.admit_from <= now < policy.admit_until
    else:
        fresh = policy.created_at <= now < policy.expires_at
    if not fresh:
        raise ArtifactError(ArtifactErrorCode.AUTHORIZATION_DENIED)
