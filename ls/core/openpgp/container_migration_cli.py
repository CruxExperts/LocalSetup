"""Controller-selected migration authority; observations never select keys."""
from pathlib import Path

from ..live_configuration_files import editable_json
from .container_consolidation_runtime import _context
from .container_legacy import LegacyVerification, MigrationError, inspect_legacy
from .container_migration import migrate_legacy
from .container_preparation import ContainerNamespace, ContainerPreparationLimits
from .container_runtime import _parse_key
from .container_storage import read_private_file


def add_commands(actions):
    inspect = actions.add_parser('inspect-legacy', help='Observe legacy format, size and exact digest without decryption')
    inspect.add_argument('--source', type=Path, required=True)
    inspect.add_argument('--timeout', type=int, default=300)
    migrate = actions.add_parser('migrate-legacy', help='Create and verify encrypted custody of exact legacy bytes')
    migrate.add_argument('--source', type=Path, required=True)
    migrate.add_argument('--expected-sha256', required=True)
    migrate.add_argument('--artifact-id', required=True)
    migrate.add_argument('--output-dir', type=Path, required=True)
    migrate.add_argument('--authority-file', type=Path, required=True)
    migrate.add_argument('--preserve-unverified', action='store_true')
    migrate.add_argument('--timeout', type=int, default=300)


def run(args):
    if not 1 <= args.timeout <= 3600:
        raise MigrationError('migration_invalid_deadline')
    limits = ContainerPreparationLimits(timeout_seconds=args.timeout)
    if args.action == 'inspect-legacy':
        return {'ok': True, **inspect_legacy(args.source, limits=limits)}
    raw = read_private_file(args.authority_file, maximum=128*1024)
    binding = editable_json(raw)
    if (type(binding) is not dict or set(binding) != {'schema', 'version', 'namespace',
            'publisher', 'controller', 'legacy'} or binding['schema'] != 'localsetup.legacy-migration-authority'
            or type(binding['version']) is not int or binding['version'] != 1):
        raise MigrationError('migration_authority_invalid')
    def current():
        if read_private_file(args.authority_file, maximum=128*1024) != raw:
            raise MigrationError('migration_authority_changed')
    paths = []
    publisher, publication = _context(binding['publisher'], args.artifact_id, 'publisher', None, paths)
    controller, approval = _context(binding['controller'], args.artifact_id, 'owner', None, paths)
    verification = None
    if binding['legacy'] is not None:
        value = binding['legacy']
        if type(value) is not dict or set(value) != {'trust_state_path', 'scope', 'role', 'signer', 'recipients', 'key'}:
            raise MigrationError('migration_authority_invalid')
        verification = LegacyVerification(Path(value['trust_state_path']), tuple(value['scope']),
            value['role'], value['signer'], tuple(value['recipients']), _parse_key(value['key']))
    prepared, receipt = migrate_legacy(args.source, expected_sha256=args.expected_sha256,
        output_dir=args.output_dir, artifact_id=args.artifact_id, namespace=ContainerNamespace(**binding['namespace']),
        publisher=publisher, controller=controller, publication_policy=publication, approval_policy=approval,
        verification=verification, allow_unverified=args.preserve_unverified, authorized=True,
        check=current, limits=limits)
    return dict(ok=True, **receipt, volume_count=len(prepared.volumes), output_dir=str(args.output_dir))
