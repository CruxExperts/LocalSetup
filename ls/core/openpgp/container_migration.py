"""Archive exact legacy bytes with a new, independently authorized custody record."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import secrets
import tempfile
import time

from . import artifacts
from .artifact_records import ArtifactProvenance, canonical_bytes, validate_digest, validate_opaque_id
from .container_legacy import MigrationError, inspect_source, verify_legacy
from .container_preparation import ContainerInput, ContainerPreparationLimits, _Budget
from .container_reconstruction import extract_container_file
from .container_sources import LocalFileRangeSource
from .container_storage import ensure_private_directory, write_private_file
from .envelope_io import AnchoredFile


def migrate_legacy(path, *, expected_sha256, output_dir, artifact_id, namespace,
                   publisher, controller, publication_policy, approval_policy,
                   authorized, allow_unverified=False, verification=None,
                   check=lambda: None, limits=ContainerPreparationLimits()):
    """Create new custody evidence, never alter a legacy source or its receipts.

    Output is create-only. A failed operation retains any ciphertext already
    created for inspection; retrying requires an explicitly chosen new output.
    No carrier publication, enrollment or historical trust upgrade is performed.
    """
    if authorized is not True or type(allow_unverified) is not bool or not callable(check):
        raise MigrationError('migration_unauthorized')
    validate_digest(expected_sha256)
    validate_opaque_id(artifact_id)
    if verification is None and not allow_unverified:
        raise MigrationError('migration_verification_required')
    if approval_policy.controller_fingerprint not in approval_policy.audience:
        raise MigrationError('migration_output_verification_unavailable')
    budget = _Budget(limits)
    def current():
        budget.checkpoint()
        check()
        artifacts._current_authorities(publisher.trust_state_path, publication_policy, include_controller=False)
        artifacts._current_authorities(controller.trust_state_path, approval_policy, include_controller=True)
        artifacts._require_fresh_policy(publication_policy)
        artifacts._require_fresh_policy(approval_policy)
        if not source.unchanged():
            raise MigrationError('migration_source_changed')
    with AnchoredFile(path) as source:
        observed = inspect_source(source, budget)
        if observed['sha256'] != expected_sha256:
            raise MigrationError('migration_source_changed')
        current()
        output_dir = Path(output_dir)
        if not output_dir.is_absolute() or '..' in output_dir.parts:
            raise MigrationError('migration_output_invalid')
        ensure_private_directory(output_dir.parent, create=False)
        output_dir.mkdir(mode=0o700)  # Never reuse another operation's outputs.
        ensure_private_directory(output_dir, create=False)
        original, approved = output_dir/'origin', output_dir/'approved'
        original.mkdir(mode=0o700); approved.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix='.migration-', dir=output_dir) as temporary:
            workspace = Path(temporary)
            copied = workspace/'original'
            budget.reserve_temp(source.size)
            with copied.open('xb') as destination:
                os.chmod(copied, 0o600)
                offset, digest = 0, hashlib.sha256()
                while offset < source.size:
                    current()
                    data = os.pread(source.file, min(65536, source.size-offset), offset)
                    if not data:
                        raise MigrationError('migration_source_changed')
                    destination.write(data)
                    digest.update(data)
                    offset += len(data)
                destination.flush(); os.fsync(destination.fileno())
            if digest.hexdigest() != expected_sha256:
                raise MigrationError('migration_source_changed')
            if verification is not None:
                observed = verify_legacy(copied, workspace/'verified-legacy', observed, verification, budget)
                (workspace/'verified-legacy').unlink()
            current()
            custody = dict(schema='localsetup.legacy-custody', version=1, source=observed,
                custodian=publication_policy.origin_fingerprint, migrated_at=int(time.time()),
                statement='Custody of exact bytes; no retroactive origin authentication.')
            raw = canonical_bytes(custody)
            budget.reserve_temp(len(raw))
            record = workspace/'custody.json'
            write_private_file(record, raw, replace=False)
            source_id, record_id = secrets.token_hex(16), secrets.token_hex(16)
            provenance = ArtifactProvenance(source_hashes=(expected_sha256,),
                operational_metadata={'migration': 'legacy-custody-v1', 'legacy_format': observed['format'],
                                      'legacy_verification': observed['verification']})
            prepared = artifacts.prepare_container((ContainerInput(copied, source_id, provenance),
                ContainerInput(record, record_id, provenance)), output_dir=original, namespace=namespace,
                artifact_id=artifact_id, policy=publication_policy, trust_state_path=publisher.trust_state_path,
                gnupg_home=publisher.gnupg_home, passphrase_reference=publisher.passphrase_reference,
                secret_resolver=publisher.secret_resolver, publication_check=current, limits=limits, _budget=budget)
            volumes = []
            for volume in prepared.volumes:
                current()
                with LocalFileRangeSource(volume.path) as stream:
                    volumes.append(artifacts.approve_container_distribution(stream, output_dir=approved,
                        policy=approval_policy, trust_state_path=controller.trust_state_path,
                        gnupg_home=controller.gnupg_home, passphrase_reference=controller.passphrase_reference,
                        secret_resolver=controller.secret_resolver, publication_check=current,
                        limits=limits, _budget=budget))
            result = replace(prepared, volumes=tuple(volumes))
            with ExitStack() as stack:
                sources = {v.key: stack.enter_context(LocalFileRangeSource(v.path)) for v in result.volumes}
                for identifier, name, expected in ((source_id, 'checked-source', expected_sha256),
                    (record_id, 'checked-record', hashlib.sha256(raw).hexdigest())):
                    current()
                    receipt = extract_container_file(sources, identifier, name, authorized=True,
                        policy=replace(approval_policy, boundary=workspace), trust_state_path=controller.trust_state_path,
                        gnupg_home=controller.gnupg_home, passphrase_reference=controller.passphrase_reference,
                        secret_resolver=controller.secret_resolver, limits=limits, _budget=budget)
                    if receipt.plaintext_sha256 != expected:
                        raise MigrationError('migration_output_verification_failed')
                    (workspace/name).unlink()
            current()
            receipt = dict(schema='localsetup.legacy-migration-receipt', version=1, state='verified',
                artifact_id=result.artifact_id, version_id=result.version_id,
                logical_commitment_sha256=result.logical_commitment_sha256,
                source_file_id=source_id, custody_file_id=record_id, verification=observed['verification'])
            write_private_file(output_dir/'receipt.json', canonical_bytes(receipt), replace=False)
            current()
            return result, receipt
