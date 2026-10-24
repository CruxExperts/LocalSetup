"""Encrypted, local-only ledger storage for secure-container observations.

The store persists immutable signed/encrypted snapshots and a tiny plaintext
HEAD containing only the schema version and opaque ciphertext digest. Every
catalog claim, source locator, verification observation, and transition remains
inside authenticated ciphertext.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from ..agent.runtime_lock import runtime_use
from ..paths import global_layout
from . import trust_state
from .artifact_records import validate_opaque_id
from .container_storage import ensure_private_directory, read_private_file, write_private_file
from .envelope_io import _open_parent, _parent_chain_matches
from .contracts import EnvelopePolicy, LocalTrust, normalize_fingerprint
from .envelope import MAX_PAYLOAD_BYTES, seal_envelope
from .historical import open_historical_envelope
from .secrets import SecretReference, SecretResolver


CATALOG_SCHEMA = "localsetup.secure-container.catalog"
CATALOG_HEAD_SCHEMA = "localsetup.secure-container.catalog-head"
CATALOG_MAJOR = 1
CATALOG_MINOR = 1
ADMISSION_BATCH_FEATURE = "independent-admission-batch-v1"
MAX_PLAINTEXT_BYTES = 8 * 1024 * 1024
MAX_CIPHERTEXT_BYTES = 16 * 1024 * 1024
MAX_TRANSACTION_BYTES = 1024 * 1024
MAX_ENTITIES = 10_000
MAX_JOURNAL_ENTRIES = 64
MAX_RETAINED_CIPHERTEXT_BYTES = 512 * 1024 * 1024
MAX_RETAINED_OBJECTS = 4096
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 250_000
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_HEAD_NAME = "HEAD"
_OBJECT_DIRECTORY = "objects"
_SNAPSHOT_TOKEN = object()


class _SnapshotProof:
    """Immutable capture issued only by authenticated catalog loading."""

    __slots__ = ("_digest",)

    def __init__(self, issuer: object, digest: str) -> None:
        if issuer is not _SNAPSHOT_TOKEN:
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED)
        object.__setattr__(self, "_digest", digest)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("catalog proof is immutable")

    def matches(self, digest: str) -> bool:
        return self._digest == digest


class CatalogErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    BUSY = "BUSY"
    CONFLICT = "CONFLICT"
    BOUNDED = "BOUNDED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    CORRUPT = "CORRUPT"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"


class CatalogError(RuntimeError):
    """Redacted, stable failure from local catalog storage or authority."""

    def __init__(
        self,
        code: CatalogErrorCode,
        *,
        candidate_digest: str | None = None,
        current_head: str | None = None,
    ) -> None:
        self.code = CatalogErrorCode(code)
        self.candidate_digest = candidate_digest
        self.current_head = current_head
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class CatalogLimits:
    """Hard, configurable upper bounds for catalog memory and retained bytes."""

    checkpoint_bytes: int = MAX_PLAINTEXT_BYTES
    transaction_bytes: int = MAX_TRANSACTION_BYTES
    max_entities: int = MAX_ENTITIES
    max_journal_entries: int = MAX_JOURNAL_ENTRIES
    max_retained_ciphertext_bytes: int = MAX_RETAINED_CIPHERTEXT_BYTES
    max_retained_objects: int = MAX_RETAINED_OBJECTS
    lock_timeout: float = 0.0

    def __post_init__(self) -> None:
        integer_limits = (
            ("checkpoint_bytes", self.checkpoint_bytes, MAX_PLAINTEXT_BYTES),
            ("transaction_bytes", self.transaction_bytes, MAX_TRANSACTION_BYTES),
            ("max_entities", self.max_entities, MAX_ENTITIES),
            ("max_journal_entries", self.max_journal_entries, MAX_JOURNAL_ENTRIES),
            ("max_retained_ciphertext_bytes", self.max_retained_ciphertext_bytes,
             MAX_RETAINED_CIPHERTEXT_BYTES),
            ("max_retained_objects", self.max_retained_objects, MAX_RETAINED_OBJECTS),
        )
        if any(type(value) is not int or value < 1 or value > ceiling
               for _name, value, ceiling in integer_limits):
            raise ValueError("catalog limits exceed the supported bounds")
        if (type(self.lock_timeout) not in (int, float)
                or not math.isfinite(self.lock_timeout)
                or self.lock_timeout < 0 or self.lock_timeout > 1):
            raise ValueError("catalog lock timeout must be between zero and one second")


@dataclass(frozen=True, slots=True)
class CatalogContext:
    """Operator-supplied identity, trust, audience, and secret selection.

    ``signer_fingerprint`` must equal the currently active trust-store role for
    new writes. Retired signer pins are supplied separately and are considered
    only through the exact-ciphertext historical receipt API.
    """

    catalog_id: str
    trust_state_path: str | os.PathLike[str]
    trust_store_id: str
    expected_scope: tuple[str, ...]
    role: str
    trust_revision: int
    governance_epoch: int
    signer_fingerprint: str
    recipient_fingerprints: tuple[str, ...]
    gnupg_home: str | os.PathLike[str]
    passphrase_reference: SecretReference
    secret_resolver: SecretResolver
    historical_signer_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            validate_opaque_id(self.catalog_id)
            if (type(self.trust_store_id) is not str
                    or _DIGEST_RE.fullmatch(self.trust_store_id) is None):
                raise ValueError
            if (type(self.expected_scope) is not tuple or not self.expected_scope
                    or len(self.expected_scope) > 32
                    or self.expected_scope != tuple(sorted(set(self.expected_scope)))):
                raise ValueError
            if any(type(item) is not str or not item or len(item.encode("utf-8")) > 128
                   for item in self.expected_scope):
                raise ValueError
            if self.role not in {"owner", "publisher"}:
                raise ValueError
            if (type(self.trust_revision) is not int or self.trust_revision < 1
                    or type(self.governance_epoch) is not int or self.governance_epoch < 1):
                raise ValueError
            signer = normalize_fingerprint(self.signer_fingerprint)
            if type(self.recipient_fingerprints) is not tuple or not self.recipient_fingerprints:
                raise ValueError
            recipients = tuple(sorted({normalize_fingerprint(item)
                                       for item in self.recipient_fingerprints}))
            if len(recipients) != len(self.recipient_fingerprints) or len(recipients) > 64:
                raise ValueError
            if (type(self.historical_signer_fingerprints) is not tuple
                    or len(self.historical_signer_fingerprints) > 8):
                raise ValueError
            historical = tuple(sorted({normalize_fingerprint(item)
                                       for item in self.historical_signer_fingerprints}))
            if len(historical) != len(self.historical_signer_fingerprints):
                raise ValueError
            if not isinstance(self.passphrase_reference, SecretReference):
                raise ValueError
            if not isinstance(self.secret_resolver, SecretResolver):
                raise ValueError
            if not isinstance(self.trust_state_path, (str, os.PathLike)):
                raise ValueError
            if not isinstance(self.gnupg_home, (str, os.PathLike)):
                raise ValueError
            object.__setattr__(self, "signer_fingerprint", signer)
            object.__setattr__(self, "recipient_fingerprints", recipients)
            object.__setattr__(self, "historical_signer_fingerprints", historical)
        except Exception:
            raise CatalogError(CatalogErrorCode.INVALID_INPUT) from None

    @property
    def allowed_signers(self) -> tuple[str, ...]:
        return tuple(sorted({self.signer_fingerprint,
                             *self.historical_signer_fingerprints}))

    def stable_binding(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "trust_store_id": self.trust_store_id,
            "scope": list(self.expected_scope),
            "role": self.role,
            "recipients": list(self.recipient_fingerprints),
        }


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """One fully authenticated catalog state and its current opaque head."""

    head: str | None
    sequence: int
    catalog: dict[str, Any]
    chain_depth: int
    _proof: object = field(default=None, repr=False, compare=False)
    required_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.required_features) is not tuple
                or any(item != ADMISSION_BATCH_FEATURE for item in self.required_features)
                or len(self.required_features) > 1):
            raise CatalogError(CatalogErrorCode.INVALID_INPUT)
        if self._proof is not None and (
                type(self._proof) is not _SnapshotProof
                or not self._proof.matches(_snapshot_capture_sha256(
                    self.head, self.sequence, self.catalog, self.chain_depth, self.required_features,
                ))):
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED)


def global_catalog_root(home: Path, catalog_id: str) -> Path:
    """Resolve the private per-user catalog root without creating or enrolling it."""
    try:
        validate_opaque_id(catalog_id)
    except Exception:
        raise CatalogError(CatalogErrorCode.INVALID_INPUT) from None
    return global_layout(home).state_root / "secure-containers" / catalog_id


class CatalogStore:
    """Immutable encrypted object store with lock, CAS, recovery, and migration."""

    def __init__(
        self, root: str | os.PathLike[str], catalog_id: str,
        *, limits: CatalogLimits = CatalogLimits(),
    ) -> None:
        try:
            path = Path(root)
            validate_opaque_id(catalog_id)
            if (not path.is_absolute() or not path.name or ".." in path.parts
                    or not isinstance(limits, CatalogLimits)):
                raise ValueError
        except Exception:
            raise CatalogError(CatalogErrorCode.INVALID_INPUT) from None
        self.root = path
        self.catalog_id = catalog_id
        self.limits = limits
        self.objects = self.root / _OBJECT_DIRECTORY

    def load(self, context: CatalogContext) -> CatalogSnapshot:
        """Load the exact HEAD and bounded authenticated predecessor chain."""
        self._validate_context(context)
        self._ensure_directories()
        try:
            with runtime_use(self.root, timeout=self.limits.lock_timeout, create=True):
                return self._load_unlocked(context)
        except TimeoutError:
            raise CatalogError(CatalogErrorCode.BUSY) from None
        except CatalogError:
            raise
        except Exception:
            raise CatalogError(CatalogErrorCode.UNAVAILABLE) from None

    def commit(
        self, expected_head: str | None, transition: object,
        context: CatalogContext,
    ) -> CatalogSnapshot:
        """Apply one deterministic reconciliation transition through CAS.

        A candidate encrypted object and exact trust receipt are durable before
        the atomic HEAD switch. Failures preserve HEAD and expose the candidate
        digest for an explicit, exact-byte recovery attempt.
        """
        self._validate_context(context)
        self._validate_head_token(expected_head)
        self._ensure_directories()
        try:
            with runtime_use(self.root, exclusive=True,
                             timeout=self.limits.lock_timeout, create=True):
                current = self._load_unlocked(context)
                if current.head != expected_head:
                    raise CatalogError(CatalogErrorCode.CONFLICT,
                                       current_head=current.head)
                if current.chain_depth >= self.limits.max_journal_entries:
                    raise CatalogError(CatalogErrorCode.BOUNDED,
                                       current_head=current.head)
                transition_record = self._transition_record(transition)
                encoded_transition = _canonical_json(transition_record)
                if len(encoded_transition) > self.limits.transaction_bytes:
                    raise CatalogError(CatalogErrorCode.BOUNDED,
                                       current_head=current.head)
                from .container_reconcile import ReconcileBatch, apply_reconcile_batch, apply_reconcile_transition

                next_catalog = (apply_reconcile_batch(current.catalog, transition.transitions,
                                  max_entities=self.limits.max_entities)
                                if isinstance(transition, ReconcileBatch) else
                                apply_reconcile_transition(current.catalog, transition,
                                  max_entities=self.limits.max_entities))
                if _canonical_json(next_catalog) == _canonical_json(current.catalog):
                    return current
                state = self._next_state(
                    current, context, next_catalog, transition_record,
                    previous_head=current.head, checkpoint_anchor=None,
                    migration_anchor=None,
                )
                return self._publish_unlocked(
                    state, context, expected_head=current.head,
                )
        except TimeoutError:
            raise CatalogError(CatalogErrorCode.BUSY) from None
        except CatalogError:
            raise
        except Exception as exc:
            from .container_reconcile import ReconcileError
            if isinstance(exc, ReconcileError):
                code = (CatalogErrorCode.BOUNDED
                        if exc.code == "BOUNDED"
                        else CatalogErrorCode.INVALID_INPUT)
                raise CatalogError(code, current_head=expected_head) from None
            raise CatalogError(CatalogErrorCode.UNAVAILABLE) from None

    def checkpoint(
        self, expected_head: str, context: CatalogContext,
    ) -> CatalogSnapshot:
        """Start a bounded authenticated chain segment after validating it fully."""
        self._validate_head_token(expected_head)
        self._validate_context(context)
        self._ensure_directories()
        try:
            with runtime_use(self.root, exclusive=True,
                             timeout=self.limits.lock_timeout, create=True):
                current = self._load_unlocked(context)
                if current.head != expected_head:
                    raise CatalogError(CatalogErrorCode.CONFLICT,
                                       current_head=current.head)
                if current.head is None:
                    return current
                self._require_fresh_authority(context)
                state = self._next_state(
                    current, context, current.catalog,
                    {"kind": "checkpoint", "from": current.head},
                    previous_head=None, checkpoint_anchor=current.head,
                    migration_anchor=None,
                )
                return self._publish_unlocked(
                    state, context, expected_head=current.head,
                )
        except TimeoutError:
            raise CatalogError(CatalogErrorCode.BUSY) from None
        except CatalogError:
            raise
        except Exception:
            raise CatalogError(CatalogErrorCode.UNAVAILABLE) from None

    def migrate(
        self, expected_head: str, *, old_context: CatalogContext,
        new_context: CatalogContext,
        transform: Any = None,
    ) -> CatalogSnapshot:
        """Write a separately encrypted checkpoint and retain every old object.

        ``transform`` is an explicit schema migration supplied by trusted code;
        it receives a private in-memory catalog value and must return a bounded
        JSON object. No migration is inferred from filenames or HEAD contents.
        """
        self._validate_head_token(expected_head)
        self._validate_context(old_context)
        self._validate_context(new_context)
        if old_context.catalog_id != new_context.catalog_id:
            raise CatalogError(CatalogErrorCode.INVALID_INPUT)
        self._ensure_directories()
        try:
            with runtime_use(self.root, exclusive=True,
                             timeout=self.limits.lock_timeout, create=True):
                current = self._load_unlocked(old_context)
                if current.head != expected_head:
                    raise CatalogError(CatalogErrorCode.CONFLICT,
                                       current_head=current.head)
                if current.head is None:
                    raise CatalogError(CatalogErrorCode.UNAVAILABLE)
                self._require_fresh_authority(new_context)
                migrated = current.catalog
                if transform is not None:
                    if not callable(transform):
                        raise CatalogError(CatalogErrorCode.INVALID_INPUT)
                    migrated = transform(_json_copy(current.catalog))
                if type(migrated) is not dict:
                    raise CatalogError(CatalogErrorCode.CORRUPT)
                _validate_catalog(migrated, self.limits.max_entities)
                migration = {
                    "kind": "migration",
                    "from": current.head,
                    "old_binding": old_context.stable_binding(),
                    "new_binding": new_context.stable_binding(),
                }
                state = self._next_state(
                    current, new_context, migrated, migration,
                    previous_head=None, checkpoint_anchor=None,
                    migration_anchor=current.head,
                )
                return self._publish_unlocked(
                    state, new_context, expected_head=current.head,
                )
        except TimeoutError:
            raise CatalogError(CatalogErrorCode.BUSY) from None
        except CatalogError:
            raise
        except Exception:
            raise CatalogError(CatalogErrorCode.UNAVAILABLE) from None

    def recover_candidate(
        self, candidate_digest: str, *, expected_head: str | None,
        context: CatalogContext, previous_context: CatalogContext | None = None,
    ) -> CatalogSnapshot:
        """Explicitly advance HEAD to one exact, receipted orphan candidate."""
        self._validate_head_token(expected_head)
        if type(candidate_digest) is not str or _DIGEST_RE.fullmatch(candidate_digest) is None:
            raise CatalogError(CatalogErrorCode.INVALID_INPUT)
        self._validate_context(context)
        if previous_context is not None:
            self._validate_context(previous_context)
        self._ensure_directories()
        try:
            with runtime_use(self.root, exclusive=True,
                             timeout=self.limits.lock_timeout, create=True):
                record = self._read_object(candidate_digest)
                state = self._open_record(record, context)
                current = self._load_unlocked(previous_context or context,
                                              recover_candidate=candidate_digest)
                if current.head != expected_head:
                    raise CatalogError(CatalogErrorCode.CONFLICT,
                                       candidate_digest=candidate_digest,
                                       current_head=current.head)
                anchored = state.get("checkpoint_anchor") == expected_head or (
                    state.get("migration_anchor") == expected_head
                )
                follows = state.get("previous_head") == expected_head
                if ((not follows and not anchored)
                        or state.get("sequence") != current.sequence + 1):
                    raise CatalogError(CatalogErrorCode.CONFLICT,
                                       candidate_digest=candidate_digest,
                                       current_head=current.head)
                self._require_stable_binding(state, context)
                self._validate_state(state)
                self._require_fresh_authority(context)
                if self._read_head() != candidate_digest:
                    self._replace_head(candidate_digest)
                self._write_anchor(context, "committed", state["sequence"],
                                   expected_head, candidate_digest)
                return self._load_unlocked(context)
        except TimeoutError:
            raise CatalogError(CatalogErrorCode.BUSY) from None
        except CatalogError:
            raise
        except Exception:
            try:
                observed_head = self._read_head()
            except Exception:
                observed_head = expected_head
            raise CatalogError(CatalogErrorCode.UNAVAILABLE,
                               candidate_digest=candidate_digest,
                               current_head=observed_head) from None

    def _validate_context(self, context: CatalogContext) -> None:
        if not isinstance(context, CatalogContext) or context.catalog_id != self.catalog_id:
            raise CatalogError(CatalogErrorCode.INVALID_INPUT)

    @staticmethod
    def _validate_head_token(head: str | None) -> None:
        if head is not None and (type(head) is not str or _DIGEST_RE.fullmatch(head) is None):
            raise CatalogError(CatalogErrorCode.INVALID_INPUT)

    def _ensure_directories(self) -> None:
        try:
            ensure_private_directory(self.root, create=True)
            ensure_private_directory(self.objects, create=True)
        except Exception:
            raise CatalogError(CatalogErrorCode.UNAVAILABLE) from None

    def _read_head(self) -> str | None:
        path = self.root / _HEAD_NAME
        try:
            payload = _read_regular_file(path, maximum=1024, minimum=1)
        except FileNotFoundError:
            return None
        payload = _parse_canonical_json(payload, max_bytes=1024, max_depth=4, max_nodes=16)
        if (set(payload) != {"schema", "major", "cipher_sha256"}
                or payload.get("schema") != CATALOG_HEAD_SCHEMA
                or payload.get("major") != CATALOG_MAJOR
                or type(payload.get("major")) is not int
                or type(payload.get("cipher_sha256")) is not str
                or _DIGEST_RE.fullmatch(payload["cipher_sha256"]) is None):
            raise CatalogError(CatalogErrorCode.CORRUPT)
        return payload["cipher_sha256"]

    def _write_head(self, digest: str) -> None:
        payload = _canonical_json({
            "schema": CATALOG_HEAD_SCHEMA,
            "major": CATALOG_MAJOR,
            "cipher_sha256": digest,
        })
        write_private_file(self.root / _HEAD_NAME, payload, replace=True)

    def _replace_head(self, digest: str) -> None:
        self._write_head(digest)

    def _inventory_objects(self) -> tuple[int, int]:
        total = 0
        count = 0
        directory = -1
        try:
            directory, _, selected, chain = _open_parent(self.objects / ".probe", private=True)
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > self.limits.max_retained_objects:
                        raise CatalogError(CatalogErrorCode.BOUNDED)
                    info = entry.stat(follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or info.st_mode & 0o077 or info.st_nlink != 1
                            or _DIGEST_RE.fullmatch(entry.name.removesuffix(".blob")) is None
                            or not entry.name.endswith(".blob")):
                        raise CatalogError(CatalogErrorCode.CORRUPT)
                    total += info.st_size
                    if total > self.limits.max_retained_ciphertext_bytes:
                        raise CatalogError(CatalogErrorCode.BOUNDED)
            if not _parent_chain_matches(selected, directory, chain, private=True):
                raise CatalogError(CatalogErrorCode.UNAVAILABLE)
        except CatalogError:
            raise
        except Exception:
            raise CatalogError(CatalogErrorCode.UNAVAILABLE) from None
        finally:
            if directory >= 0:
                os.close(directory)
        return total, count

    def _read_object(self, digest: str) -> bytes:
        path = self.objects / f"{digest}.blob"
        try:
            ciphertext = _read_regular_file(
                path, maximum=MAX_CIPHERTEXT_BYTES, minimum=1,
            )
        except FileNotFoundError:
            raise CatalogError(CatalogErrorCode.CORRUPT) from None
        if hashlib.sha256(ciphertext).hexdigest() != digest:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        return ciphertext

    def _write_object(self, ciphertext: bytes) -> str:
        if type(ciphertext) is not bytes or not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
            raise CatalogError(CatalogErrorCode.BOUNDED)
        digest = hashlib.sha256(ciphertext).hexdigest()
        total, count = self._inventory_objects()
        path = self.objects / f"{digest}.blob"
        if path.exists():
            prior = self._read_object(digest)
            if prior != ciphertext:
                raise CatalogError(CatalogErrorCode.CORRUPT,
                                   candidate_digest=digest)
            return digest
        if count + 1 > self.limits.max_retained_objects or (
            total + len(ciphertext) > self.limits.max_retained_ciphertext_bytes
        ):
            raise CatalogError(CatalogErrorCode.BOUNDED,
                               candidate_digest=digest)
        try:
            write_private_file(path, ciphertext, replace=False)
        except FileExistsError:
            prior = self._read_object(digest)
            if prior != ciphertext:
                raise CatalogError(CatalogErrorCode.CORRUPT,
                                   candidate_digest=digest)
        return digest

    def _load_unlocked(self, context: CatalogContext, *,
                       recover_candidate: str | None = None) -> CatalogSnapshot:
        self._inventory_objects()
        observed_head = self._read_head()
        anchor = self._anchor(context)
        if recover_candidate is None:
            if observed_head != anchor.committed_head:
                code = (CatalogErrorCode.UNAVAILABLE
                        if observed_head == anchor.pending_head
                        else CatalogErrorCode.CORRUPT)
                raise CatalogError(code, candidate_digest=anchor.pending_head,
                                   current_head=observed_head)
            head = observed_head
        else:
            if (anchor.pending_head != recover_candidate
                    or observed_head not in {anchor.committed_head, recover_candidate}):
                raise CatalogError(CatalogErrorCode.CONFLICT,
                                   candidate_digest=recover_candidate,
                                   current_head=observed_head)
            head = anchor.committed_head
        if head is None:
            return _authenticated_snapshot(None, 0, _empty_catalog(), 0)
        visited: set[str] = set()
        chain: list[tuple[str, dict[str, Any]]] = []
        current_digest: str | None = head
        expected_sequence: int | None = None
        while current_digest is not None:
            if len(chain) >= self.limits.max_journal_entries:
                raise CatalogError(CatalogErrorCode.BOUNDED, current_head=head)
            if current_digest in visited:
                raise CatalogError(CatalogErrorCode.CORRUPT, current_head=head)
            visited.add(current_digest)
            ciphertext = self._read_object(current_digest)
            state = self._open_record(ciphertext, context)
            self._validate_state(state)
            self._require_stable_binding(state, context)
            sequence = state["sequence"]
            if expected_sequence is not None and sequence != expected_sequence:
                raise CatalogError(CatalogErrorCode.CORRUPT, current_head=head)
            chain.append((current_digest, state))
            previous = state["previous_head"]
            if previous is None:
                anchor = state["checkpoint_anchor"] or state["migration_anchor"]
                if anchor is not None:
                    self._verify_anchor_exists(anchor)
                break
            if type(previous) is not str or _DIGEST_RE.fullmatch(previous) is None:
                raise CatalogError(CatalogErrorCode.CORRUPT, current_head=head)
            expected_sequence = sequence - 1
            if expected_sequence < 1:
                raise CatalogError(CatalogErrorCode.CORRUPT, current_head=head)
            current_digest = previous
        oldest = chain[-1][1]
        if (oldest["sequence"] != 1 and oldest["checkpoint_anchor"] is None
                and oldest["migration_anchor"] is None):
            raise CatalogError(CatalogErrorCode.CORRUPT, current_head=head)
        latest = chain[0][1]
        return _authenticated_snapshot(head, latest["sequence"], latest["catalog"],
                                       len(chain), tuple(latest['required_features']))

    def _anchor(self, context: CatalogContext) -> trust_state._CatalogAnchor:
        try:
            return trust_state._catalog_anchor_read(
                context.trust_state_path, catalog_id=self.catalog_id,
                expected_store_id=context.trust_store_id,
                expected_scope=context.expected_scope,
            )
        except trust_state.TrustStateError:
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED) from None

    def _write_anchor(self, context: CatalogContext, phase: str, sequence: int,
                      previous_head: str | None, digest: str) -> None:
        try:
            trust_state._catalog_anchor_write(
                context.trust_state_path, catalog_id=self.catalog_id,
                phase=phase, sequence=sequence, previous_head=previous_head,
                digest=digest, expected_store_id=context.trust_store_id,
                expected_revision=context.trust_revision,
                expected_epoch=context.governance_epoch,
                expected_scope=context.expected_scope, role=context.role,
                signer_fingerprint=context.signer_fingerprint,
            )
        except trust_state.TrustStateError:
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED,
                               candidate_digest=digest,
                               current_head=self._read_head()) from None

    def _verify_anchor_exists(self, digest: str) -> None:
        if type(digest) is not str or _DIGEST_RE.fullmatch(digest) is None:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        self._read_object(digest)

    def _open_record(self, ciphertext: bytes, context: CatalogContext) -> dict[str, Any]:
        try:
            snapshot = trust_state.load_trust_state(
                context.trust_state_path, expected_scope=context.expected_scope,
            )
        except Exception:
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED) from None
        if (snapshot.store_id != context.trust_store_id
                or snapshot.scope != context.expected_scope):
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED)
        payload = None
        opened_signer = None
        opened_epoch = None
        receipt_found = False
        for signer in context.allowed_signers:
            try:
                authority = trust_state.authorize_historical_content(
                    context.trust_state_path, ciphertext,
                    signer_fingerprint=signer, role=context.role,
                    expected_scope=context.expected_scope,
                )
            except Exception:
                continue
            receipt_found = True
            policy = EnvelopePolicy(
                expected_signers=(signer,),
                expected_recipients=context.recipient_fingerprints,
            )
            try:
                payload = open_historical_envelope(
                    ciphertext, trust_state_path=context.trust_state_path,
                    expected_scope=context.expected_scope, role=context.role,
                    signer_fingerprint=signer, gnupg_home=context.gnupg_home,
                    policy=policy, passphrase_reference=context.passphrase_reference,
                    secret_resolver=context.secret_resolver,
                )
            except Exception:
                raise CatalogError(CatalogErrorCode.CORRUPT) from None
            opened_signer = signer
            opened_epoch = authority.accepted_epoch
            break
        if payload is None or opened_signer is None:
            code = (CatalogErrorCode.CORRUPT if receipt_found
                    else CatalogErrorCode.AUTHORIZATION_DENIED)
            raise CatalogError(code)
        state = _parse_catalog_payload(payload, self.limits)
        authority = state.get("commit_authority")
        if (type(authority) is not dict
                or authority.get("signer") != opened_signer
                or authority.get("role") != context.role
                or authority.get("trust_store_id") != context.trust_store_id
                or authority.get("scope") != list(context.expected_scope)
                or authority.get("recipients") != list(context.recipient_fingerprints)
                or type(authority.get("epoch")) is not int
                or opened_epoch != authority.get("epoch")):
            raise CatalogError(CatalogErrorCode.CORRUPT)
        return state

    def _validate_state(self, state: dict[str, Any]) -> None:
        required = {
            "schema", "major", "minor", "required_features", "extensions",
            "sequence", "previous_head", "checkpoint_anchor", "migration_anchor",
            "binding", "commit_authority", "catalog", "transition",
        }
        if set(state) != required:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        if state["schema"] != CATALOG_SCHEMA:
            raise CatalogError(CatalogErrorCode.UNSUPPORTED)
        if type(state["major"]) is not int or state["major"] != CATALOG_MAJOR:
            raise CatalogError(CatalogErrorCode.UNSUPPORTED)
        if type(state["minor"]) is not int or state["minor"] > CATALOG_MINOR:
            raise CatalogError(CatalogErrorCode.UNSUPPORTED)
        if state["minor"] < 0:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        features = state["required_features"]
        if type(features) is not list or any(type(item) is not str for item in features):
            raise CatalogError(CatalogErrorCode.CORRUPT)
        if (features != sorted(set(features)) or set(features) - {ADMISSION_BATCH_FEATURE}
                or features and state['minor'] < 1):
            raise CatalogError(CatalogErrorCode.UNSUPPORTED)
        if type(state['transition']) is dict and state['transition'].get('kind') == 'reconcile-batch':
            batch = state['transition']
            if type(batch.get('version')) is not int or batch['version'] != 1:
                raise CatalogError(CatalogErrorCode.UNSUPPORTED)
            if (ADMISSION_BATCH_FEATURE not in features
                    or set(batch) != {'kind', 'version', 'transitions'}
                    or type(batch['transitions']) is not list or not 2 <= len(batch['transitions']) <= 16):
                raise CatalogError(CatalogErrorCode.CORRUPT)
        if type(state["extensions"]) is not dict:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        if type(state["sequence"]) is not int or state["sequence"] < 1:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        for name in ("previous_head", "checkpoint_anchor", "migration_anchor"):
            value = state[name]
            if value is not None and (type(value) is not str or _DIGEST_RE.fullmatch(value) is None):
                raise CatalogError(CatalogErrorCode.CORRUPT)
        if state["previous_head"] is not None and (
            state["checkpoint_anchor"] is not None or state["migration_anchor"] is not None
        ):
            raise CatalogError(CatalogErrorCode.CORRUPT)
        if (state["checkpoint_anchor"] is not None
                and state["migration_anchor"] is not None):
            raise CatalogError(CatalogErrorCode.CORRUPT)
        if type(state["binding"]) is not dict or type(state["commit_authority"]) is not dict:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        authority = state["commit_authority"]
        if (set(authority) != {
            "trust_store_id", "revision", "epoch", "scope", "role", "signer", "recipients",
        } or type(authority["trust_store_id"]) is not str
                or _DIGEST_RE.fullmatch(authority["trust_store_id"]) is None
                or type(authority["revision"]) is not int or authority["revision"] < 1
                or type(authority["epoch"]) is not int or authority["epoch"] < 1
                or type(authority["scope"]) is not list
                or type(authority["role"]) is not str
                or authority["role"] not in {"owner", "publisher"}
                or type(authority["signer"]) is not str
                or type(authority["recipients"]) is not list
                or any(type(item) is not str for item in authority["recipients"])
                or authority["recipients"] != sorted(set(authority["recipients"]))):
            raise CatalogError(CatalogErrorCode.CORRUPT)
        try:
            normalize_fingerprint(authority["signer"])
            for recipient in authority["recipients"]:
                normalize_fingerprint(recipient)
        except Exception:
            raise CatalogError(CatalogErrorCode.CORRUPT) from None
        if type(state["transition"]) is not dict:
            raise CatalogError(CatalogErrorCode.CORRUPT)
        _validate_catalog(state["catalog"], self.limits.max_entities)

    @staticmethod
    def _require_stable_binding(state: dict[str, Any], context: CatalogContext) -> None:
        if state.get("binding") != context.stable_binding():
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED)

    def _require_fresh_authority(self, context: CatalogContext) -> trust_state.Authority:
        try:
            snapshot = trust_state.load_trust_state(
                context.trust_state_path, expected_scope=context.expected_scope,
            )
        except Exception:
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED) from None
        authority = snapshot.owner if context.role == "owner" else snapshot.publisher
        if (
            snapshot.store_id != context.trust_store_id
            or snapshot.revision != context.trust_revision
            or snapshot.epoch != context.governance_epoch
            or authority is None
            or authority.fingerprint != context.signer_fingerprint
        ):
            raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED)
        return authority

    def _next_state(
        self, current: CatalogSnapshot, context: CatalogContext,
        catalog: dict[str, Any], transition: dict[str, Any], *,
        previous_head: str | None, checkpoint_anchor: str | None,
        migration_anchor: str | None,
    ) -> dict[str, Any]:
        authority = self._require_fresh_authority(context)
        features = set(current.required_features)
        if transition.get('kind') == 'reconcile-batch':
            features.add(ADMISSION_BATCH_FEATURE)
        return {
            "schema": CATALOG_SCHEMA,
            "major": CATALOG_MAJOR,
            "minor": 1 if features else 0,
            "required_features": sorted(features),
            "extensions": {},
            "sequence": current.sequence + 1,
            "previous_head": previous_head,
            "checkpoint_anchor": checkpoint_anchor,
            "migration_anchor": migration_anchor,
            "binding": context.stable_binding(),
            "commit_authority": {
                "trust_store_id": authority.store_id,
                "revision": authority.revision,
                "epoch": authority.epoch,
                "scope": list(authority.scope),
                "role": authority.role,
                "signer": authority.fingerprint,
                "recipients": list(context.recipient_fingerprints),
            },
            "catalog": catalog,
            "transition": transition,
        }

    def _transition_record(self, transition: object) -> dict[str, Any]:
        from .container_reconcile import ReconcileBatch, ReconcileTransition

        if not isinstance(transition, (ReconcileTransition, ReconcileBatch)):
            raise CatalogError(CatalogErrorCode.INVALID_INPUT)
        try:
            record = transition.to_record()
            if type(record) is not dict:
                raise ValueError
            _validate_json_tree(record, max_depth=MAX_JSON_DEPTH,
                                max_nodes=MAX_JSON_NODES)
            return record
        except CatalogError:
            raise
        except Exception:
            raise CatalogError(CatalogErrorCode.INVALID_INPUT) from None

    def _publish_unlocked(
        self, state: dict[str, Any], context: CatalogContext, *,
        expected_head: str | None,
    ) -> CatalogSnapshot:
        self._validate_state(state)
        plaintext = _canonical_json(state)
        if len(plaintext) > self.limits.checkpoint_bytes or len(plaintext) > MAX_PAYLOAD_BYTES:
            raise CatalogError(CatalogErrorCode.BOUNDED, current_head=expected_head)
        self._require_fresh_authority(context)
        try:
            ciphertext = seal_envelope(
                plaintext, gnupg_home=context.gnupg_home,
                signer_fingerprint=context.signer_fingerprint,
                recipient_fingerprints=context.recipient_fingerprints,
                local_trust=LocalTrust({context.signer_fingerprint}),
                passphrase_reference=context.passphrase_reference,
                secret_resolver=context.secret_resolver,
            )
        except Exception:
            raise CatalogError(CatalogErrorCode.UNAVAILABLE,
                               current_head=expected_head) from None
        if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
            raise CatalogError(CatalogErrorCode.BOUNDED,
                               current_head=expected_head)
        candidate = hashlib.sha256(ciphertext).hexdigest()
        try:
            self._write_object(ciphertext)
            authority = self._require_fresh_authority(context)
            trust_state.record_accepted_content(
                context.trust_state_path, ciphertext,
                signer_fingerprint=context.signer_fingerprint,
                role=context.role, expected_scope=context.expected_scope,
                expected_store_id=authority.store_id,
                expected_revision=authority.revision,
            )
            fresh = self._require_fresh_authority(context)
            if (fresh.store_id, fresh.revision, fresh.epoch, fresh.fingerprint) != (
                authority.store_id, authority.revision, authority.epoch,
                authority.fingerprint,
            ):
                raise CatalogError(CatalogErrorCode.AUTHORIZATION_DENIED,
                                   candidate_digest=candidate,
                                   current_head=expected_head)
            if self._read_head() != expected_head:
                raise CatalogError(CatalogErrorCode.CONFLICT,
                                   candidate_digest=candidate,
                                   current_head=self._read_head())
            self._write_anchor(context, "pending", state["sequence"],
                               expected_head, candidate)
            self._write_head(candidate)
            self._write_anchor(context, "committed", state["sequence"],
                               expected_head, candidate)
        except CatalogError as exc:
            if exc.candidate_digest is None:
                exc.candidate_digest = candidate
                try:
                    exc.current_head = self._read_head()
                except Exception:
                    exc.current_head = expected_head
            raise
        except Exception:
            try:
                observed_head = self._read_head()
            except Exception:
                observed_head = expected_head
            raise CatalogError(CatalogErrorCode.UNAVAILABLE,
                               candidate_digest=candidate,
                               current_head=observed_head) from None
        return self._load_unlocked(context)


def _envelope_policy(signer: str, recipients: tuple[str, ...]):
    from .contracts import EnvelopePolicy

    return EnvelopePolicy(expected_signers=(signer,), expected_recipients=recipients)


def _empty_catalog() -> dict[str, Any]:
    return {
        "containers": [],
        "versions": [],
        "sources": [],
        "reconciliation_history": [],
    }


def _validate_catalog(value: Any, max_entities: int) -> None:
    if type(value) is not dict or set(value) != {
        "containers", "versions", "sources", "reconciliation_history",
    }:
        raise CatalogError(CatalogErrorCode.CORRUPT)
    if any(type(value[name]) is not list for name in value):
        raise CatalogError(CatalogErrorCode.CORRUPT)
    total = sum(len(items) for items in value.values())
    if total > max_entities:
        raise CatalogError(CatalogErrorCode.BOUNDED)
    _validate_json_tree(value, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES)


def _parse_catalog_payload(payload: bytes, limits: CatalogLimits) -> dict[str, Any]:
    state = _parse_canonical_json(
        payload, max_bytes=limits.checkpoint_bytes,
        max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES,
    )
    return state


def _parse_canonical_json(
    data: bytes, *, max_bytes: int, max_depth: int, max_nodes: int,
) -> dict[str, Any]:
    if type(data) is not bytes or len(data) > max_bytes:
        raise CatalogError(CatalogErrorCode.BOUNDED)
    _preflight_json(data, max_depth=max_depth, max_nodes=max_nodes)
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text, object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except Exception:
        raise CatalogError(CatalogErrorCode.CORRUPT) from None
    if type(value) is not dict or _canonical_json(value) != data:
        raise CatalogError(CatalogErrorCode.CORRUPT)
    _validate_json_tree(value, max_depth=max_depth, max_nodes=max_nodes)
    return value


def _preflight_json(data: bytes, *, max_depth: int, max_nodes: int) -> None:
    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
            nodes += 1
        elif byte in (0x7B, 0x5B):
            depth += 1
            nodes += 1
            if depth > max_depth:
                raise CatalogError(CatalogErrorCode.BOUNDED)
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise CatalogError(CatalogErrorCode.CORRUPT)
        elif byte in (0x2C, 0x3A):
            nodes += 1
        if nodes > max_nodes:
            raise CatalogError(CatalogErrorCode.BOUNDED)
    if in_string or depth != 0:
        raise CatalogError(CatalogErrorCode.CORRUPT)


def _validate_json_tree(value: Any, *, max_depth: int, max_nodes: int) -> None:
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > max_nodes:
            raise CatalogError(CatalogErrorCode.BOUNDED)
        if depth > max_depth:
            raise CatalogError(CatalogErrorCode.BOUNDED)
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise CatalogError(CatalogErrorCode.CORRUPT)
            continue
        if type(item) is list:
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise CatalogError(CatalogErrorCode.CORRUPT)
                pending.append((child, depth + 1))
            continue
        raise CatalogError(CatalogErrorCode.CORRUPT)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except Exception:
        raise CatalogError(CatalogErrorCode.INVALID_INPUT) from None


def _bounded_json_sha256(value: Any, maximum: int) -> str:
    """Fingerprint JSON proof data without serializing an unbounded mutation."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    minimum_bytes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise CatalogError(CatalogErrorCode.BOUNDED)
        if item is None or type(item) in (bool, int, float):
            if type(item) is float and not math.isfinite(item):
                raise CatalogError(CatalogErrorCode.INVALID_INPUT)
            continue
        if type(item) is str:
            minimum_bytes += len(item)
        elif type(item) is list:
            if len(item) > MAX_JSON_NODES - nodes - len(pending):
                raise CatalogError(CatalogErrorCode.BOUNDED)
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is dict:
            if len(item) > MAX_JSON_NODES - nodes - len(pending):
                raise CatalogError(CatalogErrorCode.BOUNDED)
            for key, child in item.items():
                if type(key) is not str:
                    raise CatalogError(CatalogErrorCode.INVALID_INPUT)
                minimum_bytes += len(key)
                pending.append((child, depth + 1))
        else:
            raise CatalogError(CatalogErrorCode.INVALID_INPUT)
        if minimum_bytes > maximum:
            raise CatalogError(CatalogErrorCode.BOUNDED)
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256()
        size = 0
        for fragment in encoder.iterencode(value):
            encoded = fragment.encode("utf-8")
            size += len(encoded)
            if size > maximum:
                raise CatalogError(CatalogErrorCode.BOUNDED)
            digest.update(encoded)
        return digest.hexdigest()
    except CatalogError:
        raise
    except Exception:
        raise CatalogError(CatalogErrorCode.INVALID_INPUT) from None


def _snapshot_capture_sha256(
    head: str | None, sequence: int, catalog: dict[str, Any], chain_depth: int,
    required_features: tuple[str, ...] = (),
) -> str:
    return _bounded_json_sha256({
        "head": head, "sequence": sequence, "catalog": catalog,
        "chain_depth": chain_depth,
        **({'required_features': list(required_features)} if required_features else {}),
    }, MAX_PLAINTEXT_BYTES + 512)


def _authenticated_snapshot(
    head: str | None, sequence: int, catalog: dict[str, Any], chain_depth: int,
    required_features: tuple[str, ...] = (),
) -> CatalogSnapshot:
    """Capture exactly the catalog fields returned by authenticated loading."""
    return CatalogSnapshot(
        head, sequence, catalog, chain_depth,
        _SnapshotProof(_SNAPSHOT_TOKEN, _snapshot_capture_sha256(
            head, sequence, catalog, chain_depth, required_features,
        )),
        required_features,
    )


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value).decode("utf-8"), object_pairs_hook=_unique_object)


def _read_regular_file(path: Path, *, maximum: int, minimum: int) -> bytes:
    """Read one stable private regular file without blocking on special files."""
    try:
        return read_private_file(path, maximum=maximum, minimum=minimum)
    except FileNotFoundError:
        raise
    except Exception:
        raise CatalogError(CatalogErrorCode.CORRUPT) from None
