"""Agent Q v2 migration regression tests (controller runs this suite)."""
from __future__ import annotations
import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

_ROOT = Path(__file__).resolve().parents[4]
_PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_PKG))

LOCAL = "A" * 40
PEER = "B" * 40

def registry(tmp_path: Path) -> dict:
    return {"version": 2, "local_agent_id": "local",
        "local": {"fingerprint": LOCAL, "gnupg_home": tmp_path, "trust_state": tmp_path / "local.db",
                  "expected_scope": ("agentq",), "role": "publisher",
                  "secret_ref": {"provider": "env", "name": "AGENTQ_TEST"}},
        "agents": {"peer": {"fingerprint": PEER, "trust_state": tmp_path / "peer.db",
            "expected_scope": ("agentq",), "role": "publisher", "expected_recipient_ids": ("local",),
            "allowed_inbound_roots": (tmp_path,), "allowed_outbound_roots": (tmp_path,),
            "mail_accounts": ("acct",), "mail_addresses": ("peer@example.com",)}}}

def manifest() -> dict:
    return {"manifest_version": "1", "from_agent_id": "peer", "to_agent_ids": ["local"],
            "prd_body": "# Verified\n", "prd_filename": "verified.prd.md"}

def authority(revision: int = 1):
    return SimpleNamespace(fingerprint=PEER, store_id="store", revision=revision, role="publisher")

def local_authority(revision: int = 1):
    return SimpleNamespace(fingerprint=LOCAL, store_id="local-store", revision=revision, role="publisher")

def mail_row(blob: bytes) -> dict:
    return {"filename": "a" * 40 + ".agentq.lspgp", "content_type": "application/octet-stream",
            "size": len(blob), "content_bytes_base64": base64.b64encode(blob).decode()}

def test_registry_v1_and_short_pin_require_migration(tmp_path: Path):
    from agentq_transport_client.registry import RegistryError, validate_registry
    with pytest.raises(RegistryError, match="MIGRATION_REQUIRED"):
        validate_registry({"version": 1})
    raw = {"version": 2, "local_agent_id": "local",
           "local": {"fingerprint": "1234", "gnupg_home": str(tmp_path),
                     "secret_ref": {"provider": "env", "name": "SECRET"}}, "agents": {}}
    with pytest.raises(ValueError):
        validate_registry(raw)

def test_open_selects_one_peer_before_decrypt_and_binds_manifest(monkeypatch, tmp_path: Path):
    from agentq_transport_client import crypto_pipeline as cp
    selected = []
    monkeypatch.setattr(cp, "_authority", lambda cfg, peer_id, signer=None: selected.append((peer_id, signer)) or authority())
    monkeypatch.setattr(cp, "_local_authority", lambda cfg: local_authority())
    def opener(blob, **kwargs):
        assert selected == [("peer", None)]
        assert kwargs["policy"].expected_signers == {PEER}
        assert kwargs["policy"].expected_recipients == {LOCAL}
        return json.dumps(manifest()).encode()
    monkeypatch.setattr(cp, "open_envelope", opener)
    monkeypatch.setattr(cp, "authorize_inbound_peer", lambda *a, **k: authority())
    result, pin = cp.open_manifest(b"opaque", registry(tmp_path), peer_id="peer")
    assert result["from_agent_id"] == "peer" and pin.fingerprint == PEER

def test_open_rejects_forged_sender_after_verified_envelope(monkeypatch, tmp_path: Path):
    from agentq_transport_client import crypto_pipeline as cp
    monkeypatch.setattr(cp, "_authority", lambda *a, **k: authority())
    monkeypatch.setattr(cp, "_local_authority", lambda cfg: local_authority())
    bad = {**manifest(), "from_agent_id": "attacker"}
    monkeypatch.setattr(cp, "open_envelope", lambda *a, **k: json.dumps(bad).encode())
    with pytest.raises(cp.CryptoPipelineError, match="SENDER_BINDING_FAILED"):
        cp.open_manifest(b"opaque", registry(tmp_path), peer_id="peer")

def test_open_rejects_revision_change_before_return(monkeypatch, tmp_path: Path):
    from agentq_transport_client import crypto_pipeline as cp
    monkeypatch.setattr(cp, "_authority", lambda *a, **k: authority())
    monkeypatch.setattr(cp, "_local_authority", lambda cfg: local_authority())
    monkeypatch.setattr(cp, "open_envelope", lambda *a, **k: json.dumps(manifest()).encode())
    monkeypatch.setattr(cp, "authorize_inbound_peer", lambda *a, **k: authority(2))
    with pytest.raises(cp.CryptoPipelineError, match="STALE_AUTHORITY"):
        cp.open_manifest(b"opaque", registry(tmp_path), peer_id="peer")

def test_legacy_blob_is_preserved(tmp_path: Path):
    from agentq_transport_client.ingest import ingest_file_drop_blob
    old = tmp_path / "old.agentq.asc"
    old.write_text("legacy")
    result = ingest_file_drop_blob(old, queue_root=tmp_path / "queue",
        registry_path=tmp_path / "missing.yaml", peer_id="peer")
    assert result["code"] == "MIGRATION_REQUIRED" and old.read_text() == "legacy"

def test_file_ship_opaque_and_ready_last(monkeypatch, tmp_path: Path):
    from agentq_transport_client import ship
    monkeypatch.setattr(ship, "load_registry_yaml", lambda path: registry(tmp_path))
    monkeypatch.setattr(ship, "seal_manifest", lambda *a: b"sealed")
    observed = []
    monkeypatch.setattr(ship, "run_pre_ship_checks", lambda *a, **k: {"ok": True}, raising=False)
    out = ship.ship_file_drop({"to_agent_ids": ["peer"]}, tmp_path / "registry", tmp_path, skip_pre_ship=True)
    sealed = Path(out["sealed"])
    assert sealed.name.endswith(".agentq.lspgp") and len(sealed.name.split(".")[0]) == 40
    assert sealed.read_bytes() == b"sealed" and Path(out["ready"]).is_file()
    assert not list(tmp_path.glob("*.sidecar.json"))

def test_mail_requires_one_complete_opaque_attachment():
    from agentq_transport_client.mail_adapter import _blob_attachment
    row = {"filename": "a" * 40 + ".agentq.lspgp", "content_type": "application/octet-stream",
           "size": 3, "content_bytes_base64": base64.b64encode(b"abc").decode()}
    assert _blob_attachment([row]) == b"abc"
    with pytest.raises(ValueError):
        _blob_attachment([row, row])
    with pytest.raises(ValueError):
        _blob_attachment([{**row, "content_truncated": True}])
    with pytest.raises(ValueError):
        _blob_attachment([{**row, "size": 4}])

def test_bounded_read_rejects_over_limit(tmp_path: Path):
    from agentq_transport_client.crypto_pipeline import CryptoPipelineError, read_bounded
    path = tmp_path / "huge"
    path.write_bytes(b"12345")
    with pytest.raises(CryptoPipelineError, match="ENVELOPE_TOO_LARGE"):
        read_bounded(path, limit=4)

def test_direct_promotion_unavailable():
    from agentq_transport_client.ingest import promote_manifest
    assert promote_manifest(None, {"from_agent_id": "attacker"}, "x")["code"] == "MIGRATION_REQUIRED"

def test_ingest_receipt_precedes_promotion(monkeypatch, tmp_path: Path):
    from agentq_transport_client import ingest
    order = []
    monkeypatch.setattr(ingest, "load_registry_yaml", lambda path: registry(tmp_path))
    monkeypatch.setattr(ingest, "open_manifest", lambda *a, **k: (manifest(), authority()))
    monkeypatch.setattr(ingest, "record_verified_blob", lambda *a: order.append("receipt"))
    monkeypatch.setattr(ingest, "_promote_verified", lambda *a, **k: order.append("promote") or {"status": "ok"})
    result = ingest.ingest_blob_bytes(b"sealed", queue_root=tmp_path / "queue",
        registry_path=tmp_path / "registry", peer_id="peer", transport_id="digest")
    assert result["status"] == "ok" and order == ["receipt", "promote"]

def test_ingest_stale_receipt_never_promotes(monkeypatch, tmp_path: Path):
    from agentq_transport_client import ingest
    monkeypatch.setattr(ingest, "load_registry_yaml", lambda path: registry(tmp_path))
    monkeypatch.setattr(ingest, "open_manifest", lambda *a, **k: (manifest(), authority()))
    monkeypatch.setattr(ingest, "record_verified_blob", lambda *a: (_ for _ in ()).throw(ValueError("stale")))
    monkeypatch.setattr(ingest, "_promote_verified", lambda *a, **k: pytest.fail("promoted stale content"))
    result = ingest.ingest_blob_bytes(b"sealed", queue_root=tmp_path / "queue",
        registry_path=tmp_path / "registry", peer_id="peer", transport_id="digest")
    assert result["status"] == "reject" and not (tmp_path / "queue" / "in").exists()

def test_file_ship_preship_failure_never_seals(monkeypatch, tmp_path: Path):
    from agentq_transport_client import ship
    from agentq_transport_client import preship
    monkeypatch.setattr(ship, "load_registry_yaml", lambda path: registry(tmp_path))
    monkeypatch.setattr(preship, "run_pre_ship_checks", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(ship, "seal_manifest", lambda *a: pytest.fail("sealed after preflight failure"))
    result = ship.ship_file_drop({"to_agent_ids": ["peer"]}, tmp_path / "registry", tmp_path)
    assert result["code"] == "PRE_SHIP_FAILED"

def test_cli_has_no_legacy_crypto_flags(monkeypatch):
    from agentq_transport_client.cli_parser import main
    monkeypatch.setattr(sys, "argv", ["agentq", "ingest-blob", "x", "--privkey", "secret"])
    with pytest.raises(SystemExit):
        main()

def test_revoked_local_recipient_denied_before_decrypt(monkeypatch, tmp_path: Path):
    from agentq_transport_client import crypto_pipeline as cp
    monkeypatch.setattr(cp, "_authority", lambda *a, **k: authority())
    monkeypatch.setattr(cp, "_local_authority", lambda cfg: (_ for _ in ()).throw(
        cp.CryptoPipelineError("LOCAL_PIN_MISMATCH")))
    monkeypatch.setattr(cp, "open_envelope", lambda *a, **k: pytest.fail("decrypted for revoked local key"))
    with pytest.raises(cp.CryptoPipelineError, match="LOCAL_PIN_MISMATCH"):
        cp.open_manifest(b"sealed", registry(tmp_path), peer_id="peer")

def test_local_rotation_during_open_denies_even_with_old_secret(monkeypatch, tmp_path: Path):
    from agentq_transport_client import crypto_pipeline as cp
    monkeypatch.setattr(cp, "_authority", lambda *a, **k: authority())
    local_states = iter((local_authority(1), local_authority(2)))
    monkeypatch.setattr(cp, "_local_authority", lambda cfg: next(local_states))
    monkeypatch.setattr(cp, "open_envelope", lambda *a, **k: json.dumps(manifest()).encode())
    monkeypatch.setattr(cp, "authorize_inbound_peer", lambda *a, **k: authority())
    with pytest.raises(cp.CryptoPipelineError, match="STALE_LOCAL_AUTHORITY"):
        cp.open_manifest(b"sealed", registry(tmp_path), peer_id="peer")

def test_local_revision_rechecked_before_receipt_and_promotion(monkeypatch, tmp_path: Path):
    from agentq_transport_client import crypto_pipeline as cp, ingest
    cfg = registry(tmp_path)
    verified = cp.VerifiedAuthority(authority(), local_authority(1))
    monkeypatch.setattr(cp, "_local_authority", lambda cfg: local_authority(2))
    monkeypatch.setattr(cp, "record_accepted_content", lambda *a, **k: pytest.fail("recorded stale local receipt"))
    with pytest.raises(cp.CryptoPipelineError, match="STALE_LOCAL_AUTHORITY"):
        cp.record_verified_blob(b"sealed", cfg, "peer", verified)
    monkeypatch.setattr(ingest, "authorize_inbound_peer", lambda *a, **k: authority())
    monkeypatch.setattr(ingest, "assert_local_authority_unchanged", cp.assert_local_authority_unchanged)
    with pytest.raises(cp.CryptoPipelineError, match="STALE_LOCAL_AUTHORITY"):
        ingest._promote_verified(tmp_path / "queue", manifest(), "digest", cfg, "peer", verified)
    assert not (tmp_path / "queue" / "in").exists()

def test_mail_retry_scopes_account_mailbox_and_uid(monkeypatch, tmp_path: Path):
    from agentq_transport_client import mail_adapter as mail
    from agentq_transport_client.ledger import append_event
    queue = tmp_path / "queue"
    archive_blob = b"archive"
    archive_digest = hashlib.sha256(archive_blob).hexdigest()
    other_digest = hashlib.sha256(b"other").hexdigest()
    for account, mailbox in (("acct-a", "INBOX"), ("acct-b", "INBOX"), ("acct-b", "Archive")):
        digest = archive_digest if mailbox == "Archive" else other_digest
        append_event(queue, "pending_processed_move", {"account_id": account,
            "mailbox": mailbox, "uid": "7", "target_mailbox": "Processed",
            "ciphertext_sha256": digest}, transport_id=digest)
    append_event(queue, "mail_move_ok", {"account_id": "acct-b", "mailbox": "INBOX",
        "uid": "7", "ciphertext_sha256": other_digest}, transport_id=other_digest)
    append_event(queue, "pending_processed_move", {"mailbox": "INBOX", "uid": "8"})
    calls = []
    class Controller:
        def dispatch(self, action, payload):
            calls.append((action, payload))
            if action == "mail_get":
                return {"ok": True, "attachments": [mail_row(archive_blob)]}
            return {"ok": True, "code": "OK"}
    monkeypatch.setattr(mail, "_mail_controller", lambda *a: Controller())
    result = mail.mail_retry_pending_moves(queue_root=queue, account_id="acct-b",
        policy_path=tmp_path / "policy", accounts_path=tmp_path / "accounts")
    assert [action for action, _ in calls] == ["mail_get", "mail_mutate"]
    assert calls[1][1]["acct"] == "acct-b"
    assert calls[1][1]["mailbox"] == "Archive" and calls[1][1]["uids"] == ["7"]
    assert any(row.get("code") == "MIGRATION_REQUIRED" for row in result)
    assert not any(row.get("account_id") == "acct-a" and row.get("event") == "mail_move_ok"
                   for row in __import__("agentq_transport_client.ledger", fromlist=["iter_ingest_events"]).iter_ingest_events(queue))

def test_duplicate_authenticated_mail_moves_but_rejected_mail_does_not(monkeypatch, tmp_path: Path):
    from agentq_transport_client import mail_adapter as mail
    from agentq_transport_client.ledger import iter_ingest_events
    row = {"filename": "a" * 40 + ".agentq.lspgp", "content_type": "application/octet-stream",
           "size": 3, "content_bytes_base64": base64.b64encode(b"abc").decode()}
    calls = []
    class Controller:
        def dispatch(self, action, payload):
            calls.append(action)
            if action == "mail_query":
                return {"ok": True, "items": [{"id": "7"}]}
            if action == "mail_get":
                return {"ok": True, "attachments": [row]}
            return {"ok": True, "code": "OK"}
    monkeypatch.setattr(mail, "load_registry_yaml", lambda path: registry(tmp_path))
    monkeypatch.setattr(mail, "_mail_controller", lambda *a: Controller())
    monkeypatch.setattr(mail, "ingest_blob_bytes", lambda *a, **k: {"status": "skipped", "reason": "already_ingested"})
    queue = tmp_path / "queue"
    result = mail.mail_pull_and_promote(queue_root=queue, account_id="acct",
        policy_path=tmp_path / "policy", accounts_path=tmp_path / "accounts",
        registry_path=tmp_path / "registry", peer_id="peer")
    assert result[0]["moved_to"] == "LocalsetupAgentQ/Processed" and calls.count("mail_mutate") == 1
    assert any(record["event"] == "mail_move_ok" and record["account_id"] == "acct"
               for record in iter_ingest_events(queue))
    calls.clear()
    monkeypatch.setattr(mail, "ingest_blob_bytes", lambda *a, **k: {"status": "reject", "code": "BAD_SIGNATURE"})
    result = mail.mail_pull_and_promote(queue_root=queue, account_id="acct",
        policy_path=tmp_path / "policy", accounts_path=tmp_path / "accounts",
        registry_path=tmp_path / "registry", peer_id="peer")
    assert result[0]["status"] == "reject" and "mail_mutate" not in calls

def test_mail_retry_refetches_exact_ciphertext_before_move(monkeypatch, tmp_path: Path):
    from agentq_transport_client import mail_adapter as mail
    from agentq_transport_client.ledger import append_event, iter_ingest_events
    queue = tmp_path / "queue"
    original = b"sealed-original"
    digest = hashlib.sha256(original).hexdigest()
    append_event(queue, "pending_processed_move", {"account_id": "acct", "mailbox": "INBOX",
        "uid": "11", "target_mailbox": "Processed", "ciphertext_sha256": digest}, transport_id=digest)
    current = [b"changed-at-same-uid"]
    calls = []
    class Controller:
        def dispatch(self, action, payload):
            calls.append((action, payload))
            if action == "mail_get":
                return {"ok": True, "attachments": [mail_row(current[0])]}
            return {"ok": True, "code": "OK"}
    monkeypatch.setattr(mail, "_mail_controller", lambda *a: Controller())
    changed = mail.mail_retry_pending_moves(queue_root=queue, account_id="acct",
        policy_path=tmp_path / "policy", accounts_path=tmp_path / "accounts")
    assert changed[0]["code"] == "CIPHERTEXT_CHANGED"
    assert [action for action, _ in calls] == ["mail_get"]
    assert calls[0][1]["max_message_bytes"] == mail.MAX_MESSAGE
    assert calls[0][1]["max_attachment_content_bytes"] == mail.MAX_ATTACHMENT
    assert not list(iter_ingest_events(queue, "mail_move_ok"))
    current[0] = original
    calls.clear()
    accepted = mail.mail_retry_pending_moves(queue_root=queue, account_id="acct",
        policy_path=tmp_path / "policy", accounts_path=tmp_path / "accounts")
    assert accepted[0]["ok"] is True
    assert [action for action, _ in calls] == ["mail_get", "mail_mutate"]
    completed = list(iter_ingest_events(queue, "mail_move_ok"))
    assert len(completed) == 1 and completed[0]["ciphertext_sha256"] == digest
    assert completed[0]["transport_id"] == digest


def test_mail_retry_preserves_legacy_record_without_digest(monkeypatch, tmp_path: Path):
    from agentq_transport_client import mail_adapter as mail
    from agentq_transport_client.ledger import append_event
    queue = tmp_path / "queue"
    append_event(queue, "pending_processed_move", {"account_id": "acct", "mailbox": "INBOX",
        "uid": "11", "target_mailbox": "Processed"}, transport_id="mail-11")
    class Controller:
        def dispatch(self, action, payload):
            pytest.fail("legacy record must not fetch or move mail")
    monkeypatch.setattr(mail, "_mail_controller", lambda *a: Controller())
    result = mail.mail_retry_pending_moves(queue_root=queue, account_id="acct",
        policy_path=tmp_path / "policy", accounts_path=tmp_path / "accounts")
    assert result == [{"status": "manual_review", "code": "MIGRATION_REQUIRED", "uid": "11"}]
