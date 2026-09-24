import json
from pathlib import Path
import sys

import pytest

from ls.core.agent import installed_capabilities as checks


@pytest.fixture
def release(tmp_path):
    site = tmp_path / 'venv/lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    config = site / 'ls/config'
    config.mkdir(parents=True)
    (config / 'sdk-runtime.lock').write_text('fixture-dependency==1.2.3\nignored==9 ; python_version < "2"\n')
    (config / 'sdk-build.lock').write_text('fixture-dependency==1.2.3\n')
    metadata = site / 'fixture_dependency-1.2.3.dist-info/METADATA'
    metadata.parent.mkdir()
    metadata.write_text('Name: fixture-dependency\nVersion: 1.2.3\n')
    return tmp_path, metadata


def test_dependency_metadata_markers_missing_and_mismatch(release):
    root, metadata = release
    assert checks.dependencies(root) == {'status': 'verified', 'expected_count': 1, 'missing': [], 'mismatched': []}
    metadata.write_text('Name: fixture-dependency\nVersion: 1.2.2\n')
    assert checks.dependencies(root)['mismatched'] == ['fixture-dependency']
    metadata.unlink()
    assert checks.dependencies(root)['missing'] == ['fixture-dependency']


@pytest.mark.parametrize('damage', ['duplicate', 'oversized', 'symlink'])
def test_invalid_metadata_is_not_readiness(release, damage):
    root, metadata = release
    if damage == 'duplicate':
        metadata.write_text('Name: fixture-dependency\nName: other\nVersion: 1.2.3\n')
    elif damage == 'oversized':
        metadata.write_bytes(b'x' * (checks.LIMIT + 1))
    else:
        metadata.unlink()
        metadata.symlink_to('/dev/zero')
    assert checks.dependencies(root) == {'status': 'unavailable'}


def test_native_presence_never_probes_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, '_platform', lambda: None)
    assert checks.native(tmp_path) == {'status': 'missing', 'execution_tested': False}
    binary = tmp_path / 'venv/lscli-native/bwrap'
    binary.parent.mkdir(parents=True)
    binary.write_text('not executed')
    binary.chmod(0o700)
    assert checks.native(tmp_path) == {'status': 'present_unprobed', 'execution_tested': False}
    def unsupported():
        raise ValueError('unsupported')
    monkeypatch.setattr(checks, '_platform', unsupported)
    assert checks.native(tmp_path)['status'] == 'unsupported_platform'


@pytest.mark.parametrize('name,version', [('invalid!', '1.0'), ('extra', 'garbage')])
def test_malformed_extra_distribution_is_unavailable(release, name, version):
    root, metadata = release
    extra = metadata.parent.parent / 'extra-1.dist-info/METADATA'
    extra.parent.mkdir()
    extra.write_text(f'Name: {name}\nVersion: {version}\n')
    assert checks.dependencies(root) == {'status': 'unavailable'}


def _materialize_capability_files(release):
    site = release / 'venv/lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    pgp = site / 'ls/core/openpgp'
    pgp.mkdir(parents=True)
    apis = [
        'ENVELOPE_FORMAT', 'ENVELOPE_SCHEMA_VERSION',
        'apply_owner_transition', 'apply_publisher_transition', 'apply_recovery_transition',
        'approve_publishing_transition', 'approve_transition_proposal', 'authorize_historical_content',
        'authorize_inbound_peer', 'authorize_outbound', 'authorize_recovery_locally',
        'create_protected_backup', 'create_publishing_transition_proposal', 'create_recovery_challenge',
        'create_transition_proposal', 'encrypt_transition_proposal', 'enroll_recovery_key',
        'initialize_trust_state', 'load_trust_state',
        'open_envelope', 'open_historical_envelope', 'record_accepted_content', 'restore_protected_backup',
        'revoke_authority', 'seal_envelope', 'sign_publishing_transition_proposal',
        'sign_recovery_challenge', 'submit_candidate_proof', 'verify_approved_publishing_transition',
        'verify_approved_transition',
    ]
    api_groups = {
        'contracts': ['ENVELOPE_FORMAT', 'ENVELOPE_SCHEMA_VERSION'],
        'envelope': ['seal_envelope'],
        'opening': ['open_envelope'],
        'historical': ['open_historical_envelope'],
        'recovery': ['create_protected_backup', 'restore_protected_backup'],
        'transition': [
            'create_transition_proposal', 'encrypt_transition_proposal',
            'approve_transition_proposal', 'verify_approved_transition',
        ],
        'publishing_transition': [
            'create_publishing_transition_proposal', 'sign_publishing_transition_proposal',
            'approve_publishing_transition', 'verify_approved_publishing_transition',
        ],
        'trust_state': [
            'initialize_trust_state', 'load_trust_state', 'apply_owner_transition',
            'apply_publisher_transition', 'authorize_outbound', 'authorize_inbound_peer',
            'record_accepted_content', 'authorize_historical_content', 'revoke_authority',
        ],
        'recovery_transition': [
            'create_recovery_challenge', 'sign_recovery_challenge', 'submit_candidate_proof',
            'authorize_recovery_locally', 'enroll_recovery_key', 'apply_recovery_transition',
        ],
    }
    import_lines = [f"from .{module} import {', '.join(names)}"
                    for module, names in api_groups.items()]
    (pgp / '__init__.py').write_text(
        '\n'.join(import_lines) + '\n__all__ = ' + repr(apis) + '\n')
    module_sources = {
        'contracts': "ENVELOPE_FORMAT = 'localsetup.openpgp-envelope'\nENVELOPE_SCHEMA_VERSION = 1\n",
        'transition': "_RECORD_FORMAT = 'localsetup.openpgp-owner-transition'\n_RECORD_SCHEMA_VERSION = 1\n"
                      "_PROPOSAL_FORMAT = 'localsetup.openpgp-owner-transition-proposal'\n"
                      "_APPROVAL_FORMAT = 'localsetup.openpgp-owner-transition-approval'\n",
        'publishing_transition': "_RECORD_FORMAT = 'localsetup.openpgp-publishing-transition'\n_SCHEMA_VERSION = 1\n"
                                 "_PROPOSAL_FORMAT = 'localsetup.openpgp-publishing-transition-proposal'\n"
                                 "_PROOF_FORMAT = 'localsetup.openpgp-publishing-transition-proof'\n"
                                 "_APPROVAL_FORMAT = 'localsetup.openpgp-publishing-transition-approval'\n",
        'recovery_transition': "CHALLENGE = {'format': 'localsetup.openpgp.recovery-challenge', 'schema_version': 1}\n",
    }
    for module, names in api_groups.items():
        if module == 'contracts':
            continue
        module_sources[module] = module_sources.get(module, '') + ''.join(
            f'def {name}():\n    pass\n' for name in names)
    for module, contents in module_sources.items():
        (pgp / f'{module}.py').write_text(contents)

    broker = site / 'ls/core/agent/file_broker.py'
    broker.parent.mkdir(parents=True)
    broker.write_text(
        'MAX_PAGE_BYTES = 8 * 1024\nMAX_PAGE_LINES = 200\nMAX_PAGE_RESPONSE = 16 * 1024\n'
        'class FileBroker:\n'
        '    def read_page(self):\n'
        '        return self._read_page_locked()\n'
        '    def _read_page_locked(self):\n'
        '        return self._page_response()\n'
        '    def _page_response(self):\n'
        "        result = {'content': '', 'encoding': 'utf-8', 'bytes': 0, "
        "'revision': '', 'next_cursor': None}\n        return result\n")

    for relative in (
        'tools/agentq_transport_client/agentq_cli.py',
        'tools/agentq_transport_client/agentq_transport_client/cli_parser.py',
        'tools/agentq_transport_client/agentq_transport_client/ship.py',
        'tools/agentq_transport_client/agentq_transport_client/ingest.py',
        'tools/agentq_transport_client/agentq_transport_client/crypto_pipeline.py',
        'tools/agentq_transport_client/agentq_transport_client/registry.py',
        'tools/agentq_transport_client/agentq_transport_client/adapters.py',
        'tools/agentq_transport_client/agentq_transport_client/file_drop.py',
        'tools/agentq_transport_client/agentq_transport_client/mail_adapter.py',
    ):
        path = site / 'ls' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# installed fixture\n')
    for workflow in ('ls-workflow-openpgp-lifecycle', 'ls-workflow-lscli-compact-worker'):
        path = site / 'ls/workflows' / workflow
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text('# installed workflow\n')
        (path / 'workflow.yaml').write_text('workflow_id: ' + workflow + '\n')
    return site


def test_materialized_installed_capabilities_are_static_and_bounded(tmp_path, monkeypatch):
    site = _materialize_capability_files(tmp_path)
    monkeypatch.setattr(checks.shutil, 'which', lambda command: '/usr/bin/gpg' if command == 'gpg' else None)
    result = checks.capabilities(tmp_path)
    assert result['openpgp']['status'] == 'present'
    assert result['openpgp']['schemas'] == {
        'envelope': {'format': 'localsetup.openpgp-envelope', 'version': 1},
        'owner_transition': {
            'format': 'localsetup.openpgp-owner-transition', 'version': 1,
            'proposal_format': 'localsetup.openpgp-owner-transition-proposal',
            'approval_format': 'localsetup.openpgp-owner-transition-approval',
        },
        'publisher_transition': {
            'format': 'localsetup.openpgp-publishing-transition', 'version': 1,
            'proposal_format': 'localsetup.openpgp-publishing-transition-proposal',
            'proof_format': 'localsetup.openpgp-publishing-transition-proof',
            'approval_format': 'localsetup.openpgp-publishing-transition-approval',
        },
        'recovery_challenge': {'format': 'localsetup.openpgp.recovery-challenge', 'version': 1},
    }
    assert result['openpgp']['apis'] == [
        'apply_owner_transition', 'apply_publisher_transition', 'apply_recovery_transition',
        'approve_publishing_transition', 'approve_transition_proposal', 'authorize_historical_content',
        'authorize_inbound_peer', 'authorize_outbound', 'authorize_recovery_locally',
        'create_protected_backup', 'create_publishing_transition_proposal', 'create_recovery_challenge',
        'create_transition_proposal', 'encrypt_transition_proposal', 'enroll_recovery_key',
        'initialize_trust_state', 'load_trust_state',
        'open_envelope', 'open_historical_envelope', 'record_accepted_content', 'restore_protected_backup',
        'revoke_authority', 'seal_envelope', 'sign_publishing_transition_proposal',
        'sign_recovery_challenge', 'submit_candidate_proof', 'verify_approved_publishing_transition',
        'verify_approved_transition',
    ]
    assert result['reader'] == {
        'status': 'verified', 'execution_tested': False,
        'max_page_bytes': 8192, 'max_page_lines': 200, 'max_response_bytes': 16384,
        'response_fields': ['bytes', 'content', 'encoding', 'next_cursor', 'revision'],
    }
    assert result['agentq_transport']['status'] == 'present'
    assert result['agentq_transport']['file_count'] == 9
    assert result['workflows'] == {'openpgp_lifecycle': 'present', 'compact_worker': 'present'}
    assert result['gnupg'] == {'status': 'present_unprobed', 'execution_tested': False}
    assert result['crypto_execution'] == {'status': 'not_tested', 'execution_tested': False}
    assert result['openpgp']['execution_tested'] is False
    assert (site / 'ls/core/openpgp/__init__.py').is_file()


@pytest.mark.parametrize('damage', ['missing', 'symlink', 'oversized', 'malformed'])
def test_materialized_capability_metadata_fails_closed(tmp_path, damage):
    site = _materialize_capability_files(tmp_path)
    target = site / 'ls/core/openpgp/__init__.py'
    if damage == 'missing':
        target.unlink()
        expected = 'missing'
    elif damage == 'symlink':
        target.unlink()
        target.symlink_to('/dev/null')
        expected = 'invalid'
    elif damage == 'oversized':
        target.write_bytes(b'x' * (checks.LIMIT + 1))
        expected = 'invalid'
    else:
        target.write_text('not python @@@')
        expected = 'invalid'
    assert checks.capabilities(tmp_path)['openpgp']['status'] == expected


@pytest.mark.parametrize('missing_module', ['trust_state.py', 'opening.py'])
def test_missing_openpgp_implementation_module_is_not_reported_available(tmp_path, missing_module):
    site = _materialize_capability_files(tmp_path)
    (site / 'ls/core/openpgp' / missing_module).unlink()
    assert checks.capabilities(tmp_path)['openpgp']['status'] == 'missing'


def test_missing_required_openpgp_import_is_not_hidden_by_all_list(tmp_path):
    site = _materialize_capability_files(tmp_path)
    path = site / 'ls/core/openpgp/__init__.py'
    source = path.read_text()
    import_line = next(line for line in source.splitlines() if line.startswith('from .trust_state import '))
    path.write_text(source.replace(import_line, import_line.replace(', authorize_inbound_peer', '', 1)))
    assert checks.capabilities(tmp_path)['openpgp']['status'] == 'invalid'


def test_noncallable_openpgp_export_is_not_reported_as_an_implementation(tmp_path):
    site = _materialize_capability_files(tmp_path)
    path = site / 'ls/core/openpgp/envelope.py'
    path.write_text('seal_envelope = None\n')
    assert checks.capabilities(tmp_path)['openpgp']['status'] == 'invalid'


def test_missing_materialized_transport_and_workflow_are_not_reported_available(tmp_path):
    site = _materialize_capability_files(tmp_path)
    (site / 'ls/tools/agentq_transport_client/agentq_transport_client/ship.py').unlink()
    (site / 'ls/workflows/ls-workflow-lscli-compact-worker/workflow.yaml').unlink()
    result = checks.capabilities(tmp_path)
    assert result['agentq_transport']['status'] == 'missing'
    assert result['workflows'] == {'openpgp_lifecycle': 'present', 'compact_worker': 'missing'}


@pytest.mark.parametrize(
    'damage', ['missing', 'symlink', 'oversized', 'wrong_limit', 'unsafe_expression', 'wrong_shape', 'missing_method'])
def test_materialized_reader_contract_rejects_missing_or_invalid_metadata(tmp_path, damage):
    site = _materialize_capability_files(tmp_path)
    target = site / 'ls/core/agent/file_broker.py'
    if damage == 'missing':
        target.unlink()
        expected = 'missing'
    elif damage == 'symlink':
        target.unlink()
        target.symlink_to('/dev/null')
        expected = 'invalid'
    elif damage == 'oversized':
        target.write_bytes(b'x' * (checks.LIMIT + 1))
        expected = 'invalid'
    elif damage == 'wrong_limit':
        target.write_text(target.read_text().replace('MAX_PAGE_BYTES = 8 * 1024', 'MAX_PAGE_BYTES = 8193'))
        expected = 'invalid'
    elif damage == 'unsafe_expression':
        target.write_text(target.read_text().replace('MAX_PAGE_BYTES = 8 * 1024', 'MAX_PAGE_BYTES = int("8192")'))
        expected = 'invalid'
    elif damage == 'missing_method':
        target.write_text(target.read_text().replace(
            '    def read_page(self):\n        return self._read_page_locked()\n',
            '    # read_page missing\n'))
        expected = 'invalid'
    else:
        target.write_text(target.read_text().replace("'next_cursor': None", "'cursor': None"))
        expected = 'invalid'
    assert checks.capabilities(tmp_path)['reader']['status'] == expected
