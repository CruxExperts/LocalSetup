"""Thin CLI for explicitly enrolled automatic container indexing."""
import argparse
import json
from pathlib import Path

from ..terminal_ops import command_scheduler as scheduler
from .container_schedule import schedule_id
from .container_runtime import RuntimeErrorCode, load_runtime_config


def main(argv=None, *, source_root: Path, home: Path | None = None):
    parser = argparse.ArgumentParser(prog="localsetup container")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, help="Private shared scheduler state")
    actions = parser.add_subparsers(dest="action", required=True)
    install = actions.add_parser("enable-indexing", help="Enroll the configured runtime for periodic metadata scans")
    install.add_argument("--grant-id", required=True)
    install.add_argument("--owner-pid", type=int)
    install.add_argument("--owner-starttime", type=int)
    install.add_argument("--agent-id")
    actions.add_parser("status", help="Inspect configuration and periodic indexing state")
    actions.add_parser("disable-indexing", help="Revoke scheduled scans without deleting catalog data")
    args = parser.parse_args(argv)
    try:
        identity = schedule_id(args.runtime_root)
        if args.action == "enable-indexing":
            result = scheduler.install_schedule(identity, source_root=source_root,
                state_root=args.state_root, grant_id=args.grant_id,
                authority_owner={"pid": args.owner_pid, "starttime": args.owner_starttime,
                                 "agent_id": args.agent_id},
                job={"runtime_root": str(args.runtime_root)})
        elif args.action == "disable-indexing":
            result = scheduler.disable_schedule(identity, state_root=args.state_root)
        else:
            result = scheduler.status_schedule(identity, source_root=source_root, state_root=args.state_root)
            config = load_runtime_config(args.runtime_root)
            result["configuration"] = {"enabled": config.enabled, "revision": config.revision,
                "scan_interval_seconds": config.scan_interval_seconds,
                "max_scan_objects": config.max_scan_objects,
                "max_metadata_bytes": config.max_metadata_bytes, "source_count": len(config.source_ids)}
    except scheduler.SchedulerError as error:
        result = {"ok": False, **error.as_dict()}
    except (RuntimeErrorCode, OSError, ValueError):
        result = {"ok": False, "code": "runtime_unavailable"}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") else 2
