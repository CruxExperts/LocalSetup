"""CLI adapter for explicit heartbeat connection configuration and inspection."""
from pathlib import Path


def arguments(parent, target_flags):
    readiness = parent.add_parser('readiness', help='Inspect the selected LSCli agent without executing it')
    target_flags(readiness)
    readiness.add_argument('--probe', action='store_true', help='Make one bounded provider attempt; review discloses selected granted context, coding runs native preflight with model tools disabled')
    parser = parent.add_parser('configure-lscli', help='Preview or apply an existing LSCli connection without activating cron')
    target_flags(parser)
    parser.add_argument('--mode', required=True, choices=('review', 'coding'))
    parser.add_argument('--provider-profile', required=True)
    parser.add_argument('--grant', required=True, type=Path)
    parser.add_argument('--context', action='append', help='Explicit relative review input; repeat for multiple files')
    for name in ('executable', 'profiles', 'runtime-root', 'state-root', 'resource-parent'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--apply', action='store_true', help='Back up and replace configuration; default is a read-only preview')
    parser.add_argument('--expected-config-sha256', help='Refuse if current configuration differs from this preview digest')


def execute(args, repo_root, target_root):
    if args.harness_action == 'readiness':
        from .harness_readiness import inspect
        return inspect(repo_root, target_root, probe=args.probe)
    from .harness_configuration import configure
    return configure(repo_root, target_root, **{name: getattr(args, name) for name in (
        'mode', 'provider_profile', 'grant', 'context', 'executable', 'profiles',
        'runtime_root', 'state_root', 'resource_parent', 'apply', 'expected_config_sha256')})
