"""CLI handlers for the authenticated Agent Q transport."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

def _show(result: object) -> int:
    print(json.dumps(result, indent=2, default=str))
    if isinstance(result, dict):
        return 0 if result.get("status") in {"ok", "skipped"} or result.get("ok") else 1
    if isinstance(result, list):
        return 0 if all(not isinstance(row, dict) or (row.get("status") not in {"error", "reject", "manual_review"}
                        and row.get("ok") is not False)
                        for row in result) else 1
    return 0

def cmd_version(_: argparse.Namespace) -> int:
    from .version_util import read_framework_version, read_framework_hash
    return _show({"status": "ok", "version": read_framework_version(), "hash": read_framework_hash()})

def cmd_stamp_prd(args: argparse.Namespace) -> int:
    from .prd_stamp import ensure_prd_stamp
    return _show({"status": "ok", "modified": ensure_prd_stamp(Path(args.path), add_hash=args.hash)})

def cmd_key_fingerprint(args: argparse.Namespace) -> int:
    from .keygen import fingerprint_for_certificate
    return _show({"status": "ok", "fingerprint": fingerprint_for_certificate(Path(args.path))})

def cmd_key_gen(args: argparse.Namespace) -> int:
    from .keygen import generate_keypair_gnupg
    public, fingerprint = generate_keypair_gnupg(Path(args.output), name=args.name, email=args.email,
        home=Path(args.home), secret_provider=args.secret_provider, secret_name=args.secret_name)
    return _show({"status": "ok", "public": str(public), "fingerprint": fingerprint})

def cmd_registry_validate(args: argparse.Namespace) -> int:
    from .registry import load_registry_yaml
    value = load_registry_yaml(Path(args.path))
    return _show({"status": "ok", "version": value["version"], "peers": list(value["agents"])})

def cmd_key_import(args: argparse.Namespace) -> int:
    from .keygen import import_peer_certificate
    from .registry import load_registry_yaml
    return _show({"status": "ok", "fingerprint": import_peer_certificate(
        load_registry_yaml(Path(args.registry)), args.peer)})

def cmd_key_export(args: argparse.Namespace) -> int:
    from .keygen import export_public_certificate
    from .registry import load_registry_yaml
    return _show({"status": "ok", "fingerprint": export_public_certificate(
        load_registry_yaml(Path(args.registry)), Path(args.output), args.fingerprint)})

def cmd_ship_file_drop(args: argparse.Namespace) -> int:
    from .ship import load_manifest_from_path, ship_file_drop
    manifest = load_manifest_from_path(Path(args.manifest))
    if manifest.get("to_agent_ids") != [args.peer]:
        return _show({"status": "error", "code": "RECIPIENT_SET_INVALID"})
    result = ship_file_drop(manifest, Path(args.registry), Path(args.out),
        queue_root=Path(args.queue) if args.queue else None, skip_pre_ship=args.skip_pre_ship,
        pre_ship_cwd=Path(args.pre_ship_cwd) if args.pre_ship_cwd else None)
    return _show(result)

def cmd_ship_file_drop_multi(args: argparse.Namespace) -> int:
    from .ship import load_manifest_from_path, ship_file_drop_multi
    results = ship_file_drop_multi(load_manifest_from_path(Path(args.manifest)), Path(args.registry), Path(args.out),
        queue_root=Path(args.queue) if args.queue else None, skip_pre_ship=args.skip_pre_ship,
        pre_ship_cwd=Path(args.pre_ship_cwd) if args.pre_ship_cwd else None)
    return _show(results)

def cmd_ship_bundle(args: argparse.Namespace) -> int:
    from .bundle import ship_bundle_file_drop
    from .registry import load_registry_yaml
    registry = load_registry_yaml(Path(args.registry))
    result = ship_bundle_file_drop(Path(args.src_dir), Path(args.registry), Path(args.out),
        from_agent_id=registry["local_agent_id"], to_agent_ids=[args.peer],
        queue_root=Path(args.queue) if args.queue else None, skip_pre_ship=args.skip_pre_ship)
    return _show(result)

def cmd_ingest_blob(args: argparse.Namespace) -> int:
    from .ingest import ingest_file_drop_blob
    return _show(ingest_file_drop_blob(Path(args.blob), queue_root=Path(args.queue),
        registry_path=Path(args.registry), peer_id=args.peer,
        signer_fingerprint=args.signer_fingerprint or None, force=args.force,
        operator=args.operator, reason=args.reason))

def cmd_file_drop_poll(args: argparse.Namespace) -> int:
    from .ingest import run_file_drop_poll
    from .registry import load_registry_yaml, file_drop_inbound_roots
    registry = load_registry_yaml(Path(args.registry))
    roots = [Path(value) for value in args.root] or file_drop_inbound_roots(registry, args.peer)
    return _show(run_file_drop_poll(roots, queue_root=Path(args.queue), registry_path=Path(args.registry),
        peer_id=args.peer, signer_fingerprint=args.signer_fingerprint or None,
        max_per_poll=args.lim, use_lockfile=args.use_lockfile))

def cmd_ship_mail(args: argparse.Namespace) -> int:
    from .mail_adapter import mail_ship_agentq_outer
    from .ship import load_manifest_from_path
    result = mail_ship_agentq_outer(account_id=args.account, policy_path=Path(args.policy),
        accounts_path=Path(args.accounts), registry_path=Path(args.registry),
        manifest=load_manifest_from_path(Path(args.manifest)), peer_id=args.peer,
        to_addr=args.to, from_addr=args.from_addr,
        queue_root=Path(args.queue) if args.queue else None, skip_pre_ship=args.skip_pre_ship,
        pre_ship_cwd=Path(args.pre_ship_cwd) if args.pre_ship_cwd else None)
    return _show(result)

def cmd_mail_pull(args: argparse.Namespace) -> int:
    from .mail_adapter import mail_pull_and_promote
    return _show(mail_pull_and_promote(queue_root=Path(args.queue), account_id=args.account,
        policy_path=Path(args.policy), accounts_path=Path(args.accounts), registry_path=Path(args.registry),
        peer_id=args.peer, signer_fingerprint=args.signer_fingerprint or None,
        mailbox=args.mailbox, post_ingest_mailbox=args.post_mailbox,
        query=args.query, lim=args.lim, confirm_token=args.confirm_token))

def cmd_mail_move_retry(args: argparse.Namespace) -> int:
    from .mail_adapter import mail_retry_pending_moves
    return _show(mail_retry_pending_moves(queue_root=Path(args.queue), account_id=args.account,
        policy_path=Path(args.policy), accounts_path=Path(args.accounts), confirm_token=args.confirm_token))

def cmd_queue_pending(args: argparse.Namespace) -> int:
    from .queue_ops import list_in_ready, move_ack_required_to_pending, move_to_pending
    queue = Path(args.queue)
    if args.list_only:
        return _show(list_in_ready(queue))
    return _show(move_to_pending(queue, args.transport_id) if args.transport_id else move_ack_required_to_pending(queue))

def cmd_archive_prune(args: argparse.Namespace) -> int:
    from .queue_archive import prune_archive
    return _show(prune_archive(Path(args.archive_root), older_than_days=args.days or None,
        max_total_gb=args.max_gb or None, dry_run=args.dry_run))

def cmd_prune_processed(args: argparse.Namespace) -> int:
    from .prune import prune_processed
    return _show(prune_processed(Path(args.processed_root), older_than_days=args.days, dry_run=args.dry_run))

def cmd_doctor(args: argparse.Namespace) -> int:
    import shutil
    if shutil.which("gpg") is None:
        return _show({"status": "error", "code": "GPG_NOT_FOUND"})
    if args.registry:
        from .registry import load_registry_yaml
        load_registry_yaml(Path(args.registry))
    return _show({"status": "ok"})
