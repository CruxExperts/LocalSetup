"""Static sealed-runtime dependency and native capability diagnostics."""
import ast
from email.parser import Parser
import os
from pathlib import Path
import shutil
import stat
import sys

from .native_bundle import _platform

LIMIT = 1024 * 1024
_OPENPGP_IMPORTS = {
    'contracts': frozenset({'ENVELOPE_FORMAT', 'ENVELOPE_SCHEMA_VERSION'}),
    'envelope': frozenset({'seal_envelope'}),
    'opening': frozenset({'open_envelope'}),
    'historical': frozenset({'open_historical_envelope'}),
    'recovery': frozenset({'create_protected_backup', 'restore_protected_backup'}),
    'transition': frozenset({
        'create_transition_proposal', 'encrypt_transition_proposal',
        'approve_transition_proposal', 'verify_approved_transition',
    }),
    'publishing_transition': frozenset({
        'create_publishing_transition_proposal', 'sign_publishing_transition_proposal',
        'approve_publishing_transition', 'verify_approved_publishing_transition',
    }),
    'trust_state': frozenset({
        'initialize_trust_state', 'load_trust_state', 'apply_owner_transition',
        'apply_publisher_transition', 'authorize_outbound', 'authorize_inbound_peer', 'record_accepted_content',
        'authorize_historical_content', 'revoke_authority',
    }),
    'recovery_transition': frozenset({
        'create_recovery_challenge', 'sign_recovery_challenge', 'submit_candidate_proof',
        'authorize_recovery_locally', 'enroll_recovery_key', 'apply_recovery_transition',
    }),
}
_OPENPGP_APIS = frozenset().union(*(
    apis for module, apis in _OPENPGP_IMPORTS.items() if module != 'contracts'
))
_OPENPGP_EXPORTS = frozenset().union(*_OPENPGP_IMPORTS.values())
_OPENPGP_MODULES = frozenset(_OPENPGP_IMPORTS)
_AGENTQ_FILES = (
    'tools/agentq_transport_client/agentq_cli.py',
    'tools/agentq_transport_client/agentq_transport_client/cli_parser.py',
    'tools/agentq_transport_client/agentq_transport_client/ship.py',
    'tools/agentq_transport_client/agentq_transport_client/ingest.py',
    'tools/agentq_transport_client/agentq_transport_client/crypto_pipeline.py',
    'tools/agentq_transport_client/agentq_transport_client/registry.py',
    'tools/agentq_transport_client/agentq_transport_client/adapters.py',
    'tools/agentq_transport_client/agentq_transport_client/file_drop.py',
    'tools/agentq_transport_client/agentq_transport_client/mail_adapter.py',
)


def _text(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            raise ValueError('Invalid installed metadata')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('Installed metadata exceeds bounds')
        return raw.decode('utf-8')
    finally:
        os.close(fd)


def _installed_site(release: Path) -> Path:
    return release / 'venv/lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'


def _python(path: Path) -> ast.Module:
    return ast.parse(_text(path), filename=path.name)


def _literal_assignments(tree: ast.Module) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
    return values


def _module_functions(tree: ast.Module) -> set[str]:
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _package_imports(tree: ast.Module) -> dict[str, set[tuple[str, str]]]:
    imports: dict[str, set[tuple[str, str]]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module is None:
            continue
        imports.setdefault(node.module, set()).update(
            (alias.name, alias.asname or alias.name) for alias in node.names
        )
    return imports


def _bounded_positive_int(expression: ast.expr, maximum: int) -> int:
    if isinstance(expression, ast.Constant) and type(expression.value) is int:
        value = expression.value
        if 0 < value <= maximum:
            return value
    elif isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Mult):
        left = _bounded_positive_int(expression.left, maximum)
        right = _bounded_positive_int(expression.right, maximum)
        if left <= maximum // right:
            return left * right
    raise ValueError('Invalid bounded installed integer')


def _reader_limits(tree: ast.Module) -> dict[str, int]:
    expected = {'MAX_PAGE_BYTES', 'MAX_PAGE_LINES', 'MAX_PAGE_RESPONSE'}
    values: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in expected:
                continue
            if target.id in values:
                raise ValueError('Duplicate installed reader limit')
            values[target.id] = _bounded_positive_int(node.value, 16 * 1024)
    if values.keys() != expected:
        raise ValueError('Missing installed reader limit')
    return values


def _calls_instance_method(method: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == 'self'
        and node.func.attr == name
        for node in ast.walk(method)
    )


def _openpgp(site: Path) -> dict:
    root = site / 'ls/core/openpgp'
    try:
        init_tree = _python(root / '__init__.py')
        exports = _literal_assignments(init_tree).get('__all__')
        if type(exports) not in (list, tuple) or any(type(item) is not str for item in exports):
            raise ValueError('Invalid installed OpenPGP exports')
        if len(exports) != len(set(exports)) or not _OPENPGP_EXPORTS.issubset(exports):
            raise ValueError('Incomplete installed OpenPGP exports')

        imports = _package_imports(init_tree)
        modules = {name: _python(root / f'{name}.py') for name in _OPENPGP_MODULES}
        helper_bindings = {
            'generation': ('generation_models', {
                'GeneratedKey', 'KeyGenerationError', 'KeyGenerationErrorCode', 'KeyIdentity',
            }),
            'keys': ('key_records', {
                'KeyEnrollment', 'KeyInspection', 'KeyInspectionError',
                'KeyInspectionErrorCode', 'KeyRecord', '_INSPECTION_SEAL', '_parse_colon_output',
            }),
            'trust_state': ('trust_schema', {
                '_SCHEMA', '_TABLES', '_V1_SQL', '_V1_TABLES',
                '_install_recovery_schema', '_normalized_sql',
            }),
            'recovery': ('recovery_models', {
                'ProtectedBackup', 'RecoveredKey', 'RecoveryError', 'RecoveryErrorCode',
                '_MAX_BACKUP_BYTES', '_capabilities', '_fingerprint',
                '_resolve_passphrase', '_validate_policy_inputs', '_validated_executable',
            }),
        }
        for owner, (helper, names) in helper_bindings.items():
            owner_tree = modules.get(owner) or _python(root / f'{owner}.py')
            helper_tree = _python(root / f'{helper}.py')
            if not {(name, name) for name in names}.issubset(
                _package_imports(owner_tree).get(helper, set())
            ):
                raise ValueError('Missing installed OpenPGP helper binding')
            definitions = _module_functions(helper_tree) | {
                node.name for node in helper_tree.body if isinstance(node, ast.ClassDef)
            } | {
                target.id for node in helper_tree.body
                for target in (node.targets if isinstance(node, ast.Assign)
                               else [node.target] if isinstance(node, ast.AnnAssign) else [])
                if isinstance(target, ast.Name)
            }
            if not names.issubset(definitions):
                raise ValueError('Missing installed OpenPGP helper implementation')
        recovery_tree = modules['recovery']
        recovery_aliases = {
            (alias.name, alias.asname or alias.name)
            for node in recovery_tree.body
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
            for alias in node.names
        }
        if not {('recovery_io', '_recovery_io'),
                ('recovery_process', '_recovery_process')}.issubset(recovery_aliases):
            raise ValueError('Missing installed recovery helper bindings')
        for helper, names in {
            'recovery_io': {'_existing_private_home', '_new_empty_home', '_open_backup',
                            '_create_backup_file', '_overlaps', '_clear_restore_home',
                            '_selected_home_path', '_validate_private_home_contents'},
            'recovery_process': {'_run_backup_pipeline', '_run_restore_pipeline',
                                 '_require_no_secret_keys', '_gpg_environment',
                                 '_managed_home_agents', '_run_pipeline', '_run_bounded_gpg',
                                 '_validate_backup_recipient'},
        }.items():
            if not names.issubset(_module_functions(_python(root / f'{helper}.py'))):
                raise ValueError('Missing installed recovery helper implementation')
        for module, apis in _OPENPGP_IMPORTS.items():
            if not {(api, api) for api in apis}.issubset(imports.get(module, set())):
                raise ValueError('Missing installed OpenPGP public import')
            definitions = (
                _literal_assignments(modules[module]).keys()
                if module == 'contracts' else _module_functions(modules[module])
            )
            if not apis.issubset(definitions):
                raise ValueError('Missing installed OpenPGP implementation')

        contracts = _literal_assignments(modules['contracts'])
        owner = _literal_assignments(modules['transition'])
        publisher_records = _python(root / 'publishing_records.py')
        publisher_imports = _package_imports(modules['publishing_transition'])
        publisher_names = {
            '_RECORD_FORMAT', '_SCHEMA_VERSION', '_PROPOSAL_FORMAT',
            '_PROOF_FORMAT', '_APPROVAL_FORMAT', '_record_object',
            '_validate_record_shape', '_decode_proof', 'PublishingTransitionProposal',
            'PublishingTransitionProof', 'ApprovedPublishingTransitionRecord',
            'VerifiedPublishingTransition',
        }
        if not {(name, name) for name in publisher_names}.issubset(
            publisher_imports.get('publishing_records', set())
        ):
            raise ValueError('Missing installed publisher record bindings')
        if not {'_record_object', '_validate_record_shape', '_encode',
                '_decode_proposal', '_decode_proof', '_decode_approval'}.issubset(
            _module_functions(publisher_records)
        ):
            raise ValueError('Missing installed publisher record implementation')
        if not {'PublishingTransitionProposal', 'PublishingTransitionProof',
                'ApprovedPublishingTransitionRecord', 'VerifiedPublishingTransition'}.issubset({
            node.name for node in publisher_records.body if isinstance(node, ast.ClassDef)
        }):
            raise ValueError('Missing installed publisher record classes')
        publisher = _literal_assignments(publisher_records)
        recovery = modules['recovery_transition']
        challenge = any(
            isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == 'format'
                    and isinstance(value, ast.Constant)
                    and value.value == 'localsetup.openpgp.recovery-challenge'
                    for key, value in zip(node.keys, node.values))
            and any(isinstance(key, ast.Constant) and key.value == 'schema_version'
                    and isinstance(value, ast.Constant) and value.value == 1
                    for key, value in zip(node.keys, node.values))
            for node in ast.walk(recovery)
        )
        schemas = {
            'envelope': {'format': contracts.get('ENVELOPE_FORMAT'),
                         'version': contracts.get('ENVELOPE_SCHEMA_VERSION')},
            'owner_transition': {
                'format': owner.get('_RECORD_FORMAT'),
                'version': owner.get('_RECORD_SCHEMA_VERSION'),
                'proposal_format': owner.get('_PROPOSAL_FORMAT'),
                'approval_format': owner.get('_APPROVAL_FORMAT'),
            },
            'publisher_transition': {
                'format': publisher.get('_RECORD_FORMAT'),
                'version': publisher.get('_SCHEMA_VERSION'),
                'proposal_format': publisher.get('_PROPOSAL_FORMAT'),
                'proof_format': publisher.get('_PROOF_FORMAT'),
                'approval_format': publisher.get('_APPROVAL_FORMAT'),
            },
            'recovery_challenge': {'format': 'localsetup.openpgp.recovery-challenge',
                                   'version': 1 if challenge else None},
        }
        if (
            schemas['envelope'] != {'format': 'localsetup.openpgp-envelope', 'version': 1}
            or schemas['owner_transition'] != {
                'format': 'localsetup.openpgp-owner-transition', 'version': 1,
                'proposal_format': 'localsetup.openpgp-owner-transition-proposal',
                'approval_format': 'localsetup.openpgp-owner-transition-approval'}
            or schemas['publisher_transition'] != {
                'format': 'localsetup.openpgp-publishing-transition', 'version': 1,
                'proposal_format': 'localsetup.openpgp-publishing-transition-proposal',
                'proof_format': 'localsetup.openpgp-publishing-transition-proof',
                'approval_format': 'localsetup.openpgp-publishing-transition-approval'}
            or not challenge
        ):
            raise ValueError('Invalid installed OpenPGP schema metadata')
        return {'status': 'present', 'execution_tested': False,
                'schemas': schemas, 'apis': sorted(_OPENPGP_APIS)}
    except FileNotFoundError:
        status = 'missing'
    except (OSError, ValueError, TypeError, SyntaxError, UnicodeError, RecursionError):
        status = 'invalid'
    return {'status': status, 'execution_tested': False}


def _reader(site: Path) -> dict:
    path = site / 'ls/core/agent/file_broker.py'
    try:
        tree = _python(path)
        limits = _reader_limits(tree)
        broker = next((node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == 'FileBroker'), None)
        methods = {} if broker is None else {
            node.name: node for node in broker.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        read_page = methods.get('read_page')
        read_locked = methods.get('_read_page_locked')
        page_response = methods.get('_page_response')
        if (
            read_page is None or read_locked is None or page_response is None
            or not _calls_instance_method(read_page, '_read_page_locked')
            or not _calls_instance_method(read_locked, '_page_response')
        ):
            raise ValueError('Installed reader methods are not wired')
        response_keys = None
        for child in ast.walk(page_response):
            if not isinstance(child, ast.Assign) or not any(
                isinstance(target, ast.Name) and target.id == 'result' for target in child.targets
            ) or not isinstance(child.value, ast.Dict):
                continue
            keys = [key.value for key in child.value.keys if isinstance(key, ast.Constant)]
            if len(keys) == len(child.value.keys) and all(type(key) is str for key in keys):
                response_keys = sorted(keys)
                break
        expected_response = ['bytes', 'content', 'encoding', 'next_cursor', 'revision']
        page_bytes = limits.get('MAX_PAGE_BYTES')
        page_lines = limits.get('MAX_PAGE_LINES')
        response_bytes = limits.get('MAX_PAGE_RESPONSE')
        if (
            type(page_bytes) is not int or not 0 < page_bytes <= 8 * 1024
            or type(page_lines) is not int or not 0 < page_lines <= 200
            or type(response_bytes) is not int or not 0 < response_bytes <= 16 * 1024
            or response_keys != expected_response
        ):
            raise ValueError('Invalid installed reader contract')
        return {'status': 'verified', 'execution_tested': False,
                'max_page_bytes': page_bytes, 'max_page_lines': page_lines,
                'max_response_bytes': response_bytes, 'response_fields': response_keys}
    except FileNotFoundError:
        status = 'missing'
    except (OSError, ValueError, TypeError, SyntaxError, UnicodeError, RecursionError):
        status = 'invalid'
    return {'status': status, 'execution_tested': False}


def _files(site: Path, names: tuple[str, ...]) -> dict:
    try:
        for name in names:
            if not _text(site / 'ls' / name).strip():
                raise ValueError('Empty installed transport source')
        return {'status': 'present', 'file_count': len(names), 'execution_tested': False}
    except FileNotFoundError:
        status = 'missing'
    except (OSError, ValueError, TypeError, UnicodeError):
        status = 'invalid'
    return {'status': status, 'execution_tested': False}


def _workflow(site: Path, name: str) -> str:
    base = site / 'ls/workflows' / name
    try:
        if not _text(base / 'SKILL.md').strip() or not _text(base / 'workflow.yaml').strip():
            raise ValueError('Empty installed workflow metadata')
        return 'present'
    except FileNotFoundError:
        return 'missing'
    except (OSError, ValueError, TypeError, UnicodeError):
        return 'invalid'


def capabilities(release: Path) -> dict:
    """Inspect materialized runtime files without importing or executing them."""
    site = _installed_site(release)
    gpg = shutil.which('gpg')
    return {
        'openpgp': _openpgp(site),
        'reader': _reader(site),
        'agentq_transport': _files(site, _AGENTQ_FILES),
        'workflows': {
            'openpgp_lifecycle': _workflow(site, 'ls-workflow-openpgp-lifecycle'),
            'compact_worker': _workflow(site, 'ls-workflow-lscli-compact-worker'),
        },
        'gnupg': {'status': 'present_unprobed' if gpg else 'missing',
                  'execution_tested': False},
        'crypto_execution': {'status': 'not_tested', 'execution_tested': False},
    }


def dependencies(release: Path) -> dict:
    """Call only while the owning selected-runtime inventory lease is held."""
    try:
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
        from packaging.version import Version
        site = _installed_site(release)
        expected = {}
        for filename in ('sdk-runtime.lock', 'sdk-build.lock'):
            raw = _text(site / 'ls/config' / filename)
            for line in raw.splitlines():
                if not line.strip() or line.lstrip().startswith(('#', '--hash=')):
                    continue
                requirement = Requirement(line.removesuffix('\\').strip())
                if requirement.url or requirement.extras:
                    raise ValueError('Unsupported installed dependency declaration')
                if requirement.marker is not None and not requirement.marker.evaluate():
                    continue
                name = canonicalize_name(requirement.name)
                if name in expected and expected[name] != requirement.specifier:
                    raise ValueError('Conflicting installed dependency declarations')
                expected[name] = requirement.specifier
        if not expected or len(expected) > 256:
            raise ValueError('Invalid dependency inventory size')
        paths = list(site.glob('*.dist-info/METADATA'))
        if len(paths) > 512:
            raise ValueError('Installed distribution inventory exceeds bounds')
        actual = {}
        for path in paths:
            metadata = Parser().parsestr(_text(path), headersonly=True)
            names, versions = metadata.get_all('Name', []), metadata.get_all('Version', [])
            if len(names) != 1 or len(versions) != 1:
                raise ValueError('Ambiguous installed distribution identity')
            name = canonicalize_name(names[0], validate=True)
            Version(versions[0])
            if name in actual:
                raise ValueError('Duplicate installed distribution')
            actual[name] = versions[0]
        missing = sorted(set(expected) - set(actual))
        mismatched = sorted(name for name in expected if name in actual and actual[name] not in expected[name])
        return {'status': 'verified' if not missing and not mismatched else 'mismatch',
                'expected_count': len(expected), 'missing': missing, 'mismatched': mismatched}
    except (ImportError, OSError, ValueError, TypeError, UnicodeError):
        return {'status': 'unavailable'}


def native(release: Path) -> dict:
    try:
        _platform()
    except ValueError:
        return {'status': 'unsupported_platform', 'execution_tested': False}
    binary = release / 'venv/lscli-native/bwrap'
    try:
        info = binary.lstat()
    except FileNotFoundError:
        return {'status': 'missing', 'execution_tested': False}
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR:
        return {'status': 'invalid', 'execution_tested': False}
    return {'status': 'present_unprobed', 'execution_tested': False}
