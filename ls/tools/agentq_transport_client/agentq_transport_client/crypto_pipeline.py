"""Signed binary Agent Q manifests over LocalSetup's shared OpenPGP envelope."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from ls.core.openpgp import (EnvelopePolicy, LocalTrust, SecretReference, SecretResolver,
    authorize_inbound_peer, authorize_outbound, open_envelope, open_historical_envelope,
    record_accepted_content, seal_envelope)
from ls.core.openpgp.envelope import MAX_ENVELOPE_BYTES, MAX_PAYLOAD_BYTES
from .registry import peer

class CryptoPipelineError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code, self.message = code, message or code
        super().__init__(self.message)

@dataclass(frozen=True, slots=True)
class VerifiedAuthority:
    """The peer signer and local recipient authorities observed during opening."""
    peer: Any
    local: Any

    @property
    def fingerprint(self) -> str:
        return self.peer.fingerprint

    @property
    def store_id(self) -> str:
        return self.peer.store_id

    @property
    def revision(self) -> int:
        return self.peer.revision

def _secret(registry: dict[str, Any]) -> SecretReference:
    ref = registry["local"]["secret_ref"]
    return SecretReference(ref["provider"], ref["name"])

def _authority(registry: dict[str, Any], peer_id: str, signer: str | None = None):
    cfg = peer(registry, peer_id)
    if signer is None:
        authority = authorize_outbound(cfg["trust_state"], expected_scope=cfg["expected_scope"], role=cfg["role"])
        if authority.fingerprint != cfg["fingerprint"]:
            raise CryptoPipelineError("PEER_PIN_MISMATCH")
        return authority
    return authorize_inbound_peer(cfg["trust_state"], role=cfg["role"], fingerprint=signer,
                                  expected_scope=cfg["expected_scope"])

def _local_authority(registry: dict[str, Any]):
    local = registry["local"]
    authority = authorize_outbound(local["trust_state"], expected_scope=local["expected_scope"],
                                   role=local["role"])
    if authority.fingerprint != local["fingerprint"]:
        raise CryptoPipelineError("LOCAL_PIN_MISMATCH")
    checked = authorize_inbound_peer(local["trust_state"], role=local["role"],
        fingerprint=authority.fingerprint, expected_scope=local["expected_scope"])
    if (checked.store_id, checked.revision) != (authority.store_id, authority.revision):
        raise CryptoPipelineError("STALE_LOCAL_AUTHORITY")
    return authority

def assert_local_authority_unchanged(registry: dict[str, Any], observed: Any) -> None:
    """Reject a stale or rotated recipient even when its secret key remains installed."""
    current = _local_authority(registry)
    if (current.store_id, current.revision, current.fingerprint) != (
        observed.store_id, observed.revision, observed.fingerprint
    ):
        raise CryptoPipelineError("STALE_LOCAL_AUTHORITY")

def _recipients(registry: dict[str, Any], ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(registry["local"]["fingerprint"] if item == registry["local_agent_id"]
                 else _authority(registry, item).fingerprint for item in ids)

def seal_manifest(manifest: dict[str, Any], registry: dict[str, Any], *,
                  secret_resolver: SecretResolver | None = None) -> bytes:
    from .manifest_validate import validate_manifest
    validate_manifest(manifest)
    if manifest.get("from_agent_id") != registry["local_agent_id"]:
        raise CryptoPipelineError("SENDER_BINDING_FAILED")
    ids = manifest.get("to_agent_ids")
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or any(item not in registry["agents"] for item in ids):
        raise CryptoPipelineError("RECIPIENT_SET_INVALID")
    authorities = {item: _authority(registry, item) for item in ids}
    local_authority = _local_authority(registry)
    recipients = tuple(authorities[item].fingerprint for item in ids)
    if len(recipients) != len(set(recipients)):
        raise CryptoPipelineError("RECIPIENT_SET_INVALID")
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise CryptoPipelineError("MANIFEST_TOO_LARGE")
    local = registry["local"]
    blob = seal_envelope(payload, gnupg_home=local["gnupg_home"], signer_fingerprint=local["fingerprint"],
                         recipient_fingerprints=recipients, local_trust=LocalTrust({local["fingerprint"]}),
                         passphrase_reference=_secret(registry), secret_resolver=secret_resolver or SecretResolver())
    if len(blob) > MAX_ENVELOPE_BYTES:
        raise CryptoPipelineError("ENVELOPE_TOO_LARGE")
    current_local = _local_authority(registry)
    if (current_local.store_id, current_local.revision) != (local_authority.store_id, local_authority.revision):
        raise CryptoPipelineError("STALE_AUTHORITY")
    for item, authority in authorities.items():
        current = _authority(registry, item)
        if (current.store_id, current.revision) != (authority.store_id, authority.revision):
            raise CryptoPipelineError("STALE_AUTHORITY")
    return blob

def open_manifest(blob: bytes, registry: dict[str, Any], *, peer_id: str,
                  signer_fingerprint: str | None = None,
                  secret_resolver: SecretResolver | None = None) -> tuple[dict[str, Any], Any]:
    """Select one signer and exact recipient policy from private state before decrypt."""
    if type(blob) is not bytes or not blob or len(blob) > MAX_ENVELOPE_BYTES:
        raise CryptoPipelineError("ENVELOPE_TOO_LARGE")
    cfg = peer(registry, peer_id)
    authority = _authority(registry, peer_id, signer_fingerprint)
    local_authority = _local_authority(registry)
    recipients = _recipients(registry, cfg["expected_recipient_ids"])
    local = registry["local"]
    raw = open_envelope(blob, gnupg_home=local["gnupg_home"],
                        policy=EnvelopePolicy(expected_signers=(authority.fingerprint,), expected_recipients=recipients),
                        local_trust=LocalTrust({authority.fingerprint}),
                        passphrase_reference=_secret(registry), secret_resolver=secret_resolver or SecretResolver())
    try:
        manifest = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CryptoPipelineError("MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict) or manifest.get("from_agent_id") != peer_id:
        raise CryptoPipelineError("SENDER_BINDING_FAILED")
    if tuple(sorted(manifest.get("to_agent_ids", []))) != tuple(sorted(cfg["expected_recipient_ids"])):
        raise CryptoPipelineError("RECIPIENT_BINDING_FAILED")
    from .manifest_validate import validate_manifest
    validate_manifest(manifest)
    current = authorize_inbound_peer(cfg["trust_state"], role=cfg["role"], fingerprint=authority.fingerprint,
                                     expected_scope=cfg["expected_scope"])
    if (current.store_id, current.revision) != (authority.store_id, authority.revision):
        raise CryptoPipelineError("STALE_AUTHORITY")
    assert_local_authority_unchanged(registry, local_authority)
    return manifest, VerifiedAuthority(authority, local_authority)

def record_verified_blob(blob: bytes, registry: dict[str, Any], peer_id: str, authority: Any) -> str:
    cfg = peer(registry, peer_id)
    assert_local_authority_unchanged(registry, authority.local)
    return record_accepted_content(cfg["trust_state"], blob, signer_fingerprint=authority.fingerprint,
                                   role=cfg["role"], expected_store_id=authority.store_id,
                                   expected_revision=authority.revision, expected_scope=cfg["expected_scope"])

def read_bounded(path: Path, limit: int = MAX_ENVELOPE_BYTES) -> bytes:
    with Path(path).open("rb") as source:
        blob = source.read(limit + 1)
    if len(blob) > limit:
        raise CryptoPipelineError("ENVELOPE_TOO_LARGE")
    return blob

def open_historical_manifest(blob: bytes, registry: dict[str, Any], *, peer_id: str,
                             signer_fingerprint: str, recipient_fingerprints: tuple[str, ...],
                             secret_resolver: SecretResolver | None = None) -> dict[str, Any]:
    """Read an exact prior receipt; this API never records or promotes content."""
    if type(blob) is not bytes or not blob or len(blob) > MAX_ENVELOPE_BYTES:
        raise CryptoPipelineError("ENVELOPE_TOO_LARGE")
    cfg = peer(registry, peer_id)
    from ls.core.openpgp import authorize_historical_content
    authority = authorize_historical_content(cfg["trust_state"], blob,
        signer_fingerprint=signer_fingerprint, role=cfg["role"], expected_scope=cfg["expected_scope"])
    recipients = recipient_fingerprints
    if registry["local"]["fingerprint"] not in recipients:
        raise CryptoPipelineError("RECIPIENT_BINDING_FAILED")
    raw = open_historical_envelope(blob, trust_state_path=cfg["trust_state"],
        expected_scope=cfg["expected_scope"], role=cfg["role"],
        signer_fingerprint=authority.fingerprint, gnupg_home=registry["local"]["gnupg_home"],
        policy=EnvelopePolicy(expected_signers=(authority.fingerprint,), expected_recipients=recipients),
        passphrase_reference=_secret(registry), secret_resolver=secret_resolver or SecretResolver())
    try:
        manifest = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CryptoPipelineError("MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict) or manifest.get("from_agent_id") != peer_id:
        raise CryptoPipelineError("SENDER_BINDING_FAILED")
    if registry["local_agent_id"] not in manifest.get("to_agent_ids", []):
        raise CryptoPipelineError("RECIPIENT_BINDING_FAILED")
    from .manifest_validate import validate_manifest
    validate_manifest(manifest)
    return manifest
