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


def open_historical_envelope_file(
    envelope_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    limits: "FileEnvelopeLimits",
    trust_state_path: str | os.PathLike[str],
    expected_scope: tuple[str, ...],
    role: str,
    signer_fingerprint: str,
    gnupg_home: str | os.PathLike[str],
    policy: EnvelopePolicy,
    authorized: bool,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    cancel_check: "Callable[[], bool] | None" = None,
) -> "Path":
    """Open exact previously receipted file bytes after local role expiry."""
    from . import envelope, opening, trust_state
    from .envelope_io import FileEnvelopeLimits, OperationBudget, open_file

    if not isinstance(limits, FileEnvelopeLimits):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.INVALID_INPUT)
    try:
        budget = OperationBudget(limits, cancel_check)
        budget.checkpoint()
    except envelope.EnvelopeError as exc:
        raise opening._map_envelope_error(exc) from None
    try:
        authority, digest = trust_state._authorize_historical_content_file(
            trust_state_path, envelope_path,
            signer_fingerprint=signer_fingerprint, role=role,
            expected_scope=expected_scope, max_bytes=limits.max_envelope_bytes,
            budget=budget,
        )
    except trust_state.TrustStateError:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.AUTHORIZATION_DENIED) from None
    except envelope.EnvelopeError as exc:
        raise opening._map_envelope_error(exc) from None
    if (
        not isinstance(policy, EnvelopePolicy)
        or policy.expected_signers != frozenset({authority.fingerprint})
    ):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH)
    return open_file(
        envelope_path, output_path, limits=limits, gnupg_home=gnupg_home,
        policy=policy, local_trust=LocalTrust({authority.fingerprint}),
        authorized=authorized, passphrase_reference=passphrase_reference,
        secret_resolver=secret_resolver, cancel_check=cancel_check,
        allow_historical_signature=True, expected_ciphertext_sha256=digest,
        operation_budget=budget,
    )
