"""OpenPGP envelope classification and verified plaintext handling for FileBroker."""
from __future__ import annotations

from dataclasses import dataclass
import codecs
import hashlib
import json
import os

from ..openpgp.contracts import EnvelopeHeader, EnvelopePolicy, LocalTrust
from ..openpgp.opening import open_envelope
from ..openpgp.secrets import SecretReference, SecretResolver


_OPENPGP_HEADER = EnvelopeHeader().to_json().encode('utf-8')
_OPENPGP_FORMAT_MARKER = b'localsetup.openpgp'


def _binary_control_byte(value):
    return value == 0 or value == 127 or (value < 32 and value not in (9, 10, 12, 13))


def _is_openpgp_candidate(prefix):
    prefix = prefix[:258]
    if len(prefix) < 2:
        return False
    if not any(_binary_control_byte(value) for value in prefix):
        try:
            codecs.getincrementaldecoder('utf-8')('strict').decode(prefix, final=False)
        except UnicodeDecodeError:
            pass
        else:
            return False
    advertised_length = int.from_bytes(prefix[:2], 'big')
    available_header = prefix[2:]
    if advertised_length == len(_OPENPGP_HEADER):
        return True
    if 0 < advertised_length <= 256:
        header = available_header[:advertised_length]
        if len(header) == advertised_length:
            try:
                parsed = json.loads(header.decode('utf-8'))
            except (UnicodeDecodeError, ValueError, RecursionError):
                parsed = None
            if type(parsed) is dict:
                return True
        if header.lstrip(b' \t\r\n').startswith(b'{') or (
            header and (
                header.startswith(_OPENPGP_HEADER)
                or _OPENPGP_HEADER.startswith(header)
            )
        ):
            return True
    return _OPENPGP_FORMAT_MARKER in available_header


def _openpgp_context(gnupg_home, policy, local_trust, passphrase_reference, secret_resolver):
    supplied = (gnupg_home, policy, local_trust, passphrase_reference, secret_resolver)
    if not any(value is not None for value in supplied):
        return None
    if (
        gnupg_home is None
        or not isinstance(policy, EnvelopePolicy)
        or not isinstance(local_trust, LocalTrust)
        or (passphrase_reference is None) != (secret_resolver is None)
        or (passphrase_reference is not None and not isinstance(passphrase_reference, SecretReference))
        or (secret_resolver is not None and not isinstance(secret_resolver, SecretResolver))
    ):
        raise ValueError('OpenPGP read authority requires a selected home, policy, and local trust')
    try:
        home = os.fspath(gnupg_home)
    except Exception:
        raise ValueError('OpenPGP read authority requires a selected home, policy, and local trust') from None
    if not isinstance(home, str) or not home:
        raise ValueError('OpenPGP read authority requires a selected home, policy, and local trust')
    value = {
        'home': home,
        'signers': sorted(policy.expected_signers),
        'recipients': sorted(policy.expected_recipients),
        'owner': policy.owner_fingerprint,
        'publisher': policy.publisher_fingerprint,
        'trust': sorted(local_trust.fingerprints),
        'passphrase': None if passphrase_reference is None else {
            'provider': passphrase_reference.provider.value,
            'name': passphrase_reference.name,
        },
        'resolver': None if secret_resolver is None else (
            type(secret_resolver).__module__, type(secret_resolver).__qualname__,
        ),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True, repr=False)
class _VerifiedPageCache:
    authority: str
    source_stat: tuple[int, ...]
    source_digest: str
    plaintext_digest: str
    revision: str
    plaintext: bytearray
    text: bool
    expires_at: float
    next_cursor: str | None
    stream_nonce: str
    pages_served: int


def _open_verified_envelope(serialized, *, gnupg_home, policy, local_trust,
                            passphrase_reference, secret_resolver):
    failed = False
    plaintext = None
    try:
        plaintext = open_envelope(
            serialized,
            gnupg_home=gnupg_home,
            policy=policy,
            local_trust=local_trust,
            passphrase_reference=passphrase_reference,
            secret_resolver=secret_resolver,
        )
    except Exception as error:
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        failed = True
    if failed or type(plaintext) is not bytes:
        serialized = b''
        plaintext = None
        gnupg_home = policy = local_trust = passphrase_reference = secret_resolver = None
        raise PermissionError('Protected OpenPGP envelope could not be verified')
    return plaintext
