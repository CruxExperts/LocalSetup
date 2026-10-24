"""Thin CLI for explicitly enrolled automatic container indexing."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from ..terminal_ops import command_scheduler as scheduler
from .container_schedule import schedule_id
from .container_runtime import load_runtime_config


def main(argv=None, *, source_root: Path, home: Path | None = None):
    parser = argparse.ArgumentParser(prog="localsetup container")
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--state-root", type=Path, help="Private shared scheduler state")
    actions = parser.add_subparsers(dest="action", required=True)
    install = actions.add_parser("enable-indexing", help="Enroll the configured runtime for periodic metadata scans")
    install.add_argument("--grant-id", required=True)
    install.add_argument("--owner-pid", type=int)
    install.add_argument("--owner-starttime", type=int)
    install.add_argument("--agent-id")
    actions.add_parser("status", help="Inspect configuration and periodic indexing state")
    actions.add_parser("disable-indexing", help="Revoke scheduled scans without deleting catalog data")
    configuration = actions.add_parser('configuration', help='Bind signed resource tuning without enabling the runtime')
    configuration.add_argument('--authority-file', type=Path, required=True)
    configuration.add_argument('--config-owner', required=True)
    binding = actions.add_parser('configure-consolidation', help='Bind explicit snapshot write authority')
    binding.add_argument('--authority-file', type=Path, required=True)
    binding.add_argument('--expected-revision', type=int, required=True)
    revoke = actions.add_parser('disable-consolidation', help='Revoke automatic snapshot publication without deleting history')
    revoke.add_argument('--expected-revision', type=int, required=True)
    consolidate = actions.add_parser('consolidate', help='Consolidate one exact artifact/version through its configured mode')
    consolidate.add_argument('--reference', required=True)
    consolidate.add_argument('--event', choices=('manual', 'change', 'scheduled'), default='manual')
    consolidate.add_argument('--timeout', type=int, default=300)
    from .container_migration_cli import add_commands
    add_commands(actions)
    args = parser.parse_args(argv)
    migration = args.action in ('inspect-legacy', 'migrate-legacy')
    if not migration and args.runtime_root is None:
        parser.error('--runtime-root is required for this operation')
    try:
        identity = None if migration else schedule_id(args.runtime_root)
        if migration:
            from .container_migration_cli import run
            result = run(args)
        elif args.action == "enable-indexing":
            result = scheduler.install_schedule(identity, source_root=source_root,
                state_root=args.state_root, grant_id=args.grant_id,
                authority_owner={"pid": args.owner_pid, "starttime": args.owner_starttime,
                                 "agent_id": args.agent_id},
                job={"runtime_root": str(args.runtime_root)})
        elif args.action == "disable-indexing":
            result = scheduler.disable_schedule(identity, state_root=args.state_root)
        elif args.action == 'configuration':
            from .container_live_configuration import bind_configuration
            result = bind_configuration(args.runtime_root, authority_file=args.authority_file,
                config_owner=args.config_owner, home=home or Path.home(), authorized=True)
        elif args.action == 'configure-consolidation':
            from .container_consolidation_runtime import bind_consolidation
            config = bind_consolidation(args.runtime_root, args.authority_file,
                expected_revision=args.expected_revision, authorized=True)
            result = {'ok': True, 'state': 'bound', 'revision': config.revision}
        elif args.action == 'disable-consolidation':
            from .container_runtime import configure_runtime
            config = configure_runtime(args.runtime_root, expected_revision=args.expected_revision,
                proposed=replace(load_runtime_config(args.runtime_root), consolidation=None))
            result = {'ok': True, 'state': 'disabled', 'revision': config.revision}
        elif args.action == 'consolidate':
            from .container_access import resolve_catalog
            if not 1 <= args.timeout <= 3600:
                raise ValueError('Invalid consolidation deadline')
            deadline = time.monotonic()+args.timeout
            service = resolve_catalog(args.runtime_root, deadline=deadline)
            result = {'ok': True, **service.consolidate(args.reference, trigger=args.event, deadline=deadline)}
        else:
            result = scheduler.status_schedule(identity, source_root=source_root, state_root=args.state_root)
            config = load_runtime_config(args.runtime_root)
            from .container_live_configuration import ContainerConfiguration
            consumer = ContainerConfiguration(args.runtime_root, config)
            config = consumer.config
            result["configuration"] = {"enabled": config.enabled, "revision": config.revision,
                "scan_interval_seconds": config.scan_interval_seconds,
                "max_scan_objects": config.max_scan_objects,
                "max_metadata_bytes": config.max_metadata_bytes, "source_count": len(config.source_ids),
                "signed_revision": None if consumer.shared is None else consumer.shared.accepted.revision,
                "signed_digest": None if consumer.shared is None else consumer.shared.accepted.digest,
                "consolidation_configured": config.consolidation is not None}
    except scheduler.SchedulerError as error:
        result = {"ok": False, **error.as_dict()}
    except Exception as error:
        from ..live_configuration import ConfigurationError
        from .container_reader import ContainerReadError
        from .container_transfer import TransferError
        from .container_worker_authority import WorkerAuthorityError
        from .container_legacy import MigrationError
        if isinstance(error, (ConfigurationError, ContainerReadError, TransferError, WorkerAuthorityError, MigrationError)):
            result = {'ok': False, 'code': error.code}
        else:
            result = {"ok": False, "code": "runtime_unavailable"}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") else 2
