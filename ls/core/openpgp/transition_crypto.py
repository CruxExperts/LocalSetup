"""Certificate inspection, detached proofs, and selected-home GPG helpers."""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import unquote_to_bytes

from . import envelope as _envelope
from . import keys as _keys
from . import recovery as _recovery
from .contracts import KeyCapability, OpenPGPContractError, RSA_4096_TWO_YEAR_PROFILE, normalize_fingerprint, validate_key_profile
from .envelope import EnvelopeError, EnvelopeErrorCode
from .keys import KeyInspection, KeyInspectionError, inspect_key
from .secrets import SecretReference, SecretResolver
from .transition_contracts import (
    _GPG_AGENT_START_TIMEOUT_SECONDS,
    _GPG_VERIFY_STATUS_PREFIX,
    _MAX_CERTIFICATE_BYTES,
    _MAX_IDENTITY_BYTES,
    _MAX_IDENTITY_TOTAL_BYTES,
    _MAX_IDENTITIES,
    _MAX_PACKAGE_BYTES,
    _MAX_SIGNATURE_BYTES,
    TransitionError,
    TransitionErrorCode,
    _utc_date,
    _validate_record_owner_shape,
    _validate_signing_component,
)

@contextmanager
def _managed_transition_agent(
    gnupg_home: str | os.PathLike[str],
) -> Iterator[Path]:
    """Start a selected-home agent while preserving existing agent state.

    Transition crypto uses the envelope runner's ``--no-autostart`` policy.
    Explicitly launch only the caller-selected home so freshly generated keys
    can be used without enabling ambient GnuPG agent discovery.
    """
    home = _envelope._validated_gnupg_home(gnupg_home)
    try:
        with _recovery._managed_home_agents((home,)):
            if not _recovery._home_agent_is_running(home):
                gpgconf = shutil.which("gpgconf")
                if not gpgconf:
                    raise EnvelopeError(EnvelopeErrorCode.GPG_NOT_FOUND)
                try:
                    result = subprocess.run(
                        (
                            gpgconf,
                            "--homedir",
                            os.fspath(home),
                            "--launch",
                            "gpg-agent",
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        env=_recovery._gpg_environment(home),
                        timeout=_GPG_AGENT_START_TIMEOUT_SECONDS,
                        check=False,
                    )
                except (OSError, ValueError, subprocess.SubprocessError):
                    raise EnvelopeError(
                        EnvelopeErrorCode.SIGN_ENCRYPT_FAILED
                    ) from None
                if result.returncode != 0 or not _recovery._home_agent_is_running(home):
                    raise EnvelopeError(EnvelopeErrorCode.SIGN_ENCRYPT_FAILED)
            yield home
    except _recovery.RecoveryError:
        raise EnvelopeError(EnvelopeErrorCode.SIGN_ENCRYPT_FAILED) from None


def _inspect_owner_certificate(
    certificate: bytes, gpg_binary: str
) -> tuple[KeyInspection, dict[str, object]]:
    if type(certificate) is not bytes or not (
        1 <= len(certificate) <= _MAX_CERTIFICATE_BYTES
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    try:
        inspection = inspect_key(certificate=certificate, gpg_binary=gpg_binary)
        identities = _read_identities(
            certificate, inspection.primary_fingerprint, gpg_binary
        )
    except (KeyInspectionError, OpenPGPContractError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    return inspection, _inspection_facts(inspection, identities)


def _read_identities(
    certificate: bytes, fingerprint: str, executable: str
) -> list[dict[str, str]]:
    try:
        with tempfile.TemporaryDirectory(prefix="localsetup-transition-") as path:
            home = Path(path)
            os.chmod(home, 0o700)
            certificate_path = home / "owner.asc"
            _keys._write_private_file(certificate_path, certificate)
            _keys._run_gpg(executable, home, ("--import", os.fspath(certificate_path)))
            output = _keys._run_gpg(
                executable,
                home,
                (
                    "--with-colons",
                    "--fixed-list-mode",
                    "--list-keys",
                    fingerprint,
                ),
            )
    except (KeyInspectionError, OSError, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    identities: list[dict[str, str]] = []
    primary_count = 0
    in_primary = False
    total_bytes = 0
    try:
        for line in output.splitlines():
            fields = line.split(b":")
            if fields[0] == b"pub":
                primary_count += 1
                in_primary = primary_count == 1
            elif fields[0] == b"uid" and in_primary:
                if len(fields) <= 9:
                    raise ValueError
                validity = fields[1].decode("ascii")
                if validity not in {
                    "",
                    "-",
                    "o",
                    "q",
                    "n",
                    "m",
                    "f",
                    "u",
                    "w",
                    "e",
                    "r",
                    "d",
                    "i",
                }:
                    raise ValueError
                identity_bytes = unquote_to_bytes(fields[9].decode("utf-8"))
                identity = identity_bytes.decode("utf-8")
                encoded_length = len(identity_bytes)
                if (
                    not identity
                    or encoded_length > _MAX_IDENTITY_BYTES
                    or len(identities) >= _MAX_IDENTITIES
                    or "\x00" in identity
                    or unicodedata.normalize("NFC", identity) != identity
                    or any(
                        unicodedata.category(character) == "Cc"
                        for character in identity
                    )
                ):
                    raise ValueError
                total_bytes += encoded_length
                if total_bytes > _MAX_IDENTITY_TOTAL_BYTES:
                    raise ValueError
                identities.append({"text": identity, "validity": validity})
    except (UnicodeDecodeError, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    if primary_count != 1 or not identities or not any(
        identity["validity"] not in {"r", "i", "d"} for identity in identities
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    identities.sort(key=lambda identity: (identity["text"], identity["validity"]))
    if len({(item["text"], item["validity"]) for item in identities}) != len(identities):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    return identities


def _inspection_facts(
    inspection: KeyInspection, identities: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "fingerprint": inspection.primary_fingerprint,
        "identities": identities,
        "records": [
            {
                "fingerprint": record.fingerprint,
                "primary": record.is_primary,
                "algorithm": record.algorithm,
                "bits": record.bits,
                "capabilities": sorted(
                    capability.value for capability in record.capabilities
                ),
                "direct_capabilities": sorted(
                    capability.value for capability in record.direct_capabilities
                ),
                "aggregate_capabilities": sorted(
                    capability.value for capability in record.aggregate_capabilities
                ),
                "created_on": record.created_on.isoformat(),
                "expires_on": (
                    None if record.expires_on is None else record.expires_on.isoformat()
                ),
                "expires_epoch": record.expires_epoch,
                "revoked": record.revoked,
                "disabled": record.disabled,
            }
            for record in inspection.records
        ],
    }


def _owner_record(
    certificate: bytes, facts: dict[str, object]
) -> dict[str, object]:
    return {
        "fingerprint": facts["fingerprint"],
        "certificate": base64.b64encode(certificate).decode("ascii"),
        "inspection": facts,
    }


def _inspect_owner_from_record(
    owner: Mapping[str, object], executable: str
) -> tuple[KeyInspection, dict[str, object]]:
    _validate_record_owner_shape(owner)
    certificate = _certificate_from_owner_record(owner)
    inspection, facts = _inspect_owner_certificate(certificate, executable)
    if (
        owner.get("fingerprint") != inspection.primary_fingerprint
        or owner.get("inspection") != facts
    ):
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    return inspection, facts


def _certificate_from_owner_record(owner: Mapping[str, object]) -> bytes:
    encoded = owner.get("certificate")
    if (
        not isinstance(encoded, str)
        or len(encoded) > ((_MAX_CERTIFICATE_BYTES + 2) // 3) * 4
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    try:
        certificate = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    if (
        not certificate
        or len(certificate) > _MAX_CERTIFICATE_BYTES
        or base64.b64encode(certificate).decode("ascii") != encoded
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    return certificate


def _sign_detached(
    payload: bytes,
    *,
    fingerprint: str,
    gnupg_home: str | os.PathLike[str],
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    gpg_binary: str,
) -> bytes:
    passphrase: bytearray | None = None
    secret: object | None = None
    encoded: bytes | None = None
    try:
        if (
            type(payload) is not bytes
            or len(payload) > _MAX_PACKAGE_BYTES
            or not isinstance(passphrase_reference, SecretReference)
            or not isinstance(secret_resolver, SecretResolver)
        ):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        home = _envelope._validated_gnupg_home(gnupg_home)
        try:
            secret = secret_resolver.resolve(passphrase_reference)
            encoded = _envelope._encode_passphrase(secret)
        except Exception:
            secret = None
            encoded = None
            raise TransitionError(TransitionErrorCode.PASSPHRASE_UNAVAILABLE) from None
        passphrase = bytearray(encoded)
        secret = None
        encoded = None
        with _managed_transition_agent(home):
            try:
                signature, _diagnostics = _envelope._run_gpg(
                    gpg_binary,
                    home,
                    (
                        "--pinentry-mode",
                        "loopback",
                        "--passphrase-fd",
                        "{passphrase_fd}",
                        "--no-armor",
                        "--local-user",
                        normalize_fingerprint(fingerprint),
                        "--output",
                        "-",
                        "--detach-sign",
                        "-",
                    ),
                    stdin_data=payload,
                    passphrase=passphrase,
                    failure_code=_envelope.EnvelopeErrorCode.SIGN_ENCRYPT_FAILED,
                )
            except EnvelopeError:
                raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE) from None
        if not signature or len(signature) > _MAX_SIGNATURE_BYTES:
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
        return signature
    except TransitionError:
        raise
    except EnvelopeError:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    except Exception:
        secret = None
        encoded = None
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    finally:
        secret = None
        encoded = None
        if passphrase is not None:
            passphrase[:] = b"\x00" * len(passphrase)
        passphrase = None


def _verify_detached_signature(
    payload: bytes,
    signature: bytes,
    *,
    certificate: bytes,
    inspection: KeyInspection,
    expected_fingerprint: str,
    valid_until: int | None = None,
    gpg_binary: str,
) -> None:
    if (
        type(payload) is not bytes
        or len(payload) > _MAX_PACKAGE_BYTES
        or type(signature) is not bytes
        or not (1 <= len(signature) <= _MAX_SIGNATURE_BYTES)
        or type(certificate) is not bytes
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    expected = normalize_fingerprint(expected_fingerprint)
    if inspection.primary_fingerprint != expected:
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    if valid_until is not None and (
        type(valid_until) is not int or not (0 <= valid_until <= 2**63 - 1)
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    try:
        with tempfile.TemporaryDirectory(prefix="localsetup-transition-verify-") as path:
            home = Path(path)
            os.chmod(home, 0o700)
            certificate_path = home / "owner.asc"
            signature_path = home / "record.sig"
            payload_path = home / "record.json"
            _keys._write_private_file(certificate_path, certificate)
            _keys._write_private_file(signature_path, signature)
            _keys._write_private_file(payload_path, payload)
            _keys._run_gpg(
                gpg_binary, home, ("--import", os.fspath(certificate_path))
            )
            status = _keys._run_gpg(
                gpg_binary,
                home,
                (
                    "--status-fd=1",
                    "--verify",
                    os.fspath(signature_path),
                    os.fspath(payload_path),
                ),
            )
    except (KeyInspectionError, OSError, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE) from None
    valid: list[tuple[str, str]] = []
    rejected = {
        b"BADSIG",
        b"ERRSIG",
        b"EXPSIG",
        b"EXPKEYSIG",
        b"REVKEYSIG",
        b"NO_PUBKEY",
        b"NODATA",
        b"FAILURE",
    }
    for line in status.splitlines():
        if not line.startswith(_GPG_VERIFY_STATUS_PREFIX):
            continue
        fields = line[len(_GPG_VERIFY_STATUS_PREFIX) :].split()
        if not fields:
            continue
        if fields[0] in rejected:
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
        if fields[0] != b"VALIDSIG":
            continue
        if len(fields) not in {10, 11}:
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
        try:
            signing_fingerprint = normalize_fingerprint(fields[1].decode("ascii"))
            primary_fingerprint = (
                normalize_fingerprint(fields[10].decode("ascii"))
                if len(fields) == 11
                else signing_fingerprint
            )
        except (UnicodeDecodeError, OpenPGPContractError):
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE) from None
        if valid_until is not None:
            _validate_signing_component(
                inspection, signing_fingerprint, valid_until=valid_until
            )
        valid.append((signing_fingerprint, primary_fingerprint))
    if (
        len(valid) != 1
        or valid[0][1] != expected
        or valid[0][0] not in inspection.signing_fingerprints
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
