"""Agent Q command line with explicit private registry and expected peer."""
from __future__ import annotations
import argparse
import json
import sys
from . import cli_commands as cmd

def _registry_peer(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry", required=True)
    parser.add_argument("--peer", required=True)

def _manifest(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--queue", default="")
    parser.add_argument("--skip-pre-ship", action="store_true")
    parser.add_argument("--pre-ship-cwd", default="")

def main() -> int:
    parser = argparse.ArgumentParser(prog="agentq")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("version"); p.set_defaults(run=cmd.cmd_version)
    p = sub.add_parser("stamp-prd"); p.add_argument("path"); p.add_argument("--hash", action="store_true"); p.set_defaults(run=cmd.cmd_stamp_prd)
    p = sub.add_parser("key-fingerprint"); p.add_argument("path"); p.set_defaults(run=cmd.cmd_key_fingerprint)
    p = sub.add_parser("key-gen"); p.add_argument("output"); p.add_argument("--home", required=True)
    p.add_argument("--name", required=True); p.add_argument("--email", required=True)
    p.add_argument("--secret-provider", choices=("env", "envman"), required=True); p.add_argument("--secret-name", required=True)
    p.set_defaults(run=cmd.cmd_key_gen)
    p = sub.add_parser("registry-validate"); p.add_argument("path"); p.set_defaults(run=cmd.cmd_registry_validate)
    p = sub.add_parser("key-import"); _registry_peer(p); p.set_defaults(run=cmd.cmd_key_import)
    p = sub.add_parser("key-export"); p.add_argument("--registry", required=True)
    p.add_argument("--fingerprint", required=True); p.add_argument("--output", required=True)
    p.set_defaults(run=cmd.cmd_key_export)
    p = sub.add_parser("ship-file-drop"); _manifest(p); p.add_argument("--registry", required=True); p.add_argument("--peer", required=True); p.add_argument("--out", required=True)
    p.set_defaults(run=cmd.cmd_ship_file_drop)
    p = sub.add_parser("ship-file-drop-multi"); _manifest(p); p.add_argument("--registry", required=True); p.add_argument("--out", required=True)
    p.set_defaults(run=cmd.cmd_ship_file_drop_multi)
    p = sub.add_parser("ship-bundle"); _registry_peer(p); p.add_argument("src_dir"); p.add_argument("--out", required=True)
    p.add_argument("--queue", default=""); p.add_argument("--skip-pre-ship", action="store_true")
    p.set_defaults(run=cmd.cmd_ship_bundle)
    p = sub.add_parser("ingest-blob"); _registry_peer(p); p.add_argument("blob"); p.add_argument("--queue", required=True)
    p.add_argument("--signer-fingerprint", default=""); p.add_argument("--force", action="store_true")
    p.add_argument("--operator", default=""); p.add_argument("--reason", default="")
    p.set_defaults(run=cmd.cmd_ingest_blob)
    p = sub.add_parser("file-drop-poll"); _registry_peer(p); p.add_argument("--queue", required=True)
    p.add_argument("--root", action="append", default=[]); p.add_argument("--signer-fingerprint", default="")
    p.add_argument("--lim", type=int, default=50); p.add_argument("--use-lockfile", action="store_true")
    p.set_defaults(run=cmd.cmd_file_drop_poll)
    p = sub.add_parser("ship-mail"); _manifest(p); p.add_argument("--registry", required=True); p.add_argument("--peer", required=True)
    p.add_argument("--account", required=True); p.add_argument("--from-addr", required=True); p.add_argument("--to", required=True)
    p.add_argument("--policy", required=True); p.add_argument("--accounts", required=True)
    p.set_defaults(run=cmd.cmd_ship_mail)
    p = sub.add_parser("mail-pull"); _registry_peer(p); p.add_argument("--queue", required=True)
    p.add_argument("--account", required=True); p.add_argument("--policy", required=True); p.add_argument("--accounts", required=True)
    p.add_argument("--mailbox", default="INBOX"); p.add_argument("--post-mailbox", default="LocalsetupAgentQ/Processed")
    p.add_argument("--query", default="UNSEEN"); p.add_argument("--lim", type=int, default=25)
    p.add_argument("--signer-fingerprint", default=""); p.add_argument("--confirm-token", default="")
    p.set_defaults(run=cmd.cmd_mail_pull)
    p = sub.add_parser("mail-move-retry"); p.add_argument("--queue", required=True); p.add_argument("--account", required=True)
    p.add_argument("--policy", required=True); p.add_argument("--accounts", required=True); p.add_argument("--confirm-token", default="")
    p.set_defaults(run=cmd.cmd_mail_move_retry)
    p = sub.add_parser("queue-pending"); p.add_argument("--queue", required=True); p.add_argument("--transport-id", default="")
    p.add_argument("--list", action="store_true", dest="list_only"); p.set_defaults(run=cmd.cmd_queue_pending)
    p = sub.add_parser("archive-prune"); p.add_argument("archive_root"); p.add_argument("--days", type=float, default=0)
    p.add_argument("--max-gb", type=float, default=0); p.add_argument("--dry-run", action="store_true"); p.set_defaults(run=cmd.cmd_archive_prune)
    p = sub.add_parser("prune-processed"); p.add_argument("processed_root"); p.add_argument("--days", type=float, default=30)
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(run=cmd.cmd_prune_processed)
    p = sub.add_parser("doctor"); p.add_argument("--registry", default=""); p.set_defaults(run=cmd.cmd_doctor)
    args = parser.parse_args()
    try:
        return args.run(args)
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(json.dumps({"status": "error", "code": str(code)}), file=sys.stderr)
        return 1
