"""Bounded OpenPGP envelope verification and opening.

Plaintext is returned only after GnuPG reports successful integrity-protected
decryption and one valid signature, and the authenticated inner manifest matches
the caller's exact local policy and trust pins.
"""

from __future__ import annotations

import os
import re
import shutil
from enum import StrEnum

from . import envelope as _envelope
from .contracts import (
    ContractErrorCode,
    EnvelopePolicy,
    LocalTrust,
    OpenPGPContractError,
    normalize_fingerprint,
    validate_envelope_header,
    validate_envelope_participants,
)
from .envelope import (
    EnvelopeError,
    EnvelopeErrorCode,
    parse_envelope,
    parse_inner_message,
)
from .secrets import SecretReference, SecretResolver

__all__ = ("EnvelopeOpenError", "EnvelopeOpenErrorCode", "open_envelope")

_MAX_RECIPIENTS = 64
_STATUS_PREFIX = b"[GNUPG:] "
_FINGERPRINT_RE = re.compile(r"[0-9A-Fa-f]{40}\Z")
_KEY_ID_RE = re.compile(r"[0-9A-Fa-f]{16}\Z")
_SIGNATURE_FAILURE_STATUSES = frozenset(
    {
        b"BADSIG",
        b"ERRSIG",
        b"EXPSIG",
        b"EXPKEYSIG",
        b"REVKEYSIG",
        b"NO_PUBKEY",
        b"SIG_CREATED",
    }
)
_DECRYPTION_FAILURE_STATUSES = frozenset(
    {b"BADMDC", b"DECRYPTION_FAILED", b"NODATA", b"UNEXPECTED"}
)


class EnvelopeOpenErrorCode(StrEnum):
    """Stable opening failures that never include envelope or GnuPG content."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_GPG_HOME = "INVALID_GPG_HOME"
    GPG_NOT_FOUND = "GPG_NOT_FOUND"
    GPG_TIMEOUT = "GPG_TIMEOUT"
    GPG_OUTPUT_LIMIT = "GPG_OUTPUT_LIMIT"
    PASSPHRASE_UNAVAILABLE = "PASSPHRASE_UNAVAILABLE"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    MALFORMED_CIPHERTEXT = "MALFORMED_CIPHERTEXT"
    DECRYPTION_FAILED = "DECRYPTION_FAILED"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    UNTRUSTED_SIGNER = "UNTRUSTED_SIGNER"
    SIGNER_SET_MISMATCH = "SIGNER_SET_MISMATCH"
    RECIPIENT_SET_MISMATCH = "RECIPIENT_SET_MISMATCH"
    INVALID_INNER_MESSAGE = "INVALID_INNER_MESSAGE"


class EnvelopeOpenError(RuntimeError):
    """Safe opening failure containing only a stable code."""

    def __init__(self, code: EnvelopeOpenErrorCode) -> None:
        self.code = EnvelopeOpenErrorCode(code)
        super().__init__(self.code.value)


def _open_envelope(
    serialized: bytes,
    *,
    gnupg_home: str | os.PathLike[str],
    policy: EnvelopePolicy,
    local_trust: LocalTrust,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
    allow_historical_signature: bool = False,
) -> bytes:
    """Verify, authorize, and return the exact payload from one sealed envelope.

    ``gnupg_home`` must select an existing private GnuPG home. GnuPG is always
    invoked with that home and with automatic key discovery/import disabled.
    A passphrase reference and resolver may be supplied together when the
    selected secret key is protected; neither is resolved unless envelope
    framing, policy, trust, and hidden-recipient packet counts are preflighted.

    The OpenPGP hidden recipient IDs cannot identify the recipient keys. The
    authenticated manifest is checked against ``policy.expected_recipients``;
    the encrypted packet list is checked only for the corresponding count.
    The returned bytes are not exposed until every framing, integrity,
    signature, manifest, policy, and local-trust check has completed.
    """
    if (
        type(serialized) is not bytes
        or not isinstance(policy, EnvelopePolicy)
        or not isinstance(local_trust, LocalTrust)
        or (passphrase_reference is None) != (secret_resolver is None)
        or (
            passphrase_reference is not None
            and not isinstance(passphrase_reference, SecretReference)
        )
        or (
            secret_resolver is not None
            and not isinstance(secret_resolver, SecretResolver)
        )
    ):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.INVALID_INPUT)

    try:
        parsed = parse_envelope(serialized)
        validate_envelope_header(parsed.header)
    except EnvelopeError as exc:
        raise _map_envelope_error(exc) from None
    except OpenPGPContractError:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.INVALID_ENVELOPE) from None

    if len(policy.expected_signers) != 1:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH)
    if not all(signer in local_trust.fingerprints for signer in policy.expected_signers):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.UNTRUSTED_SIGNER)
    recipient_count = len(policy.expected_recipients)
    if recipient_count > _MAX_RECIPIENTS:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.INVALID_INPUT)

    try:
        home = _envelope._validated_gnupg_home(gnupg_home)
    except EnvelopeError as exc:
        raise _map_envelope_error(exc) from None
    executable = shutil.which("gpg") or shutil.which("gpg2")
    if not executable:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.GPG_NOT_FOUND)

    try:
        _envelope._validate_hidden_recipient_packets(
            parsed.ciphertext,
            recipient_count=recipient_count,
            executable=executable,
            home=home,
        )
    except EnvelopeError as exc:
        raise _map_envelope_error(exc) from None

    passphrase: bytes | None = None
    if passphrase_reference is not None and secret_resolver is not None:
        try:
            resolved = secret_resolver.resolve(passphrase_reference)
        except Exception:
            raise EnvelopeOpenError(
                EnvelopeOpenErrorCode.PASSPHRASE_UNAVAILABLE
            ) from None
        try:
            passphrase = _envelope._encode_passphrase(resolved)
        except EnvelopeError as exc:
            raise _map_envelope_error(exc) from None

    arguments: list[str] = ["--status-fd=2"]
    if passphrase is None:
        arguments.extend(("--pinentry-mode", "error"))
    else:
        arguments.extend(
            ("--pinentry-mode", "loopback", "--passphrase-fd", "{passphrase_fd}")
        )
    arguments.extend(("--output", "-", "--decrypt", "-"))
    try:
        plaintext, diagnostics = _envelope._run_gpg(
            executable,
            home,
            tuple(arguments),
            stdin_data=parsed.ciphertext,
            passphrase=passphrase,
            failure_code=EnvelopeErrorCode.MALFORMED_CIPHERTEXT,
        )
    except EnvelopeError as exc:
        raise _map_envelope_error(exc, decryption=True) from None

    signer = _verified_signer(diagnostics, allow_historical=allow_historical_signature)
    try:
        inner = parse_inner_message(plaintext)
    except EnvelopeError as exc:
        raise _map_envelope_error(exc) from None

    if inner.manifest.header != parsed.header:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.INVALID_INNER_MESSAGE)
    if inner.manifest.signer_fingerprint != signer:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH)
    try:
        validate_envelope_participants(
            (signer,), inner.manifest.recipient_fingerprints, policy
        )
        local_trust.require_signer(signer)
    except OpenPGPContractError as exc:
        raise _map_contract_error(exc) from None

    return inner.payload


def open_envelope(
    serialized: bytes,
    *,
    gnupg_home: str | os.PathLike[str],
    policy: EnvelopePolicy,
    local_trust: LocalTrust,
    passphrase_reference: SecretReference | None = None,
    secret_resolver: SecretResolver | None = None,
) -> bytes:
    """Verify a current envelope completely before returning its exact payload.

    Expired or revoked signatures remain invalid here. Historical access uses
    the separate persisted-receipt API, never a caller-controlled mode switch.
    """
    return _open_envelope(
        serialized, gnupg_home=gnupg_home, policy=policy, local_trust=local_trust,
        passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
    )


def _verified_signer(diagnostics: bytes, *, allow_historical: bool = False) -> str:
    records: list[tuple[bytes, tuple[bytes, ...]]] = []
    for line in diagnostics.splitlines():
        if not line.startswith(_STATUS_PREFIX):
            continue
        fields = tuple(line[len(_STATUS_PREFIX) :].split())
        if not fields:
            raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID)
        records.append((fields[0], fields[1:]))

    def values(name: bytes) -> list[tuple[bytes, ...]]:
        return [arguments for status, arguments in records if status == name]

    historical_statuses = frozenset({b"EXPSIG", b"EXPKEYSIG", b"REVKEYSIG"}) if allow_historical else frozenset()
    failures = _SIGNATURE_FAILURE_STATUSES - historical_statuses
    if any(status in failures for status, _args in records):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID)
    if any(status in _DECRYPTION_FAILURE_STATUSES for status, _args in records):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.DECRYPTION_FAILED)
    # GnuPG reports NO_SECKEY for each other recipient whose private key is
    # absent from this local home, even after decrypting with this recipient's
    # key. DECRYPTION_OKAY and the integrity/participant checks below still
    # have to pass before any plaintext is returned.
    if values(b"NO_SECKEY") and not values(b"DECRYPTION_OKAY"):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.DECRYPTION_FAILED)

    newsig = values(b"NEWSIG")
    goodsig = [arguments for status, arguments in records
               if status == b"GOODSIG" or status in historical_statuses]
    validsig = values(b"VALIDSIG")
    if (
        (newsig and len(newsig) != 1)
        or any(len(arguments) > 1 for arguments in newsig)
        or len(goodsig) != 1
        or len(validsig) != 1
        or len(values(b"SIG_ID")) > 1
    ):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID)

    goodsig_arguments = goodsig[0]
    try:
        goodsig_identity = goodsig_arguments[0].decode("ascii")
    except (IndexError, UnicodeDecodeError):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID) from None
    if (
        len(goodsig_arguments) < 2
        or (
            _KEY_ID_RE.fullmatch(goodsig_identity) is None
            and _FINGERPRINT_RE.fullmatch(goodsig_identity) is None
        )
    ):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID)

    valid_arguments = validsig[0]
    if len(valid_arguments) not in (9, 10):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID)
    signer_value = valid_arguments[0]
    primary_value = valid_arguments[9] if len(valid_arguments) == 10 else signer_value
    try:
        signer = normalize_fingerprint(signer_value.decode("ascii"))
        primary = normalize_fingerprint(primary_value.decode("ascii"))
    except (UnicodeDecodeError, OpenPGPContractError):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID) from None
    if (
        _FINGERPRINT_RE.fullmatch(signer) is None
        or _FINGERPRINT_RE.fullmatch(primary) is None
    ):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.SIGNATURE_INVALID)

    decrypt_okay = values(b"DECRYPTION_OKAY")
    decrypt_info = values(b"DECRYPTION_INFO")
    good_mdc = values(b"GOODMDC")
    if len(decrypt_okay) != 1 or decrypt_okay[0]:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.DECRYPTION_FAILED)
    if len(decrypt_info) != 1 or len(decrypt_info[0]) not in (2, 3, 4):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.DECRYPTION_FAILED)
    try:
        mdc_method = int(decrypt_info[0][0])
        aead_algorithm = int(decrypt_info[0][2]) if len(decrypt_info[0]) >= 3 else 0
        compliance_error = int(decrypt_info[0][3]) if len(decrypt_info[0]) == 4 else 0
    except ValueError:
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.DECRYPTION_FAILED) from None
    if (
        mdc_method < 0
        or aead_algorithm < 0
        or compliance_error != 0
        or (mdc_method == 0 and aead_algorithm == 0)
        or any(arguments for arguments in good_mdc)
        or len(good_mdc) > 1
    ):
        raise EnvelopeOpenError(EnvelopeOpenErrorCode.DECRYPTION_FAILED)
    return primary


def _map_contract_error(exc: OpenPGPContractError) -> EnvelopeOpenError:
    if exc.code is ContractErrorCode.UNTRUSTED_SIGNER:
        code = EnvelopeOpenErrorCode.UNTRUSTED_SIGNER
    elif exc.code is ContractErrorCode.SIGNER_SET_MISMATCH:
        code = EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH
    elif exc.code is ContractErrorCode.RECIPIENT_SET_MISMATCH:
        code = EnvelopeOpenErrorCode.RECIPIENT_SET_MISMATCH
    else:
        code = EnvelopeOpenErrorCode.INVALID_INPUT
    return EnvelopeOpenError(code)


def _map_envelope_error(
    exc: EnvelopeError, *, decryption: bool = False
) -> EnvelopeOpenError:
    if decryption and exc.code is EnvelopeErrorCode.MALFORMED_CIPHERTEXT:
        code = EnvelopeOpenErrorCode.DECRYPTION_FAILED
    else:
        code = {
            EnvelopeErrorCode.INVALID_INPUT: EnvelopeOpenErrorCode.INVALID_INPUT,
            EnvelopeErrorCode.INVALID_GPG_HOME: EnvelopeOpenErrorCode.INVALID_GPG_HOME,
            EnvelopeErrorCode.GPG_NOT_FOUND: EnvelopeOpenErrorCode.GPG_NOT_FOUND,
            EnvelopeErrorCode.GPG_TIMEOUT: EnvelopeOpenErrorCode.GPG_TIMEOUT,
            EnvelopeErrorCode.GPG_OUTPUT_LIMIT: EnvelopeOpenErrorCode.GPG_OUTPUT_LIMIT,
            EnvelopeErrorCode.MALFORMED_CIPHERTEXT: EnvelopeOpenErrorCode.MALFORMED_CIPHERTEXT,
            EnvelopeErrorCode.INVALID_ENVELOPE: EnvelopeOpenErrorCode.INVALID_ENVELOPE,
            EnvelopeErrorCode.INVALID_INNER_MESSAGE: EnvelopeOpenErrorCode.INVALID_INNER_MESSAGE,
        }.get(exc.code, EnvelopeOpenErrorCode.DECRYPTION_FAILED)
    return EnvelopeOpenError(code)
