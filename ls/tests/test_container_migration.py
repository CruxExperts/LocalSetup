"""Legacy custody preserves exact bytes and never invents historical trust."""
from contextlib import ExitStack
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from ls.core.openpgp import artifacts, container_runtime as runtime, trust_state
from ls.core.openpgp.container_cli import main
from ls.core.openpgp.container_legacy import inspect_legacy, MigrationError
from ls.core.openpgp.container_preparation import ContainerPreparationLimits
from ls.core.openpgp.container_reconstruction import extract_container_file
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.contracts import ENVELOPE_FORMAT, EnvelopePolicy, LocalTrust
from ls.core.openpgp.envelope import seal_envelope
from ls.core.openpgp.opening import accept_envelope_file
from ls.core.openpgp.container_storage import write_private_file
from ls.tests.openpgp_key_helpers import _gpg, PASSPHRASE
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE, SECRET_NAME, SCOPE


def test_inspection_is_bounded_read_only_and_rejects_future_versions(tmp_path, capsys):
    source = tmp_path/'unsigned'
    raw = b'unsigned historical record'
    source.write_bytes(raw)
    before = source.stat()
    assert main(['inspect-legacy', '--source', str(source)], source_root=tmp_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['verification'] == 'unverified' and result['format'] == 'opaque-legacy'
    assert result['sha256'] == hashlib.sha256(raw).hexdigest()
    assert source.stat().st_mtime_ns == before.st_mtime_ns and list(tmp_path.iterdir()) == [source]
    header = json.dumps(dict(format=ENVELOPE_FORMAT, schema_version=2)).encode()
    source.write_bytes(struct.pack('>H', len(header))+header+b'unknown ciphertext')
    with pytest.raises(MigrationError, match='unsupported_version'):
        inspect_legacy(source)
    source.write_bytes(raw)
    with pytest.raises(MigrationError, match='limit'):
        inspect_legacy(source, limits=ContainerPreparationLimits(
            file_limits=replace(ContainerPreparationLimits().file_limits, max_plaintext_bytes=2)))
    alias = tmp_path/'alias'; alias.symlink_to(source)
    with pytest.raises(OSError):
        inspect_legacy(alias)


@pytest.mark.parametrize('verified', [False, True])
def test_migration_preserves_bytes_and_separates_custody_from_origin(tmp_path, separate_machine_env, monkeypatch, capsys, verified):
    env = separate_machine_env
    monkeypatch.setenv(SECRET_NAME, PASSPHRASE)
    original = (seal_envelope(b'old authenticated payload', gnupg_home=env.homes['origin'],
        signer_fingerprint=env.publisher, recipient_fingerprints=env.policies['origin'].audience,
        local_trust=LocalTrust({env.publisher}), passphrase_reference=SECRET_REFERENCE,
        secret_resolver=env.resolver) if verified else b'unsigned older manifest with no origin proof')
    source = tmp_path/'legacy'; source.write_bytes(original); source.chmod(0o600)
    digest = hashlib.sha256(original).hexdigest()
    def key(role, fingerprint):
        return runtime._key_dict(runtime.KeySelection(fingerprint,
            _gpg(env.homes[role], '--armor', '--export', fingerprint), env.homes[role], SECRET_REFERENCE))
    def context(role, fingerprint):
        return dict(policy=asdict(env.policies[role]), trust_state_path=str(env.paths[role]),
                    key=key(role, fingerprint))
    legacy = (dict(trust_state_path=str(env.paths['receiver']), scope=list(SCOPE), role='publisher',
        signer=env.publisher, recipients=list(env.policies['receiver'].audience),
        key=key('receiver', env.recipient)) if verified else None)
    binding = dict(schema='localsetup.legacy-migration-authority', version=1,
        namespace=dict(tenant_id='tenant', project_id='project', publisher_opaque_id='a'*32),
        publisher=context('origin', env.publisher), controller=context('controller', env.owner), legacy=legacy)
    authority = tmp_path/'authority.json'
    write_private_file(authority, json.dumps(binding).encode(), replace=False)
    def args(output):
        return ['migrate-legacy', '--source', str(source), '--expected-sha256', digest,
            '--artifact-id', 'b'*32, '--authority-file', str(authority), '--output-dir', str(output)]
    denied = tmp_path/'denied'
    # Explicit unverified preservation cannot downgrade a requested historical
    # verification when its exact receipt is missing.
    assert main(args(denied) + (['--preserve-unverified'] if verified else []), source_root=tmp_path) == 2
    capsys.readouterr()
    assert not list(denied.rglob('*.lssc')) and not (denied/'receipt.json').exists()
    if verified:
        snapshot = trust_state.load_trust_state(env.paths['receiver'], expected_scope=SCOPE)
        accepted = tmp_path/'old-clear'
        accept_envelope_file(source, accepted, authorized=True, limits=ContainerPreparationLimits().file_limits,
            trust_state_path=env.paths['receiver'], expected_scope=SCOPE, role='publisher',
            expected_store_id=snapshot.store_id, expected_revision=snapshot.revision,
            gnupg_home=env.homes['receiver'], policy=EnvelopePolicy(expected_signers={env.publisher},
                expected_recipients=set(env.policies['receiver'].audience)),
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
        assert accepted.read_bytes() == b'old authenticated payload'
        accepted.unlink()
        trust_state.revoke_authority(env.paths['receiver'], role='publisher', expected_scope=SCOPE,
            expected_store_id=snapshot.store_id, expected_revision=snapshot.revision)
    with sqlite3.connect(env.paths['receiver']) as db:
        old_receipts = db.execute('SELECT * FROM receipts').fetchall()
    before = source.stat()
    output = tmp_path/'migration'
    command = args(output) + ([] if verified else ['--preserve-unverified'])
    assert main(command, source_root=tmp_path) == 0, capsys.readouterr().out
    receipt = json.loads(capsys.readouterr().out)
    expected = 'historically_verified' if verified else 'unverified'
    assert receipt['verification'] == expected and receipt['state'] == 'verified'
    assert source.read_bytes() == original and source.stat().st_mtime_ns == before.st_mtime_ns
    with sqlite3.connect(env.paths['receiver']) as db:
        assert db.execute('SELECT * FROM receipts').fetchall() == old_receipts
    restored = tmp_path/'restored'; restored.mkdir(mode=0o700)
    policy = replace(env.policies['controller'], boundary=restored)
    crypto = dict(policy=policy, trust_state_path=env.paths['controller'], gnupg_home=env.homes['controller'],
                  passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    with ExitStack() as stack:
        sources = {}
        for path in (output/'approved').glob('*.lssc'):
            stream = stack.enter_context(LocalFileRangeSource(path))
            metadata = artifacts.inspect_container_metadata(stream, **crypto)
            sources[metadata.key] = stream
            assert all(row['provenance']['operational_metadata']['legacy_verification'] == expected
                       for row in metadata.logical['files'])
        extract_container_file(sources, receipt['source_file_id'], 'original', authorized=True, **crypto)
        extract_container_file(sources, receipt['custody_file_id'], 'custody', authorized=True, **crypto)
    assert (restored/'original').read_bytes() == original
    custody = json.loads((restored/'custody').read_bytes())
    assert custody['source']['sha256'] == digest and custody['source']['verification'] == expected
    assert custody['custodian'] == env.publisher
    assert ('legacy_signer' in custody['source']) is verified
    if verified:
        assert custody['source']['legacy_role'] == 'publisher' and custody['source']['legacy_scope'] == list(SCOPE)
    assert not list(output.glob('.migration-*'))
    saved = {path: path.read_bytes() for path in output.rglob('*.lssc')}
    assert main(command, source_root=tmp_path) == 2
    capsys.readouterr()
    assert all(path.read_bytes() == value for path, value in saved.items())
    changed = args(tmp_path/'changed') + ['--preserve-unverified']
    source.write_bytes(original+b'changed')
    assert main(changed, source_root=tmp_path) == 2
    capsys.readouterr()
    assert not (tmp_path/'changed').exists()
