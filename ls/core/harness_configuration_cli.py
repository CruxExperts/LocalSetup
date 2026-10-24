"""CLI adapter for explicit heartbeat provider connection configuration."""
from pathlib import Path


def arguments(parent, target_flags):
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
    from .harness_configuration import configure
    return configure(repo_root, target_root, **{name: getattr(args, name) for name in (
        'mode', 'provider_profile', 'grant', 'context', 'executable', 'profiles',
        'runtime_root', 'state_root', 'resource_parent', 'apply', 'expected_config_sha256')})
