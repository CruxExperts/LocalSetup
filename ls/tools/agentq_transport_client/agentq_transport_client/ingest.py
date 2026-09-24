"""Authenticated Agent Q ingest, exact receipts, and queue promotion."""
from __future__ import annotations
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from ls.core.openpgp import authorize_inbound_peer
from .crypto_pipeline import (CryptoPipelineError, assert_local_authority_unchanged,
                              open_manifest, read_bounded, record_verified_blob)
from .file_drop import claim_to_processing, iter_candidates, move_to_processed, ready_marker_path
from .ledger import already_ingested, append_event
from .registry import load_registry_yaml, peer, require_file_drop_path
from .ship import SUFFIX

def sanitize_transport_id(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))[:64] or "unknown"

def promote_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Legacy direct promotion is intentionally unavailable."""
    return {"status": "reject", "code": "MIGRATION_REQUIRED"}

def _promote_verified(queue_root: Path, manifest: dict[str, Any], transport_id: str,
                      registry: dict[str, Any], peer_id: str, authority: Any, *,
                      force: bool = False, operator: str = "", reason: str = "") -> dict[str, Any]:
    from .attachments_extract import extract_attachments_to_staging
    from .manifest_validate import validate_manifest
    validate_manifest(manifest)
    if manifest.get("from_agent_id") != peer_id:
        raise CryptoPipelineError("SENDER_BINDING_FAILED")
    tid = sanitize_transport_id(transport_id)
    if already_ingested(queue_root, tid) and not force:
        return {"status": "skipped", "transport_id": tid, "reason": "already_ingested"}
    prd_name = manifest.get("prd_filename") or "ingested.prd.md"
    if not isinstance(prd_name, str) or Path(prd_name).name != prd_name or prd_name in {".", "..", "manifest.json"}:
        raise CryptoPipelineError("MANIFEST_INVALID")
    staging = Path(queue_root) / "inbox" / ".staging" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)
    try:
        extract_attachments_to_staging(staging, manifest)
        body = manifest.get("prd_body")
        if body:
            (staging / prd_name).write_text(str(body), encoding="utf-8")
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        cfg = peer(registry, peer_id)
        current = authorize_inbound_peer(cfg["trust_state"], role=cfg["role"],
            fingerprint=authority.fingerprint, expected_scope=cfg["expected_scope"])
        if (current.store_id, current.revision) != (authority.store_id, authority.revision):
            raise CryptoPipelineError("STALE_AUTHORITY")
        assert_local_authority_unchanged(registry, authority.local)
        target = Path(queue_root) / "in" / tid
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Force may reprocess a duplicate receipt but cannot delete a prior queue item.
            return {"status": "skipped", "transport_id": tid, "reason": "target_exists"}
        staging.rename(target)
        append_event(queue_root, "ingest_forced" if force else "ingest_promote_ok",
                     {"blob_id": tid, "from_agent_id": peer_id, "operator": operator, "reason": reason},
                     transport_id=tid)
        return {"status": "ok", "transport_id": tid, "promoted_to": str(target)}
    finally:
        if staging.exists():
            shutil.rmtree(staging)

def ingest_blob_bytes(blob: bytes, *, queue_root: Path, registry_path: Path, peer_id: str,
                      transport_id: str, signer_fingerprint: str | None = None,
                      force: bool = False, operator: str = "", reason: str = "") -> dict[str, Any]:
    registry = load_registry_yaml(registry_path)
    try:
        manifest, authority = open_manifest(blob, registry, peer_id=peer_id,
                                             signer_fingerprint=signer_fingerprint)
        tid = sanitize_transport_id(transport_id)
        if already_ingested(queue_root, tid) and not force:
            return {"status": "skipped", "transport_id": tid, "reason": "already_ingested"}
        record_verified_blob(blob, registry, peer_id, authority)
        return _promote_verified(queue_root, manifest, tid, registry, peer_id, authority,
                                 force=force, operator=operator, reason=reason)
    except Exception as exc:
        code = str(getattr(exc, "code", "INGEST_VERIFY_FAILED"))
        append_event(queue_root, "ingest_verify_fail", {"code": code, "blob_id": transport_id},
                     transport_id=transport_id)
        return {"status": "reject", "code": code}

def ingest_file_drop_blob(sealed_path: Path, *, queue_root: Path, registry_path: Path,
                          peer_id: str, signer_fingerprint: str | None = None,
                          processed_root: Path | None = None, force: bool = False,
                          operator: str = "", reason: str = "") -> dict[str, Any]:
    selected = Path(sealed_path)
    if not re.fullmatch(r"[0-9a-f]{40}\.agentq\.lspgp", selected.name):
        return {"status": "reject", "code": "MIGRATION_REQUIRED"}
    registry = load_registry_yaml(registry_path)
    require_file_drop_path(registry, peer_id, selected, inbound=True)
    ready = ready_marker_path(selected, SUFFIX)
    if not ready.is_file():
        return {"status": "reject", "code": "READY_MARKER_MISSING"}
    blob = read_bounded(selected)
    result = ingest_blob_bytes(blob, queue_root=queue_root, registry_path=registry_path,
                               peer_id=peer_id, transport_id=__import__("hashlib").sha256(blob).hexdigest(),
                               signer_fingerprint=signer_fingerprint, force=force,
                               operator=operator, reason=reason)
    if result.get("status") in {"ok", "skipped"}:
        processed_root = processed_root or selected.parent / "processed"
        processed_root.mkdir(parents=True, exist_ok=True)
        target = processed_root / uuid.uuid4().hex
        target.mkdir()
        shutil.move(str(selected), str(target / selected.name))
        shutil.move(str(ready), str(target / ready.name))
    return result

def run_file_drop_poll(roots: list[Path], *, queue_root: Path, registry_path: Path,
                       peer_id: str, signer_fingerprint: str | None = None,
                       max_per_poll: int = 50, use_lockfile: bool = False) -> list[dict[str, Any]]:
    registry = load_registry_yaml(registry_path)
    for root in roots:
        require_file_drop_path(registry, peer_id, root, inbound=True)
    results: list[dict[str, Any]] = []
    processing = Path(queue_root) / "inbox" / ".processing"
    for sealed, _ in iter_candidates(roots, SUFFIX):
        if len(results) >= max_per_poll:
            break
        claimed = claim_to_processing(sealed, processing, SUFFIX, use_lockfile=use_lockfile)
        if claimed is None:
            continue
        candidate = claimed / sealed.name
        try:
            blob = read_bounded(candidate)
            result = ingest_blob_bytes(blob, queue_root=queue_root, registry_path=registry_path,
                peer_id=peer_id, signer_fingerprint=signer_fingerprint,
                transport_id=__import__("hashlib").sha256(blob).hexdigest())
        except Exception as exc:
            result = {"status": "reject", "code": str(getattr(exc, "code", "INGEST_FAILED"))}
        results.append(result)
        if result.get("status") in {"ok", "skipped"}:
            move_to_processed(claimed, sealed.parent / "processed")
    return results
