"""Read-only legacy framing and exact-byte, receipt-bound verification."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct

from .contracts import ENVELOPE_FORMAT, EnvelopePolicy
from .envelope_io import AnchoredFile, _outer_header
from .historical import open_historical_envelope_file
from .container_preparation import ContainerPreparationLimits, _Budget


class MigrationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LegacyVerification:
    trust_state_path: Path
    scope: tuple[str, ...]
    role: str
    signer: str
    recipients: tuple[str, ...]
    key: object


def inspect_source(source, budget):
    if not 0 < source.size <= budget.limits.file_limits.max_plaintext_bytes:
        raise MigrationError('migration_limit')
    digest, offset = hashlib.sha256(), 0
    while offset < source.size:
        budget.checkpoint()
        data = os.pread(source.file, min(65536, source.size-offset), offset)
        if not data:
            raise MigrationError('migration_source_changed')
        digest.update(data)
        offset += len(data)
    prefix = os.pread(source.file, 258, 0)
    kind = 'opaque-legacy'
    if prefix.startswith(b'format: localsetup.secure-container\n'):
        raise MigrationError('migration_requires_legacy_source')
    if len(prefix) >= 2:
        length = struct.unpack('>H', prefix[:2])[0]
        if 0 < length <= 256 and len(prefix) >= length+2:
            try:
                header = json.loads(prefix[2:2+length])
            except (ValueError, UnicodeError):
                header = None
            if type(header) is dict and header.get('format') == ENVELOPE_FORMAT:
                if header.get('schema_version') != 1 or type(header.get('schema_version')) is not int:
                    raise MigrationError('migration_unsupported_version')
                try:
                    _outer_header(source, budget.limits.file_limits)
                except Exception:
                    kind = 'invalid-envelope-v1'
                else:
                    kind = 'openpgp-envelope-v1'
    budget.checkpoint()
    if not source.unchanged():
        raise MigrationError('migration_source_changed')
    return dict(format=kind, length=source.size, sha256=digest.hexdigest(), verification='unverified')


def inspect_legacy(path, *, limits=ContainerPreparationLimits()):
    """Framing and a digest are observations, never proof of a legacy signature."""
    with AnchoredFile(path) as source:
        return inspect_source(source, _Budget(limits))


def verify_legacy(path, output, evidence, verification, budget):
    if not isinstance(verification, LegacyVerification) or evidence['format'] != 'openpgp-envelope-v1':
        raise MigrationError('migration_verification_invalid')
    from .container_runtime import KeySelection
    from .secrets import SecretResolver
    key = verification.key
    if not isinstance(key, KeySelection) or key.fingerprint not in verification.recipients:
        raise MigrationError('migration_verification_invalid')
    budget.use_crypto()
    try:
        open_historical_envelope_file(path, output, authorized=True,
            limits=budget.file_limits(plaintext_output=True), cancel_check=budget.cancelled,
            trust_state_path=verification.trust_state_path, expected_scope=verification.scope,
            role=verification.role, signer_fingerprint=verification.signer,
            gnupg_home=key.home, policy=EnvelopePolicy(expected_signers={verification.signer},
                                                     expected_recipients=set(verification.recipients)),
            passphrase_reference=key.reference,
            secret_resolver=SecretResolver(protected_file_root=key.protected_file_root))
    except Exception:
        raise MigrationError('migration_legacy_verification_failed') from None
    budget.checkpoint()
    budget.retain(output.stat().st_size)
    return evidence | {'verification': 'historically_verified', 'legacy_signer': verification.signer,
                       'legacy_role': verification.role, 'legacy_scope': list(verification.scope)}
