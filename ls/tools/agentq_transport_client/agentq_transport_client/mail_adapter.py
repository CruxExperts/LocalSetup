"""Policy-gated mail carrier for one opaque Agent Q ciphertext attachment."""
from __future__ import annotations
import base64
import binascii
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any
from .crypto_pipeline import seal_manifest
from .ingest import ingest_blob_bytes
from .ledger import append_event, append_ship_event, iter_ingest_events, pending_processed_moves
from .registry import load_registry_yaml, require_mail_account, require_mail_address

_ENGINE = Path(__file__).resolve().parents[3]
_MAIL_SCRIPTS = _ENGINE / "skills" / "ls-mail-protocol-control" / "scripts"
MAX_ATTACHMENT = 4 * 1024 * 1024
MAX_MESSAGE = 6 * 1024 * 1024

def _mail_controller(policy_path: Path, accounts_path: Path) -> Any:
    sys.path.insert(0, str(_MAIL_SCRIPTS))
    from mail_protocol_control import EnvCredentialProvider, MailProtocolControl  # type: ignore
    from mail_types import AccountConfig  # type: ignore
    data = json.loads(Path(accounts_path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("invalid accounts file")
    allowed = {"account_id", "smtp_host", "smtp_port", "smtp_tls_mode", "imap_host", "imap_port", "imap_tls"}
    accounts = [AccountConfig(**{key: value for key, value in row.items() if key in allowed})
                for row in data if isinstance(row, dict) and row.get("account_id")
                and row.get("smtp_host") and row.get("imap_host")]
    if not accounts:
        raise ValueError("no usable mail account")
    return MailProtocolControl(policy_path=policy_path, accounts=accounts,
                               credential_provider=EnvCredentialProvider())

def _blob_attachment(rows: Any) -> bytes:
    if not isinstance(rows, list) or not rows:
        raise ValueError("MIGRATION_REQUIRED")
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("INVALID_ATTACHMENT")
    row = rows[0]
    if row.get("content_type") != "application/octet-stream" or row.get("content_truncated"):
        raise ValueError("invalid or truncated attachment")
    name = row.get("filename")
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{40}\.agentq\.lspgp", name):
        raise ValueError("MIGRATION_REQUIRED")
    size = row.get("size")
    encoded = row.get("content_bytes_base64")
    if type(size) is not int or not 0 < size <= MAX_ATTACHMENT or not isinstance(encoded, str) or len(encoded) > 6_000_000:
        raise ValueError("attachment size invalid")
    blob = base64.b64decode(encoded, validate=True)
    if len(blob) != size:
        raise ValueError("attachment truncated")
    return blob

def mail_ship_agentq_outer(*, account_id: str, policy_path: Path, accounts_path: Path,
                           registry_path: Path, manifest: dict[str, Any], peer_id: str,
                           to_addr: str, from_addr: str, queue_root: Path | None = None,
                           skip_pre_ship: bool = False, pre_ship_cwd: Path | None = None) -> dict[str, Any]:
    from .preship import run_pre_ship_checks
    try:
        registry = load_registry_yaml(registry_path)
        require_mail_account(registry, peer_id, account_id)
        require_mail_address(registry, peer_id, to_addr)
        if manifest.get("to_agent_ids") != [peer_id]:
            raise ValueError("mail requires one exact recipient")
        if not skip_pre_ship and not run_pre_ship_checks(manifest, cwd=pre_ship_cwd).get("ok"):
            raise ValueError("PRE_SHIP_FAILED")
        blob = seal_manifest(manifest, registry)
        if len(blob) > MAX_ATTACHMENT:
            raise ValueError("ciphertext exceeds mail limit")
        ctrl = _mail_controller(policy_path, accounts_path)
        result = ctrl.dispatch("mail_send", {"acct": account_id, "from": from_addr, "to": [to_addr],
            "subject": "Secure message", "body": "Encrypted attachment enclosed.",
            "max_attachment_count": 1, "max_attachment_size_bytes": MAX_ATTACHMENT,
            "max_total_attachment_bytes": MAX_ATTACHMENT,
            "attachments": [{"filename": secrets.token_hex(20) + ".agentq.lspgp",
                "content_type": "application/octet-stream",
                "content_bytes_base64": base64.b64encode(blob).decode("ascii")}]})
    except Exception as exc:
        result = {"ok": False, "code": str(getattr(exc, "code", "SHIP_MAIL_FAILED"))}
    if queue_root:
        append_ship_event(queue_root, "ship_mail_ok" if result.get("ok") else "ship_mail_fail",
                          {"code": result.get("code"), "account_id": account_id})
    return result

def mail_pull_and_promote(*, queue_root: Path, account_id: str, policy_path: Path,
                          accounts_path: Path, registry_path: Path, peer_id: str,
                          signer_fingerprint: str | None = None, mailbox: str = "INBOX",
                          post_ingest_mailbox: str = "LocalsetupAgentQ/Processed",
                          query: str = "UNSEEN", lim: int = 25,
                          confirm_token: str = "") -> list[dict[str, Any]]:
    registry = load_registry_yaml(registry_path)
    require_mail_account(registry, peer_id, account_id)
    ctrl = _mail_controller(policy_path, accounts_path)
    queried = ctrl.dispatch("mail_query", {"acct": account_id, "mailbox": mailbox, "query": query, "lim": lim})
    if not queried.get("ok"):
        return [{"status": "error", "code": queried.get("code")}]
    results = []
    for item in queried.get("items", []):
        uid = item.get("id") if isinstance(item, dict) else None
        if not uid:
            continue
        got = ctrl.dispatch("mail_get", {"acct": account_id, "mailbox": mailbox, "id": uid,
            "detail": True, "max_message_bytes": MAX_MESSAGE, "include_attachment_content": True,
            "max_attachment_content_bytes": MAX_ATTACHMENT})
        if not got.get("ok"):
            results.append({"status": "reject", "uid": uid, "code": got.get("code")})
            continue
        try:
            blob = _blob_attachment(got.get("attachments"))
        except (ValueError, binascii.Error) as exc:
            code = "MIGRATION_REQUIRED" if str(exc) == "MIGRATION_REQUIRED" else "INVALID_ATTACHMENT"
            results.append({"status": "reject", "uid": uid, "code": code})
            continue
        tid = hashlib.sha256(blob).hexdigest()
        result = ingest_blob_bytes(blob, queue_root=queue_root, registry_path=registry_path,
            peer_id=peer_id, signer_fingerprint=signer_fingerprint, transport_id=tid)
        results.append({"uid": uid, **result})
        authenticated_terminal = result.get("status") == "ok" or (
            result.get("status") == "skipped" and result.get("reason") == "already_ingested"
        )
        if authenticated_terminal:
            moved = ctrl.dispatch("mail_mutate", {"acct": account_id, "mailbox": mailbox,
                "mutate_action": "move_messages", "uids": [uid], "target_mailbox": post_ingest_mailbox,
                "count": 1, "confirm_token": confirm_token})
            if not moved.get("ok"):
                append_event(queue_root, "pending_processed_move", {"account_id": account_id,
                    "uid": uid, "mailbox": mailbox,
                    "target_mailbox": post_ingest_mailbox, "ciphertext_sha256": tid,
                    "code": moved.get("code")}, transport_id=tid)
                results[-1]["move_pending"] = True
            else:
                append_event(queue_root, "mail_move_ok", {"account_id": account_id,
                    "uid": uid, "mailbox": mailbox, "target_mailbox": post_ingest_mailbox,
                    "ciphertext_sha256": tid},
                    transport_id=tid)
                results[-1]["moved_to"] = post_ingest_mailbox
    return results

def mail_retry_pending_moves(*, queue_root: Path, account_id: str, policy_path: Path,
                             accounts_path: Path, confirm_token: str = "") -> list[dict[str, Any]]:
    ctrl = _mail_controller(policy_path, accounts_path)
    results = []
    def identity(record: dict[str, Any]) -> tuple[str, str, str, str] | None:
        account, mailbox, uid = record.get("account_id"), record.get("mailbox"), record.get("uid")
        digest = record.get("ciphertext_sha256", record.get("transport_id"))
        if (not isinstance(account, str) or not account or not isinstance(mailbox, str)
                or not mailbox or not isinstance(uid, (str, int)) or str(uid) == ""
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or record.get("transport_id") != digest):
            return None
        return account, mailbox, str(uid), digest

    completed = {key for row in iter_ingest_events(queue_root, "mail_move_ok")
                 if (key := identity(row)) is not None}
    outstanding: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in pending_processed_moves(queue_root):
        key = identity(row)
        if key is None:
            # Old records without an exact account, mailbox, UID and digest remain untouched.
            results.append({"status": "manual_review", "code": "MIGRATION_REQUIRED",
                            "uid": row.get("uid")})
        elif key[0] == account_id and key not in completed:
            outstanding[key] = row
    for record in outstanding.values():
        uid = record.get("uid")
        got = ctrl.dispatch("mail_get", {"acct": account_id, "mailbox": record["mailbox"], "id": uid,
            "detail": True, "max_message_bytes": MAX_MESSAGE, "include_attachment_content": True,
            "max_attachment_content_bytes": MAX_ATTACHMENT})
        if not got.get("ok"):
            results.append({"uid": uid, "status": "reject", "code": got.get("code")})
            continue
        try:
            blob = _blob_attachment(got.get("attachments"))
        except (ValueError, binascii.Error):
            results.append({"uid": uid, "status": "reject", "code": "INVALID_ATTACHMENT"})
            continue
        digest = hashlib.sha256(blob).hexdigest()
        if digest != record["transport_id"]:
            results.append({"uid": uid, "status": "reject", "code": "CIPHERTEXT_CHANGED"})
            continue
        result = ctrl.dispatch("mail_mutate", {"acct": account_id, "mailbox": record["mailbox"],
            "mutate_action": "move_messages", "uids": [uid],
            "target_mailbox": record.get("target_mailbox", "LocalsetupAgentQ/Processed"),
            "count": 1, "confirm_token": confirm_token})
        if result.get("ok"):
            append_event(queue_root, "mail_move_ok", {"account_id": account_id,
                "uid": uid, "mailbox": record["mailbox"],
                "target_mailbox": record.get("target_mailbox", "LocalsetupAgentQ/Processed"),
                "ciphertext_sha256": digest},
                transport_id=record.get("transport_id"))
        else:
            append_event(queue_root, "pending_processed_move", {"account_id": account_id,
                "uid": uid, "mailbox": record["mailbox"],
                "target_mailbox": record.get("target_mailbox", "LocalsetupAgentQ/Processed"),
                "ciphertext_sha256": digest, "code": result.get("code"), "retry": True},
                transport_id=record.get("transport_id"))
        results.append({"uid": uid, "ok": bool(result.get("ok")), "code": result.get("code")})
    return results
