"""Historical envelope opening bound to a persisted exact-content receipt."""
from __future__ import annotations

import os

from .contracts import EnvelopePolicy, LocalTrust
from .opening import EnvelopeOpenError, EnvelopeOpenErrorCode, _open_envelope
from .secrets import SecretReference, SecretResolver
from .trust_state import authorize_historical_content


def open_historical_envelope(
    serialized: bytes,
    *,
    trust_state_path: str | os.PathLike[str],
    expected_scope: tuple[str, ...],
    role: str,
    signer_fingerprint: str,
    gnupg_home: str | os.PathLike[str],
    policy: EnvelopePolicy,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
) -> bytes:
    """Open only exact previously accepted ciphertext, never admit new traffic.

    The caller selects a protected persistent trust store and exact recipient
    policy. The receipt is loaded from that store, not accepted as a caller
    assertion. Expiry or later revocation does not erase prior authenticity.
    Bad/missing signatures, changed ciphertext, integrity failure and participant
    mismatch still fail before any payload is returned. Missing recipient secret
    keys cannot be recovered by this API.
    """
    authority = authorize_historical_content(
        trust_state_path, serialized, signer_fingerprint=signer_fingerprint,
        role=role, expected_scope=expected_scope,
    )
    if (not isinstance(policy, EnvelopePolicy)
            or policy.expected_signers != frozenset({authority.fingerprint})):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH)
    return _open_envelope(
        serialized, gnupg_home=gnupg_home, policy=policy,
        local_trust=LocalTrust({authority.fingerprint}),
        passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
        allow_historical_signature=True,
    )
