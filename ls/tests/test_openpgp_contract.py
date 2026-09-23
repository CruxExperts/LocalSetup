from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from ls.core.openpgp import (
    ENVELOPE_FORMAT,
    ENVELOPE_SCHEMA_VERSION,
    ContractErrorCode,
    EnvelopeHeader,
    EnvelopePolicy,
    KeyCapability,
    LocalTrust,
    OpenPGPContractError,
    RSA_4096_TWO_YEAR_PROFILE,
    assess_signer_trust,
    expected_expiry_date,
    normalize_fingerprint,
    require_capabilities,
    validate_envelope_header,
    validate_envelope_participants,
    validate_key_profile,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "openpgp_contract_cases.json").read_text(
        encoding="utf-8"
    )
)
VALID = FIXTURES["valid"]
INVALID = FIXTURES["invalid"]


def _policy() -> EnvelopePolicy:
    return EnvelopePolicy(**VALID["policy"])


def _header_from_fixture() -> EnvelopeHeader:
    return EnvelopeHeader.from_json(json.dumps(VALID["header"]))


def test_full_fingerprint_normalization_and_short_id_rejection() -> None:
    fingerprint = VALID["policy"]["expected_signers"][0]
    grouped = " ".join(fingerprint[index : index + 4] for index in range(0, 40, 4))

    assert normalize_fingerprint(grouped.lower()) == fingerprint.upper()
    with pytest.raises(OpenPGPContractError) as error:
        normalize_fingerprint(INVALID["short_fingerprint"])
    assert error.value.code is ContractErrorCode.INVALID_FINGERPRINT
    assert str(error.value) == ContractErrorCode.INVALID_FINGERPRINT.value
    with pytest.raises(OpenPGPContractError) as error:
        normalize_fingerprint("A" * 38 + "\ufb00")
    assert error.value.code is ContractErrorCode.INVALID_FINGERPRINT


def test_policy_requires_nonempty_distinct_exact_participant_pins() -> None:
    with pytest.raises(OpenPGPContractError) as error:
        EnvelopePolicy(expected_signers=(), expected_recipients=("b" * 40,))
    assert error.value.code is ContractErrorCode.EMPTY_SIGNER_SET

    with pytest.raises(OpenPGPContractError) as error:
        EnvelopePolicy(
            expected_signers=("a" * 40,),
            expected_recipients=("b" * 40, "B" * 40),
        )
    assert error.value.code is ContractErrorCode.DUPLICATE_FINGERPRINT
    configured_roles = _policy()
    assert configured_roles.owner_fingerprint == "A" * 40
    assert configured_roles.publisher_fingerprint == "A" * 40

    with pytest.raises(OpenPGPContractError) as error:
        EnvelopePolicy(
            expected_signers=("a" * 40,),
            expected_recipients=("b" * 40,),
            owner_fingerprint=INVALID["short_fingerprint"],
        )
    assert error.value.code is ContractErrorCode.INVALID_FINGERPRINT


    optional_roles = EnvelopePolicy(
        expected_signers=("a" * 40,), expected_recipients=("b" * 40,)
    )
    assert optional_roles.owner_fingerprint is None
    assert optional_roles.publisher_fingerprint is None


def test_header_exposes_only_format_and_schema() -> None:
    header = _header_from_fixture()
    validate_envelope_header(header)
    assert json.loads(header.to_json()) == {
        "format": ENVELOPE_FORMAT,
        "schema_version": ENVELOPE_SCHEMA_VERSION,
    }
    assert EnvelopeHeader.from_json(header.to_json()) == header


def test_extra_clear_identity_is_rejected() -> None:
    with pytest.raises(OpenPGPContractError) as error:
        EnvelopeHeader.from_json(json.dumps(INVALID["header_extra_member"]))
    assert error.value.code is ContractErrorCode.INVALID_HEADER

def test_verified_participant_sets_are_checked_independently_of_header() -> None:
    policy = _policy()
    signer = next(iter(policy.expected_signers))
    recipients = policy.expected_recipients

    validate_envelope_participants((signer,), recipients, policy)
    with pytest.raises(OpenPGPContractError) as error:
        validate_envelope_participants((), recipients, policy)
    assert error.value.code is ContractErrorCode.SIGNER_SET_MISMATCH

    with pytest.raises(OpenPGPContractError) as error:
        validate_envelope_participants(
            (signer, "d" * 40), recipients, policy
        )
    assert error.value.code is ContractErrorCode.SIGNER_SET_MISMATCH


def test_header_parser_rejects_duplicate_and_malformed_members() -> None:
    duplicate_member = (
        '{"format":"localsetup.openpgp-envelope",'
        '"format":"localsetup.openpgp-envelope","schema_version":1}'
    )

    for serialized in (duplicate_member, "[]", "not json"):
        with pytest.raises(OpenPGPContractError) as error:
            EnvelopeHeader.from_json(serialized)
        assert error.value.code is ContractErrorCode.INVALID_HEADER
    for serialized in (" " * 257, '{"format":"localsetup.openpgp-envelope","schema_version":1,"extra":' + "[" * 300):
        with pytest.raises(OpenPGPContractError) as error:
            EnvelopeHeader.from_json(serialized)
        assert error.value.code is ContractErrorCode.INVALID_HEADER


def test_rsa_4096_two_calendar_year_profile_handles_leap_day() -> None:
    profile = VALID["key"]
    created_on = date.fromisoformat(profile["created_on"])
    expires_on = date.fromisoformat(profile["expires_on"])

    assert expected_expiry_date(created_on) == date(2026, 2, 28)
    assert expected_expiry_date(date(2023, 2, 28)) == date(2025, 2, 28)
    validate_key_profile(
        profile["algorithm"],
        profile["bits"],
        created_on,
        expires_on,
        profile=RSA_4096_TWO_YEAR_PROFILE,
    )

    weak_key = INVALID["small_key"]
    with pytest.raises(OpenPGPContractError) as error:
        validate_key_profile(
            weak_key["algorithm"],
            weak_key["bits"],
            date.fromisoformat(weak_key["created_on"]),
            date.fromisoformat(weak_key["expires_on"]),
        )
    assert error.value.code is ContractErrorCode.INVALID_PROFILE

    wrong_expiry = INVALID["wrong_leap_day_expiry"]
    with pytest.raises(OpenPGPContractError) as error:
        validate_key_profile(
            wrong_expiry["algorithm"],
            wrong_expiry["bits"],
            date.fromisoformat(wrong_expiry["created_on"]),
            date.fromisoformat(wrong_expiry["expires_on"]),
        )
    assert error.value.code is ContractErrorCode.KEY_EXPIRY_MISMATCH


def test_local_trust_is_authoritative_and_discovery_only_reports_mismatch() -> None:
    trusted = VALID["policy"]["expected_signers"][0]
    unknown = "d" * 40
    local = LocalTrust(fingerprints=(trusted,))

    assessment = assess_signer_trust(
        trusted, local, discovered_fingerprint=unknown
    )
    assert assessment.trusted
    assert assessment.discovery_mismatch
    assert local.require_signer(trusted, discovered_fingerprint=unknown) == assessment
    malformed_remote = local.require_signer(trusted, discovered_fingerprint="DEADBEEF")
    assert malformed_remote.trusted
    assert malformed_remote.discovery_mismatch

    remote_only = assess_signer_trust(
        unknown, LocalTrust(), discovered_fingerprint=unknown
    )
    assert not remote_only.trusted
    assert not remote_only.discovery_mismatch
    with pytest.raises(OpenPGPContractError) as error:
        LocalTrust().require_signer(unknown, discovered_fingerprint=unknown)
    assert error.value.code is ContractErrorCode.UNTRUSTED_SIGNER


def test_capability_contract_checks_required_uses_and_rejects_unknown_values() -> None:
    capabilities = require_capabilities(
        VALID["capabilities"], (KeyCapability.SIGN, KeyCapability.ENCRYPT)
    )
    assert capabilities == frozenset({KeyCapability.SIGN, KeyCapability.ENCRYPT})

    with pytest.raises(OpenPGPContractError) as error:
        require_capabilities(("sign",), ("encrypt",))
    assert error.value.code is ContractErrorCode.MISSING_CAPABILITY

    with pytest.raises(OpenPGPContractError) as error:
        require_capabilities((INVALID["unknown_capability"],), ("sign",))
    assert error.value.code is ContractErrorCode.INVALID_CAPABILITY
