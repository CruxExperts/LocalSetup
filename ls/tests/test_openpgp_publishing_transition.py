from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ls.core.openpgp.contracts import KeyCapability, LocalTrust
from ls.core.openpgp.keys import KeyInspection, KeyRecord
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver
from ls.core.openpgp import transition as transition_api
from ls.core.openpgp.publishing_transition import (
    ApprovedPublishingTransitionRecord,
    PublishingTransitionProposal,
    TransitionError,
    TransitionErrorCode,
    approve_publishing_transition,
    create_publishing_transition_proposal,
    sign_publishing_transition_proposal,
    verify_approved_publishing_transition,
)
from ls.core.openpgp import publishing_transition as publishing_api


OWNER = "A" * 40
OLD_PUBLISHER = "B" * 40
NEW_PUBLISHER = "C" * 40
NOW = int(datetime(2026, 9, 23, 12, tzinfo=timezone.utc).timestamp())
TRANSITION_ID = "123e4567-e89b-42d3-a456-426614174000"
NONCE = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
OWNER_CERT = b"current owner certificate"
OLD_PUBLISHER_CERT = b"old publisher certificate"
NEW_PUBLISHER_CERT = b"proposed publisher certificate"
PASSPHRASE = SecretReference(SecretProvider.ENV, "L10_TRANSITION_TEST")


class _Resolver(SecretResolver):
    def resolve(self, reference: SecretReference) -> str:
        if reference != PASSPHRASE:
            raise RuntimeError
        return "synthetic transition test passphrase"


def _epoch(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def _inspection(fingerprint: str, *, owner: bool = False) -> KeyInspection:
    created = date(2025, 1, 1)
    expires = date(2027, 1, 1)
    capabilities = {KeyCapability.SIGN}
    if owner:
        capabilities.add(KeyCapability.CERTIFY)
    primary = KeyRecord(
        fingerprint=fingerprint,
        algorithm="RSA",
        bits=4096,
        capabilities=frozenset(capabilities),
        direct_capabilities=frozenset(capabilities),
        aggregate_capabilities=frozenset(capabilities),
        created_on=created,
        expires_on=expires,
        expires_epoch=_epoch(expires),
        is_primary=True,
    )
    return KeyInspection(primary=primary, subkeys=())


def _facts(fingerprint: str) -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "identities": [{"text": f"Test {fingerprint[0]}", "validity": "u"}],
        "records": [],
    }


def _signature(fingerprint: str, payload: bytes) -> bytes:
    return fingerprint.encode("ascii") + b":" + hashlib.sha256(payload).digest()


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    inspections = {
        OWNER_CERT: (_inspection(OWNER, owner=True), _facts(OWNER)),
        OLD_PUBLISHER_CERT: (_inspection(OLD_PUBLISHER), _facts(OLD_PUBLISHER)),
        NEW_PUBLISHER_CERT: (_inspection(NEW_PUBLISHER), _facts(NEW_PUBLISHER)),
    }

    def inspect_certificate(certificate: bytes, _executable: str):
        try:
            return inspections[certificate]
        except KeyError:
            raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None

    def sign(payload: bytes, *, fingerprint: str, **_kwargs: object) -> bytes:
        return _signature(fingerprint, payload)

    def verify(
        payload: bytes,
        signature: bytes,
        *,
        expected_fingerprint: str,
        **_kwargs: object,
    ) -> None:
        expected = _signature(expected_fingerprint, payload)
        if signature != expected and not signature.startswith(expected + b":"):
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)

    monkeypatch.setattr(transition_api, "_inspect_owner_certificate", inspect_certificate)
    monkeypatch.setattr(transition_api, "_sign_detached", sign)
    monkeypatch.setattr(transition_api, "_verify_detached_signature", verify)


def _approved() -> ApprovedPublishingTransitionRecord:
    proposal = create_publishing_transition_proposal(
        current_owner_certificate=OWNER_CERT,
        old_publisher_certificate=OLD_PUBLISHER_CERT,
        proposed_publisher_certificate=NEW_PUBLISHER_CERT,
        current_owner_fingerprint=OWNER,
        current_publisher_fingerprint=OLD_PUBLISHER,
        transition_id=TRANSITION_ID,
        nonce=NONCE,
        effective_at=NOW + 3600,
        overlap_seconds=900,
        scope=("release-signing", "package-publishing"),
        epoch=2,
        reason="Rotate the publishing key",
        proposed_publisher_home=Path("candidate-home"),
        passphrase_reference=PASSPHRASE,
        secret_resolver=_Resolver(),
        now=NOW,
        gpg_binary="synthetic-gpg",
    )
    proof = sign_publishing_transition_proposal(
        proposal,
        old_publisher_fingerprint=OLD_PUBLISHER,
        old_publisher_home=Path("publisher-home"),
        passphrase_reference=PASSPHRASE,
        secret_resolver=_Resolver(),
        now=NOW,
        gpg_binary="synthetic-gpg",
    )
    return approve_publishing_transition(
        proof,
        current_owner_fingerprint=OWNER,
        current_publisher_fingerprint=OLD_PUBLISHER,
        current_epoch=1,
        expected_scope=("release-signing", "package-publishing"),
        local_trust=LocalTrust(frozenset({OWNER, OLD_PUBLISHER})),
        known_transition_ids=(),
        known_nonces=(),
        gnupg_home=Path("owner-home"),
        passphrase_reference=PASSPHRASE,
        secret_resolver=_Resolver(),
        now=NOW,
        gpg_binary="synthetic-gpg",
    )


def _verify(
    approved: ApprovedPublishingTransitionRecord,
    **overrides: object,
):
    arguments: dict[str, object] = {
        "current_owner_fingerprint": OWNER,
        "current_publisher_fingerprint": OLD_PUBLISHER,
        "current_epoch": 1,
        "expected_scope": ("release-signing", "package-publishing"),
        "local_trust": LocalTrust(frozenset({OWNER, OLD_PUBLISHER})),
        "known_transition_ids": (),
        "known_nonces": (),
        "now": NOW,
        "gpg_binary": "synthetic-gpg",
    }
    arguments.update(overrides)
    return verify_approved_publishing_transition(approved, **arguments)  # type: ignore[arg-type]


def test_all_three_parties_prove_and_owner_approval_binds_proof_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    approved = _approved()

    verified = _verify(approved)

    assert verified.current_owner_fingerprint == OWNER
    assert verified.old_publisher_fingerprint == OLD_PUBLISHER
    assert verified.proposed_publisher_fingerprint == NEW_PUBLISHER
    assert verified.epoch == 2
    assert b"Test C" in verified.proposed_publisher_inspection
    assert NEW_PUBLISHER not in LocalTrust(frozenset({OWNER, OLD_PUBLISHER})).fingerprints


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("proposed_publisher_signature", b""),
        ("old_publisher_signature", b""),
        ("owner_signature", b""),
    ),
)
def test_each_party_signature_is_required(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: bytes,
) -> None:
    _configure(monkeypatch)
    approved = _approved()
    changed = ApprovedPublishingTransitionRecord(
        approved.record,
        value if field == "proposed_publisher_signature" else approved.proposed_publisher_signature,
        value if field == "old_publisher_signature" else approved.old_publisher_signature,
        value if field == "owner_signature" else approved.owner_signature,
    )

    with pytest.raises(TransitionError) as error:
        _verify(changed)

    assert error.value.code is TransitionErrorCode.INVALID_AUTHORIZATION


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("current_owner_fingerprint", "D" * 40),
        ("current_publisher_fingerprint", "E" * 40),
        ("current_epoch", 2),
    ),
)
def test_supplied_owner_publisher_and_epoch_must_match(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _configure(monkeypatch)
    approved = _approved()

    with pytest.raises(TransitionError) as error:
        _verify(approved, **{field: value})

    assert error.value.code is TransitionErrorCode.RECORD_MISMATCH


def test_owner_must_be_trusted_and_replay_history_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    approved = _approved()

    with pytest.raises(TransitionError) as untrusted:
        _verify(approved, local_trust=LocalTrust(frozenset({OLD_PUBLISHER})))
    assert untrusted.value.code is TransitionErrorCode.UNTRUSTED_OLD_OWNER

    with pytest.raises(TransitionError) as replayed_id:
        _verify(approved, known_transition_ids=(TRANSITION_ID,))
    assert replayed_id.value.code is TransitionErrorCode.REPLAY

    with pytest.raises(TransitionError) as replayed_nonce:
        _verify(approved, known_nonces=(NONCE,))
    assert replayed_nonce.value.code is TransitionErrorCode.REPLAY


def test_verification_observes_the_scheduled_overlap_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    approved = _approved()
    record = json.loads(approved.record)
    end = record["effective_at"] + record["overlap_seconds"]

    verified = _verify(approved, now=end)
    assert verified.effective_at == record["effective_at"]

    with pytest.raises(TransitionError) as after_window:
        _verify(approved, now=end + 1)
    assert after_window.value.code is TransitionErrorCode.UNSAFE_TIME


def test_scope_change_invalidates_candidate_proof_and_owner_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    approved = _approved()
    record = json.loads(approved.record)
    record["scope"] = ["different-scope"]
    changed_record = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    changed = ApprovedPublishingTransitionRecord(
        changed_record,
        approved.proposed_publisher_signature,
        approved.old_publisher_signature,
        approved.owner_signature,
    )

    with pytest.raises(TransitionError) as error:
        _verify(changed)

    assert error.value.code is TransitionErrorCode.INVALID_SIGNATURE


def test_scope_must_match_the_callers_expected_publishing_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    approved = _approved()

    with pytest.raises(TransitionError) as error:
        _verify(approved, expected_scope=("different-scope",))

    assert error.value.code is TransitionErrorCode.RECORD_MISMATCH


def test_owner_approval_binds_the_exact_old_publisher_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    approved = _approved()
    proposal = PublishingTransitionProposal(
        approved.record, approved.proposed_publisher_signature
    )
    alternate_valid_old_proof = _signature(
        OLD_PUBLISHER, proposal.to_bytes()
    ) + b":alternate-valid-signature"
    changed = ApprovedPublishingTransitionRecord(
        approved.record,
        approved.proposed_publisher_signature,
        alternate_valid_old_proof,
        approved.owner_signature,
    )

    with pytest.raises(TransitionError) as error:
        _verify(changed)

    assert error.value.code is TransitionErrorCode.INVALID_SIGNATURE


def test_substituted_publisher_certificate_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    approved = _approved()
    record = json.loads(approved.record)
    record["proposed_publisher"]["certificate"] = base64.b64encode(
        b"substituted publisher certificate"
    ).decode("ascii")
    changed_record = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    changed = ApprovedPublishingTransitionRecord(
        changed_record,
        approved.proposed_publisher_signature,
        approved.old_publisher_signature,
        approved.owner_signature,
    )

    with pytest.raises(TransitionError) as error:
        _verify(changed)

    assert error.value.code is TransitionErrorCode.INVALID_CERTIFICATE


def test_truncated_or_wrong_format_package_is_rejected() -> None:
    with pytest.raises(TransitionError) as error:
        PublishingTransitionProposal.from_bytes(b"{}")

    assert error.value.code is TransitionErrorCode.INVALID_INPUT
